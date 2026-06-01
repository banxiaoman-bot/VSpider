"""
新模块单元测试：
- loop_detector
- judge
- a11y_enhancer
- message_compaction
- failure_classifier
- prompts_templates
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# ─── Loop Detector Tests ────────────────────────────────────────────────────

from loop_detector import (
    ActionLoopDetector,
    LoopDetectorConfig,
    PageFingerprint,
)


def _make_fp(url="https://example.com", title="Example", elem=10, scroll=0):
    return PageFingerprint(url=url, title=title, element_count=elem, scroll_position=scroll)


def test_loop_detector_no_false_positive_on_varied_actions():
    """不同 action 不应触发循环检测。"""
    det = ActionLoopDetector(config=LoopDetectorConfig(
        window_size=5, action_repeat_threshold=3, stagnation_threshold=5,
    ))
    actions = [
        {"action": "click", "target_id": 1, "type_value": "", "thought": "Click the submit button"},
        {"action": "type", "target_id": 2, "type_value": "hello", "thought": "Type username into input field"},
        {"action": "scroll", "target_id": 0, "type_value": "down", "thought": "Scroll down to see more content"},
        {"action": "click", "target_id": 3, "type_value": "", "thought": "Click the next page link"},
    ]
    for i, act in enumerate(actions):
        fp = _make_fp(scroll=i * 100)
        result = det.check(act, fp, step=i)
        assert not result.loop_detected, f"Step {i} should not trigger loop"


def test_loop_detector_detects_action_repeat():
    """连续相同 action 应触发循环检测。"""
    det = ActionLoopDetector(config=LoopDetectorConfig(
        window_size=5, action_repeat_threshold=3, stagnation_threshold=5,
    ))
    same_action = {"action": "click", "target_id": 5, "type_value": ""}
    fp = _make_fp()
    for i in range(4):
        result = det.check(same_action, fp, step=i)
    assert result.loop_detected
    assert result.loop_type == "action_repeat"


def test_loop_detector_detects_page_stagnation():
    """页面指纹不变应触发停滞检测。"""
    det = ActionLoopDetector(config=LoopDetectorConfig(
        window_size=8, action_repeat_threshold=10, stagnation_threshold=3,
    ))
    fp = _make_fp()
    for i in range(4):
        act = {"action": "click", "target_id": i + 1, "type_value": ""}
        result = det.check(act, fp, step=i)
    assert result.loop_detected
    assert result.loop_type == "page_stagnation"


def test_loop_detector_reset():
    """reset() 应清空历史。"""
    det = ActionLoopDetector()
    det.check({"action": "click", "target_id": 1, "type_value": ""}, _make_fp(), 0)
    det.reset()
    assert len(det._history) == 0
    assert det._nudge_count == 0


# ─── Judge Tests ─────────────────────────────────────────────────────────────

from judge import TaskJudge, JudgeConfig, JudgeResult


def test_judge_disabled_always_passes():
    """Judge 关闭时 evaluate 应直接返回 passed=True。"""
    judge = TaskJudge(vlm_client=None, config=JudgeConfig(enabled=False))
    result = asyncio.run(judge.evaluate(
        goal="test",
        screenshot_b64=None,
        history_summary="did stuff",
        current_url="https://example.com",
        page_title="Example",
    ))
    assert result.passed is True
    assert result.verdict == "pass"


def test_judge_config_defaults():
    """JudgeConfig 默认值正确。"""
    cfg = JudgeConfig()
    assert cfg.enabled is True
    assert cfg.max_retries_after_fail == 2
    assert 0 < cfg.pass_threshold <= 1.0


def test_judge_result_dataclass():
    """JudgeResult 字段正确。"""
    r = JudgeResult(
        verdict="fail",
        passed=False,
        confidence=0.85,
        reasoning="not done",
        rejection_reason="still loading",
        suggested_action="wait for page",
    )
    assert not r.passed
    assert r.confidence == 0.85
    assert r.suggested_action == "wait for page"


# ─── A11y Enhancer Tests ────────────────────────────────────────────────────

from a11y_enhancer import A11yEnhancer, A11yEnhancerConfig, PageMetadata


def test_a11y_enhancer_passthrough_empty():
    """空输入应返回空结果。"""
    enhancer = A11yEnhancer()
    result = enhancer.enhance("", PageMetadata(url="https://example.com"))
    assert result.text == ""


def test_a11y_enhancer_preserves_content():
    """增强后内容应包含原始 AX Tree 关键信息。"""
    ax_tree = """
