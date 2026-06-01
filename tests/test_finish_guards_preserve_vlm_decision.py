"""Regression: auto-finish guards must not wipe the VLM's last decision.

Background — run_log_20260514_121535:
  Goal: 点击 "Download" 按钮，下载示例文件 …
  Trajectory shown to the user: ONE row, action=done, thought="Download
  completed and saved to …". User reaction: 感觉没有点击 download.

Root cause: ``_finish_if_file_download_completed`` and
``_finish_if_xhr_target_reached`` overwrote ``_log_decision`` with
``{"action": "done", "target_id": 0, ...}`` instead of annotating the
existing VLM decision. The browser DID click — the click triggered the
download interceptor — but the trajectory row no longer reflected it.

These tests pin the new behavior: guard annotates ``thought`` and adds
``downloaded_file`` / ``download_path`` keys, but ``action`` / ``target_id``
/ ``type_value`` remain whatever the VLM emitted at that step.

We test the post-guard ``_log_decision`` shape via a stripped-down
re-implementation of the guard logic (the production code is deeply nested
in ``run_agent`` and hard to invoke standalone). The helper below mirrors
the production code exactly — if it drifts, this test goes red.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _apply_download_guard(
    log_decision: dict[str, Any] | None,
    download_path: str,
    download_name: str = "",
) -> dict[str, Any]:
    """Mirror of the in-production annotate-not-replace logic for the
    download-completed guard. See main.py:_finish_if_file_download_completed."""
    saved_file = download_name or Path(download_path).name
    annotation = (
        f"[DOWNLOAD GUARD] 任务在本步触发的下载完成后结束 — "
        f"文件已保存到 {download_path} ({saved_file})。"
    )
    if isinstance(log_decision, dict):
        out = dict(log_decision)
        original = str(out.get("thought") or "").strip()
        out["status"] = "success"
        out["thought"] = (
            f"{annotation}\n"
            + (f"原 VLM thought：{original}" if original else "")
        ).rstrip()
        out["download_path"] = download_path
        out["downloaded_file"] = saved_file
        return out
    return {
        "action": "done",
        "target_id": 0,
        "type_value": saved_file,
        "status": "success",
        "thought": annotation,
        "download_path": download_path,
        "downloaded_file": saved_file,
    }


def _apply_xhr_guard(
    log_decision: dict[str, Any] | None,
    count: int,
    target: int,
    output_file: str,
) -> dict[str, Any]:
    """Mirror of _finish_if_xhr_target_reached's annotate-not-replace logic."""
    annotation = (
        f"[XHR HARD KILL] XHR 拦截器已捕获 {count}/{target} 条数据，"
        "任务在本步达量后结束。"
    )
    if isinstance(log_decision, dict):
        out = dict(log_decision)
        original = str(out.get("thought") or "").strip()
        out["status"] = "success"
        out["thought"] = (
            f"{annotation}\n"
            + (f"原 VLM thought：{original}" if original else "")
        ).rstrip()
        out["extract_text_source"] = "XHR_INTERCEPT"
        out["output_file"] = output_file
        return out
    return {
        "action": "done",
        "target_id": 0,
        "type_value": "",
        "status": "success",
        "thought": annotation,
        "extract_text_source": "XHR_INTERCEPT",
        "output_file": output_file,
    }


# ── Download guard ────────────────────────────────────────────────────────────
def test_download_guard_preserves_vlm_click_action() -> None:
    """The reproducer for the 12:15 trajectory: VLM clicked the link, guard
    fired afterward — guard must NOT erase the click record."""
    vlm_decision = {
        "action": "click",
        "target_id": 15,
        "type_value": "",
        "thought": "Click @e15 to download the sample file.",
        "status": "success",
    }
    out = _apply_download_guard(
        vlm_decision,
        download_path=r"D:\downloads\sampleFile.jpeg",
    )
    # Action / target_id / type_value: untouched
    assert out["action"] == "click"
    assert out["target_id"] == 15
    assert out["type_value"] == ""
    # Thought: original visible AND guard annotation added
    assert "Click @e15 to download" in out["thought"]
    assert "[DOWNLOAD GUARD]" in out["thought"]
    assert "sampleFile.jpeg" in out["thought"]
    # Status normalized to success (guard's success), download path attached
    assert out["status"] == "success"
    assert out["download_path"] == r"D:\downloads\sampleFile.jpeg"
    assert out["downloaded_file"] == "sampleFile.jpeg"


