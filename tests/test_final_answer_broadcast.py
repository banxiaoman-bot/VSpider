"""Regression tests for Final Answer broadcast wiring.

These tests pin the *centralised* answer-payload behaviour we wired into
``visual_web_agent.main`` (and indirectly ``api_server``):

  * ``_record_run_answer`` updates module-level state.
  * ``_reset_run_answer`` clears that state.
  * ``_derive_answer_type`` maps ``_goal_output_mode`` → ``answer_type``.
  * ``_broadcast_done_safe`` reads the run-scoped state and forwards
    ``answer_type`` / ``answer`` to ``api_server.broadcast_done``.
  * Failure broadcasts (``success=False``) intentionally strip the answer
    payload so traceback / cancellation text never renders as a "final
    answer" in the Final Answer panel.

The tests stub ``api_server.broadcast_done`` so they run without the
FastAPI server / WebSocket layer being live.
"""
from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace

import pytest

# ── Module-under-test loader ────────────────────────────────────────────────
# We avoid importing the full ``visual_web_agent.main`` (heavy, side-effectful).
# Instead we re-import a fresh module of the small surface we need by reading
# the relevant symbols directly. ``main.py`` is huge but the answer-broadcast
# code lives near the top, so a normal import is fine — we just guard against
# the import being unavailable in unusual environments.

main = pytest.importorskip("visual_web_agent.main")


@pytest.fixture(autouse=True)
def _reset_state_around_test():
    """Each test starts/ends with a clean run-scoped answer state."""
    main._reset_run_answer()
    yield
    main._reset_run_answer()


@pytest.fixture
def captured_broadcast(monkeypatch):
    """Capture every call into ``api_server.broadcast_done``."""
    calls: list[dict] = []

    def _capture(success, message="", *, answer_type=None, answer=None, answer_domain=None):
        calls.append(
            {
                "success": success,
                "message": message,
                "answer_type": answer_type,
                "answer": answer,
            }
        )

    fake_api = SimpleNamespace(broadcast_done=_capture)
    # ``_broadcast_done_safe`` imports inside the function body, so it picks
    # up the stub from sys.modules at call time.
    monkeypatch.setitem(sys.modules, "api_server", fake_api)
    return calls


# ── _record_run_answer / _reset_run_answer ──────────────────────────────────
def test_record_run_answer_sets_mode_only():
    main._record_run_answer(mode="answer")
    assert main._RUN_OUTPUT_MODE == "answer"
    assert main._RUN_ANSWER_TEXT is None


def test_record_run_answer_sets_text_only():
    main._record_run_answer(text="hello world")
    assert main._RUN_OUTPUT_MODE is None
    assert main._RUN_ANSWER_TEXT == "hello world"


def test_record_run_answer_later_call_overwrites_text():
    main._record_run_answer(text="first")
    main._record_run_answer(text="second")
    assert main._RUN_ANSWER_TEXT == "second"


def test_record_run_answer_partial_update_keeps_other_field():
    main._record_run_answer(mode="artifact", text="rows saved")
    main._record_run_answer(text="rows saved v2")
    assert main._RUN_OUTPUT_MODE == "artifact"  # not cleared
    assert main._RUN_ANSWER_TEXT == "rows saved v2"


def test_reset_run_answer_clears_both_fields(captured_broadcast):
    main._record_run_answer(mode="answer", text="leftover")
    main._broadcast_done_safe(True, "previous run done")
    main._reset_run_answer()
    assert main._RUN_OUTPUT_MODE is None
    assert main._RUN_ANSWER_TEXT is None
    assert main._run_done_was_broadcasted() is False


def test_format_extracted_rows_as_answer_prefers_answer_field():
    text = main._format_extracted_rows_as_answer(
        [{"answer": "明天上海有小雨，气温 22~29°C。", "url": "https://example.com"}]
    )

    assert text == "明天上海有小雨，气温 22~29°C。"


def test_format_extracted_rows_as_answer_lists_structured_fields():
    text = main._format_extracted_rows_as_answer(
        [{"date": "05-23", "weather": "小雨", "temperature": "22~29°C"}]
    )

    assert text == "会下雨，天气为小雨；气温 22~29°C。"


