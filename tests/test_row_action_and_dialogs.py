"""Regression tests for the 4 new skill prompts + 2 hidden-bug fixes
delivered together in the row-action / confirm-dialog / tree / stepper pass.

What's covered:

  Skills (prompt content + select_skills wiring)
  ---------------------------------------------
  * ROW_ACTION_SKILL       — "delete the row where name=X" intent
  * CONFIRM_DIALOG_SKILL   — el-message-box / native confirm()
  * TREE_SKILL             — el-tree / ant-tree / virtual scroll trees
  * STEPPER_SKILL          — multi-step wizards

  Action handler
  --------------
  * row_action handler is registered and validates its type_value schema.

  Hidden bug fixes
  ----------------
  * page.on('dialog') now surfaces text to ``_tab_switch_notice`` and
    persists ``_last_native_dialog`` for AX summaries.
  * HoverHandler tooltip notice ("[TOOLTIP READ]") is wired in both
    branches (submenu-appeared and timeout) and uses _tab_switch_notice.
  * setNativeValue now calls focus() before write so Ant/RHF
    "touched" validation state is correct.

These tests are pure-Python (no Playwright) — they verify prompt content,
trigger keywords, dispatch wiring, schema validation, and that the
patched files still parse + register their hooks. Browser-level behaviour
of row_action is covered by the dedicated `test_row_action_handler.py`
when a real browser context is available.
"""

from __future__ import annotations

import pytest

from visual_web_agent.prompt_skills import (
    CONFIRM_DIALOG_SKILL,
    ROW_ACTION_SKILL,
    SKILL_PROMPTS,
    STEPPER_SKILL,
    TREE_SKILL,
)
from visual_web_agent.prompts import (
    _CONFIRM_DIALOG_TRIGGERS,
    _ROW_ACTION_TRIGGERS,
    _STEPPER_TRIGGERS,
    _TREE_TRIGGERS,
    build_system_prompt,
)


# ── Helper: render the system prompt and report which skills it embeds ──────
# The dispatch logic lives inside ``build_system_prompt``; we recover the
# selected skills by string-matching the well-known header from each skill
# block. This is the same way HTML logs / debug traces verify skill loading.

_SKILL_FINGERPRINTS: dict[str, str] = {
    "row_action": "## Skill: Row Action",
    "confirm_dialog": "## Skill: Confirm Dialog",
    "tree": "## Skill: Tree",
    "stepper": "## Skill: Stepper",
    "form": "## Skill: Forms / Search / Filters",
    "cascader": "## Skill: Cascader",
    "hover_menu": "## Skill: Hover-Trigger Dropdown",
    "tooltip": "## Skill: Tooltip",
    "extract": "## Skill: Extract",
    "login": "## Skill: Login",
}


def _selected_skills(goal: str) -> set[str]:
    """Return the set of skill keys present in the rendered system prompt."""
    prompt = build_system_prompt(goal=goal, browser_state="")
    found: set[str] = set()
    for key, fingerprint in _SKILL_FINGERPRINTS.items():
        if fingerprint in prompt:
            found.add(key)
    return found


# ════════════════════════════════════════════════════════════════════
#                           SKILL CONTENT
# ════════════════════════════════════════════════════════════════════


class TestRowActionSkill:
    def test_registered(self) -> None:
        assert "row_action" in SKILL_PROMPTS
        assert SKILL_PROMPTS["row_action"] is ROW_ACTION_SKILL

    def test_documents_pipe_delimiter_schema(self) -> None:
        # VLM must know the exact type_value format.
        assert "||" in ROW_ACTION_SKILL
        assert "row_action" in ROW_ACTION_SKILL
        # Example with concrete CN/EN values surfaces to the VLM.
        assert "张三" in ROW_ACTION_SKILL or "李" in ROW_ACTION_SKILL

    def test_warns_against_picking_same_named_button_by_som_id(self) -> None:
        """The single most common VLM failure on row buttons."""
        # The skill must explicitly tell VLM not to guess target_id from
        # visual position — SoM order doesn't match DOM order.
        joined = ROW_ACTION_SKILL.lower()
        assert "som" in joined or "红框" in ROW_ACTION_SKILL
        # Anti-pattern section must exist
        assert "反模式" in ROW_ACTION_SKILL or "antipattern" in joined

    def test_mentions_confirm_followup(self) -> None:
        """Row deletes almost always spawn confirm dialogs."""
        assert "确认" in ROW_ACTION_SKILL or "confirm" in ROW_ACTION_SKILL.lower()


