"""Exercise dependency selection and source fallback with the pilot's real toolchain."""

import json
import os
from pathlib import Path
import subprocess
import time

from scripts.ci.dependency_cache import HEADERS, smoke_test


def main() -> None:
    root = Path.cwd()
    reports = root / "build-metrics/dependencies"
    results = []
    specs = {dependency: json.loads((root / "build/.vsag-dependency-cache" /
                                     f"{dependency}.json").read_text()) for dependency in HEADERS}

    def configure(name, *, build="build", flags="", environment=None):
        command = ["make", "configure-asan", "COMPILE_JOBS=3", f"DEBUG_BUILD_DIR={build}",
                   f"EXTRA_DEFINED={os.environ.get('EXTRA_DEFINED', '')} {flags}"]
        result = subprocess.run(command, check=False, capture_output=True, text=True,
                                env=environment)
        (reports / f"{name}.log").write_text(result.stdout + result.stderr)
        result.check_returncode()
        return result.stdout

    def expect(name, state, *, dependency="antlr4", build="build"):
        selection = json.loads((root / build / ".vsag-dependency-cache" /
                                f"{dependency}-selection.json").read_text())
        assert selection["resolution"] == state, selection
        results.append({"case": name, "dependency": dependency, "selection": selection})

    for dependency in HEADERS:
        expect("restored", "prebuilt", dependency=dependency)
        header = root / ".ci-dependencies" / dependency / HEADERS[dependency]
        contents = header.read_bytes()
        try:
            header.write_bytes(contents + b"\n// invalid payload\n")
            configure(f"invalid-{dependency}")
            expect("invalid payload", "source", dependency=dependency)
        finally:
            header.write_bytes(contents)

    configure("changed-abi", flags="-DENABLE_CXX11_ABI=OFF")
    expect("changed ABI", "source")
    disabled = configure("disabled", flags="-DENABLE_CXX11_ABI=ON -DVSAG_USE_PREBUILT_DEPS=OFF -DVSAG_USE_SYSTEM_OPENBLAS=OFF")
    assert "Building OpenBLAS from source" in disabled, disabled
    assert "prebuilt installations disabled" in disabled, disabled
    results.append({"case": "disabled", "antlr4": "source", "hdf5": "source", "openblas": "source"})

    # A fresh source build uses the same verified archives available to offline builds.
    environment = dict(os.environ)
    for variable, archive in [
        ("VSAG_THIRDPARTY_ANTLR4_4_13_2", "antlr4_4.13.2.tar.gz"),
        ("VSAG_THIRDPARTY_HDF5_1_14_4", "hdf5_1.14.4.tar.gz"),
    ]:
        path = root / ".ci-downloads" / archive
        assert path.is_file(), path
        environment[variable] = path.as_uri()
    started = time.monotonic()
    fallback = configure("source-fallback", build="build-dependency-fallback",
                         flags="-DVSAG_USE_PREBUILT_DEPS=OFF", environment=environment)
    assert fallback.count("source=pinned") >= 2, fallback
    with (reports / "source-fallback-build.log").open("w") as output:
        subprocess.run(["cmake", "--build", "build-dependency-fallback", "--target",
                        "antlr4", "hdf5", "--parallel", "3"], check=True,
                       stdout=output, stderr=subprocess.STDOUT, env=environment)
    for dependency in HEADERS:
        smoke_test(specs[dependency], root / "build-dependency-fallback" / dependency / "install")
    results.append({"case": "fresh source fallback with pinned local archives",
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "relocated_consumers": "passed"})
    configure("restored-again", flags="-DENABLE_CXX11_ABI=ON -DVSAG_USE_PREBUILT_DEPS=ON")
    for dependency in HEADERS:
        expect("restored after fallback checks", "prebuilt", dependency=dependency)
    (reports / "selection-tests.json").write_text(json.dumps(results, indent=2) + "\n")


if __name__ == "__main__":
    main()