def test_weather_answer_search_query_handles_city_before_rain_question():
    goal = (
        "\u5e2e\u6211\u67e5\u4e00\u4e0b\u660e\u5929\u660c\u5409\u5e02"
        "\u4f1a\u4e0d\u4f1a\u4e0b\u96e8\uff1f\u5982\u679c\u53ef\u80fd\u7684\u8bdd\uff0c"
        "\u628a\u6c14\u6e29\u4e5f\u544a\u8bc9\u6211\u3002"
    )

    assert main._infer_answer_weather_search_query(goal) == "\u660c\u5409\u5e02\u660e\u5929\u5929\u6c14"


def test_build_answer_search_fast_path_url_avoids_stale_baidu_query():
    query, url = main._build_answer_search_fast_path_url(
        (
            "\u5e2e\u6211\u67e5\u4e00\u4e0b\u660e\u5929\u660c\u5409\u5e02"
            "\u4f1a\u4e0d\u4f1a\u4e0b\u96e8\uff0c\u628a\u6c14\u6e29\u4e5f\u544a\u8bc9\u6211"
        ),
        start_url="https://www.baidu.com",
        current_url=(
            "https://www.baidu.com/s?wd=%E4%B8%8A%E6%B5%B7%E6%98%8E%E5%A4%A9%E5%A4%A9%E6%B0%94"
        ),
        output_mode="answer",
    )

    assert query == "\u660c\u5409\u5e02\u660e\u5929\u5929\u6c14"
    assert url.startswith("https://www.baidu.com/s?wd=")
    assert "%E6%98%8C%E5%90%89" in url


def test_compact_weather_answer_ignores_previous_city_page_text():
    goal = (
        "\u5e2e\u6211\u67e5\u4e00\u4e0b\u660e\u5929\u660c\u5409\u5e02"
        "\u4f1a\u4e0d\u4f1a\u4e0b\u96e8\uff1f\u5982\u679c\u53ef\u80fd\u7684\u8bdd\uff0c"
        "\u628a\u6c14\u6e29\u4e5f\u544a\u8bc9\u6211\u3002"
    )
    raw = (
        "\u4e0a\u6d77\u660e\u5929\u5929\u6c14\n"
        "\u4e0a\u6d77\u660e\u5929\uff082026\u5e7405\u670823\u65e5\uff09"
        "\u4e3a\u5c0f\u96e8\u5929\u6c14\uff0c\u6e29\u5ea6\u8303\u56f422~29\u2103\uff0c"
        "\u964d\u6c34\u6982\u738790%\u3002\n"
        "\u660c\u5409\u5e02\u660e\u5929\u5929\u6c14\n"
        "\u200c\u660c\u5409\u5e02\u660e\u5929\uff082026\u5e7405\u670823\u65e5\uff09"
        "\u4e3a\u591a\u4e91\u5929\u6c14\uff0c\u6e29\u5ea6\u8303\u56f48~25\u2103\uff0c"
        "\u897f\u5317\u98ce3\u7ea7\uff0c\u65e0\u964d\u6c34\uff0c\u7a7a\u6c14\u8d28\u91cf\u4e3a\u826f\u200c\u3002"
    )

    assert main._compact_answer_text_for_goal(raw, goal) == (
        "\u660e\u5929\u660c\u5409\u5e02\u4e0d\u4f1a\u4e0b\u96e8\uff0c"
        "\u5929\u6c14\u4e3a\u591a\u4e91\uff0c\u6c14\u6e29 8~25\u2103\u3002"
    )


# ── _derive_answer_type ────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "mode,expected",
    [
        ("answer", "text"),
        ("artifact", "file"),
        ("mixed", None),     # mixed → let frontend heuristic decide
        ("default", None),
        (None, None),
        ("", None),
        ("unknown_future_mode", None),
    ],
)
def test_derive_answer_type_mapping(mode, expected):
    assert main._derive_answer_type(mode) == expected


