#!/usr/bin/env python3
"""Fingerprint, validate and prepare VSAG's install-only dependency payloads."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from typing import Any

from scripts.collect_build_metrics import new_ninja_log_rows, parse_ninja_log


LIBRARIES = {
    "antlr4": ["lib/libantlr4-runtime.a"],
    "hdf5": ["lib/libhdf5_cpp.a", "lib/libhdf5.a"],
}
HEADERS = {"antlr4": "include/antlr4-runtime/antlr4-runtime.h", "hdf5": "include/H5Cpp.h"}
LICENSES = {"antlr4": "LICENSE.txt", "hdf5": "COPYING"}


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def command_output(command: list[str], source: str | None = None) -> str:
    return subprocess.run(command, input=source, text=True, capture_output=True,
                          check=True, timeout=60).stdout.strip()


def compiler_identity(compiler: str) -> dict[str, str]:
    executable = Path(shutil.which(compiler) or compiler).resolve(strict=True)
    return {
        "sha256": file_hash(executable),
        "version": command_output([str(executable), "--version"]),
        "target": command_output([str(executable), "-dumpmachine"]),
    }


def fingerprint(identity: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(identity)).hexdigest()


def plan(args: argparse.Namespace) -> None:
    fields = dict(value.split("=", 1) for value in args.field)
    libraries = {}
    for name in ["libstdc++.so", "libgcc_s.so", "libz.so", "libsz.so", "libaec.so"]:
        value = command_output([args.cxx, f"-print-file-name={name}"])
        path = Path(value)
        libraries[name] = file_hash(path) if path.is_file() else "absent"
    macros = command_output(
        [args.cxx, *shlex.split(fields["cxx_flags"]), "-dM", "-E", "-x", "c++",
         "-include", "bits/c++config.h", "-"], ""
    )
    identity = {
        "schema_version": 1,
        "dependency": args.dependency,
        "pin": args.pin,
        "source_hash": args.source_hash,
        "profile": fields,
        "cc": compiler_identity(args.cc),
        "cxx": compiler_identity(args.cxx),
        "libc": os.confstr("CS_GNU_LIBC_VERSION"),
        "cxx_macros_sha256": hashlib.sha256(macros.encode()).hexdigest(),
        "system_libraries": libraries,
        "recipes": {Path(path).name: file_hash(Path(path)) for path in args.recipe},
    }
    spec = {"identity": identity, "fingerprint": fingerprint(identity),
            "cc": args.cc, "cxx": args.cxx}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(canonical(spec) + b"\n")
    print(spec["fingerprint"])


def payload_files(prefix: Path) -> dict[str, dict[str, Any]]:
    if prefix.is_symlink():
        raise ValueError("installation root is a symlink")
    result = {}
    for path in sorted(prefix.rglob("*")):
        if path.is_symlink():
            raise ValueError("installation contains a symlink")
        if path.is_dir():
            continue
        name = path.relative_to(prefix).as_posix()
        if name == "manifest.json":
            continue
        if not path.is_file():
            raise ValueError("installation contains a non-regular file")
        if not (name.startswith("include/") or name.startswith("licenses/")
                or name in {value for values in LIBRARIES.values() for value in values}):
            raise ValueError(f"unexpected installed file: {name}")
        result[name] = {"sha256": file_hash(path), "size": path.stat().st_size}
    return result


def validate(spec: dict[str, Any], prefix: Path) -> tuple[bool, str]:
    try:
        if prefix.is_symlink():
            raise ValueError("installation root is a symlink")
        manifest_path = prefix / "manifest.json"
        if not manifest_path.is_file():
            return False, "installation manifest is missing"
        if manifest_path.is_symlink() or manifest_path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("installation manifest is invalid")
        manifest = json.loads(manifest_path.read_text())
        if not isinstance(manifest, dict):
            return False, "installation manifest is not an object"
        if manifest.get("identity") != spec["identity"]:
            return False, "dependency fingerprint does not match"
        if manifest.get("fingerprint") != spec["fingerprint"]:
            return False, "manifest digest does not match"
        files = payload_files(prefix)
        if files != manifest.get("files"):
            return False, "installed file hashes or sizes do not match"
        dependency = spec["identity"]["dependency"]
        required = [*LIBRARIES[dependency], HEADERS[dependency],
                    f"licenses/{LICENSES[dependency]}"]
        if not all(name in files for name in required):
            return False, "required library, header or license is missing"
        return True, "exact fingerprint and file hashes match"
    except (OSError, ValueError, TypeError, KeyError) as error:
        return False, str(error)


def smoke_test(spec: dict[str, Any], prefix: Path) -> None:
    dependency = spec["identity"]["dependency"]
    with tempfile.TemporaryDirectory(prefix="vsag-dependency-") as directory:
        root = Path(directory)
        relocated = root / "relocated" / "installation"
        shutil.copytree(prefix, relocated)
        source = root / "consumer.cpp"
        if dependency == "antlr4":
            source.write_text('#include <antlr4-runtime.h>\n'
                              'int main() { antlr4::ANTLRInputStream input("test"); '
                              'return input.size() == 4 ? 0 : 1; }\n')
            include = relocated / "include/antlr4-runtime"
            links = ["-pthread"]
        else:
            source.write_text('#include <H5Cpp.h>\n'
                              'int main() { hsize_t n = 1; H5::DataSpace space(1, &n); '
                              'return space.getSimpleExtentNdims() == 1 ? 0 : 1; }\n')
            include = relocated / "include"
            links = ["-lz", "-ldl", "-lm", "-pthread"]
            for library, flag in [("libsz.so", "-lsz"), ("libaec.so", "-laec")]:
                if spec["identity"]["system_libraries"][library] != "absent":
                    links.append(flag)
        executable = root / "consumer"
        command = [spec["cxx"], *shlex.split(spec["identity"]["profile"]["cxx_flags"]),
                   "-std=c++17", f"-I{include}", str(source),
                   *(str(relocated / path) for path in LIBRARIES[dependency]),
                   *links, "-o", str(executable)]
        subprocess.run(command, check=True, timeout=120, capture_output=True, text=True)
        subprocess.run([str(executable)], cwd=root, check=True, timeout=30,
                       capture_output=True, text=True)


def inspect(args: argparse.Namespace) -> None:
    spec = json.loads(args.spec.read_text())
    started = time.monotonic()
    valid, reason = validate(spec, args.prefix)
    if valid:
        try:
            smoke_test(spec, args.prefix)
        except (OSError, subprocess.SubprocessError) as error:
            valid, reason = False, f"relocation/link check failed: {error}"
    result = {"dependency": spec["identity"]["dependency"],
              "fingerprint": spec["fingerprint"], "resolution": "prebuilt" if valid else "source",
              "cache_state": "hit" if valid else ("invalid" if args.prefix.exists() else "miss"),
              "reason": reason, "validation_seconds": round(time.monotonic() - started, 3)}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2) + "\n")
    print("hit" if valid else "miss")


def prepare(args: argparse.Namespace) -> None:
    spec = json.loads(args.spec.read_text())
    dependency = spec["identity"]["dependency"]
    root = args.source_dir.resolve()
    cache_root = root / ".ci-dependencies"
    if (args.prefix.name != dependency or args.prefix.parent.resolve() != cache_root
            or cache_root.is_symlink()):
        raise ValueError("preparation only writes named installations under .ci-dependencies")
    args.prefix = cache_root / dependency
    started = time.monotonic()
    valid, reason = validate(spec, args.prefix)
    stages = {}
    if valid:
        try:
            smoke_test(spec, args.prefix)
        except (OSError, subprocess.SubprocessError) as error:
            valid, reason = False, f"relocation/link check failed: {error}"
    cache_state = "hit" if valid else ("invalid" if args.prefix.exists() else "miss")
    if not valid:
        if args.prefix.is_symlink():
            args.prefix.unlink()
        elif args.prefix.exists():
            shutil.rmtree(args.prefix)
        subprocess.run(["cmake", "-S", str(args.source_dir), "-B", str(args.build_dir)], check=True)
        if json.loads(args.spec.read_text())["fingerprint"] != spec["fingerprint"]:
            raise ValueError("dependency options changed after the cache key was selected")
        ninja_log = args.build_dir / ".ninja_log"
        before = ninja_log.read_text() if ninja_log.is_file() else ""
        subprocess.run(["cmake", "--build", str(args.build_dir), "--target", dependency,
                        "--parallel", str(args.jobs)], check=True)
        if ninja_log.is_file():
            log = args.report.with_suffix(".ninja_log")
            log.write_text(new_ninja_log_rows(before, ninja_log.read_text()))
            categories = parse_ninja_log(log, {})["categories"]
            stages = {stage: categories.get(f"dependency_{stage}", {}).get("cumulative_seconds", 0)
                      for stage in ["prepare", "configure", "build", "install"]}
        install = args.build_dir / dependency / "install"
        args.prefix.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f"{dependency}-", dir=args.prefix.parent) as directory:
            package = Path(directory) / "installation"
            header_root = "include/antlr4-runtime" if dependency == "antlr4" else "include"
            shutil.copytree(install / header_root, package / header_root, symlinks=True)
            for name in LIBRARIES[dependency]:
                if (install / name).is_symlink():
                    raise ValueError("installed static library is a symlink")
                destination = package / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(install / name, destination)
            license_path = package / "licenses" / LICENSES[dependency]
            license_path.parent.mkdir(parents=True, exist_ok=True)
            source_license = args.build_dir / dependency / "source" / LICENSES[dependency]
            if source_license.is_symlink():
                raise ValueError("dependency license is a symlink")
            shutil.copyfile(source_license, license_path)
            manifest = {"identity": spec["identity"], "fingerprint": spec["fingerprint"],
                        "files": payload_files(package)}
            (package / "manifest.json").write_bytes(canonical(manifest) + b"\n")
            smoke_test(spec, package)
            package.rename(args.prefix)
    report = {"dependency": dependency, "fingerprint": spec["fingerprint"],
              "cache_state": cache_state, "source_stage_seconds": stages,
              "resolution": "prebuilt" if valid else "source", "fallback_reason": "" if valid else reason,
              "preparation_seconds": round(time.monotonic() - started, 3),
              "installed_bytes": sum(value["size"] for value in payload_files(args.prefix).values())}
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as output:
            output.write(f"\n### {dependency} installation\n\n"
                         f"Resolution: {report['resolution']}. Preparation: {report['preparation_seconds']} s. "
                         f"Installed payload: {report['installed_bytes']} bytes.\n\n"
                         f"Fingerprint: `{spec['fingerprint']}`.\n\n"
                         f"Cache validation: {cache_state}. Source stages (seconds): `{json.dumps(stages)}`.\n\n"
                         f"Fallback reason: {report['fallback_reason'] or 'none'}.\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    planner = commands.add_parser("plan")
    planner.add_argument("--dependency", choices=LIBRARIES, required=True)
    for name in ["pin", "source-hash", "cc", "cxx"]:
        planner.add_argument(f"--{name}", required=True)
    planner.add_argument("--recipe", action="append", default=[])
    planner.add_argument("--field", action="append", default=[])
    planner.add_argument("--output", type=Path, required=True)
    planner.set_defaults(function=plan)
    for name, function in [("inspect", inspect), ("prepare", prepare)]:
        command = commands.add_parser(name)
        command.add_argument("--spec", type=Path, required=True)
        command.add_argument("--prefix", type=Path, required=True)
        command.add_argument("--report", type=Path, required=True)
        command.set_defaults(function=function)
        if name == "prepare":
            command.add_argument("--source-dir", type=Path, default=Path.cwd())
            command.add_argument("--build-dir", type=Path, default=Path("build"))
            command.add_argument("--jobs", type=int, default=3)
    args = parser.parse_args()
    args.function(args)


if __name__ == "__main__":
    main()
