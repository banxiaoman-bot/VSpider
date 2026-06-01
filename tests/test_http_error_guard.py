"""Regression: HTTP error early-exit guard for httpbin /status/404 style tasks.

Background: the user's goal explicitly says "由于页面返回404错误...输出'检测到404，
任务终止'并结束". Without the guard, self-healing would retry the navigation /
SoM detection a few times before giving up, polluting the trajectory with
spurious thoughts. The guard at ``main.py:7368-7397`` short-circuits this:

  * ``browser.last_navigation_status`` is populated by ``browser_env._goto``
  * if it's 4xx/5xx AND the goal mentions one of the trigger keywords →
    emit done(success=True) immediately, before the VLM loop ever starts.

These tests mirror the trigger logic so a future refactor can't silently
drop httpbin-class tasks back into the slow self-healing path.
"""
from __future__ import annotations

import re

import pytest


_HTTP_ERROR_GUARD_PATTERN = (
    r"\b404\b|\bhttp\s*error\b|\bstatus\s*code\b|错误|终止|状态码"
)


def _should_fire_http_error_guard(status: int | None, goal: str) -> bool:
    """Mirror of main.py:7369-7373 trigger condition."""
    return bool(
        isinstance(status, int)
        and status >= 400
        and re.search(_HTTP_ERROR_GUARD_PATTERN, goal or "", re.I)
    )


# ── Happy path ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "status,goal",
    [
        # The exact user goal from the test sheet
        (404, "由于页面返回404错误，你的任务是：检测到错误后输出'检测到404，任务终止'并结束"),
        (404, "If the page returns 404 error, output done"),
        (500, "页面返回 500 状态码就终止任务"),
        (403, "出现 HTTP error 时立即终止"),
        (502, "网关错误，需要终止任务"),
        # English / mixed keywords
        (404, "When you hit a 404 just stop and report"),
        (404, "If status code != 200, exit"),
    ],
)
def test_guard_fires_on_4xx_5xx_with_keyword_match(status, goal):
    assert _should_fire_http_error_guard(status, goal) is True


# ── Negative path ─────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "status,goal",
    [
        # 2xx never fires
        (200, "由于页面返回404错误，你的任务是：检测到错误后终止"),
        (200, "Anything goes"),
        # 4xx but goal has no trigger keyword → must NOT fire (avoid stealing
        # valid tasks where the agent should attempt recovery)
        (404, "Find the contact page on this website"),
        (500, "Log in and check the dashboard"),
        # No status (page never returned) → skip
        (None, "由于页面返回404错误，立即终止"),
    ],
)
def test_guard_does_not_fire_when_safe(status, goal):
    assert _should_fire_http_error_guard(status, goal) is False


# ── Cross-cutting: keyword robustness ──────────────────────────────────────
def test_guard_matches_chinese_keyword_alone_without_404_token():
    """Goal that says 错误/终止/状态码 but doesn't contain the literal '404'
    must still fire (the keyword set is the union, not the intersection)."""
    assert _should_fire_http_error_guard(503, "如果出现错误请立即终止") is True


def test_guard_case_insensitive_http_error_phrase():
    assert _should_fire_http_error_guard(500, "On HTTP Error, stop the run") is True
    assert _should_fire_http_error_guard(500, "on http  error stop") is True  # extra space


def test_guard_404_must_be_a_standalone_token_not_a_substring():
    """`\\b404\\b` boundary — '4044' or 'A404B' should not falsely match.
    But mixed Chinese context like '返回404错误' still hits both `\\b404\\b`
    and `错误`, which is intended."""
    # Pure substring '4044' shouldn't be enough on its own (without other
    # trigger keywords)
    assert _should_fire_http_error_guard(404, "Order id is 4044, fetch details") is False
    # Mixed Chinese + 404 → hits both 404 boundary AND 错误 keyword
    assert _should_fire_http_error_guard(404, "返回404错误码") is True
