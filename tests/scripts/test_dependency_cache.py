"""Installation identity and untrusted-payload rejection tests."""

import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("dependency_cache", ROOT / "scripts/ci/dependency_cache.py")
assert SPEC is not None and SPEC.loader is not None
CACHE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CACHE)


class DependencyCacheTest(unittest.TestCase):
    def test_actual_workflow_policy_never_publishes_from_pull_requests(self):
        action = (ROOT / ".github/actions/prepare-dependencies/action.yml").read_text()
        policy = action.split("# BEGIN dependency write policy\n", 1)[1].split("# END dependency write policy", 1)[0]
        for event, ref, repository, writable in [
            ("pull_request", "refs/pull/1/merge", "boringcache/vsag", False),
            ("pull_request", "refs/heads/main", "boringcache/vsag", False),
            ("pull_request_target", "refs/heads/main", "boringcache/vsag", False),
            ("push", "refs/heads/feature", "boringcache/vsag", False),
            ("push", "refs/heads/main", "boringcache/vsag", True),
            ("schedule", "refs/heads/main", "boringcache/vsag", True),
            ("workflow_dispatch", "refs/heads/boringcache-validation", "someone/vsag", False),
            ("workflow_dispatch", "refs/heads/boringcache-validation", "boringcache/vsag", True),
        ]:
            for phase in ("auto", "warm", "cold"):
                with self.subTest(event=event, ref=ref, repository=repository, phase=phase):
                    result = subprocess.run(["bash", "-e", "-c", policy + '\nprintf "%s\\n" "${policy[@]}"'],
                                            capture_output=True, text=True,
                                            env={**os.environ, "GITHUB_EVENT_NAME": event,
                                                 "GITHUB_REF": ref, "GITHUB_REPOSITORY": repository,
                                                 "CACHE_PHASE": phase})
                    if phase == "cold" and not writable:
                        self.assertNotEqual(result.returncode, 0)
                    else:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stdout.splitlines()[0],
                                         "--write" if writable and phase != "warm" else "--read-only")

    def identity(self):
        return {
            "schema_version": 1, "dependency": "antlr4", "pin": "4.13.2",
            "source_hash": "MD5=source", "recipes": {"antlr4.cmake": "recipe"},
            "cc": {"sha256": "compiler", "version": "gcc 11", "target": "x86_64-linux-gnu"},
            "cxx": {"sha256": "compiler++", "version": "gcc 11", "target": "x86_64-linux-gnu"},
            "libc": "glibc 2.35", "cxx_macros_sha256": "headers",
            "system_libraries": {"libstdc++.so": "runtime"},
            "profile": {"architecture": "x86_64", "abi": "1", "build_type": "Release",
                        "sanitizer": "off", "coverage": "off", "lto": "off", "pic": "on",
                        "cxx_flags": "-O3 -fPIC", "hdf5_cpp": "on", "hdf5_shared": "off"},
        }

    def payload(self, root):
        identity = self.identity()
        spec = {"identity": identity, "fingerprint": CACHE.fingerprint(identity), "cxx": "g++"}
        for name in ["lib/libantlr4-runtime.a", "include/antlr4-runtime/antlr4-runtime.h",
                     "licenses/LICENSE.txt"]:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture\n")
        manifest = {"identity": identity, "fingerprint": spec["fingerprint"],
                    "files": CACHE.payload_files(root)}
        (root / "manifest.json").write_bytes(CACHE.canonical(manifest))
        return spec

    def test_identity_is_canonical_and_every_input_changes_the_fingerprint(self):
        identity = self.identity()
        digest = CACHE.fingerprint(identity)
        self.assertEqual(digest, CACHE.fingerprint(dict(reversed(list(identity.items())))))
        for key in identity:
            changed = copy.deepcopy(identity)
            changed[key] = "different"
            with self.subTest(key=key):
                self.assertNotEqual(digest, CACHE.fingerprint(changed))
        for key in identity["profile"]:
            changed = copy.deepcopy(identity)
            changed["profile"][key] = "different"
            with self.subTest(option=key):
                self.assertNotEqual(digest, CACHE.fingerprint(changed))

    def test_valid_payload_is_independent_of_its_absolute_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "original"
            spec = self.payload(original)
            relocated = Path(directory) / "relocated"
            original.rename(relocated)
            self.assertTrue(CACHE.validate(spec, relocated)[0])

    def test_missing_changed_and_extra_files_are_rejected(self):
        for mutation in ["missing", "changed", "extra"]:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                spec = self.payload(root)
                library = root / "lib/libantlr4-runtime.a"
                if mutation == "missing":
                    library.unlink()
                elif mutation == "changed":
                    library.write_text("changed")
                else:
                    (root / "CMakeCache.txt").write_text("must not be cached")
                self.assertFalse(CACHE.validate(spec, root)[0])

    def test_corrupt_manifest_and_wrong_identity_fall_back(self):
        for contents in ["{", "[]", "null", '{"files":{"../escape":{}}}']:
            with self.subTest(contents=contents), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                spec = self.payload(root)
                (root / "manifest.json").write_text(contents)
                self.assertFalse(CACHE.validate(spec, root)[0])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = self.payload(root)
            spec["identity"] = {**spec["identity"], "pin": "different"}
            self.assertFalse(CACHE.validate(spec, root)[0])

    def test_symlink_payloads_and_manifest_are_rejected(self):
        for name in ["manifest.json", "include/link.h", "include/escape"]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "payload"
                spec = self.payload(root)
                path = root / name
                if path.exists():
                    path.unlink()
                path.symlink_to(Path(directory))
                self.assertFalse(CACHE.validate(spec, root)[0])

    def test_forged_file_list_cannot_remove_required_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = self.payload(root)
            (root / "lib/libantlr4-runtime.a").unlink()
            manifest = {"identity": spec["identity"], "fingerprint": spec["fingerprint"],
                        "files": CACHE.payload_files(root)}
            (root / "manifest.json").write_bytes(CACHE.canonical(manifest))
            self.assertFalse(CACHE.validate(spec, root)[0])

    def test_invalid_payload_is_rebuilt_and_relocation_checked_before_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix = root / ".ci-dependencies/antlr4"
            spec = self.payload(prefix)
            (prefix / "lib/libantlr4-runtime.a").write_text("corrupt")
            build = root / "build"
            install = build / "antlr4/install"
            self.payload(install)
            source = build / "antlr4/source"
            source.mkdir(parents=True)
            (source / "LICENSE.txt").write_text("license")
            spec_path = build / "spec.json"
            spec_path.write_text(json.dumps(spec))
            args = SimpleNamespace(spec=spec_path, prefix=prefix, source_dir=root,
                                   build_dir=build, jobs=3, report=build / "report.json")
            with patch.object(CACHE.subprocess, "run") as run, patch.object(CACHE, "smoke_test") as smoke:
                CACHE.prepare(args)
            self.assertEqual(run.call_count, 2)
            self.assertIn("antlr4", run.call_args.args[0])
            smoke.assert_called_once()
            self.assertTrue(CACHE.validate(spec, prefix)[0])
            self.assertEqual(json.loads(args.report.read_text())["resolution"], "source")

    def test_valid_payload_does_not_build_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix = root / ".ci-dependencies/antlr4"
            spec = self.payload(prefix)
            spec_path = root / "spec.json"
            spec_path.write_text(json.dumps(spec))
            args = SimpleNamespace(spec=spec_path, prefix=prefix, source_dir=root,
                                   build_dir=root / "build", jobs=3, report=root / "report.json")
            with patch.object(CACHE.subprocess, "run") as run, patch.object(CACHE, "smoke_test"):
                CACHE.prepare(args)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