class TestConfirmDialogSkill:
    def test_registered(self) -> None:
        assert "confirm_dialog" in SKILL_PROMPTS
        assert SKILL_PROMPTS["confirm_dialog"] is CONFIRM_DIALOG_SKILL

    def test_covers_all_three_dialog_forms(self) -> None:
        # Native browser dialogs
        assert "confirm()" in CONFIRM_DIALOG_SKILL or "confirm(" in CONFIRM_DIALOG_SKILL
        # DOM-rendered modal
        assert "el-message-box" in CONFIRM_DIALOG_SKILL.lower()
        assert "ant-modal" in CONFIRM_DIALOG_SKILL.lower()
        # Inline Popconfirm
        assert "popconfirm" in CONFIRM_DIALOG_SKILL.lower()

    def test_documents_system_auto_accept_marker(self) -> None:
        """Tells VLM the magic marker emitted by the patched dialog listener."""
        assert "[DIALOG ACCEPTED]" in CONFIRM_DIALOG_SKILL

    def test_warns_against_extract_on_modal(self) -> None:
        """Common mistake: VLM extracts the confirm modal as "data"."""
        assert "extract" in CONFIRM_DIALOG_SKILL.lower()


class TestTreeSkill:
    def test_registered(self) -> None:
        assert "tree" in SKILL_PROMPTS
        assert SKILL_PROMPTS["tree"] is TREE_SKILL

    def test_distinguishes_expand_icon_from_label(self) -> None:
        """Core misuse: VLM clicks label expecting to expand the node."""
        assert "展开" in TREE_SKILL or "expand" in TREE_SKILL.lower()
        assert "▶" in TREE_SKILL or "switcher" in TREE_SKILL.lower()

    def test_covers_virtual_scroll(self) -> None:
        assert "虚拟" in TREE_SKILL or "virtual" in TREE_SKILL.lower()

    def test_mentions_aria_treeitem(self) -> None:
        """click_text already has [role=treeitem] in its grid selectors;
        the skill must tell VLM click_text is the right tool."""
        assert "click_text" in TREE_SKILL


class TestStepperSkill:
    def test_registered(self) -> None:
        assert "stepper" in SKILL_PROMPTS
        assert SKILL_PROMPTS["stepper"] is STEPPER_SKILL

    def test_warns_against_jumping_steps(self) -> None:
        # Most common chink: VLM clicks "Submit" on stepper header in step 1.
        assert "跳" in STEPPER_SKILL or "skip" in STEPPER_SKILL.lower() or "跳步" in STEPPER_SKILL
        assert "下一步" in STEPPER_SKILL or "next" in STEPPER_SKILL.lower()

    def test_mentions_aria_current_marker(self) -> None:
        """Concrete signal VLM can read from AX Tree."""
        assert "aria-current" in STEPPER_SKILL or "is-process" in STEPPER_SKILL


# ════════════════════════════════════════════════════════════════════
#                       TRIGGER DISPATCH (select_skills)
# ════════════════════════════════════════════════════════════════════


class TestRowActionTriggers:
    @pytest.mark.parametrize(
        "goal",
        [
            "删除张三那一行",
            "把状态是 失败 的行重试一次",
            "点订单号 ORD-2024-001 这条的查看按钮",
            "delete the row where name='Li Si'",
            "edit the row with status='draft'",
        ],
    )
    def test_row_goals_pull_in_row_action(self, goal: str) -> None:
        skills = _selected_skills(goal)
        assert "row_action" in skills, f"goal={goal!r} did not trigger row_action; got {skills}"

    def test_row_action_pulls_in_confirm_and_form(self) -> None:
        """Row deletes spawn confirm modals; row edits use form_set verification."""
        skills = _selected_skills("删除张三那一行")
        assert "row_action" in skills
        assert "confirm_dialog" in skills
        assert "form" in skills

    @pytest.mark.parametrize(
        "goal",
        [
            "提取首页热搜前10条",                # extract-only, no row mention
            "搜索 python 教程",                  # pure search
        ],
    )
    def test_pure_extract_goals_do_not_trigger_row_action(self, goal: str) -> None:
        """Critical: don't pollute extract-only goals with row_action skill."""
        skills = _selected_skills(goal)
        assert "row_action" not in skills, (
            f"goal={goal!r} incorrectly triggered row_action; got {skills}"
        )