# ── _broadcast_done_safe forwarding ────────────────────────────────────────
def test_success_with_text_mode_forwards_text_payload(captured_broadcast):
    main._record_run_answer(mode="answer", text="**Done.** 42°C, sunny.")
    main._broadcast_done_safe(True, "Task completed")

    assert len(captured_broadcast) == 1
    assert main._run_done_was_broadcasted() is True
    call = captured_broadcast[0]
    assert call["success"] is True
    assert call["message"] == "Task completed"
    assert call["answer_type"] == "text"
    assert call["answer"] == "**Done.** 42°C, sunny."


def test_success_with_artifact_mode_forwards_file_type_no_text(captured_broadcast):
    # artifact mode: no answer text because rows are in the Excel file
    main._record_run_answer(mode="artifact")
    main._broadcast_done_safe(True, "Excel exported")

    call = captured_broadcast[0]
    assert call["answer_type"] == "file"
    assert call["answer"] is None  # not set → wire as None


def test_success_default_mode_leaves_answer_type_none(captured_broadcast):
    # Common case when output_contract was not classified — let the frontend
    # heuristic (artifact count grew? → file, else → text) take over.
    main._record_run_answer(mode="default")
    main._broadcast_done_safe(True, "ok")

    call = captured_broadcast[0]
    assert call["answer_type"] is None
    assert call["answer"] is None


def test_failure_strips_answer_payload_even_if_recorded(captured_broadcast):
    # The agent already produced text mid-run, but then crashed/was stopped.
    # We must NOT forward that text as "the answer" — it would mislead the user.
    main._record_run_answer(mode="answer", text="partially generated reply")
    main._broadcast_done_safe(False, "Agent exception: TimeoutError")

    call = captured_broadcast[0]
    assert call["success"] is False
    assert call["answer_type"] is None
    assert call["answer"] is None


def test_explicit_kwargs_override_module_state(captured_broadcast):
    main._record_run_answer(mode="answer", text="stash text")
    main._broadcast_done_safe(
        True,
        "Task completed",
        answer_type="file",
        answer="override",
    )

    call = captured_broadcast[0]
    # Explicit caller wins over the run-scoped stash.
    assert call["answer_type"] == "file"
    assert call["answer"] == "override"


def test_broadcast_swallows_api_import_error(monkeypatch):
    """If api_server is unavailable (CLI mode), the call must not raise."""
    monkeypatch.setitem(sys.modules, "api_server", None)
    # No assertion needed — we only require that this does not raise.
    main._broadcast_done_safe(True, "ok")


# ── Reset-on-new-run guarantee ──────────────────────────────────────────────
def test_reset_then_broadcast_carries_no_stale_answer(captured_broadcast):
    # Previous run set an answer
    main._record_run_answer(mode="answer", text="from previous run")
    main._reset_run_answer()

    main._broadcast_done_safe(True, "second run done")
    call = captured_broadcast[0]
    assert call["answer_type"] is None  # reset cleared mode
    assert call["answer"] is None       # reset cleared text


# ── Chat-extract path (microadjustment 1) ──────────────────────────────────
# Pin that the chat_extract done branch records the raw AI answer text into
# the run-scoped stash BEFORE its broadcast_done call. Without this hook,
# chat tasks would emit a generic "chat_extract completed" with no answer
# payload, forcing the frontend onto the heuristic fallback.
def test_chat_extract_done_branch_records_raw_answer_before_broadcast():
    """The chat-extract block (~main.py:14443+) must call _record_run_answer
    on the raw _ai_answer_obj['answer'] *before* its _broadcast_done_safe.

    We verify this by static inspection of main.py source so the behaviour
    is pinned even if the surrounding lines are refactored. The test stays
    decoupled from the heavy chat_extract runtime path.
    """
    import inspect

    source = inspect.getsource(main)

    # The chat path must build _chat_raw_answer from the AI answer dict.
    assert "_chat_raw_answer = _chat_answer_row[\"answer\"]" in source, (
        "chat_extract path no longer extracts _chat_raw_answer from the row"
    )

    # And it must record the user-facing answer into the run-scoped stash.
    assert "_record_run_answer(text=_chat_final_answer)" in source, (
        "chat_extract path no longer feeds the AI answer into _record_run_answer"
    )

    # The record call must come BEFORE the chat_extract broadcast.
    record_pos = source.find("_record_run_answer(text=_chat_final_answer)")
    broadcast_pos = source.find('_broadcast_done_safe(True, "chat_extract completed")')
    assert 0 < record_pos < broadcast_pos, (
        "_record_run_answer must be called BEFORE _broadcast_done_safe in the "
        "chat_extract done branch (otherwise the WS done event is sent without "
        "the AI answer, defeating the Final Answer panel wiring)"
    )


