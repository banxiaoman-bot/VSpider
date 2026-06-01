from visual_web_agent.perception.targeted import TargetedProbeResult
from visual_web_agent.perception.targeted import choose_click_handoff_candidate
from visual_web_agent.perception.targeted import choose_type_handoff_candidate
from visual_web_agent.perception.targeted import infer_target_kinds
from visual_web_agent.perception.targeted import rank_candidates


def test_infer_target_kinds_from_goal() -> None:
    assert infer_target_kinds("在搜索框输入 vue3") == ("input",)
    assert infer_target_kinds("点击 View profile 链接") == ("link",)
    assert infer_target_kinds("提取弹窗里的内容并关闭") == ("button", "dialog")


def test_rank_candidates_prefers_goal_matching_input() -> None:
    ranked = rank_candidates(
        [
            {
                "kind": "input",
                "text": "Newsletter",
                "tag": "input",
                "attributes": {"placeholder": "Email address"},
                "bbox": {"x": 10, "y": 10, "width": 120, "height": 30},
            },
            {
                "kind": "input",
                "text": "Search",
                "tag": "input",
                "attributes": {"placeholder": "Search docs", "id": "search"},
                "bbox": {"x": 10, "y": 60, "width": 220, "height": 32},
            },
        ],
        goal="search docs for router",
        inferred_kinds=("input",),
    )

    assert ranked[0].text == "Search"
    assert ranked[0].confidence > ranked[1].confidence
    assert "goal_term:docs" in ranked[0].evidence
    assert "has_stable_attr" in ranked[0].evidence


def test_probe_result_serializes_candidates() -> None:
    ranked = rank_candidates(
        [
            {
                "kind": "button",
                "text": "Close",
                "tag": "button",
                "attributes": {"ariaLabel": "Close"},
                "bbox": {"x": 1, "y": 2, "width": 30, "height": 20},
            }
        ],
        goal="close modal",
        inferred_kinds=("button", "dialog"),
    )
    result = TargetedProbeResult(
        goal="close modal",
        requested_kinds=("button",),
        inferred_kinds=("button", "dialog"),
        candidates=tuple(ranked),
        frames_checked=1,
    )
    payload = result.as_dict()

    assert payload["ok"] is True
    assert payload["candidates"][0]["kind"] == "button"
    assert payload["candidates"][0]["attributes"]["ariaLabel"] == "Close"


def test_choose_click_handoff_requires_confident_button_or_link() -> None:
    candidates = rank_candidates(
        [
            {
                "kind": "link",
                "text": "Learn more",
                "tag": "a",
                "selector": "a.more",
                "attributes": {"href": "/more"},
                "bbox": {"x": 10, "y": 10, "width": 90, "height": 20},
            },
            {
                "kind": "input",
                "text": "More",
                "tag": "input",
                "selector": "input.more",
                "attributes": {"placeholder": "More"},
                "bbox": {"x": 10, "y": 40, "width": 120, "height": 30},
            },
        ],
        goal="click more information",
        inferred_kinds=("link",),
    )
    result = TargetedProbeResult(
        goal="click more information",
        requested_kinds=("link",),
        inferred_kinds=("link",),
        candidates=tuple(candidates),
        frames_checked=1,
    )

    picked = choose_click_handoff_candidate(result, target_text="More information")

    assert picked is not None
    assert picked.kind == "link"
    assert picked.selector == "a.more"


def test_choose_click_handoff_ignores_attr_only_submit_false_positive() -> None:
    candidates = rank_candidates(
        [
            {
                "kind": "button",
                "text": "Button to search",
                "tag": "div",
                "selector": "div#tnb-google-search-submit-btn",
                "attributes": {"id": "tnb-google-search-submit-btn"},
                "bbox": {"x": 10, "y": 10, "width": 80, "height": 24},
            },
            {
                "kind": "button",
                "text": "Submit",
                "tag": "input",
                "selector": "input[type=submit]",
                "attributes": {"type": "submit", "value": "Submit"},
                "bbox": {"x": 10, "y": 60, "width": 70, "height": 24},
            },
        ],
        goal="click submit",
        inferred_kinds=("button",),
    )
    result = TargetedProbeResult(
        goal="click submit",
        requested_kinds=("button",),
        inferred_kinds=("button",),
        candidates=tuple(candidates),
        frames_checked=1,
    )

    picked = choose_click_handoff_candidate(result, target_text="Submit")

    assert picked is not None
    assert picked.selector == "input[type=submit]"


def test_choose_type_handoff_skips_non_text_inputs() -> None:
    candidates = rank_candidates(
        [
            {
                "kind": "input",
                "text": "Subscribe",
                "tag": "input",
                "selector": "input[type=checkbox]",
                "attributes": {"type": "checkbox", "name": "subscribe"},
                "bbox": {"x": 10, "y": 10, "width": 20, "height": 20},
            },
            {
                "kind": "input",
                "text": "Search",
                "tag": "input",
                "selector": "input.search",
                "attributes": {"type": "search", "placeholder": "Search docs", "id": "search"},
                "bbox": {"x": 10, "y": 40, "width": 180, "height": 30},
            },
        ],
        goal="search docs",
        inferred_kinds=("input",),
    )
    result = TargetedProbeResult(
        goal="search docs",
        requested_kinds=("input",),
        inferred_kinds=("input",),
        candidates=tuple(candidates),
        frames_checked=1,
    )

    picked = choose_type_handoff_candidate(result)

    assert picked is not None
    assert picked.selector == "input.search"
