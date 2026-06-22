"""Tests for ``visual_web_agent.run_checkpoint`` (RUN-RESUME1 step 1).

Pure I/O + pure decision policy, kept hermetic with ``tmp_path`` so no test
writes under the real ``runs/`` directory. Mirrors the ``crawl_checkpoint`` /
``batch_resume`` / ``download_resume`` slice test style (no agent, no network).
"""

from __future__ import annotations

import json
from pathlib import Path

from visual_web_agent.run_checkpoint import (
    CHECKPOINT_VERSION,
    RUN_CHECKPOINT_FILENAME,
    ResumeDecision,
    RunCheckpointer,
    build_checkpoint_state,
    clear_run_checkpoint,
    decide_resume,
    load_run_checkpoint,
    mark_manifest_resumed,
    run_checkpoint_path,
    save_run_checkpoint,
)
from visual_web_agent.io_contract.manifest import (
    Manifest,
    new_manifest,
    set_resumed_from,
)
from visual_web_agent.io_contract.persistence import read_manifest


class TestBuildCheckpointState:
    def test_defaults(self) -> None:
        state = build_checkpoint_state("run_x")
        assert state["version"] == CHECKPOINT_VERSION
        assert state["run_id"] == "run_x"
        assert state["status"] == "in_progress"
        assert state["turn"] == 0
        assert state["completed_steps"] == []
        assert state["item_count"] == 0
        assert "updated_at" in state

    def test_values_coerced(self) -> None:
        state = build_checkpoint_state(
            "run_y",
            goal="抓列表",
            start_url="https://a.com",
            turn=7,
            phase="execute",
            status="in_progress",
            completed_steps=["s1", "s2"],
            item_count=12,
            last_action="click",
            extra={"k": "v"},
        )
        assert state["goal"] == "抓列表"
        assert state["start_url"] == "https://a.com"
        assert state["turn"] == 7
        assert state["phase"] == "execute"
        assert state["completed_steps"] == ["s1", "s2"]
        assert state["item_count"] == 12
        assert state["last_action"] == "click"
        assert state["extra"] == {"k": "v"}


class TestSaveLoadCheckpoint:
    def test_round_trip_under_run_dir(self, tmp_path: Path) -> None:
        state = build_checkpoint_state("run_a", goal="g", turn=3)
        target = save_run_checkpoint("run_a", state, base_dir=tmp_path)
        assert target == run_checkpoint_path("run_a", base_dir=tmp_path)
        assert target.name == RUN_CHECKPOINT_FILENAME
        assert target.parent == tmp_path / "run_a"

        loaded = load_run_checkpoint("run_a", base_dir=tmp_path)
        assert loaded is not None
        assert loaded["goal"] == "g"
        assert loaded["turn"] == 3

    def test_load_missing_returns_none(self, tmp_path: Path) -> None:
        assert load_run_checkpoint("nope", base_dir=tmp_path) is None

    def test_load_corrupt_returns_none(self, tmp_path: Path) -> None:
        target = run_checkpoint_path("run_c", base_dir=tmp_path)
        target.write_text("{ not json", encoding="utf-8")
        assert load_run_checkpoint("run_c", base_dir=tmp_path) is None

    def test_save_is_atomic_overwrite(self, tmp_path: Path) -> None:
        save_run_checkpoint("run_d", build_checkpoint_state("run_d", turn=1), base_dir=tmp_path)
        save_run_checkpoint("run_d", build_checkpoint_state("run_d", turn=9), base_dir=tmp_path)
        loaded = load_run_checkpoint("run_d", base_dir=tmp_path)
        assert loaded is not None
        assert loaded["turn"] == 9
        # exactly one checkpoint file, no leftover temp files
        leftovers = list((tmp_path / "run_d").glob(".run_cp_*"))
        assert leftovers == []

    def test_clear_removes(self, tmp_path: Path) -> None:
        save_run_checkpoint("run_e", build_checkpoint_state("run_e"), base_dir=tmp_path)
        assert clear_run_checkpoint("run_e", base_dir=tmp_path) is True
        assert load_run_checkpoint("run_e", base_dir=tmp_path) is None
        # clearing a missing checkpoint is safe / False
        assert clear_run_checkpoint("run_e", base_dir=tmp_path) is False