# ── Domain dispatch in _compact_answer_text_for_goal ───────────────────────
# These tests pin the domain router so adding a new domain (e.g. flights,
# news) doesn't accidentally break weather/stock/recipe routing.
@pytest.mark.parametrize(
    "goal,expected_domain",
    [
        ("帮我查一下明天上海会不会下雨", "weather"),
        ("上海明天的气温是多少", "weather"),
        ("查一下贵州茅台现在的股价", "stock"),
        ("苹果公司的股票今天涨跌如何", "stock"),
        ("西红柿炒鸡蛋怎么做？", "recipe"),
        ("红烧肉的做法是什么", "recipe"),
        ("Compose a short poem about autumn", "generic"),
        ("帮我搜一下 python asyncio 教程", "recipe"),  # "教程" is recipe-flagged (intended overlap)
        ("", "generic"),
    ],
)
def test_detect_answer_domain_classifies_goal(goal, expected_domain):
    assert main._detect_answer_domain(goal) == expected_domain


def test_compact_stock_answer_extracts_name_price_and_change():
    """Stock-quote page → concise '现价 / 涨跌幅' answer."""
    text = (
        "贵州茅台 600519\n"
        "现价 1612.50 元 涨跌幅 +1.25%  涨跌额 +19.90\n"
        "今开 1592.60 昨收 1592.60 最高 1620.00 最低 1590.00\n"
    )
    result = main._compact_answer_text_for_goal(text, "查一下贵州茅台现在的股价")
    assert "贵州茅台" in result
    assert "1612.50" in result
    assert "1.25" in result  # appears inside +1.25%


def test_compact_stock_answer_returns_raw_when_no_price_signal():
    """Stock goal but page has no price → fall back to raw text, not lose data."""
    text = "贵州茅台公司简介：成立于 1951 年，主营业务为白酒生产与销售。"
    result = main._compact_answer_text_for_goal(text, "查一下贵州茅台现在的股价")
    assert result == text  # raw fallback


def test_compact_recipe_answer_extracts_name_ingredients_and_steps():
    """Recipe page → condensed '用料 / 步骤' answer."""
    text = (
        "西红柿炒鸡蛋\n"
        "主料：西红柿 2个，鸡蛋 3个\n"
        "辅料：葱花、盐、油\n"
        "步骤：\n"
        "1. 鸡蛋打散加盐搅匀\n"
        "2. 西红柿切块\n"
        "3. 热油下锅炒鸡蛋盛出\n"
        "4. 重新热油下西红柿翻炒后加鸡蛋同炒\n"
    )
    result = main._compact_answer_text_for_goal(text, "西红柿炒鸡蛋怎么做？")
    assert "西红柿炒鸡蛋" in result
    assert "西红柿" in result
    assert "鸡蛋" in result
    assert "步骤" in result or "→" in result  # at least one signal of step compression


def test_compact_recipe_answer_returns_raw_when_dish_name_absent_from_text():
    """Recipe goal but body text doesn't even mention the dish → keep raw."""
    text = "今天给大家介绍一道家常菜，做法非常简单。"
    result = main._compact_answer_text_for_goal(text, "西红柿炒鸡蛋怎么做？")
    assert result == text


def test_compact_generic_goal_returns_raw_text():
    """Goal not matching any domain → identity passthrough."""
    raw = "This is some arbitrary content that doesn't match any domain."
    assert main._compact_answer_text_for_goal(raw, "Tell me about Python") == raw


