"""Regression: prompt docs must surface fetch_link_content so VLM picks it.

The handler is registered and the JSON schema enum allows it, but the model
won't choose it unless the system prompt documents (a) the action name in the
enum, (b) target_id semantics, (c) when to prefer it over click_new_tab.

If any of these get stripped during a future prompt refactor, the action
falls back to dead-code status — these tests catch that.
"""

from __future__ import annotations

from visual_web_agent.prompt_skills import (
    ACTION_REFERENCE_PROMPT,
    JSON_SCHEMA_PROMPT,
    MULTI_TAB_SKILL,
)


def test_action_enum_lists_fetch_link_content() -> None:
    """JSON schema 'action' field must include the new vocab entry."""
    assert "fetch_link_content" in JSON_SCHEMA_PROMPT
    assert "fetch_links_batch" in JSON_SCHEMA_PROMPT
    # Sanity: still listed alongside other tab-related actions
    for sibling in ("click_new_tab", "switch_tab", "close_tab"):
        assert sibling in JSON_SCHEMA_PROMPT


def test_action_field_requirements_explain_target_id_for_fetch_link_content() -> None:
    """target_id contract for fetch_link_content needs to be explicit — it
    differs from regular click (URL fallback via type_value)."""
    # The schema includes the action twice: (1) in the `action` enum, and
    # (2) in the per-action `字段要求` bullet section. We want the second.
    occurrences = [
        i for i in range(len(JSON_SCHEMA_PROMPT))
        if JSON_SCHEMA_PROMPT.startswith("fetch_link_content", i)
    ]
    assert len(occurrences) >= 2, (
        "fetch_link_content should appear in both the enum AND the field-"
        "requirements bullet section"
    )
    # Inspect the bullet block (last occurrence)
    bullet_idx = occurrences[-1]
    block = JSON_SCHEMA_PROMPT[bullet_idx : bullet_idx + 200]
    assert ("target_id" in block) or ("URL" in block) or ("type_value" in block)
    assert "memory_key" in block
    assert "selectors" in JSON_SCHEMA_PROMPT
    assert "mode" in JSON_SCHEMA_PROMPT


def test_action_reference_includes_fetch_link_content_description() -> None:
    """The action reference is where VLM learns *when* to choose each action."""
    assert "fetch_link_content" in ACTION_REFERENCE_PROMPT
    desc = ACTION_REFERENCE_PROMPT[
        ACTION_REFERENCE_PROMPT.index("fetch_link_content") :
    ]
    # Must surface the core differentiators
    for needle in (
        "memory_key",          # required field
        "url",                 # what the action produces
        "title",               # result key
        "content",             # result key
        "fetch_links_batch",    # batch fast path
        "selectors",            # precision extraction
        "mode",                 # dom/ax extraction mode
        "javascript:",         # rejected scheme reminder
    ):
        assert needle in desc, f"missing in action reference: {needle!r}"


def test_multi_tab_skill_recommends_fetch_link_content_for_bulk_extract() -> None:
    """The MULTI_TAB skill is loaded when goal mentions tabs / bulk extract.
    It must steer VLM toward fetch_link_content for the read-only case."""
    assert "fetch_link_content" in MULTI_TAB_SKILL
    assert "fetch_links_batch" in MULTI_TAB_SKILL
    assert "selectors" in MULTI_TAB_SKILL
    assert "mode" in MULTI_TAB_SKILL
    # Must include a decision rule (when to use vs not)
    assert "决策" in MULTI_TAB_SKILL or "❌" in MULTI_TAB_SKILL
    # Must mention the cost-saving rationale
    assert "VLM" in MULTI_TAB_SKILL or "截图" in MULTI_TAB_SKILL


def test_multi_tab_skill_warns_about_interaction_required_case() -> None:
    """If new tab needs interaction → must use click_new_tab, not fetch."""
    text = MULTI_TAB_SKILL.lower()
    # Some signal of "if you need to interact, don't use fetch"
    assert ("交互" in MULTI_TAB_SKILL) or ("interact" in text)


def test_multi_tab_skill_calls_out_clf_anti_bot_fallback() -> None:
    """Real-world gotcha: Cloudflare / 反爬 → falls through fetch_link_content.
    VLM needs to know the failure mode + recovery path (click_new_tab)."""
    assert "Cloudflare" in MULTI_TAB_SKILL or "反爬" in MULTI_TAB_SKILL


def test_legacy_prompts_module_action_enum_in_sync() -> None:
    """prompt_templates.py has the SYSTEM_PROMPT with action enum; keep consistent."""
    from visual_web_agent import prompt_templates as _templates
    import inspect
    src = inspect.getsource(_templates)
    assert "fetch_link_content" in src, (
        "prompt_templates.py must list fetch_link_content in its action enum"
    )
    assert "fetch_links_batch" in src, (
        "prompt_templates.py must list fetch_links_batch in its action enum"
    )


def test_multi_tab_triggers_match_bulk_extract_goals() -> None:
    """The multi-tab skill must load for goals like 'batch fetch the title of
    each link', not only for goals literally mentioning tabs."""
    from visual_web_agent.prompts import _MULTI_TAB_TRIGGERS

    triggers = " ".join(_MULTI_TAB_TRIGGERS).lower()
    # At least one bulk-fetch phrase must be present
    bulk_phrases_zh = ("每个链接", "每条结果", "依次抓取", "批量")
    matched_zh = sum(1 for p in bulk_phrases_zh if p in " ".join(_MULTI_TAB_TRIGGERS))
    assert matched_zh >= 2, (
        "Expected at least 2 bulk-fetch trigger phrases in _MULTI_TAB_TRIGGERS"
    )
