"""Tests for the mid-task session-expiry sniffer.

The contract:
  - Detect when the agent was working on a business URL and the current
    URL suddenly looks like a login/auth surface (cookie expiry or risk
    control bouncing us).
  - Capture the business URL so the main loop can goto back after the
    user resolves the login via ask_human.
  - Be CONSERVATIVE about firing — false positives convert real progress
    into wasted ask_human prompts. Specifically:
      * never fire on step 1 (no prior business URL)
      * never fire on chat hosts (CHAT DRIFT GUARD owns that)
      * never fire if initial start URL already was login-shaped
        (legitimate "登录后...." task)
      * never fire on pure query-string changes that happen to contain
        the word "login" by coincidence
"""

from __future__ import annotations

import pytest

from visual_web_agent.session_drop_guard import (
    SessionDropSignal,
    build_feedback,
    detect_session_drop,
    looks_like_login_url,
)


# ── looks_like_login_url ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "https://passport.taobao.com/login.htm?redirect=...",
        "https://login.bilibili.com/login",
        "https://sso.example.com/auth/realms/foo/login",
        "https://www.example.com/account/login",
        "https://accounts.google.com/signin/oauth",
        "https://www.zhihu.com/signin?next=/",
        "https://auth.example.com/oauth/callback",
        "https://id.example.com/account/sign-in",
    ],
)
def test_looks_like_login_positive(url: str) -> None:
    assert looks_like_login_url(url) is True, f"missed login URL: {url}"


@pytest.mark.parametrize(
    "url",
    [
        "https://trade.taobao.com/orderlist.htm?page=2",
        "https://www.bilibili.com/video/BV1abc",
        "https://www.zhihu.com/question/12345",
        "https://yiyan.baidu.com/chat/abc",
        "https://chat.openai.com/c/123",
        "https://news.ycombinator.com/",
        "https://en.wikipedia.org/wiki/Python",
        "",
        "not a url",
    ],
)
def test_looks_like_login_negative(url: str) -> None:
    assert looks_like_login_url(url) is False, f"false positive on: {url}"


# ── detect_session_drop core cases ──────────────────────────────────────────


def test_fires_on_mid_task_redirect_to_login() -> None:
    sig = detect_session_drop(
        current_url="https://passport.taobao.com/login.htm",
        last_business_url="https://trade.taobao.com/orderlist.htm?page=2",
        initial_url="https://trade.taobao.com/orderlist.htm",
        step=9,
    )
    assert sig is not None
    assert sig.return_url == "https://trade.taobao.com/orderlist.htm?page=2"
    assert "passport.taobao.com" in sig.current_url


def test_does_not_fire_on_step_1() -> None:
    """No "previous business URL" to return to → can't fire."""
    sig = detect_session_drop(
        current_url="https://passport.taobao.com/login.htm",
        last_business_url="",
        initial_url="https://trade.taobao.com/",
        step=1,
    )
    assert sig is None


def test_does_not_fire_when_initial_url_was_login() -> None:
    """User explicitly asked to start on a login page — that's a real task,
    not a session drop."""
    sig = detect_session_drop(
        current_url="https://passport.taobao.com/login.htm?from=...",
        last_business_url="https://passport.taobao.com/login.htm",
        initial_url="https://passport.taobao.com/login.htm",
        step=2,
    )
    assert sig is None


def test_does_not_fire_when_current_not_login_shaped() -> None:
    sig = detect_session_drop(
        current_url="https://trade.taobao.com/orderlist.htm?page=3",
        last_business_url="https://trade.taobao.com/orderlist.htm?page=2",
        initial_url="https://trade.taobao.com/",
        step=5,
    )
    assert sig is None


def test_does_not_fire_when_last_business_url_already_login() -> None:
    """If the previous URL was already login-shaped, the tracker mis-fed
    us — don't compound the error."""
    sig = detect_session_drop(
        current_url="https://passport.taobao.com/login.htm?step=2",
        last_business_url="https://passport.taobao.com/login.htm",
        initial_url="https://trade.taobao.com/",
        step=5,
    )
    assert sig is None


def test_does_not_fire_on_chat_hosts() -> None:
    """yiyan.baidu.com/search/... is normal — CHAT DRIFT GUARD's job, not ours."""
    sig = detect_session_drop(
        current_url="https://yiyan.baidu.com/login?from=chat",
        last_business_url="https://yiyan.baidu.com/chat/abc",
        initial_url="https://yiyan.baidu.com/",
        step=3,
    )
    assert sig is None