def test_compact_empty_text_returns_empty_string():
    assert main._compact_answer_text_for_goal("", "any goal") == ""
    assert main._compact_answer_text_for_goal("   \n   ", "any goal") == ""


# ── Fast-path URL builder: early-exit branches ─────────────────────────────
# These pin the conservative guards so the fast-path never hijacks navigation
# for non-answer modes, non-baidu sites, non-weather goals, or already-correct
# search pages.
def test_fast_path_skips_when_not_answer_mode():
    """Default/artifact/mixed modes must NOT trigger fast-path nav."""
    for mode in ("default", "artifact", "mixed", ""):
        q, u = main._build_answer_search_fast_path_url(
            "帮我查一下明天上海会不会下雨",
            start_url="https://www.baidu.com",
            current_url="",
            output_mode=mode,
        )
        assert (q, u) == ("", ""), f"unexpected fast-path for mode={mode!r}"


def test_fast_path_skips_when_neither_url_is_baidu():
    """Only baidu-rooted runs use the fast-path; google/bing/etc. are ignored."""
    for host in ("https://www.google.com", "https://www.bing.com", "https://example.com"):
        q, u = main._build_answer_search_fast_path_url(
            "明天上海会不会下雨",
            start_url=host,
            current_url=host,
            output_mode="answer",
        )
        assert (q, u) == ("", ""), f"unexpected fast-path for host={host!r}"


def test_fast_path_skips_when_no_query_inferable_for_any_domain():
    """Goals not matching any wired domain → no fast-path."""
    for goal in (
        "Tell me about Python's history",  # no domain hits
        "Compose a haiku about autumn",
        "",
    ):
        q, u = main._build_answer_search_fast_path_url(
            goal,
            start_url="https://www.baidu.com",
            current_url="https://www.baidu.com",
            output_mode="answer",
        )
        assert (q, u) == ("", ""), f"unexpected fast-path for goal={goal!r}"


def test_fast_path_kicks_in_for_stock_answer_goal():
    """Stock-flavoured answer goal on Baidu → returns ``{name} 股价`` URL."""
    q, u = main._build_answer_search_fast_path_url(
        "查一下贵州茅台现在的股价",
        start_url="https://www.baidu.com",
        current_url="https://www.baidu.com",
        output_mode="answer",
    )
    assert q == "贵州茅台 股价"
    assert u.startswith("https://www.baidu.com/s?wd=")
    # "贵州" URL-encoded
    assert "%E8%B4%B5%E5%B7%9E" in u or "%E8%b4%b5%E5%b7%9E" in u


def test_fast_path_kicks_in_for_recipe_answer_goal():
    """Recipe-flavoured answer goal on Baidu → returns ``{dish} 做法`` URL."""
    q, u = main._build_answer_search_fast_path_url(
        "西红柿炒鸡蛋怎么做？",
        start_url="https://www.baidu.com",
        current_url="https://www.baidu.com",
        output_mode="answer",
    )
    assert q == "西红柿炒鸡蛋 做法"
    assert u.startswith("https://www.baidu.com/s?wd=")


def test_fast_path_kicks_in_for_flight_answer_goal():
    """Flight-flavoured answer goal on Baidu → returns ``{flight} 航班动态`` URL."""
    q, u = main._build_answer_search_fast_path_url(
        "查一下 CA1234 的航班动态",
        start_url="https://www.baidu.com",
        current_url="https://www.baidu.com",
        output_mode="answer",
    )
    assert q == "CA1234 航班动态"
    assert u.startswith("https://www.baidu.com/s?wd=")
    assert "CA1234" in main._infer_answer_flight_search_query("查询 CA1234 的航班动态")


def test_fast_path_chain_priority_weather_first():
    """When a goal mentions both weather AND another domain, weather wins
    (most specific / most fragile to stale state — see the original bug)."""
    q, u = main._build_answer_search_fast_path_url(
        "上海明天天气怎么样？另外查一下贵州茅台股价",
        start_url="https://www.baidu.com",
        current_url="https://www.baidu.com",
        output_mode="answer",
    )
    assert "明天天气" in q  # weather inferer wins, not stock
    assert u.startswith("https://www.baidu.com/s?wd=")