[ID:1] button "Submit" focused
[ID:2] textbox "Username" value="admin"
[ID:3] link "Home"
[ID:4] checkbox "Remember me" checked
""".strip()
    enhancer = A11yEnhancer(config=A11yEnhancerConfig(
        enable_grouping=True,
        enable_state_annotation=True,
        enable_hidden_hints=True,
        max_output_chars=15000,
    ))
    meta = PageMetadata(
        url="https://example.com/login",
        title="Login Page",
        total_elements=10,
        visible_elements=4,
    )
    result = enhancer.enhance(ax_tree, meta)
    assert "Submit" in result.text
    assert "Username" in result.text
    assert len(result.text) > 0


def test_a11y_enhancer_truncation():
    """超长 AX Tree 应被截断到 max_output_chars。"""
    ax_tree = "[ID:1] textbox 'Name'\n" * 2000  # ~40K chars
    enhancer = A11yEnhancer(config=A11yEnhancerConfig(max_output_chars=500))
    result = enhancer.enhance(ax_tree, PageMetadata(url="https://example.com"))
    assert len(result.text) <= 600  # 允许截断消息的额外开销


def test_a11y_enhancer_disabled_features():
    """关闭所有增强功能时应基本保持原文。"""
    ax_tree = "[ID:1] button 'OK'\n[ID:2] link 'Cancel'"
    enhancer = A11yEnhancer(config=A11yEnhancerConfig(
        enable_grouping=False,
        enable_state_annotation=False,
        enable_hidden_hints=False,
    ))
    result = enhancer.enhance(ax_tree, PageMetadata(url="https://example.com"))
    assert "OK" in result.text
    assert "Cancel" in result.text


# ─── Message Compaction Tests ────────────────────────────────────────────────

from message_compaction import MessageCompactor, CompactionConfig


def test_compaction_config_defaults():
    """CompactionConfig 默认值合理。"""
    cfg = CompactionConfig()
    assert cfg.trigger_count > 0
    assert cfg.keep_last_items > 0
    assert cfg.compact_cooldown_steps >= 0


def test_compactor_below_threshold_no_compact():
    """历史条数低于阈值时不应触发压缩。"""
    compactor = MessageCompactor(config=CompactionConfig(
        trigger_count=10, trigger_char_count=50000, keep_last_items=3, compact_cooldown_steps=0,
    ))
    history = [{"role": "user", "content": f"msg {i}"} for i in range(5)]
    should = compactor.needs_compaction(history, current_step=5)
    assert not should


def test_compactor_above_threshold_should_compact():
    """历史条数超过阈值时应触发压缩。"""
    compactor = MessageCompactor(config=CompactionConfig(
        trigger_count=5, trigger_char_count=50000, keep_last_items=3, compact_cooldown_steps=0,
    ))
    history = [{"role": "user", "content": f"msg {i}"} for i in range(8)]
    should = compactor.needs_compaction(history, current_step=8)
    assert should


def test_compactor_cooldown_blocks():
    """冷却期内不应触发压缩。"""
    compactor = MessageCompactor(config=CompactionConfig(
        trigger_count=3, trigger_char_count=50000, keep_last_items=2, compact_cooldown_steps=5,
    ))
    history = [{"role": "user", "content": f"msg {i}"} for i in range(10)]
    compactor.state.last_compact_step = 8
    should = compactor.needs_compaction(history, current_step=10)
    assert not should


def test_compactor_compact_keeps_recent():
    """compact 应保留最近 N 条。"""
    compactor = MessageCompactor(config=CompactionConfig(
        trigger_count=3, trigger_char_count=50000, keep_last_items=2, compact_cooldown_steps=0,
    ))
    history = [
        {"thought": "Navigate", "action": "goto", "result": "ok"},
        {"thought": "Click", "action": "click", "result": "ok"},
        {"thought": "Extract", "action": "extract", "result": "5 rows"},
        {"thought": "Save", "action": "done", "result": "saved"},
    ]
    result = asyncio.run(compactor.compact(history, current_step=4, goal="test"))
    # 应保留最近 2 条
    assert len(result) <= len(history)
    assert result[-1]["action"] == "done"


# ─── Failure Classifier Tests ────────────────────────────────────────────────

from failure_classifier import classify_and_log, FailureStats


def test_failure_classifier_timeout():
    """超时异常应分类为 PAGE_LOAD_TIMEOUT。"""
    result = classify_and_log(
        error_msg="page load timed out",
        exception=TimeoutError("page load timed out"),
        step=1,
    )
    assert result is not None
    assert "TIMEOUT" in result.category.value or "timeout" in result.category.value.lower()


def test_failure_classifier_element_not_found():
    """元素未找到异常应分类为 ELEMENT_NOT_FOUND。"""
    result = classify_and_log(
        error_msg="Element not found: #btn-submit",
        exception=Exception("Element not found"),
        step=2,
    )
    assert result is not None
    assert "NOT_FOUND" in result.category.value


def test_failure_classifier_unknown():
    """未知异常应有分类结果（fallback 到 UNKNOWN 或其他）。"""
    result = classify_and_log(
        error_msg="something weird happened",
        exception=RuntimeError("something weird happened"),
        step=3,
    )
    # classify_and_log 可能返回 None 或 FailureClassification
    # 只要不抛异常就算通过
    assert result is None or hasattr(result, "category")


def test_failure_stats_summary():
    """FailureStats.summary() 应返回可读摘要。"""
    stats = FailureStats()
    r1 = classify_and_log(error_msg="timeout loading page", exception=TimeoutError("timeout"), step=1)
    if r1:
        stats.record(r1)
    r2 = classify_and_log(error_msg="timeout again", exception=TimeoutError("timeout"), step=2)
    if r2:
        stats.record(r2)
    r3 = classify_and_log(error_msg="element not found", exception=Exception("not found"), step=3)
    if r3:
        stats.record(r3)
    summary = stats.summary()
    assert isinstance(summary, str)
    assert len(summary) > 0


# ─── Prompts Templates Tests ─────────────────────────────────────────────────

from prompts_templates import load_template, load_all_templates, list_templates


def test_load_system_prompt_template():
    """system_prompt.md 应能成功加载。"""
    text = load_template("system_prompt")
    assert len(text) > 50
    assert "VSpider" in text


def test_load_json_schema_template():
    """json_schema.md 应能成功加载。"""
    text = load_template("json_schema")
    assert "actions" in text


def test_load_all_templates():
    """load_all_templates 应拼接多个模板。"""
    text = load_all_templates(["system_prompt", "json_schema"])
    assert "VSpider" in text
    assert "actions" in text


def test_load_nonexistent_template_raises():
    """加载不存在的模板应抛出 FileNotFoundError。"""
    import pytest
    with pytest.raises(FileNotFoundError):
        load_template("nonexistent_template_xyz")


def test_list_templates_includes_known():
    """list_templates 应包含已知模板。"""
    templates = list_templates()
    assert "system_prompt" in templates
    assert "json_schema" in templates


def test_template_cache_works():
    """二次加载应命中缓存。"""
    t1 = load_template("system_prompt", use_cache=True)
    t2 = load_template("system_prompt", use_cache=True)
    assert t1 is t2  # 同一对象（缓存命中）


# ─── Prompt Skills Integration Test ─────────────────────────────────────────

def test_prompt_skills_core_prompt_loaded():
    """prompt_skills.CORE_PROMPT 应从模板或 fallback 加载成功。"""
    from prompt_skills import CORE_PROMPT, JSON_SCHEMA_PROMPT
    assert len(CORE_PROMPT) > 50
    assert "VSpider" in CORE_PROMPT
    assert len(JSON_SCHEMA_PROMPT) > 50
    assert "actions" in JSON_SCHEMA_PROMPT


# ─── Response Cache Tests ───────────────────────────────────────────────────

import tempfile
import shutil

from response_cache import (
    ResponseCache,
    CacheMode,
    CacheEntry,
    CacheReplayMissError,
    CacheInputMismatchError,
)


def test_cache_mode_from_str():
    """CacheMode.from_str 应正确解析合法 / 非法值。"""
    assert CacheMode.from_str("off") == CacheMode.OFF
    assert CacheMode.from_str("RECORD") == CacheMode.RECORD
    assert CacheMode.from_str(" replay ") == CacheMode.REPLAY
    assert CacheMode.from_str(None) == CacheMode.OFF
    assert CacheMode.from_str("") == CacheMode.OFF
    # 非法值降级到 OFF（不抛错）
    assert CacheMode.from_str("invalid_mode") == CacheMode.OFF


def test_cache_off_mode_is_passthrough():
    """OFF 模式：lookup 永远返回 None，store 不写盘。"""
    with tempfile.TemporaryDirectory() as tmp:
        cache = ResponseCache(mode=CacheMode.OFF, cache_dir=tmp, session_id="test")
        assert cache.lookup(step=1, goal="g", screenshot_b64="x") is None
        cache.store(
            step=1, goal="g", screenshot_b64="x",
            input_descriptions="d", output=[{"action": "click"}],
        )
        # 不会创建 session 文件
        assert not cache.session_path().exists()
        assert cache.stats.records == 0
        assert not cache.is_active()


def test_cache_record_then_replay_roundtrip():
    """Record 写入 → 用同 session_id 切换到 Replay 应能回放。"""
    with tempfile.TemporaryDirectory() as tmp:
        sid = "rt_session"
        # ── Record 阶段 ──
        rec = ResponseCache(mode=CacheMode.RECORD, cache_dir=tmp, session_id=sid)
        out1 = [{"action": "click", "target_id": 1}]
        out2 = [{"action": "type", "target_id": 2, "type_value": "hello"}]
        rec.store(step=1, goal="goal A", screenshot_b64="img1",
                  input_descriptions="d1", output=out1, history_summary="")
        rec.store(step=2, goal="goal A", screenshot_b64="img2",
                  input_descriptions="d2", output=out2, history_summary="h1")
        assert rec.stats.records == 2
        assert rec.session_path().exists()

        # ── Replay 阶段（新实例，同 session_id）──
        rep = ResponseCache(mode=CacheMode.REPLAY, cache_dir=tmp, session_id=sid)
        assert rep.list_steps() == [1, 2]
        replayed1 = rep.lookup(step=1, goal="goal A", screenshot_b64="img1")
        replayed2 = rep.lookup(step=2, goal="goal A", screenshot_b64="img2",
                                history_summary="h1")
        assert replayed1 == out1
        assert replayed2 == out2
        assert rep.stats.hits == 2
        assert rep.stats.mismatches == 0


def test_cache_replay_miss_strict_raises():
    """Replay 模式找不到 step 时默认抛 CacheReplayMissError。"""
    with tempfile.TemporaryDirectory() as tmp:
        sid = "miss_session"
        # 先写一个 session 文件
        rec = ResponseCache(mode=CacheMode.RECORD, cache_dir=tmp, session_id=sid)
        rec.store(step=1, goal="g", screenshot_b64="x",
                  input_descriptions="", output=[{"action": "click"}])
        # Replay：查找未记录的 step
        rep = ResponseCache(mode=CacheMode.REPLAY, cache_dir=tmp, session_id=sid)
        try:
            rep.lookup(step=99, goal="g", screenshot_b64="x")
            assert False, "应该抛 CacheReplayMissError"
        except CacheReplayMissError:
            pass
        assert rep.stats.misses == 1


def test_cache_replay_miss_fallback_returns_none():
    """Replay 模式 fallback=True 时找不到 step 应返回 None（让调用方回落）。"""
    with tempfile.TemporaryDirectory() as tmp:
        sid = "miss_fb"
        rec = ResponseCache(mode=CacheMode.RECORD, cache_dir=tmp, session_id=sid)
        rec.store(step=1, goal="g", screenshot_b64="x",
                  input_descriptions="", output=[{"action": "click"}])
        rep = ResponseCache(
            mode=CacheMode.REPLAY, cache_dir=tmp, session_id=sid,
            replay_fallback_on_miss=True,
        )
        result = rep.lookup(step=99, goal="g", screenshot_b64="x")
        assert result is None
        assert rep.stats.misses == 1


def test_cache_replay_input_mismatch_warns_but_returns():
    """Replay 默认遇到输入哈希不一致仅警告，仍返回缓存。"""
    with tempfile.TemporaryDirectory() as tmp:
        sid = "mm_session"
        rec = ResponseCache(mode=CacheMode.RECORD, cache_dir=tmp, session_id=sid)
        rec.store(step=1, goal="original goal",
                  screenshot_b64="screenshot_data",
                  input_descriptions="", output=[{"action": "click"}])
        # Replay 时换一个 goal 文本
        rep = ResponseCache(mode=CacheMode.REPLAY, cache_dir=tmp, session_id=sid)
        result = rep.lookup(step=1, goal="DIFFERENT GOAL",
                            screenshot_b64="screenshot_data")
        assert result == [{"action": "click"}]
        assert rep.stats.mismatches == 1
        assert rep.stats.hits == 1


def test_cache_replay_input_mismatch_strict_raises():
    """Replay strict=True 时输入哈希不一致应抛 CacheInputMismatchError。"""
    with tempfile.TemporaryDirectory() as tmp:
        sid = "strict_mm"
        rec = ResponseCache(mode=CacheMode.RECORD, cache_dir=tmp, session_id=sid)
        rec.store(step=1, goal="original",
                  screenshot_b64="img",
                  input_descriptions="", output=[{"action": "click"}])
        rep = ResponseCache(
            mode=CacheMode.REPLAY, cache_dir=tmp, session_id=sid,
            replay_strict=True,
        )
        try:
            rep.lookup(step=1, goal="DIFFERENT", screenshot_b64="img")
            assert False, "应该抛 CacheInputMismatchError"
        except CacheInputMismatchError:
            pass


def test_cache_replay_missing_session_file():
    """Replay 模式 session 文件不存在时不应崩溃，lookup 应抛 miss。"""
    with tempfile.TemporaryDirectory() as tmp:
        rep = ResponseCache(
            mode=CacheMode.REPLAY, cache_dir=tmp,
            session_id="nonexistent_session",
        )
        assert rep.list_steps() == []
        try:
            rep.lookup(step=1, goal="g", screenshot_b64="x")
            assert False, "应该抛 CacheReplayMissError"
        except CacheReplayMissError:
            pass


def test_cache_entry_serialization_roundtrip():
    """CacheEntry.to_dict / from_dict 应保留所有字段。"""
    e = CacheEntry(
        step=5,
        key="abc",
        goal_hash="gh",
        screenshot_hash="sh",
        history_hash="hh",
        timestamp="2026-01-01T00:00:00",
        input_summary={"goal": "test", "screenshot_chars": 100},
        output=[{"action": "done"}],
    )
    d = e.to_dict()
    e2 = CacheEntry.from_dict(d)
    assert e2.step == e.step
    assert e2.output == e.output
    assert e2.input_summary == e.input_summary


def test_cache_stats_summary_format():
    """CacheStats.summary 应返回可读字符串。"""
    with tempfile.TemporaryDirectory() as tmp:
        cache = ResponseCache(mode=CacheMode.OFF, cache_dir=tmp)
        s = cache.stats.summary()
        assert "hits=" in s and "misses=" in s and "records=" in s


# ─── Element Tracker Tests ──────────────────────────────────────────────────

from element_tracker import (
    ElementTracker,
    ElementSignature,
    RelocateResult,
    TrackerConfig,
    _text_similarity,
    _set_jaccard,
    _bbox_proximity,
    _sequence_similarity,
)


def _make_element(som_id, tag="button", text="Login", role="button",
                  aria_label="", class_list=None, bbox=(100, 200, 80, 30),
                  parent_chain=None):
    return {
        "som_id": som_id,
        "tag": tag,
        "text": text,
        "role": role,
        "aria_label": aria_label,
        "class_list": class_list or ["btn", "btn-primary"],
        "bbox": list(bbox),
        "parent_chain": parent_chain or ["body", "form#login"],
    }


def test_tracker_track_and_get():
    """track() 应写入签名，get() 应能读回。"""
    t = ElementTracker()
    el = _make_element(som_id=5)
    sig = t.track("login_btn", el, step=3)
    assert sig.som_id == 5
    assert sig.text == "Login"
    assert sig.captured_step == 3
    assert t.get("login_btn") is sig
    assert "login_btn" in t.list_tracked()


def test_tracker_track_requires_name():
    """track() 空名应抛 ValueError。"""
    t = ElementTracker()
    try:
        t.track("", _make_element(som_id=1))
        assert False, "应抛 ValueError"
    except ValueError:
        pass


def test_tracker_untrack_and_reset():
    """untrack 删除单个；reset 清空全部。"""
    t = ElementTracker()
    t.track("a", _make_element(som_id=1))
    t.track("b", _make_element(som_id=2))
    assert t.untrack("a") is True
    assert t.untrack("nonexistent") is False
    assert t.list_tracked() == ["b"]
    t.reset()
    assert t.list_tracked() == []


def test_tracker_relocate_exact_match():
    """同元素结构在新快照里 SoM ID 变了也能找回。"""
    t = ElementTracker()
    t.track("login", _make_element(som_id=5, text="Sign In", tag="button",
                                    role="button"), step=1)
    # 新快照：同元素但 som_id 变成 12，另有干扰元素
    new_snapshot = [
        _make_element(som_id=10, text="Cancel", tag="button"),
        _make_element(som_id=12, text="Sign In", tag="button", role="button"),
        _make_element(som_id=14, text="Help", tag="a"),
    ]
    result = t.relocate("login", new_snapshot, current_step=5)
    assert result.found
    assert result.new_som_id == 12
    assert result.confidence >= 0.55


def test_tracker_relocate_high_confidence():
    """完全一致的元素应触发 is_high_confidence。"""
    t = ElementTracker()
    el = _make_element(som_id=5, text="Submit", aria_label="submit form")
    t.track("submit", el, step=1)
    result = t.relocate("submit", [el], current_step=2)
    assert result.found
    assert result.is_high_confidence


def test_tracker_relocate_below_threshold():
    """完全不相关的元素应不被匹配。"""
    t = ElementTracker()
    t.track("login", _make_element(
        som_id=5, text="Sign In", tag="button", role="button",
        class_list=["primary"], bbox=(100, 100, 80, 30),
        parent_chain=["form#auth"],
    ), step=1)
    # 干扰元素：完全不同的 tag/text/role/bbox
    snapshot = [
        _make_element(
            som_id=99, text="Footer copyright info", tag="p", role="paragraph",
            aria_label="copyright", class_list=["footer-text"],
            bbox=(0, 2000, 1200, 20), parent_chain=["body", "footer"],
        ),
    ]
    result = t.relocate("login", snapshot)
    assert not result.found
    assert result.confidence < 0.55
    assert "below_threshold" in result.reason


def test_tracker_relocate_unknown_name():
    """relocate 未追踪的名字应返回 not_tracked。"""
    t = ElementTracker()
    result = t.relocate("ghost", [_make_element(som_id=1)])
    assert not result.found
    assert result.reason == "not_tracked"


def test_tracker_relocate_empty_snapshot():
    """空快照应返回 empty_snapshot。"""
    t = ElementTracker()
    t.track("a", _make_element(som_id=1))
    result = t.relocate("a", [])
    assert not result.found
    assert result.reason == "empty_snapshot"


def test_tracker_relocate_all():
    """relocate_all 应返回所有追踪条目的结果。"""
    t = ElementTracker()
    t.track("login", _make_element(som_id=5, text="Login"), step=1)
    t.track("cancel", _make_element(som_id=8, text="Cancel"), step=1)
    snapshot = [
        _make_element(som_id=20, text="Login"),
        _make_element(som_id=22, text="Cancel"),
    ]
    results = t.relocate_all(snapshot, current_step=3)
    assert set(results.keys()) == {"login", "cancel"}
    assert results["login"].found and results["login"].new_som_id == 20
    assert results["cancel"].found and results["cancel"].new_som_id == 22


def test_tracker_signature_serialization():
    """ElementSignature 序列化 / 反序列化保留字段。"""
    sig = ElementSignature(
        name="x", som_id=7, tag="button", text="OK", role="button",
        aria_label="confirm", class_list=("btn", "primary"),
        bbox=(10, 20, 30, 40), parent_chain=("body", "div"),
        captured_step=4, metadata={"url": "https://example.com"},
    )
    d = sig.to_dict()
    sig2 = ElementSignature.from_dict(d)
    assert sig2.som_id == 7 and sig2.text == "OK"
    assert sig2.class_list == ("btn", "primary")
    assert sig2.bbox == (10.0, 20.0, 30.0, 40.0)
    assert sig2.metadata == {"url": "https://example.com"}


def test_tracker_bbox_dict_format():
    """bbox 用 {x,y,w,h} dict 形式也应解析正确。"""
    t = ElementTracker()
    el = {
        "som_id": 3, "tag": "button", "text": "OK",
        "bbox": {"x": 50, "y": 60, "w": 100, "h": 30},
    }
    sig = t.track("ok", el)
    assert sig.bbox == (50.0, 60.0, 100.0, 30.0)


def test_text_similarity_basics():
    """字符 bigram Jaccard 行为应合理。"""
    assert _text_similarity("hello", "hello") == 1.0
    assert _text_similarity("", "") == 0.5  # 双空：中性
    assert _text_similarity("hello", "") == 0.0
    # 相似但非完全相等
    sim = _text_similarity("Sign In", "Sign Up")
    assert 0 < sim < 1


def test_set_jaccard_basics():
    """集合 Jaccard 行为应合理。"""
    assert _set_jaccard({"a", "b"}, {"a", "b"}) == 1.0
    assert _set_jaccard(set(), set()) == 0.5
    assert _set_jaccard({"a"}, {"b"}) == 0.0
    assert _set_jaccard({"a", "b", "c"}, {"a"}) == 1 / 3


def test_bbox_proximity_basics():
    """bbox 距离归一化行为。"""
    # 同一矩形：1.0
    assert _bbox_proximity((100, 100, 50, 30), (100, 100, 50, 30)) == 1.0
    # 双方都无 bbox：0.5（中性）
    assert _bbox_proximity((0, 0, 0, 0), (0, 0, 0, 0)) == 0.5
    # 远离应低分（默认 reference=1500，距离 1500+ → 0）
    assert _bbox_proximity((0, 0, 10, 10), (2000, 2000, 10, 10)) == 0.0


def test_sequence_similarity_basics():
    """父链序列相似度行为。"""
    assert _sequence_similarity(("body", "form"), ("body", "form")) == 1.0
    assert _sequence_similarity((), ()) == 0.5
    assert _sequence_similarity(("body", "form"), ()) == 0.0
    # 部分匹配
    sim = _sequence_similarity(("body", "div", "form"), ("body", "section", "form"))
    assert 0 < sim < 1


def test_vlm_client_tracker_public_api():
    """端到端：VLMClient 暴露的 track / relocate API 应工作正常。

    验证最小集成是否成功：构造 VLMClient → track 一个元素 → 在
    重排后的快照里 relocate 应能找回新 SoM ID。
    """
    from vlm_client import VLMClient
    client = VLMClient()
    # 默认 ELEMENT_TRACKER_ENABLED=true，应有 tracker 实例
    assert client._element_tracker_enabled
    assert client._element_tracker is not None

    el = _make_element(som_id=5, text="Confirm", role="button", tag="button")
    ok = client.track_element("confirm_btn", el, step=1)
    assert ok is True
    assert "confirm_btn" in client.list_tracked_elements()

    # 模拟下一步：DOM 重排，SoM ID 变成 17
    new_snapshot = [
        _make_element(som_id=10, text="Cancel"),
        _make_element(som_id=17, text="Confirm", role="button", tag="button"),
    ]
    result = client.relocate_element("confirm_btn", new_snapshot, current_step=2)
    assert result is not None
    assert result.found
    assert result.new_som_id == 17

    # untrack 应生效
    assert client.untrack_element("confirm_btn") is True
    assert client.list_tracked_elements() == []

    # reset_element_tracker 不应抛错
    client.track_element("a", _make_element(som_id=1))
    client.reset_element_tracker()
    assert client.list_tracked_elements() == []


def test_vlm_client_tracker_disabled_methods_are_noop():
    """当 tracker 开关关闭时，所有 public API 应安全降级为 no-op。"""
    from vlm_client import VLMClient
    client = VLMClient()
    # 手动关闭模拟用户配置
    client._element_tracker_enabled = False
    client._element_tracker = None

    assert client.track_element("x", _make_element(som_id=1)) is False
    assert client.untrack_element("x") is False
    assert client.relocate_element("x", [_make_element(som_id=1)]) is None
    assert client.relocate_all_tracked([_make_element(som_id=1)]) == {}
    assert client.list_tracked_elements() == []
    # reset 不应抛错
    client.reset_element_tracker()


def test_vlm_client_named_anchor_section_empty_when_no_tracking():
    """没有任何已追踪元素时，_build_named_anchor_section 应返回空串。"""
    from vlm_client import VLMClient
    client = VLMClient()
    section = client._build_named_anchor_section(
        som_elements=[_make_element(som_id=1)], current_step=1,
    )
    assert section == ""


def test_vlm_client_named_anchor_section_includes_tracked():
    """已追踪元素被重新定位后，应出现在 named anchor 段落里。"""
    from vlm_client import VLMClient
    client = VLMClient()
    # track 一个元素
    client.track_element(
        "last_click",
        _make_element(som_id=5, text="Login", role="button"),
        step=2,
    )
    # 构建段落
    new_snapshot = [
        # 用 SoM 输出格式（'id' 而不是 'som_id'，'name' 而不是 'text'）
        {"id": 17, "tag": "button", "name": "Login", "role": "button",
         "rect": {"x": 100, "y": 200, "width": 80, "height": 30}},
        {"id": 18, "tag": "a", "name": "Cancel", "role": "link"},
    ]
    section = client._build_named_anchor_section(new_snapshot, current_step=5)
    assert "Named Anchors" in section
    assert "last_click" in section
    assert "som_id=**17**" in section
    # 确认告警 / 使用说明也在
    assert "锚点只是" in section


def test_vlm_client_named_anchor_section_marks_missing_element():
    """当原元素已不存在于新快照时，应被标记为 ✗ 未找到。"""
    from vlm_client import VLMClient
    client = VLMClient()
    client.track_element(
        "last_click",
        _make_element(som_id=5, text="Original Specific Button",
                      role="button", tag="button"),
        step=1,
    )
    # 完全无关的快照
    new_snapshot = [
        {"id": 99, "tag": "p", "name": "Footer text",
         "role": "paragraph", "rect": {"x": 0, "y": 2000, "width": 1200, "height": 20}},
    ]
    section = client._build_named_anchor_section(new_snapshot, current_step=10)
    assert "last_click" in section
    assert "✗" in section


def test_vlm_client_named_anchor_section_disabled_returns_empty():
    """开关关闭时段落应是空串，不引入 prompt 噪音。"""
    from vlm_client import VLMClient
    client = VLMClient()
    client.track_element("last_click", _make_element(som_id=1))
    client._element_tracker_enabled = False
    section = client._build_named_anchor_section(
        som_elements=[_make_element(som_id=1)], current_step=2,
    )
    assert section == ""


# ─── Chat Submit Tests ──────────────────────────────────────────────────────

from chat_send_locator import (
    find_send_button,
    normalize_point_to_thousand,
    _build_locator_js,
)


def test_normalize_point_to_thousand_basics():
    """归一化坐标转换：基础场景。"""
    assert normalize_point_to_thousand(640, 360, 1280, 720) == [500, 500]
    assert normalize_point_to_thousand(0, 0, 1280, 720) == [0, 0]
    assert normalize_point_to_thousand(1280, 720, 1280, 720) == [1000, 1000]
    # 越界裁剪
    assert normalize_point_to_thousand(2000, 2000, 1280, 720) == [1000, 1000]
    # 异常视口
    assert normalize_point_to_thousand(100, 100, 0, 0) == [0, 0]


def test_chat_send_locator_js_compiles():
    """locator JS 应是有效字符串，且包含选择器列表 / 启发式逻辑标记。"""
    js = _build_locator_js()
    # 关键标记
    assert "found:" in js or '"found"' in js or "found" in js
    assert "method" in js
    assert "querySelectorAll" in js
    # 选择器级联里至少包含一些品牌
    assert "send" in js.lower()
    assert "submit" in js.lower()


def test_chat_send_locator_rejects_menu_triggers_in_js():
    """回归：JS 必须包含 yiyan.baidu.com 误点 '码牛模式' bug 的修复逻辑。

    bug 场景：chat_submit 把模型选择 dropdown (码牛模式) 当成发送按钮点了。
    修复点：proximity heuristic 显式排除 aria-haspopup / aria-expanded /
    role=combobox / 含 '模式/思考/联网搜索' 等关键词的按钮。
    """
    js = _build_locator_js()
    # 菜单触发器排除
    assert "aria-haspopup" in js
    assert "aria-expanded" in js
    assert "combobox" in js
    # 模型选择关键词排除（中文 & 英文）
    assert "模式" in js
    assert "思考" in js
    assert "联网搜索" in js
    assert "mode" in js.lower()
    assert "think" in js.lower()
    # 右侧偏好分层
    assert "tbR.right" in js  # right-edge anchor
    # 平局取最右
    assert "(b.x + b.w) - (a.x + a.w)" in js


def test_chat_send_locator_shape_detector_in_js():
    """JS 必须包含 send-icon-shape 检测器（提升通用性）。

    覆盖 paper-plane / arrow-up / arrow-right SVG path 识别 + send/发送/
    submit/提交 命名识别。这是把通用性从 85% 拉到 95% 的关键。
    """
    js = _build_locator_js()
    # 命名信号
    assert "SEND_NAME_RE" in js
    assert "SEND_CN_NAME_RE" in js
    assert "paper" in js.lower() and "plane" in js.lower()
    assert "arrow" in js.lower()
    assert "发送" in js
    assert "提交" in js
    # 形状信号
    assert "looksLikeArrowOrPlaneSvg" in js
    assert "hasSendIconShape" in js
    # 强加分（命名命中 +120，形状命中 +40）
    assert "120" in js and "shapeStrength === 2" in js


def test_chat_send_locator_shadow_dom_penetration_in_js():
    """JS 必须用 deepQSA 穿透 shadow DOM，覆盖 web component 化的 chat widget。"""
    js = _build_locator_js()
    assert "deepQSA" in js
    assert "shadowRoot" in js
    # 关键三处都得用 deepQSA
    assert js.count("deepQSA") >= 3


def test_som_inject_marks_icon_only_buttons():
    """回归：som_inject_v6.js 必须能识别纯图标 div 按钮（yiyan + / mic /
    送 等无文字按钮）。

    bug 来源：用户截图显示 yiyan.baidu.com 的 + 号、麦克风、飞机发送按钮
    都没有 SoM 红框号。原 L3 在 name.length<1 时直接 return 0，导致这些
    `<div><svg/></div>` 形态的图标按钮全部被漏标。

    修复 1：deriveName 加 4 重图标兜底（svg title / icon aria-label / class
    名提取 / [icon] 占位符）
    修复 2：interactLevel L3 对 cursor:pointer + 含 svg/img/icon 子元素 +
    20-90px 接近正方形的紧凑形状，即便没有名字也接受
    """
    from pathlib import Path
    js_path = Path(__file__).parent / "som_inject_v6.js"
    assert js_path.exists(), "som_inject_v6.js 不存在"
    js = js_path.read_text(encoding="utf-8")
    # ── deriveName 兜底逻辑必须存在 ──
    assert "svg > title" in js, "deriveName 应优先读 SVG <title>"
    assert "innerIcon" in js, "deriveName 应有 innerIcon 兜底分支"
    assert "[icon]" in js, "deriveName 应有 [icon] 通用占位符"
    # ── L3 接受图标按钮的逻辑 ──
    assert "hasIconChild" in js, "L3 应识别 hasIconChild 标志"
    assert "isCompactIconShape" in js, "L3 应做紧凑形状判定"
    # 关键尺寸条件
    assert "r.width >= 20 && r.width <= 90" in js
    assert "r.height >= 20 && r.height <= 90" in js
    assert "Math.abs(r.width - r.height)" in js


def test_som_inject_marks_reply_toolbar_icons():
    """Reply action toolbars use compact SVG-only controls below AI answers."""
    from pathlib import Path
    js_path = Path(__file__).parent / "som_inject_v6.js"
    js = js_path.read_text(encoding="utf-8")

    assert "_isIconToolbarCandidate" in js
    assert "rowMates.length >= 2" in js
    assert "toolbarIconCandidate" in js
    for marker in ("copy|clipboard", "share|forward", "more|ellipsis", "thumbs?[-_ ]?up", "thumbs?[-_ ]?down"):
        assert marker in js
    assert "text === '[icon]'" in js


def test_chat_send_locator_broad_keywords_removed():
    """回归：原版的过宽关键词应已被移除/收紧，避免误杀合法发送按钮。

    用"被逗号分隔的 regex 字面量"模式判断，避免把注释里的中文 /搜索/ 文本
    当成实际生效的 reject pattern。
    """
    js = _build_locator_js()
    # 提取 REJECT_TEXT_PATTERNS 数组体（介于 `REJECT_TEXT_PATTERNS = [` 和
    # 紧随其后的 `]` 之间）
    import re as _re
    m = _re.search(r"REJECT_TEXT_PATTERNS\s*=\s*\[(.*?)\]", js, _re.DOTALL)
    assert m, "REJECT_TEXT_PATTERNS 数组在 JS 里找不到"
    body = m.group(1)
    # 这些过宽 pattern 现在不应该作为 regex 字面量出现
    # （/搜索/ 改为 /联网搜索/；/快速/ 整个删除；/工具/→/工具栏/；
    # /历史/→/历史记录/；/会话/→/会话列表/）
    assert "/搜索/" not in body, "宽匹配 /搜索/ 会跟用户合法 goal 冲突"
    assert "/快速/" not in body, "/快速/ 误杀 '快速发送' 类标签"
    assert "/工具/" not in body
    assert "/历史/" not in body
    assert "/会话/" not in body
    # 反向 sanity：收紧后的版本必须仍然存在
    assert "/联网搜索/" in body
    assert "/工具栏/" in body
    assert "/历史记录/" in body


class _FakePage:
    """异步 mock：模拟 Playwright Page 的 evaluate(...) 方法。"""

    def __init__(self, result):
        self._result = result
        self.eval_calls = 0

    async def evaluate(self, _js):
        self.eval_calls += 1
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def test_find_send_button_found():
    """JS 返回 found=True 时应正确解析为 dict。"""
    page = _FakePage({
        "found": True,
        "method": "selector:[aria-label='发送']",
        "x": 1200, "y": 800,
        "bbox": [1180, 780, 40, 40],
        "selector": "[aria-label='发送']",
        "reason": "matched selector",
    })
    result = asyncio.run(find_send_button(page))
    assert result["found"] is True
    assert result["x"] == 1200
    assert result["y"] == 800
    assert result["selector"] == "[aria-label='发送']"
    assert page.eval_calls == 1


def test_find_send_button_not_found():
    """JS 返回 found=False 时应保留 reason。"""
    page = _FakePage({
        "found": False,
        "method": "none",
        "x": 0, "y": 0,
        "bbox": [0, 0, 0, 0],
        "selector": None,
        "reason": "no editable textbox found on page",
    })
    result = asyncio.run(find_send_button(page))
    assert result["found"] is False
    assert "no editable textbox" in result["reason"]


def test_find_send_button_evaluate_raises():
    """JS evaluate 抛错时应吞错并返回 found=False + error 字段。"""
    page = _FakePage(RuntimeError("disconnected"))
    result = asyncio.run(find_send_button(page))
    assert result["found"] is False
    assert "evaluate failed" in result["reason"]
    assert result.get("error") == "disconnected"


def test_find_send_button_handles_non_dict_result():
    """JS 返回 None / 非 dict 时不应崩溃。"""
    page = _FakePage(None)
    result = asyncio.run(find_send_button(page))
    assert result["found"] is False
    assert "non-dict" in result["reason"]


def test_chat_submit_action_in_schema():
    """chat_submit 必须出现在 VSpiderAction.action 的 Literal 枚举里。"""
    from vlm_client import VSpiderAction
    # Pydantic v2 schema 下 Literal 字段的 allowed 值在 model_fields 元数据里
    field = VSpiderAction.model_fields["action"]
    # 拿到 Literal 的全部允许值
    allowed = []
    try:
        from typing import get_args, get_origin
        ann = field.annotation
        # 可能被 Annotated 包裹，逐层取
        for arg in get_args(ann):
            sub_args = get_args(arg)
            if sub_args:
                allowed.extend(sub_args)
            else:
                allowed.append(arg)
        if not allowed:
            allowed = list(get_args(ann))
    except Exception:
        allowed = []
    flat = [str(x) for x in allowed]
    assert "chat_submit" in flat, f"chat_submit missing from action enum, got: {flat[:8]}..."
    assert "chat_extract" in flat  # smoke check


def test_chat_submit_handler_registered():
    """chat_submit 必须在 ActionRegistry 里有对应 handler。"""
    from action_registry import build_default_action_registry
    reg = build_default_action_registry()
    tools = reg.list_tools(include_disabled=False)
    names = [t["name"] if isinstance(t, dict) else getattr(t, "name", str(t)) for t in tools]
    assert "chat_submit" in names, f"chat_submit not registered, got: {names[:8]}..."


def test_vlm_client_replay_short_circuits_llm():
    """端到端：VLMClient.ask() 在 Replay 模式下应直接返回缓存而不调用 LLM。

    这验证了 vlm_client.py 中 ask() 顶部插入的 cache.lookup 钩子工作正常。
    构造一个带预置缓存的 VLMClient，调用 ask() 时不会触发任何真实 HTTP 请求。
    """
    from vlm_client import VLMClient
    with tempfile.TemporaryDirectory() as tmp:
        sid = "e2e_session"
        # ── 预置缓存 ──
        seed = ResponseCache(mode=CacheMode.RECORD, cache_dir=tmp, session_id=sid)
        cached_output = [{
            "action": "click",
            "target_id": 7,
            "type_value": "",
            "thought": "from cache",
            "status": "ok",
        }]
        seed.store(
            step=1, goal="任务", screenshot_b64="dummy",
            input_descriptions="", output=cached_output,
        )
        # ── 构造 VLMClient 并替换其 _response_cache 为 replay 模式 ──
        client = VLMClient()
        client._response_cache = ResponseCache(
            mode=CacheMode.REPLAY, cache_dir=tmp, session_id=sid,
        )
        # 调用 ask()。如果钩子失效，会真实发请求 → 因为没有有效 API key 多半超时/抛错。
        # 钩子生效则立即返回缓存，无需网络。
        result = asyncio.run(client.ask(
            screenshot_b64="dummy",
            goal="任务",
            step=1,
            input_descriptions="",
        ))
        assert result == cached_output
        assert client._response_cache.stats.hits == 1
        # 历史也应被记录，便于后续步骤的 history summary 一致性
        assert len(client._history) >= 1
