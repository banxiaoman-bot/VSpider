"""B1 file size baseline — M4 regression guard.

Locks in the file size reductions achieved by M4 G-slices (G1-G7)
and prevents backsliding. Thresholds reflect current state after
extraction-by-delegation; future slices that fully remove delegated
inline code should tighten these thresholds accordingly.

Ultimate M4 targets (from plan): main.py <5000, browser_env <4000,
api_server <1000. These will be reached progressively as inline code
is removed after delegation stabilises.
"""

from __future__ import annotations

from pathlib import Path

_PROJECT = Path(__file__).resolve().parents[1]


def _line_count(rel_path: str) -> int:
    return sum(1 for _ in (_PROJECT / rel_path).open(encoding="utf-8"))


class TestFileSizeBaseline:
    def test_main_py_below_9700(self):
        # S1a-S1d carved the extraction subsystem into extraction_engine/runtime.py;
        # S3 carved the 9 startup/handoff closures into phases/setup.py
        # (~280 lines further removed). main.py is now ~9669 Python lines, so the
        # baseline is tightened from 10200 to 9700 to lock in the S3 gain.
        count = _line_count("visual_web_agent/main.py")
        assert count < 9700, (
            f"main.py has {count} lines (limit 9700). "
            "New features must go into phases/ or extraction_engine/ modules, not main.py."
        )

    def test_browser_env_below_5000(self):
        count = _line_count("visual_web_agent/browser_env.py")
        assert count < 5000, (
            f"browser_env.py has {count} lines (limit 5000). "
            "SoM injection and other concerns should go into dedicated modules."
        )

    def test_api_server_below_2500(self):
        count = _line_count("api_server.py")
        assert count < 2500, (
            f"api_server.py has {count} lines (limit 2500). "
            "New routes must go into api_routes/ modules."
        )

    def test_phases_package_exists(self):
        phases = _PROJECT / "visual_web_agent" / "phases"
        assert phases.is_dir()
        expected = {
            "perception.py", "startup.py", "planning.py",
            "decision.py", "action_dispatch.py", "finalization.py",
        }
        actual = {f.name for f in phases.glob("*.py") if f.name != "__init__.py"}
        assert expected.issubset(actual), (
            f"Missing phases: {expected - actual}"
        )

    def test_som_injector_exists(self):
        assert (_PROJECT / "visual_web_agent" / "som_injector.py").exists()

    def test_api_routes_package_exists(self):
        api_routes = _PROJECT / "api_routes"
        assert api_routes.is_dir()
        assert (api_routes / "spider_api.py").exists()

    def test_extraction_runtime_carved_out(self):
        # S1 closeout: the extraction subsystem now lives in its own module.
        runtime = _PROJECT / "visual_web_agent" / "extraction_engine" / "runtime.py"
        assert runtime.exists()
        src = runtime.read_text(encoding="utf-8")
        for cls in ("class ExtractState", "class ExtractDeps", "class ExtractRuntime"):
            assert cls in src, f"{cls} missing from extraction_engine/runtime.py"

    def test_phase_setup_carved_out(self):
        # S3 closeout: the 9 startup/handoff closures now live in phases/setup.py.
        setup = _PROJECT / "visual_web_agent" / "phases" / "setup.py"
        assert setup.exists()
        src = setup.read_text(encoding="utf-8")
        for cls in ("class SetupDeps", "class SetupTools"):
            assert cls in src, f"{cls} missing from phases/setup.py"