def test_fast_path_skips_when_wd_already_matches_inferred_query():
    """Already on the right Baidu search page → don't re-navigate (preserves
    user-visible state and any inflight observation)."""
    q, u = main._build_answer_search_fast_path_url(
        "帮我查一下明天昌吉市会不会下雨",
        start_url="https://www.baidu.com",
        current_url=(
            "https://www.baidu.com/s?wd="
            "%E6%98%8C%E5%90%89%E5%B8%82%E6%98%8E%E5%A4%A9%E5%A4%A9%E6%B0%94"
        ),  # already "昌吉市明天天气"
        output_mode="answer",
    )
    assert (q, u) == ("", "")


def test_fast_path_kicks_in_for_stale_baidu_with_weather_answer_goal():
    """Happy path counter-example: when wd is stale, return a fresh URL."""
    q, u = main._build_answer_search_fast_path_url(
        "帮我查一下明天昌吉市会不会下雨",
        start_url="https://www.baidu.com",
        current_url="https://www.baidu.com/s?wd=%E4%B8%8A%E6%B5%B7%E6%98%8E%E5%A4%A9%E5%A4%A9%E6%B0%94",
        output_mode="answer",
    )
    assert q == "昌吉市明天天气"
    assert u.startswith("https://www.baidu.com/s?wd=")
    assert "%E6%98%8C%E5%90%89" in u  # "昌吉" URL-encoded


# ── Flight domain ──────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "goal,expected",
    [
        ("查一下 CA1234 的航班动态", "CA1234"),
        ("MU5678航班几点起飞？", "MU5678"),
        ("CZ3001现在到哪了", "CZ3001"),     # via flight number alone (domain auto-detected)
        ("AA100 是否准点", "AA100"),         # 3-digit suffix
        ("没有航班号但有 ‘航班' 字样", ""),    # no number → empty
    ],
)
def test_extract_flight_number_from_goal(goal, expected):
    assert main._extract_flight_number_from_goal(goal) == expected


def test_detect_answer_domain_recognises_flight_via_keyword():
    assert main._detect_answer_domain("CA1234 航班什么时候降落") == "flight"


def test_detect_answer_domain_recognises_flight_via_number_alone():
    """Even without 'flight' vocab, a bare flight number → flight domain."""
    assert main._detect_answer_domain("CA1234 现在状态") == "flight"


def test_compact_flight_answer_extracts_status_and_times():
    """Flight-info page → '{flight} {status} {dep} → {arr}。'"""
    text = (
        "CA1234 北京首都(PEK) → 上海浦东(PVG)\n"
        "计划起飞 09:30  实际起飞 09:45\n"
        "已起飞，预计到达 12:00\n"
    )
    result = main._compact_answer_text_for_goal(text, "查一下 CA1234 的航班动态")
    assert "CA1234" in result
    assert "已起飞" in result
    assert "09:30" in result or "09:45" in result


def test_compact_flight_answer_returns_raw_when_number_absent_from_text():
    """Flight goal but body text doesn't mention the flight → keep raw."""
    text = "今日航班正常运行，请到值机柜台办理登机。"
    result = main._compact_answer_text_for_goal(text, "查一下 CA1234 的航班动态")
    assert result == text


# ── Artifact accumulator + file-mode auto-summary ──────────────────────────
@pytest.fixture(autouse=True)
def _reset_artifact_accumulator():
    """Each artifact-aware test starts with an empty accumulator."""
    main._RUN_NEW_ARTIFACT_NAMES.clear()
    yield
    main._RUN_NEW_ARTIFACT_NAMES.clear()


def test_record_new_artifact_appends_unique_names():
    main._record_new_artifact("output_001.xlsx")
    main._record_new_artifact("xhr_001.xlsx")
    main._record_new_artifact("output_001.xlsx")  # duplicate → ignored
    assert main._RUN_NEW_ARTIFACT_NAMES == ["output_001.xlsx", "xhr_001.xlsx"]