class TestDecideResume:
    def test_resume_off_is_fresh(self) -> None:
        prior = build_checkpoint_state("r", turn=5)
        d = decide_resume(prior, resume=False)
        assert isinstance(d, ResumeDecision)
        assert d.should_resume is False
        assert d.reason == "resume_disabled"

    def test_no_checkpoint_is_fresh(self) -> None:
        assert decide_resume(None, resume=True).reason == "no_checkpoint"
        assert decide_resume({}, resume=True).reason == "no_checkpoint"

    def test_prior_completed_is_fresh(self) -> None:
        for status in ("done", "completed", "success"):
            prior = build_checkpoint_state("r", turn=4, status=status)
            d = decide_resume(prior, resume=True)
            assert d.should_resume is False
            assert d.reason == "prior_completed"

    def test_in_progress_resumes_from_turn(self) -> None:
        prior = build_checkpoint_state(
            "r", goal="g", start_url="https://a.com",
            turn=6, phase="execute", status="in_progress",
            completed_steps=["s1", "s2", "s3"], item_count=10,
        )
        d = decide_resume(prior, resume=True, goal="g", start_url="https://a.com")
        assert d.should_resume is True
        assert d.reason == "resumed"
        assert d.from_turn == 6
        assert d.completed_steps == ["s1", "s2", "s3"]
        assert d.item_count == 10
        assert d.resumed_from["turn"] == 6
        assert d.resumed_from["completed_steps"] == 3
        assert d.resumed_from["item_count"] == 10

    def test_failed_prior_is_resumable(self) -> None:
        prior = build_checkpoint_state("r", turn=2, status="failed")
        d = decide_resume(prior, resume=True)
        assert d.should_resume is True
        assert d.from_turn == 2

    def test_goal_mismatch_is_fresh(self) -> None:
        prior = build_checkpoint_state("r", goal="old goal", turn=3)
        d = decide_resume(prior, resume=True, goal="new goal")
        assert d.should_resume is False
        assert d.reason == "goal_mismatch"

    def test_url_mismatch_is_fresh(self) -> None:
        prior = build_checkpoint_state("r", start_url="https://a.com", turn=3)
        d = decide_resume(prior, resume=True, start_url="https://b.com")
        assert d.should_resume is False
        assert d.reason == "url_mismatch"

    def test_missing_current_goal_does_not_block(self) -> None:
        # caller without a goal/url to compare still resumes (no false mismatch)
        prior = build_checkpoint_state("r", goal="g", turn=3)
        d = decide_resume(prior, resume=True)
        assert d.should_resume is True


class TestManifestResumedFrom:
    def test_default_to_dict_has_no_resumed_from(self) -> None:
        # unused-resume path stays byte-identical: no extra key
        m = new_manifest("run_m")
        assert "resumed_from" not in m.to_dict()

    def test_set_then_present_and_round_trips(self) -> None:
        m = new_manifest("run_m")
        set_resumed_from(m, {"turn": 5, "completed_steps": 2})
        d = m.to_dict()
        assert d["resumed_from"] == {"turn": 5, "completed_steps": 2}
        m2 = Manifest.from_dict(d)
        assert m2.resumed_from == {"turn": 5, "completed_steps": 2}

    def test_set_empty_clears(self) -> None:
        m = new_manifest("run_m")
        set_resumed_from(m, {"turn": 5})
        set_resumed_from(m, {})
        assert "resumed_from" not in m.to_dict()

    def test_from_dict_ignores_bad_resumed_from(self) -> None:
        m = Manifest.from_dict({"run_id": "x", "resumed_from": "oops"})
        assert m.resumed_from == {}


