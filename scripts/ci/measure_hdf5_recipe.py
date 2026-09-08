"""Compare HDF5's original default targets with the minimized installation."""

import json
import os
from pathlib import Path
import re
import subprocess
import time


def cache_values(path):
    return dict(re.findall(r"^([^/#\n][^:\n]*):[^=\n]+=(.*)$", path.read_text(), re.MULTILINE))


def installed_bytes(path):
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file() and not item.is_symlink())


def main():
    root = Path.cwd()
    reports = root / "build-metrics/dependencies"
    source = root / "build/hdf5/source"
    minimized = cache_values(source / "build/CMakeCache.txt")
    baseline_build = root / "build-hdf5-baseline"
    baseline_install = baseline_build / "install"
    configure = ["cmake", "-S", str(source), "-B", str(baseline_build),
                 f"-DCMAKE_INSTALL_PREFIX={baseline_install}",
                 "-DHDF5_BUILD_CPP_LIB=ON", "-DHDF5_ENABLE_NONSTANDARD_FEATURE_FLOAT16=OFF"]
    for key in ("CMAKE_C_COMPILER", "CMAKE_CXX_COMPILER", "CMAKE_C_FLAGS", "CMAKE_CXX_FLAGS",
                "CMAKE_POSITION_INDEPENDENT_CODE", "CMAKE_EXE_LINKER_FLAGS",
                "CMAKE_SHARED_LINKER_FLAGS", "CMAKE_INCLUDE_PATH", "CMAKE_LIBRARY_PATH"):
        configure.append(f"-D{key}={minimized[key]}")
    elapsed = {}
    for phase, command in [
        ("configure", configure),
        ("build_and_install", ["cmake", "--build", str(baseline_build), "--target", "install", "--parallel", "3"]),
    ]:
        started = time.monotonic()
        with (reports / f"hdf5-baseline-{phase}.log").open("w") as output:
            subprocess.run(command, check=True, stdout=output, stderr=subprocess.STDOUT)
        elapsed[phase] = round(time.monotonic() - started, 3)
    baseline = cache_values(baseline_build / "CMakeCache.txt")
    options = ("BUILD_SHARED_LIBS", "BUILD_STATIC_LIBS", "BUILD_TESTING", "HDF5_BUILD_CPP_LIB",
               "HDF5_BUILD_EXAMPLES", "HDF5_BUILD_TOOLS", "HDF5_BUILD_HL_LIB")
    prepared = json.loads((reports / "hdf5.json").read_text())
    result = {
        "baseline": {"seconds": elapsed, "installed_bytes": installed_bytes(baseline_install),
                     "options": {key: baseline.get(key) for key in options}},
        "minimized": {"seconds": prepared["source_stage_seconds"],
                      "installed_bytes": installed_bytes(root / "build/hdf5/install"),
                      "cached_payload_bytes": prepared["installed_bytes"],
                      "options": {key: minimized.get(key) for key in options}},
    }
    (reports / "hdf5-recipe-comparison.json").write_text(json.dumps(result, indent=2) + "\n")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as output:
            output.write("\n### HDF5 recipe comparison\n\n```json\n" + json.dumps(result, indent=2) + "\n```\n")


if __name__ == "__main__":
    main()
