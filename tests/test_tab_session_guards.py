"""Unit tests for tab_session_guards."""

from __future__ import annotations

from visual_web_agent.tab_session_guards import (
    _registrable_suffix,
    apply_tab_session_guards,
    goal_requests_tab_return,
    infer_tab_session_anchor,
    normalize_switch_tab_decision,
    url_matches_start_host,
)


# ── Goal parsing ───────────────────────────────────────────────────────────────
def test_goal_requests_tab_return() -> None:
    assert goal_requests_tab_return("切换回第一个标签页再输入")
    assert goal_requests_tab_return("回到原标签继续操作")
    assert goal_requests_tab_return("切回起始页")
    assert not goal_requests_tab_return("只点击第一个结果")


def test_infer_anchor() -> None:
    assert infer_tab_session_anchor("切回最初的标签页") == 0
    assert infer_tab_session_anchor("提取表格") is None


# ── switch_tab normalization (no magic IDs) ────────────────────────────────────
def test_normalize_switch_tab_target_id_yields_to_type_value() -> None:
    d = {"action": "switch_tab", "target_id": 21, "type_value": "0"}
    normalize_switch_tab_decision(d)
    assert d["target_id"] == 0
    assert d["type_value"] == "0"


def test_normalize_switch_tab_no_magic_id_threshold() -> None:
    # Old impl only fired for tid > 9 or tid in {21,12,15,18}. New impl trusts
    # any digit type_value regardless of target_id size.
    d = {"action": "switch_tab", "target_id": 3, "type_value": "1"}
    normalize_switch_tab_decision(d)
    assert d["target_id"] == 1
    assert d["type_value"] == "1"


def test_normalize_switch_tab_missing_type_value_falls_back() -> None:
    d = {"action": "switch_tab", "target_id": 2, "type_value": ""}
    normalize_switch_tab_decision(d)
    assert d["target_id"] == 2
    assert d["type_value"] == "2"


def test_normalize_switch_tab_ignores_non_switch_actions() -> None:
    d = {"action": "click", "target_id": 21, "type_value": "0"}
    normalize_switch_tab_decision(d)
    assert d["target_id"] == 21
    assert d["type_value"] == "0"


# ── URL host comparison (suffix-based, no substring false positives) ───────────
def test_url_matches_start_host_subdomains() -> None:
    assert url_matches_start_host("https://cn.bing.com/search?q=x", "www.bing.com")
    assert url_matches_start_host("https://www.bing.com/", "bing.com")


def test_url_matches_start_host_rejects_substring_false_positive() -> None:
    # The OLD substring impl matched 'bing.com' inside 'bingo.com'. The new
    # netloc-suffix impl must not.
    assert not url_matches_start_host("https://www.bingo.com/", "bing.com")
    assert not url_matches_start_host("https://x.bingbar.com/", "www.bing.com")


def test_registrable_suffix_compound_tld() -> None:
    assert _registrable_suffix("a.b.google.co.uk") == "google.co.uk"
    assert _registrable_suffix("cn.bing.com") == "bing.com"
    assert _registrable_suffix("example.com") == "example.com"


# ── Anchor pre-empt ────────────────────────────────────────────────────────────
class _FakeBrowser:
    def __init__(self, *, url: str, tab_index: int) -> None:
        self._url = url
        self._idx = tab_index

    @property
    def current_url(self) -> str:
        return self._url

    def get_active_tab_index(self) -> int:
        return self._idx


def _decision(action: str, **overrides) -> dict:
    base = {
        "action": action,
        "target_id": 5,
        "type_value": "",
        "thought": "",
        "status": "success",
        "memory_key": "",
    }
    base.update(overrides)
    return base


def test_prepend_anchor_before_type() -> None:
    g = "打开后切回第一个标签页。在搜索框重新输入「x」。"
    assert infer_tab_session_anchor(g) == 0
    decisions = [_decision("type", target_id=5, type_value="hello", thought="输入")]
    br = _FakeBrowser(url="https://evil.example/", tab_index=2)
    assert (
        apply_tab_session_guards(
            br, decisions, goal=g, start_url="https://www.bing.com/", tab_anchor_index=0
        )
        is None
    )
    assert len(decisions) == 2
    assert decisions[0]["action"] == "switch_tab"
    assert decisions[0]["target_id"] == 0
    assert decisions[1]["action"] == "type"


def test_prepend_anchor_before_click() -> None:
    """The failure mode in the Bing log was repeated `click @e21` on the wrong
    tab — preempt must cover click, not only type."""
    g = "切回第一个标签页继续"
    decisions = [_decision("click", target_id=21)]
    br = _FakeBrowser(url="https://www.aidaxue.com/foo", tab_index=4)
    assert (
        apply_tab_session_guards(
            br, decisions, goal=g, start_url="https://www.bing.com/", tab_anchor_index=0
        )
        is None
    )
    assert len(decisions) == 2
    assert decisions[0]["action"] == "switch_tab"
    assert decisions[0]["target_id"] == 0
    assert decisions[1]["action"] == "click"