def test_does_not_fire_on_query_param_only_change() -> None:
    """Same origin AND same path — likely a query-param refresh that
    coincidentally contains 'login' (e.g. a deep-link return URL with
    redirect=...login...). Skip to avoid false alarm."""
    sig = detect_session_drop(
        current_url="https://example.com/?redirect=login",
        last_business_url="https://example.com/?redirect=other",
        initial_url="https://example.com/",
        step=3,
    )
    # Path "/" is not login-shaped, so this falls through anyway
    assert sig is None


def test_cross_origin_to_login_fires() -> None:
    """The canonical case: business host → completely different auth host."""
    sig = detect_session_drop(
        current_url="https://accounts.google.com/signin",
        last_business_url="https://mail.google.com/mail/u/0/#inbox",
        initial_url="https://mail.google.com/",
        step=4,
    )
    assert sig is not None
    assert "mail.google.com" in sig.return_url


def test_same_origin_path_change_to_login_fires() -> None:
    """E.g. taobao.com/orderlist → taobao.com/login. Same origin but
    different path that is login-shaped."""
    sig = detect_session_drop(
        current_url="https://www.example.com/account/login",
        last_business_url="https://www.example.com/dashboard",
        initial_url="https://www.example.com/",
        step=6,
    )
    assert sig is not None


# ── Edge cases ──────────────────────────────────────────────────────────────


def test_missing_inputs_return_none() -> None:
    assert detect_session_drop(
        current_url="", last_business_url="", step=3,
    ) is None


def test_malformed_urls_do_not_raise() -> None:
    """URL parsing failures must yield None, not exceptions."""
    sig = detect_session_drop(
        current_url="not a url",
        last_business_url="also not a url",
        step=2,
    )
    assert sig is None


def test_build_feedback_includes_return_url() -> None:
    sig = SessionDropSignal(
        return_url="https://trade.taobao.com/orderlist.htm?page=2",
        current_url="https://passport.taobao.com/login.htm",
        reason="test",
    )
    msg = build_feedback(sig)
    assert "trade.taobao.com" in msg
    assert "passport.taobao.com" in msg
    assert "ask_human" in msg or "ask human" in msg.lower() or "登录" in msg


# ── Realistic trajectory smoke test ─────────────────────────────────────────


def test_realistic_taobao_session_drop() -> None:
    """8 steps on trade.taobao.com → step 9 lands on passport. Captures
    the most recent orderlist URL and arms ask_human."""
    last = "https://trade.taobao.com/orderlist.htm?page=2&filter=all"
    sig = detect_session_drop(
        current_url="https://passport.taobao.com/login.htm?style=mini&from=tbtop&redirect_url=https%3A%2F%2Ftrade.taobao.com%2Forderlist.htm%3Fpage%3D2%26filter%3Dall",
        last_business_url=last,
        initial_url="https://trade.taobao.com/orderlist.htm",
        step=9,
    )
    assert sig is not None
    assert sig.return_url == last
    # The return URL is the one we recorded, NOT the login-page's redirect
    # query (which would be auth-decorated and might 302 again).


def test_long_running_paginated_extraction_session_drop() -> None:
    """Step 25, deep pagination, suddenly cookie expires."""
    last = "https://www.example.com/list?page=25&sort=date"
    sig = detect_session_drop(
        current_url="https://accounts.example.com/login",
        last_business_url=last,
        initial_url="https://www.example.com/list",
        step=25,
    )
    assert sig is not None
    assert sig.return_url == last


# ── Real-world scenario: VLM gives up with done/error on login wall ─────────


def test_baidu_yiyan_to_passport_redirect_fires() -> None:
    """run_log_20260518_153055 exact pattern:
    yiyan.baidu.com (business) → passport.baidu.com (login popup)
    VLM emits done+status=error — guard MUST capture the return URL
    before _consecutive_errors crashes the run."""
    sig = detect_session_drop(
        current_url="https://passport.baidu.com/v2/?reg&tt=1779089533593&overseas=1&gid=A1638FA-138D-",
        last_business_url="https://yiyan.baidu.com/",
        initial_url="https://yiyan.baidu.com/",
        step=5,
    )
    assert sig is not None
    assert sig.return_url == "https://yiyan.baidu.com/"
    # The reason should mention both URLs for debuggability
    assert "yiyan.baidu.com" in sig.reason or "passport" in sig.reason


def test_passport_url_with_overseas_param_recognised_as_login() -> None:
    """passport.baidu.com query-string variants must still be caught."""
    assert looks_like_login_url(
        "https://passport.baidu.com/v2/?reg&tt=1779089533593&overseas=1"
    ) is True
    assert looks_like_login_url("https://passport.baidu.com/") is True
    assert looks_like_login_url("https://passport.baidu.com/v2/index") is True