def test_record_new_artifact_ignores_empty_or_none():
    main._record_new_artifact("")
    main._record_new_artifact(None)
    main._record_new_artifact("   ")
    assert main._RUN_NEW_ARTIFACT_NAMES == []


def test_reset_run_answer_clears_artifact_accumulator():
    main._record_new_artifact("a.xlsx")
    main._record_new_artifact("b.xlsx")
    main._reset_run_answer()
    assert main._RUN_NEW_ARTIFACT_NAMES == []


def test_synthesize_file_mode_summary_basic_listing():
    s = main._synthesize_file_mode_summary(["output_001.xlsx", "xhr_001.xlsx"])
    assert "output_001.xlsx" in s
    assert "xhr_001.xlsx" in s
    assert "Artifacts" in s
    assert "请前往" in s


def test_synthesize_file_mode_summary_truncates_long_lists():
    names = [f"file_{i:03d}.xlsx" for i in range(8)]
    s = main._synthesize_file_mode_summary(names)
    # Only the first 5 are listed verbatim
    for nm in names[:5]:
        assert nm in s
    # Tail count is rendered
    assert "8" in s
    # Trailing names are NOT all listed individually (the head was truncated)
    assert names[7] not in s


def test_synthesize_file_mode_summary_returns_empty_for_empty_input():
    assert main._synthesize_file_mode_summary([]) == ""


def test_broadcast_done_auto_attaches_file_mode_summary(captured_broadcast):
    """File mode without explicit answer text → auto-synthesized summary."""
    main._record_run_answer(mode="artifact")  # → answer_type="file"
    main._record_new_artifact("output_001.xlsx")
    main._record_new_artifact("xhr_001.xlsx")
    main._broadcast_done_safe(True, "Excel exported")

    call = captured_broadcast[0]
    assert call["answer_type"] == "file"
    assert call["answer"] is not None
    assert "output_001.xlsx" in call["answer"]
    assert "xhr_001.xlsx" in call["answer"]


def test_broadcast_done_skips_auto_summary_when_explicit_answer_given(captured_broadcast):
    """Caller-provided answer text wins; accumulator is ignored to avoid dupes."""
    main._record_run_answer(mode="artifact")
    main._record_new_artifact("output_001.xlsx")
    main._broadcast_done_safe(True, "ok", answer="caller-provided summary")

    call = captured_broadcast[0]
    assert call["answer"] == "caller-provided summary"
    # Accumulator state preserved (cleared only by _reset_run_answer)
    assert "output_001.xlsx" in main._RUN_NEW_ARTIFACT_NAMES


def test_broadcast_done_skips_auto_summary_when_text_mode(captured_broadcast):
    """Text-mode runs never get the file summary even if artifacts were registered."""
    main._record_run_answer(mode="answer", text="42°C, sunny.")
    main._record_new_artifact("output_001.xlsx")  # rare but possible
    main._broadcast_done_safe(True, "ok")

    call = captured_broadcast[0]
    assert call["answer_type"] == "text"
    assert call["answer"] == "42°C, sunny."
    assert "output_001.xlsx" not in (call["answer"] or "")


def test_broadcast_done_skips_auto_summary_when_no_artifacts_registered(captured_broadcast):
    """File mode but no artifacts recorded → no synthesis (avoid lying)."""
    main._record_run_answer(mode="artifact")
    main._broadcast_done_safe(True, "ok")

    call = captured_broadcast[0]
    assert call["answer_type"] == "file"
    assert call["answer"] is None  # nothing synthesised


def test_broadcast_done_failure_strips_summary_even_with_artifacts(captured_broadcast):
    """A failed run with artifacts must NOT show 'task complete' summary."""
    main._record_run_answer(mode="artifact")
    main._record_new_artifact("output_001.xlsx")
    main._broadcast_done_safe(False, "agent crashed")

    call = captured_broadcast[0]
    assert call["success"] is False
    assert call["answer_type"] is None
    assert call["answer"] is None
