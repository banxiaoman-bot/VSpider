from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Iterator

import pytest

from scripts.clean_pytest_tmp import clean_pytest_tmp_dirs, discover_pytest_tmp_dirs
from scripts.validate_y import build_validation_commands, run_validation


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def tmp_y99() -> Iterator[Path]:
    base = Path(__file__).resolve().parent / ".tmp_y99"
    base.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="validation_", dir=str(base)))
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            base.rmdir()
        except OSError:
            pass


def test_clean_pytest_tmp_dirs_defaults_to_dry_run(tmp_y99: Path) -> None:
    target = tmp_y99 / ".tmp_pytest_validate_demo_target"
    extra = tmp_y99 / ".tmp_phase_tests"
    ignored = tmp_y99 / "workspace"
    target.mkdir()
    extra.mkdir()
    ignored.mkdir()

    discovered = discover_pytest_tmp_dirs(tmp_y99)
    summary = clean_pytest_tmp_dirs(tmp_y99)

    assert target in discovered
    assert extra in discovered
    assert ignored not in discovered
    assert summary["apply"] is False
    assert summary["scanned"] == 2
    assert summary["deleted"] == 0
    assert target.exists()
    assert extra.exists()


def test_clean_pytest_tmp_dirs_apply_deletes_only_known_root_dirs(tmp_y99: Path) -> None:
    target = tmp_y99 / ".tmp_pytest_validate_demo_full"
    nested = tmp_y99 / "workspace" / ".tmp_pytest_validate_nested"
    target.mkdir()
    nested.mkdir(parents=True)

    summary = clean_pytest_tmp_dirs(tmp_y99, apply=True)

    assert summary["apply"] is True
    assert summary["scanned"] == 1
    assert summary["deleted"] == 1
    assert not target.exists()
    assert nested.exists()


def test_validate_y_builds_stable_default_commands() -> None:
    commands = build_validation_commands("Y99 Validation Workflow")

    assert [item["name"] for item in commands] == ["target", "frontend_build", "core", "full"]
    assert ".tmp_pytest_validate_y99_validation_workflow_target" in commands[0]["command"]
    assert "tests/test_capability_router.py" in commands[0]["command"]
    assert "npm run build" == commands[1]["command"]
    assert str(ROOT / "vspider-ui") == commands[1]["cwd"]
    assert "tests/test_browser_control.py" in commands[2]["command"]
    assert "pytest tests -q" in commands[3]["command"]


def test_validate_y_dry_run_does_not_execute() -> None:
    commands = build_validation_commands("Y99", include_build=False, include_core=False, include_full=False)
    summary = run_validation(commands, dry_run=True)

    assert summary["status"] == "success"
    assert summary["steps"][0]["name"] == "target"
    assert summary["steps"][0]["skipped"] is True
    assert summary["steps"][0]["returncode"] is None


def test_pytest_ini_limits_collection_to_project_tests() -> None:
    text = (ROOT / "pytest.ini").read_text(encoding="utf-8")

    assert "testpaths = tests" in text
    assert "browser-use-main" in text
    assert "skyvern-main" in text
    assert "WebVoyager-main" in text
    assert "workspace" in text
    assert ".tmp_*" in text