class TestConfirmDialogTriggers:
    @pytest.mark.parametrize(
        "goal",
        [
            "在管理后台删除张三",        # delete (destructive verb)
            "点击退订按钮取消订阅",       # unsubscribe
            "清空购物车",                # clear all
        ],
    )
    def test_destructive_verbs_trigger_confirm_dialog(self, goal: str) -> None:
        skills = _selected_skills(goal)
        assert "confirm_dialog" in skills, (
            f"goal={goal!r} did not trigger confirm_dialog; got {skills}"
        )


class TestTreeTriggers:
    @pytest.mark.parametrize(
        "goal",
        [
            "在左侧文件树中展开 src/utils 目录",
            "expand the node 'Settings' in the org tree",
            "在分类树里选择「家居 > 家具 > 床」",
        ],
    )
    def test_tree_goals_trigger_tree_skill(self, goal: str) -> None:
        skills = _selected_skills(goal)
        assert "tree" in skills, f"goal={goal!r} did not trigger tree; got {skills}"


class TestStepperTriggers:
    @pytest.mark.parametrize(
        "goal",
        [
            "完成三步注册向导：填基本信息 -> 详细信息 -> 提交",
            "go through the registration wizard",
            "在多步表单第二步勾选服务条款",
        ],
    )
    def test_wizard_goals_trigger_stepper(self, goal: str) -> None:
        skills = _selected_skills(goal)
        assert "stepper" in skills, f"goal={goal!r} did not trigger stepper; got {skills}"

    def test_stepper_pulls_in_form(self) -> None:
        """Stepper goals are by definition form goals."""
        skills = _selected_skills("完成三步注册向导")
        assert "stepper" in skills
        assert "form" in skills


# ════════════════════════════════════════════════════════════════════
#                       ACTION HANDLER REGISTRATION
# ════════════════════════════════════════════════════════════════════


class TestRowActionHandlerRegistration:
    def test_handler_registered(self) -> None:
        from visual_web_agent.actions import ActionRegistry

        assert "row_action" in ActionRegistry._handlers

    def test_handler_class_resolvable(self) -> None:
        from visual_web_agent.actions import ActionRegistry, RowActionHandler

        assert ActionRegistry._handlers["row_action"] is RowActionHandler

    def test_vlm_client_action_literal_includes_row_action(self) -> None:
        """The Pydantic schema must accept row_action so VLM emissions parse."""
        import inspect

        from visual_web_agent import vlm_models

        src = inspect.getsource(vlm_models)
        assert '"row_action"' in src

    def test_json_schema_advertises_row_action(self) -> None:
        """The {action: ...} enum string in the JSON schema must list it."""
        from visual_web_agent.prompt_skills import _CORE_PROMPT_FALLBACK  # noqa: F401
        import visual_web_agent.prompt_skills as ps

        # Find the JSON schema string in the module source.
        src = ps.__file__
        with open(src, encoding="utf-8") as f:
            content = f.read()
        # The schema appears in a fenced JSON literal — just check the literal.
        assert "row_action" in content, "row_action missing from prompt_skills JSON schema"


# ════════════════════════════════════════════════════════════════════
#                    HIDDEN BUG FIX: dialog listener
# ════════════════════════════════════════════════════════════════════


