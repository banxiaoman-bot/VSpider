from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_readme_reflects_current_layered_architecture_and_validation_workflow() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "Capability-Orchestrated Browser Agent" in text
    assert "intent_planning" in text
    assert "operations_plane" in text
    assert "runtime_guards" in text
    assert "browser_substrate" in text
    assert "data_plane" in text
    assert "execution_kernel" in text
    assert "model_plane" in text
    assert "capability_failure_fixture_api.py" in text
    assert "Failure Fixture Regression" in text
    assert "browser_action_trace.v1" in text
    assert "scripts/validate_y.py" in text
    assert "scripts/clean_pytest_tmp.py" in text
    assert "pytest.ini" in text
    assert "browser-use-main" in text
    assert "skyvern-main" in text
    assert "WebVoyager-main" in text