def test_prepend_anchor_before_click_text_and_press_key() -> None:
    g = "切回起始标签"
    for action_name in ("click_text", "click_new_tab", "press_key", "find_text"):
        decisions = [_decision(action_name, target_id=7, type_value="Enter")]
        br = _FakeBrowser(url="https://www.aidaxue.com/", tab_index=3)
        apply_tab_session_guards(
            br, decisions, goal=g, start_url="https://www.bing.com/", tab_anchor_index=0
        )
        assert decisions[0]["action"] == "switch_tab", action_name
        assert decisions[1]["action"] == action_name


def test_no_preempt_when_already_on_anchor_tab() -> None:
    g = "切回第一个标签页"
    decisions = [_decision("click", target_id=21)]
    br = _FakeBrowser(url="https://www.bing.com/search?q=x", tab_index=0)
    assert (
        apply_tab_session_guards(
            br, decisions, goal=g, start_url="https://www.bing.com/", tab_anchor_index=0
        )
        is None
    )
    assert len(decisions) == 1
    assert decisions[0]["action"] == "click"


def test_no_preempt_for_meta_actions() -> None:
    g = "切回第一个标签页"
    for meta in ("switch_tab", "close_tab", "wait", "done", "ask_human", "goto", "extract"):
        decisions = [_decision(meta)]
        br = _FakeBrowser(url="https://www.aidaxue.com/", tab_index=4)
        apply_tab_session_guards(
            br, decisions, goal=g, start_url="https://www.bing.com/", tab_anchor_index=0
        )
        assert len(decisions) == 1, meta
        assert decisions[0]["action"] == meta, meta


# ── Reality check (thought vs URL) ─────────────────────────────────────────────
def test_reality_check_coerces_to_switch_when_anchor_set() -> None:
    g = "切回第一个标签页"
    decisions = [
        _decision(
            "click",
            target_id=9,
            thought="已成功切换回起始搜索页，当前页面是结果页",
        )
    ]
    br = _FakeBrowser(url="https://www.aidaxue.com/foo", tab_index=2)
    assert (
        apply_tab_session_guards(
            br, decisions, goal=g, start_url="https://www.bing.com/", tab_anchor_index=0
        )
        is None
    )
    assert decisions[0]["action"] == "switch_tab"
    assert decisions[0]["target_id"] == 0
    assert "REALITY CHECK" in decisions[0]["thought"]


def test_reality_check_returns_msg_when_no_anchor() -> None:
    decisions = [
        _decision(
            "click",
            target_id=9,
            thought="已切换回起始页，当前已在搜索结果页",
        )
    ]
    br = _FakeBrowser(url="https://www.aidaxue.com/x", tab_index=2)
    msg = apply_tab_session_guards(
        br,
        decisions,
        goal="点击第一个非广告结果",  # no return-tab phrase → no anchor
        start_url="https://www.bing.com/",
        tab_anchor_index=None,
    )
    assert msg is not None
    assert "REALITY CHECK" in msg
    # Head decision left intact for caller to coerce to wait + inject feedback
    assert decisions[0]["action"] == "click"


def test_reality_check_passive_when_url_actually_matches_start() -> None:
    """Thought may sound boastful, but if URL truly matches start_host, no
    reality-check coercion should fire."""
    g = "切回第一个标签页"
    decisions = [
        _decision(
            "click",
            target_id=12,
            thought="已切换回起始搜索页",
        )
    ]
    br = _FakeBrowser(url="https://cn.bing.com/search?q=x", tab_index=0)
    apply_tab_session_guards(
        br, decisions, goal=g, start_url="https://www.bing.com/", tab_anchor_index=0
    )
    # Already on bing → reality check passes; on anchor tab → no preempt;
    # head decision untouched.
    assert decisions[0]["action"] == "click"
    assert decisions[0]["target_id"] == 12


def test_site_agnostic_no_brand_hardcoding() -> None:
    """Sanity: the new module must work for a non-Bing scenario without code
    changes — same protection on Google + Douyin landing."""
    g = "切回第一个标签页"
    decisions = [_decision("click", target_id=33)]
    br = _FakeBrowser(url="https://www.douyin.com/video/123", tab_index=5)
    apply_tab_session_guards(
        br, decisions, goal=g, start_url="https://www.google.com/", tab_anchor_index=0
    )
    assert decisions[0]["action"] == "switch_tab"
    assert decisions[0]["target_id"] == 0