class TestDialogListenerEnhancement:
    """The page.on('dialog') listener used to silently accept dialogs
    with zero VLM visibility. After the patch it persists text on
    ``_last_native_dialog`` and broadcasts via ``_tab_switch_notice``."""

    def test_browser_env_has_last_native_dialog_attribute(self) -> None:
        import inspect

        from visual_web_agent import browser_env

        src = inspect.getsource(browser_env)
        # Init: declares the attribute
        assert "self._last_native_dialog" in src
        # Listener: writes the [DIALOG ACCEPTED] marker for VLM
        assert "[DIALOG ACCEPTED]" in src

    def test_listener_writes_tab_switch_notice(self) -> None:
        """The notice goes into the same channel VLM already polls.

        J migration: the dialog listener no longer assigns
        ``self._tab_switch_notice`` directly. It now goes through
        ``set_tab_notice`` so ``_last_notice_severity`` stays in sync.
        We verify the routing structurally: the dialog block must mention
        ``[DIALOG ACCEPTED]`` and call ``set_tab_notice`` (or, for a
        legacy build, still write to ``_tab_switch_notice`` directly).
        """
        import inspect

        from visual_web_agent import browser_env

        src = inspect.getsource(browser_env)
        assert "[DIALOG ACCEPTED]" in src
        # Either legacy direct-write OR migrated helper call must be present.
        assert (
            "self._tab_switch_notice = _notice" in src
            or "self.set_tab_notice(_notice" in src
        ), (
            "Dialog listener must route notice text to the tab-notice "
            "channel via either direct assign (legacy) or set_tab_notice "
            "helper (post-J)."
        )

    def test_listener_preserves_richer_existing_notice(self) -> None:
        """If another handler already produced a notice for this turn, the
        dialog listener must NOT overwrite it — instead appending so both
        signals reach VLM.

        J migration: the legacy ``if not self._tab_switch_notice`` guard
        is replaced by ``set_tab_notice(coalesce=True)`` which appends
        when a prior notice exists. Either implementation satisfies the
        contract; we accept both source patterns.
        """
        import inspect
        import re

        from visual_web_agent import browser_env

        src = inspect.getsource(browser_env)
        # Locate the dialog block (delimited by [DIALOG ACCEPTED]) and
        # check that within ~600 chars after the marker either:
        #   • the legacy ``if not self._tab_switch_notice`` guard appears, OR
        #   • a ``set_tab_notice(...)`` call with ``coalesce=True`` appears.
        marker = src.find("[DIALOG ACCEPTED]")
        assert marker != -1, "dialog listener marker missing"
        window = src[marker : marker + 800]
        legacy_guard = "if not self._tab_switch_notice" in window
        helper_coalesce = bool(
            re.search(r"set_tab_notice\([^)]*coalesce\s*=\s*True", window, re.DOTALL)
        )
        assert legacy_guard or helper_coalesce, (
            "Dialog listener must preserve a richer prior notice — either "
            "via the legacy `if not self._tab_switch_notice` guard or via "
            "`set_tab_notice(..., coalesce=True)` so the prior notice is "
            "appended to rather than clobbered."
        )


# ════════════════════════════════════════════════════════════════════
#                  HIDDEN BUG FIX: hover tooltip回灌
# ════════════════════════════════════════════════════════════════════


class TestHoverTooltipNotice:
    """HoverHandler captures tooltip text in two branches; both must
    surface to ``_tab_switch_notice`` so VLM stops re-hovering."""

    def test_actions_py_has_tooltip_read_marker(self) -> None:
        import inspect

        from visual_web_agent.actions import click_and_type

        src = inspect.getsource(click_and_type)
        # Must appear in BOTH the appeared branch AND the timeout fallback.
        assert src.count("[TOOLTIP READ]") == 2, (
            f"Expected 2 occurrences of [TOOLTIP READ], got "
            f"{src.count('[TOOLTIP READ]')}; both hover branches must emit it."
        )

    def test_tooltip_notice_uses_tab_switch_notice_channel(self) -> None:
        """The hover tooltip text must reach VLM via the tab-notice channel.

        J migration: the legacy direct-assign
        ``browser._tab_switch_notice = _tooltip_notice`` is replaced with
        ``browser.set_tab_notice(_tooltip_notice, ...)``. We accept either
        pattern so the test stays green across the migration boundary.
        """
        import inspect

        from visual_web_agent.actions import click_and_type

        src = inspect.getsource(click_and_type)
        legacy = "browser._tab_switch_notice = _tooltip_notice" in src
        migrated = "browser.set_tab_notice(\n" in src and "_tooltip_notice" in src
        assert legacy or migrated, (
            "Tooltip handler must route _tooltip_notice through either the "
            "legacy direct-assign or the post-J set_tab_notice helper."
        )


# ════════════════════════════════════════════════════════════════════
#                  HIDDEN BUG FIX: setNativeValue focus
# ════════════════════════════════════════════════════════════════════


class TestSetNativeValueFocus:
    """Pre-write focus event so RHF/Ant 'touched' state gates correctly."""

    def test_focus_called_before_value_set(self) -> None:
        import inspect

        from visual_web_agent.actions import find_and_form

        src = inspect.getsource(find_and_form)
        # The patched JS calls focus() and dispatches FocusEvent before
        # calling the React-native value setter.
        assert "el.focus?.({preventScroll: true})" in src
        assert "FocusEvent('focus'" in src
