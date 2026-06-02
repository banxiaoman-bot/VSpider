"""Slice BATCH-RESUME1: batch-row resume (skip already-successful rows).

The batch orchestrator already skips rows whose ``填报状态 == "成功"``
(``batch_orchestrator.run_batch_rows_parallel`` L126-129). The only missing
piece for resume was feeding last run's progress back in: these pure helpers
in ``smart_batch_runner`` merge a prior ``_处理结果.xlsx`` into a freshly loaded
DataFrame so the orchestrator's existing skip logic resumes instead of
re-running everything.

Pure-DataFrame tests, no agent / browser / network.
"""

from __future__ import annotations

import pandas as pd

from smart_batch_runner import _merge_prior_progress, _row_resume_key


# --- _row_resume_key -------------------------------------------------------

def test_row_resume_key_prefers_url_column() -> None:
    df = pd.DataFrame({"项目编号": [1, 2], "URL": ["a", "b"]})
    assert _row_resume_key(df) == "URL"


def test_row_resume_key_matches_chinese_url_column() -> None:
    df = pd.DataFrame({"网址": ["a"], "name": ["x"]})
    assert _row_resume_key(df) == "网址"


def test_row_resume_key_empty_when_no_url_column() -> None:
    df = pd.DataFrame({"项目编号": [1], "name": ["x"]})
    assert _row_resume_key(df) == ""


# --- _merge_prior_progress (keyed) -----------------------------------------

def test_merge_by_key_carries_only_success() -> None:
    df = pd.DataFrame({"url": ["a", "b", "c"]})
    df["填报状态"] = "未处理"
    df["日志备注"] = ""
    prior = pd.DataFrame({
        "url": ["a", "b", "c"],
        "填报状态": ["成功", "失败", "未处理"],
        "日志备注": ["ok", "err", ""],
    })
    merged, resumed = _merge_prior_progress(df, prior, key_col="url")
    assert resumed == 1
    statuses = dict(zip(merged["url"], merged["填报状态"]))
    assert statuses["a"] == "成功"   # carried over
    assert statuses["b"] == "未处理"  # failed -> retried
    assert statuses["c"] == "未处理"  # never done -> run


def test_merge_by_key_handles_reorder_and_new_rows() -> None:
    df = pd.DataFrame({"url": ["c", "a", "d"]})  # reordered + new row d
    df["填报状态"] = "未处理"
    df["日志备注"] = ""
    prior = pd.DataFrame({"url": ["a", "b", "c"], "填报状态": ["成功", "成功", "失败"]})
    merged, resumed = _merge_prior_progress(df, prior, key_col="url")
    statuses = dict(zip(merged["url"], merged["填报状态"]))
    assert statuses["a"] == "成功"   # matched by key despite reorder
    assert statuses["c"] == "未处理"  # was 失败 -> retry
    assert statuses["d"] == "未处理"  # brand-new row
    assert resumed == 1


# --- _merge_prior_progress (positional fallback) ---------------------------

def test_merge_positional_fallback_without_key() -> None:
    df = pd.DataFrame({"name": ["x", "y", "z"]})
    df["填报状态"] = "未处理"
    df["日志备注"] = ""
    prior = pd.DataFrame({"name": ["x", "y", "z"], "填报状态": ["成功", "未处理", "成功"]})
    merged, resumed = _merge_prior_progress(df, prior, key_col="")
    assert list(merged["填报状态"]) == ["成功", "未处理", "成功"]
    assert resumed == 2


def test_merge_positional_shorter_prior_is_safe() -> None:
    df = pd.DataFrame({"name": ["x", "y", "z"]})
    df["填报状态"] = "未处理"
    df["日志备注"] = ""
    prior = pd.DataFrame({"name": ["x"], "填报状态": ["成功"]})
    merged, resumed = _merge_prior_progress(df, prior, key_col="")
    assert list(merged["填报状态"]) == ["成功", "未处理", "未处理"]
    assert resumed == 1


# --- guards ----------------------------------------------------------------

def test_merge_noop_when_prior_has_no_status_column() -> None:
    df = pd.DataFrame({"url": ["a"]})
    df["填报状态"] = "未处理"
    df["日志备注"] = ""
    prior = pd.DataFrame({"url": ["a"]})  # no 填报状态
    merged, resumed = _merge_prior_progress(df, prior, key_col="url")
    assert resumed == 0
    assert merged["填报状态"].iloc[0] == "未处理"


def test_merge_does_not_downgrade_already_success_row() -> None:
    df = pd.DataFrame({"url": ["a"]})
    df["填报状态"] = "成功"  # already success in current df
    df["日志备注"] = "fresh"
    prior = pd.DataFrame({"url": ["a"], "填报状态": ["成功"], "日志备注": ["old"]})
    merged, resumed = _merge_prior_progress(df, prior, key_col="url")
    # already 成功 -> not re-counted, note preserved
    assert resumed == 0
    assert merged["填报状态"].iloc[0] == "成功"