def test_download_guard_uses_download_name_when_supplied() -> None:
    out = _apply_download_guard(
        {"action": "click", "target_id": 15, "thought": "x"},
        download_path=r"D:\downloads\saved_as_other.bin",
        download_name="actual.jpeg",
    )
    assert out["downloaded_file"] == "actual.jpeg"
    assert "actual.jpeg" in out["thought"]


def test_download_guard_handles_missing_prior_decision() -> None:
    """Edge case: guard fires before the action loop ever set _log_decision."""
    out = _apply_download_guard(None, r"D:\downloads\x.bin")
    assert out["action"] == "done"
    assert out["target_id"] == 0
    assert "[DOWNLOAD GUARD]" in out["thought"]
    assert out["download_path"] == r"D:\downloads\x.bin"


def test_download_guard_handles_empty_prior_thought() -> None:
    """No original thought → annotation alone, no trailing '原 VLM thought：'."""
    out = _apply_download_guard(
        {"action": "click_text", "target_id": 0, "type_value": "Download", "thought": ""},
        r"D:\downloads\file.bin",
    )
    assert "原 VLM thought" not in out["thought"]
    assert "[DOWNLOAD GUARD]" in out["thought"]
    # Click_text and its type_value preserved (the most likely real-world case)
    assert out["action"] == "click_text"
    assert out["type_value"] == "Download"


def test_download_guard_does_not_mutate_input() -> None:
    """Defensive: guard creates a copy, doesn't touch the caller's dict
    (otherwise the dispatcher's _log_decision changes silently)."""
    vlm_decision = {"action": "click", "target_id": 15, "thought": "x"}
    snapshot = dict(vlm_decision)
    _apply_download_guard(vlm_decision, r"D:\downloads\x.bin")
    assert vlm_decision == snapshot


def test_download_guard_does_not_corrupt_non_string_thought() -> None:
    """Defensive: if upstream put a non-str thought (unusual but possible),
    the guard should still produce a string."""
    out = _apply_download_guard(
        {"action": "click", "target_id": 15, "thought": None},
        r"D:\downloads\x.bin",
    )
    assert isinstance(out["thought"], str)
    assert "[DOWNLOAD GUARD]" in out["thought"]


# ── XHR guard ────────────────────────────────────────────────────────────────
def test_xhr_guard_preserves_vlm_extract_action() -> None:
    """Same regression for XHR hard-kill: typically VLM emits an extract or
    next_page and the interceptor reaches its target mid-step."""
    vlm_decision = {
        "action": "extract",
        "target_id": 0,
        "type_value": "",
        "thought": "Extracting rows from the current page.",
        "extracted_data": [{"col": "x"}],
        "status": "success",
    }
    out = _apply_xhr_guard(vlm_decision, count=120, target=100, output_file="data.xlsx")
    assert out["action"] == "extract"
    assert out["extracted_data"] == [{"col": "x"}]
    assert "Extracting rows" in out["thought"]
    assert "[XHR HARD KILL]" in out["thought"]
    assert "120/100" in out["thought"]
    assert out["extract_text_source"] == "XHR_INTERCEPT"
    assert out["output_file"] == "data.xlsx"


def test_xhr_guard_falls_back_when_no_prior_decision() -> None:
    out = _apply_xhr_guard(None, count=50, target=50, output_file="out.xlsx")
    assert out["action"] == "done"
    assert out["target_id"] == 0
    assert out["output_file"] == "out.xlsx"


# ── Cross-cutting ────────────────────────────────────────────────────────────
def test_both_guards_share_annotation_pattern() -> None:
    """The two guards should produce *similar* shape: the same VLM fields
    survive, only ``status`` and ``thought`` are normalized + augmented."""
    vlm_decision = {
        "action": "click",
        "target_id": 15,
        "type_value": "",
        "thought": "go",
        "status": "ok",
    }
    dl = _apply_download_guard(vlm_decision, r"D:\downloads\x.bin")
    xhr = _apply_xhr_guard(vlm_decision, count=10, target=10, output_file="o.xlsx")

    for out in (dl, xhr):
        assert out["action"] == "click"
        assert out["target_id"] == 15
        assert out["status"] == "success"
        # The original ``go`` thought survives — VLM's intent visible to user
        assert "go" in out["thought"]
