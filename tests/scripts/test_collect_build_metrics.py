#!/usr/bin/env python3
"""Focused tests for the build-performance report parser."""

import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "collect_build_metrics", ROOT / "scripts/collect_build_metrics.py"
)
assert SPEC is not None and SPEC.loader is not None
METRICS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(METRICS)


class BuildMetricsTest(unittest.TestCase):
    def test_parse_ninja_log_classifies_edges_and_sorts_translation_units(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ninja_log = Path(directory) / ".ninja_log"
            ninja_log.write_text(
                "# ninja log v5\n"
                "0\t2500\t0\tsrc/CMakeFiles/vsag.dir/index.cpp.o\thash\n"
                "0\t900\t0\ttests/CMakeFiles/unittests.dir/test.cpp.o\thash\n"
                "0\t500\t0\t_deps/fmt-build/CMakeFiles/fmt.dir/format.cc.o\thash\n"
                "500\t1300\t0\thdf5-prefix/src/hdf5-stamp/hdf5-build\thash\n"
                "2500\t2800\t0\tsrc/libvsag.so\thash\n",
                encoding="utf-8",
            )

            result = METRICS.parse_ninja_log(
                ninja_log, {"index.cpp.o": "src/index.cpp", "test.cpp.o": "tests/test.cpp"}
            )

        self.assertEqual(result["categories"]["production_compile"]["cumulative_seconds"], 2.5)
        self.assertEqual(result["categories"]["test_compile"]["cumulative_seconds"], 0.9)
        self.assertEqual(result["categories"]["dependency_compile"]["cumulative_seconds"], 0.5)
        self.assertEqual(result["categories"]["dependency_build"]["cumulative_seconds"], 0.8)
        self.assertEqual(result["categories"]["link"]["cumulative_seconds"], 0.3)
        self.assertEqual(result["slowest_translation_units"][0]["source"], "src/index.cpp")

    def test_rendered_summary_uses_real_newlines(self) -> None:
        report = {
            "status": "success",
            "configuration": {
                "compiler": "gcc",
                "jobs": 3,
                "dependency_preparation_seconds": 1.25,
                "compiler_cache_key": "compiler-key",
                "dependency_cache_key": "dependency-key",
            },
            "phases": [
                {
                    "name": "configure",
                    "elapsed_seconds": 2.5,
                    "peak_rss_kib": None,
                    "ccache": {"cache_hit": 0, "cache_miss": 0},
                }
            ],
        }

        summary = METRICS.render_markdown(report, concise=True)

        self.assertIn("\n| Phase | Wall time", summary)
        self.assertNotIn("\\n", summary)
        self.assertTrue(summary.endswith("\n"))

    def test_ccache_json_supports_version_four_stats(self) -> None:
        stats = METRICS.load_ccache_stats('{"stats":{"cache_hit":7,"cache_miss":2}}')

        self.assertEqual(stats["cache_hit"], 7)
        self.assertEqual(stats["cache_miss"], 2)

    def test_ccache_machine_stats_support_ubuntu_version(self) -> None:
        stats = METRICS.load_ccache_stats(
            "direct_cache_hit\t5\npreprocessed_cache_hit\t2\ncache_miss\t3\n"
        )

        self.assertEqual(stats["cache_hit"], 7)
        self.assertEqual(stats["cache_miss"], 3)

    def test_ccache_json_counts_direct_and_preprocessed_hits(self) -> None:
        stats = METRICS.load_ccache_stats(
            '{"direct_cache_hit":4,"preprocessed_cache_hit":3,"cache_miss":2}'
        )
        self.assertEqual(stats["cache_hit"], 7)

    def test_fetchcontent_and_external_project_stages_are_counted_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".ninja_log"
            path.write_text(
                "# ninja log v5\n"
                "0\t500\t0\t../.ci-fetchcontent/fmt-build/fmt.cpp.o\taaa\n"
                "0\t500\t0\t/checkout/.ci-fetchcontent/fmt-build/fmt.cpp.o\taaa\n"
                "100\t900\t0\t.vsag-build-info/antlr4-configure\tbbb\n"
                "900\t1900\t0\t.vsag-build-info/antlr4-build\tccc\n"
                "900\t1900\t0\tantlr4/install/lib/libantlr4-runtime.a\tccc\n"
                "1900\t2100\t0\t.vsag-build-info/antlr4-install\tddd\n"
            )
            categories = METRICS.parse_ninja_log(path, {})["categories"]
        self.assertEqual(categories["dependency_compile"]["edges"], 1)
        self.assertEqual(categories["dependency_configure"]["cumulative_seconds"], 0.8)
        self.assertEqual(categories["dependency_build"]["cumulative_seconds"], 1.0)
        self.assertEqual(categories["dependency_install"]["cumulative_seconds"], 0.2)

    def test_log_delta_keeps_ninja_state_and_ignores_old_edges(self) -> None:
        before = "# ninja log v5\n0\t10\t0\tfirst.o\taaa\n"
        after = before + "0\t20\t1\tsecond.o\tbbb\n"
        self.assertEqual(METRICS.new_ninja_log_rows(before, before), "# ninja log v5\n")
        self.assertEqual(
            METRICS.new_ninja_log_rows(before, after),
            "# ninja log v5\n0\t20\t1\tsecond.o\tbbb\n",
        )

    def test_noop_runs_after_cold_build_before_clean_rebuild(self) -> None:
        collector = object.__new__(METRICS.Collector)
        with tempfile.TemporaryDirectory() as directory:
            collector.build_dir = Path(directory) / "build"
            collector.args = SimpleNamespace(clear_ccache=True, jobs=3)
            collector.failure_code = 0
            collector.ccache = Mock(return_value={})
            collector.run_logged = Mock(return_value={})
            collector.capture_build = Mock()
            collector.write_reports = Mock()
            collector.collect()
        self.assertEqual(
            [call.args[0] for call in collector.capture_build.call_args_list],
            ["cold_build", "noop_incremental_build", "warm_ccache_build"],
        )

    def test_collector_writes_json_markdown_and_summary_reports(self) -> None:
        previous_directory = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                args = SimpleNamespace(
                    build_dir="build",
                    output_dir="metrics",
                    jobs=3,
                    base_sha="base",
                    commit_sha="commit",
                    compiler="gcc-12",
                    compiler_cache_key="compiler-key",
                    dependency_cache_key="dependency-key",
                    dependency_preparation_seconds=1.25,
                    clear_ccache=True,
                )
                collector = METRICS.Collector(args)
                selection = Path("build/.vsag-dependency-cache/antlr4-selection.json")
                selection.parent.mkdir(parents=True)
                selection.write_text(json.dumps({"dependency": "antlr4", "resolution": "prebuilt",
                                                 "cache_state": "hit"}))
                Path("metrics/configure.log").write_text("-- Using system OpenBLAS as BLAS backend\n")
                collector.phases = [
                    {
                        "name": "configure",
                        "elapsed_seconds": 2.5,
                        "peak_rss_kib": 1024,
                        "ccache": {"cache_hit": 0, "cache_miss": 0},
                    }
                ]
                collector.write_reports()

                report = json.loads(Path("metrics/build-metrics.json").read_text())
                summary = Path("metrics/job-summary.md").read_text()
            finally:
                os.chdir(previous_directory)

        self.assertEqual(report["base_sha"], "base")
        self.assertEqual(report["dependency_selection"], [
            {"dependency": "antlr4", "resolution": "prebuilt", "cache_state": "hit"},
            {"dependency": "openblas", "resolution": "system"},
        ])
        self.assertIn("| antlr4 | prebuilt | hit |", summary)
        self.assertIn("| openblas | system | not applicable |", summary)
        self.assertIn("\n| Phase | Wall time", summary)
        self.assertTrue(summary.endswith("\n"))


if __name__ == "__main__":
    unittest.main()