class TestMarkManifestResumed:
    def test_writes_resumed_from_to_manifest(self, tmp_path: Path) -> None:
        mark_manifest_resumed("run_p", {"turn": 8, "item_count": 3}, base_dir=tmp_path)
        raw = json.loads((tmp_path / "run_p" / "manifest.json").read_text(encoding="utf-8"))
        assert raw["resumed_from"] == {"turn": 8, "item_count": 3}
        m = read_manifest("run_p", base_dir=tmp_path)
        assert m.resumed_from["turn"] == 8

    def test_empty_payload_stays_byte_identical(self, tmp_path: Path) -> None:
        mark_manifest_resumed("run_q", {}, base_dir=tmp_path)
        raw = json.loads((tmp_path / "run_q" / "manifest.json").read_text(encoding="utf-8"))
        assert "resumed_from" not in raw


class TestRunCheckpointer:
    def test_disabled_is_inert(self, tmp_path: Path) -> None:
        cp = RunCheckpointer.begin("run_off", resume=False, goal="g", base_dir=tmp_path)
        assert cp.enabled is False
        assert cp.should_resume is False
        # record / finish are no-ops that never write a file
        assert cp.record(3, item_count=5) is None
        assert cp.finish(False) is None
        assert load_run_checkpoint("run_off", base_dir=tmp_path) is None
        assert not (tmp_path / "run_off" / "manifest.json").exists()

    def test_enabled_fresh_writes_then_clears_on_success(self, tmp_path: Path) -> None:
        cp = RunCheckpointer.begin(
            "run_on", resume=True, goal="g", start_url="https://a.com", base_dir=tmp_path,
        )
        assert cp.enabled is True
        # fresh run: nothing to resume from
        assert cp.should_resume is False
        assert cp.decision.reason == "no_checkpoint"

        cp.record(1, phase="observe", item_count=2, last_action="click")
        cp.record(4, phase="execute", item_count=9)
        saved = load_run_checkpoint("run_on", base_dir=tmp_path)
        assert saved is not None
        assert saved["turn"] == 4
        assert saved["status"] == "in_progress"
        assert saved["item_count"] == 9
        assert saved["goal"] == "g"

        cp.finish(True)
        assert load_run_checkpoint("run_on", base_dir=tmp_path) is None

    def test_failure_persists_failed_status(self, tmp_path: Path) -> None:
        cp = RunCheckpointer.begin("run_fail", resume=True, goal="g", base_dir=tmp_path)
        cp.record(6, item_count=3)
        cp.finish(False)
        saved = load_run_checkpoint("run_fail", base_dir=tmp_path)
        assert saved is not None
        assert saved["status"] == "failed"
        assert saved["turn"] == 6
        assert saved["item_count"] == 3

    def test_resumes_from_prior_and_marks_manifest(self, tmp_path: Path) -> None:
        # a prior failed run left a checkpoint
        prior = build_checkpoint_state(
            "run_r", goal="g", start_url="https://a.com",
            turn=5, status="failed", completed_steps=["s1", "s2"], item_count=4,
        )
        save_run_checkpoint("run_r", prior, base_dir=tmp_path)

        cp = RunCheckpointer.begin(
            "run_r", resume=True, goal="g", start_url="https://a.com", base_dir=tmp_path,
        )
        assert cp.should_resume is True
        assert cp.decision.from_turn == 5
        # manifest got resumed_from provenance
        raw = json.loads((tmp_path / "run_r" / "manifest.json").read_text(encoding="utf-8"))
        assert raw["resumed_from"]["turn"] == 5
        assert raw["resumed_from"]["completed_steps"] == 2

    def test_goal_mismatch_does_not_resume_or_mark(self, tmp_path: Path) -> None:
        prior = build_checkpoint_state("run_g", goal="old goal", turn=3, status="failed")
        save_run_checkpoint("run_g", prior, base_dir=tmp_path)
        cp = RunCheckpointer.begin("run_g", resume=True, goal="new goal", base_dir=tmp_path)
        assert cp.should_resume is False
        assert cp.decision.reason == "goal_mismatch"
        # no resume -> manifest provenance is never written (file stays absent)
        assert not (tmp_path / "run_g" / "manifest.json").exists()
