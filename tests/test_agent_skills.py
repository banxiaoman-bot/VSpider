import json
from pathlib import Path

from visual_web_agent.skills.registry import build_default_skill_registry
from visual_web_agent.skills.registry import load_skill_history
from visual_web_agent.skills.registry import SkillRegistry
from visual_web_agent.skills.lint import lint_skill_registry
from visual_web_agent.skills.replay import check_skill_replays
from visual_web_agent.skills.replay import list_skill_replays
from visual_web_agent.skills.replay import replay_skill_snapshot
from visual_web_agent.skills.replay import save_skill_replay_snapshot
from visual_web_agent.skills.__main__ import main as skills_cli_main
from visual_web_agent.skills.authoring import class_name_for_skill
from visual_web_agent.skills.authoring import normalize_skill_name
from visual_web_agent.skills.authoring import register_skill_in_registry
from visual_web_agent.skills.authoring import scaffold_skill
from visual_web_agent.skills.base import SkillResult
from visual_web_agent.skills.base import SkillVerification
from visual_web_agent.skills.base import AgentSkill
from visual_web_agent.extraction_engine.strategies import infer_goal_strategy_context
from visual_web_agent.main import _clean_user_visible_done_message
from visual_web_agent.prompts import build_user_message
from visual_web_agent.trajectory_logger import HtmlLogger


def test_default_skill_registry_matches_hover_and_slider() -> None:
    registry = build_default_skill_registry()

    hover = registry.first_match(
        url="https://the-internet.herokuapp.com/hovers",
        goal="hover the middle avatar and click View profile",
    )
    assert hover is not None
    assert hover.name == "internet_hovers_profile"
    assert hover.action == "internet_hovers_macro"

    slider = registry.first_match(
        url="https://demoqa.com/slider",
        goal="move the slider to 80",
    )
    assert slider is not None
    assert slider.name == "demoqa_slider"
    assert slider.source == "DEMOQA_SLIDER_MACRO"


def test_default_skill_registry_ranks_matches_with_scores() -> None:
    registry = build_default_skill_registry()
    strategy_context = infer_goal_strategy_context(
        "move the slider to 80",
        url="https://demoqa.com/slider",
    )

    matches = registry.ranked_matches(
        url="https://demoqa.com/slider",
        goal="move the slider to 80",
        strategy_context=strategy_context,
    )

    assert matches
    assert matches[0].skill.name == "demoqa_slider"
    assert matches[0].score >= 100
    assert "match=true" in matches[0].reasons
    assert "strategy_capability:slider" in matches[0].reasons
    assert matches[0].strategy_context["preferred_modes"] == ["macro_first"]


def test_skill_history_loads_recent_report_success_rates() -> None:
    work_dir = Path("workspace") / "tmp_skill_history_test"
    work_dir.mkdir(parents=True, exist_ok=True)
    report_path = work_dir / "agent_cases_20260510_000000.json"
    report = {
        "results": [
            {
                "ok": True,
                "diagnostics": {
                    "skill_summary": {"skills": ["demoqa_slider"]}
                },
            },
            {
                "ok": False,
                "diagnostics": {
                    "skill_summary": {"skills": ["demoqa_slider"]}
                },
            },
        ]
    }
    report_path.write_text(
        json.dumps(report),
        encoding="utf-8",
    )

    history = load_skill_history(work_dir)

    assert history["demoqa_slider"]["runs"] == 2
    assert history["demoqa_slider"]["successes"] == 1
    assert history["demoqa_slider"]["success_rate"] == 0.5


def test_default_skill_registry_matches_complex_interaction_skills() -> None:
    registry = build_default_skill_registry()

    droppable = registry.first_match(
        url="https://demoqa.com/droppable",
        goal="drag Drag me to Drop here, then move the slider to 80",
    )
    assert droppable is not None
    assert droppable.name == "demoqa_droppable_slider"
    assert droppable.expected_rows == 2

    selectorshub = registry.first_match(
        url="https://selectorshub.com/xpath-practice-page/",
        goal="fill the Shadow DOM Pizza input and filter the iframe Search table",
    )
    assert selectorshub is not None
    assert selectorshub.name == "selectorshub_shadow_iframe"

    wikipedia = registry.first_match(
        url="https://en.wikipedia.org/wiki/Web_scraping",
        goal="open data mining and artificial intelligence in a New Tab",
    )
    assert wikipedia is not None
    assert wikipedia.name == "wikipedia_new_tabs"


def test_default_skill_registry_matches_modal_and_docs_skills() -> None:
    registry = build_default_skill_registry()

    modal = registry.first_match(
        url="https://demoqa.com/modal-dialogs",
        goal="click 'Small modal', extract it, close it, then click 'Large modal'",
    )
    assert modal is not None
    assert modal.name == "modal_dialogs"
    assert modal.action == "modal_dialog_macro"

    docs = registry.first_match(
        url="https://reactrouter.com/",
        goal="open Docs, click Upgrading from v6, then click Form",
    )
    assert docs is not None
    assert docs.name == "reactrouter_docs"
    assert docs.expected_rows == 2


def test_default_skill_registry_ignores_non_matching_urls() -> None:
    registry = build_default_skill_registry()

    assert registry.first_match(
        url="https://example.com/slider",
        goal="move the slider to 80",
    ) is None


def test_skill_cli_lists_registered_skills(capsys) -> None:
    assert skills_cli_main(["list", "--json"]) == 0
    output = capsys.readouterr().out

    assert '"count": 7' in output
    assert '"modal_dialogs"' in output
    assert '"reactrouter_docs"' in output
    assert '"internet_hovers_profile"' in output
    assert '"wikipedia_new_tabs"' in output


def test_skill_cli_matches_url_and_goal(capsys) -> None:
    assert skills_cli_main([
        "match",
        "--url",
        "https://demoqa.com/slider",
        "--goal",
        "move the slider to 80",
        "--json",
    ]) == 0
    output = capsys.readouterr().out

    assert '"matched": true' in output
    assert '"first_match": "demoqa_slider"' in output
    assert '"strategy_context":' in output
    assert '"macro_first"' in output
    assert '"score":' in output
    assert '"reasons":' in output


def test_strategy_context_marks_bulk_extract_fast_path() -> None:
    context = infer_goal_strategy_context(
        "extract next 7 days weather forecast",
        url="https://www.weather.com.cn/weather/101130101.shtml",
        target_count=7,
        requested_fields=["day_label", "weather"],
        data_shape={"repeated_list_items": 7, "table_rows": 0, "table_cells": 0},
    )

    assert "extract" in context["capabilities"]
    assert context["output_mode"] == "artifact"
    assert "extract_fast_path" in context["preferred_modes"]
    assert context["fallback_order"][0] == "pre_extract"
    assert "page_shape_exposes_target_rows" in context["reasons"]


def test_strategy_context_marks_weather_lookup_as_answer_mode() -> None:
    context = infer_goal_strategy_context(
        "\u5e2e\u6211\u67e5\u4e00\u4e0b\u660e\u5929\u4e0a\u6d77\u4f1a\u4e0d\u4f1a\u4e0b\u96e8\uff1f"
        "\u5982\u679c\u53ef\u80fd\u7684\u8bdd\uff0c\u628a\u6c14\u6e29\u4e5f\u544a\u8bc9\u6211\u3002",
        url="https://www.baidu.com/s?wd=weather",
    )

    assert context["output_mode"] == "answer"
    assert context["output_contract"]["answer_required"] is True
    assert "answer_first" in context["preferred_modes"]
    assert "extract_fast_path" not in context["preferred_modes"]
    assert context["fallback_order"][0] == "targeted_probe"


def test_strategy_context_marks_answer_plus_save_as_mixed_mode() -> None:
    context = infer_goal_strategy_context(
        "\u67e5\u4e00\u4e0b\u660e\u5929\u4e0a\u6d77\u4f1a\u4e0d\u4f1a\u4e0b\u96e8\uff0c"
        "\u5e76\u4fdd\u5b58\u7ed3\u679c\u3002",
        url="https://www.baidu.com/s?wd=weather",
    )

    assert context["output_mode"] == "mixed"
    assert context["output_contract"]["answer_required"] is True
    assert context["output_contract"]["save_artifact"] is True
    assert "extract_fast_path" in context["preferred_modes"]


def test_user_message_warns_answer_tasks_not_to_export_search_lists() -> None:
    message = build_user_message(
        goal=(
            "\u5e2e\u6211\u67e5\u4e00\u4e0b\u660e\u5929\u4e0a\u6d77\u4f1a\u4e0d\u4f1a\u4e0b\u96e8\uff1f"
            "\u628a\u6c14\u6e29\u4e5f\u544a\u8bc9\u6211\u3002"
        ),
        step=1,
        max_steps=10,
    )

    assert "The user is asking for a concise answer" in message
    assert "generic search-result lists" in message


def test_answer_mode_runtime_does_not_save_excel_by_default() -> None:
    source = open("visual_web_agent/main.py", encoding="utf-8").read()

    assert "answer-only result; not saving Excel artifact" in source
    assert "chat answer kept in run result" in source
    assert "answer-only auto extract kept in run result" in source
    assert "ANSWER_DONE_INTENT_GUARD" in source
    assert "__answer_done_guard" in source
    assert "_allow_done_via_answer_guard" in source
    assert "_goal_output_mode != \"answer\" or _vlm_artifact_exists" in source
    assert "ANSWER_RPA_PREFIX_ONLY" in source
    assert "_completes = False" in source
    assert "not in {\"done\", \"chat_extract\"}" in source
    assert "answer_auto_extract_completed" in source


def test_done_message_hides_answer_guard_marker() -> None:
    message = _clean_user_visible_done_message(
        "[ANSWER_DONE_INTENT_GUARD] 页面答案已满足用户问题，"
        "系统将原动作 'click' 改为 done。\n"
        "当前页面已展示完整的天气结果：小雨转阴，22~29°C。"
    )

    assert "ANSWER_DONE_INTENT_GUARD" not in message
    assert "系统将原动作" not in message
    assert message == "当前页面已展示完整的天气结果：小雨转阴，22~29°C。"

def test_done_message_hides_extract_null_marker() -> None:
    message = _clean_user_visible_done_message(
        "[EXTRACT_NULL_DOWNGRADE] 当前页面已展示答案：明天小雨，22~29°C。"
    )

    assert message == "当前页面已展示答案：明天小雨，22~29°C。"


def test_html_logger_can_use_shared_run_id(tmp_path) -> None:
    logger = HtmlLogger(goal="answer task", log_dir=tmp_path, run_id="20260522_175332")

    assert logger.path.name == "run_log_20260522_175332.html"


def test_strategy_context_does_not_treat_reading_input_as_form_fill() -> None:
    context = infer_goal_strategy_context(
        "move the slider to 80 and read the Input value",
        url="https://demoqa.com/slider",
    )

    assert "slider" in context["capabilities"]
    assert "form" not in context["capabilities"]


def test_skill_replay_snapshot_round_trip() -> None:
    work_dir = Path("workspace") / "tmp_skill_replay_test"
    work_dir.mkdir(parents=True, exist_ok=True)
    registry = build_default_skill_registry()
    match = registry.best_match(
        url="https://demoqa.com/slider",
        goal="move the slider to 80",
    )
    assert match is not None

    path = save_skill_replay_snapshot(
        skill_match=match,
        goal="move the slider to 80",
        url="https://demoqa.com/slider",
        result=SkillResult(
            source="DEMOQA_SLIDER_MACRO",
            rows=[{"step": "slider", "value": "80", "slider_value": "80"}],
            expected_rows=1,
            required_fields=["slider_value"],
            verifications=[
                SkillVerification(
                    name="DEMOQA_SLIDER_MACRO_exact_value",
                    success=True,
                    expected="80",
                    observed="80",
                )
            ],
        ),
        directory=work_dir,
    )

    replay = replay_skill_snapshot(path)
    checked = check_skill_replays(work_dir, limit=5)
    items = list_skill_replays(work_dir, limit=5)

    assert replay["ok"] is True
    assert checked["ok"] is True
    assert checked["checked"] >= 1
    assert checked["by_skill"]["demoqa_slider"]["passed"] >= 1
    assert replay["expected_skill"] == "demoqa_slider"
    assert replay["current_first_match"] == "demoqa_slider"
    assert items and items[0]["skill"] == "demoqa_slider"


def test_skill_cli_replay_list(capsys) -> None:
    assert skills_cli_main(["replay", "list", "--limit", "1", "--json"]) == 0
    output = capsys.readouterr().out

    assert '"count":' in output


def test_skill_cli_replay_check(capsys) -> None:
    assert skills_cli_main(["replay", "check", "--limit", "1", "--json"]) == 0
    output = capsys.readouterr().out

    assert '"checked":' in output
    assert '"results":' in output


def test_skill_authoring_normalizes_names() -> None:
    assert normalize_skill_name("Demo Widget!") == "demo_widget"
    assert normalize_skill_name("2026 Widget") == "skill_2026_widget"
    assert class_name_for_skill("demo_widget") == "DemoWidgetSkill"


def test_skill_scaffold_writes_skill_and_test_files() -> None:
    work_dir = Path("workspace") / "tmp_skill_scaffold_test"
    result = scaffold_skill(
        name="Demo Widget",
        aliases=["example.com/widget", "widget"],
        url_patterns=["example.com/widget"],
        goal_patterns=["widget"],
        required_fields=["title", "url"],
        expected_rows=2,
        skill_dir=work_dir / "skills",
        test_dir=work_dir / "tests",
        force=True,
    )

    skill_text = result.skill_path.read_text(encoding="utf-8")
    test_text = result.test_path.read_text(encoding="utf-8") if result.test_path else ""

    assert result.skill_name == "demo_widget"
    assert result.class_name == "DemoWidgetSkill"
    assert result.registry_import == "from .interaction.demo_widget import DemoWidgetSkill"
    assert result.registry_entry == "DemoWidgetSkill(),"
    assert "class DemoWidgetSkill" in skill_text
    assert "required_fields = ('title', 'url')" in skill_text
    assert "expected_rows = 2" in skill_text
    assert "TODO: implement deterministic browser workflow here" in skill_text
    assert "test_demo_widget_matches_expected_surface" in test_text


def test_skill_cli_scaffold_generates_files(capsys) -> None:
    work_dir = Path("workspace") / "tmp_skill_cli_scaffold_test"

    assert skills_cli_main([
        "scaffold",
        "--name",
        "CLI Widget",
        "--aliases",
        "example.com/widget,widget",
        "--url-patterns",
        "example.com/widget",
        "--goal-patterns",
        "widget",
        "--fields",
        "title,url",
        "--expected-rows",
        "2",
        "--skill-dir",
        str(work_dir / "skills"),
        "--test-dir",
        str(work_dir / "tests"),
        "--force",
        "--json",
    ]) == 0
    output = capsys.readouterr().out

    assert '"skill_name": "cli_widget"' in output
    assert (work_dir / "skills" / "cli_widget.py").exists()
    assert (work_dir / "tests" / "test_cli_widget_skill.py").exists()


def test_skill_scaffold_can_register_skill_idempotently() -> None:
    work_dir = Path("workspace") / "tmp_skill_register_test"
    registry_path = work_dir / "registry.py"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        '''"""Registry for deterministic runtime skills."""

from __future__ import annotations

from .interaction.demoqa_slider import DemoQASliderSkill


def build_default_skill_registry(*, load_history: bool = False) -> object:
    history = {}
    return SkillRegistry([
        DemoQASliderSkill(),
    ], history=history)
''',
        encoding="utf-8",
    )

    result = scaffold_skill(
        name="Register Widget",
        aliases=["example.com/register", "register"],
        url_patterns=["example.com/register"],
        goal_patterns=["register"],
        required_fields=["title"],
        skill_dir=work_dir / "skills",
        test_dir=work_dir / "tests",
        registry_path=registry_path,
        register=True,
        force=True,
    )
    changed_again = register_skill_in_registry(
        registry_path=registry_path,
        import_line=result.registry_import,
        entry_line=result.registry_entry,
    )
    registry_text = registry_path.read_text(encoding="utf-8")

    assert result.registered is True
    assert result.registry_changed is True
    assert result.registry_path == registry_path
    assert registry_text.count("from .interaction.register_widget import RegisterWidgetSkill") == 1
    assert registry_text.count("RegisterWidgetSkill(),") == 1
    assert changed_again is False


def test_skill_cli_scaffold_can_register(capsys) -> None:
    work_dir = Path("workspace") / "tmp_skill_cli_register_test"
    registry_path = work_dir / "registry.py"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        '''from .interaction.demoqa_slider import DemoQASliderSkill


def build_default_skill_registry(*, load_history: bool = False) -> object:
    return SkillRegistry([
        DemoQASliderSkill(),
    ], history={})
''',
        encoding="utf-8",
    )

    assert skills_cli_main([
        "scaffold",
        "--name",
        "CLI Register Widget",
        "--aliases",
        "example.com/register,register",
        "--url-patterns",
        "example.com/register",
        "--goal-patterns",
        "register",
        "--fields",
        "title",
        "--skill-dir",
        str(work_dir / "skills"),
        "--test-dir",
        str(work_dir / "tests"),
        "--registry-path",
        str(registry_path),
        "--register",
        "--force",
        "--json",
    ]) == 0
    output = capsys.readouterr().out
    registry_text = registry_path.read_text(encoding="utf-8")

    assert '"registered": true' in output
    assert '"registry_changed": true' in output
    assert "from .interaction.cli_register_widget import CliRegisterWidgetSkill" in registry_text
    assert "CliRegisterWidgetSkill()," in registry_text


def test_skill_lint_default_registry_passes(capsys) -> None:
    assert skills_cli_main(["lint", "--json"]) == 0
    output = capsys.readouterr().out

    assert '"ok": true' in output
    assert '"checked": 7' in output
    assert '"errors": 0' in output


def test_skill_lint_detects_contract_errors() -> None:
    class BadSkill(AgentSkill):
        name = "bad_skill"
        action = ""
        source = "bad_source"
        aliases = ("bad",)
        required_fields = ()
        expected_rows = 0

        def match(self, *, url: str, goal: str) -> object:
            return "yes"

        async def run(self, browser, goal: str) -> SkillResult:
            return SkillResult(source=self.source)

    result = lint_skill_registry(SkillRegistry([BadSkill()]))
    codes = {issue["code"] for issue in result["issues"]}

    assert result["ok"] is False
    assert result["errors"] >= 3
    assert "missing_action" in codes
    assert "missing_required_fields" in codes
    assert "invalid_expected_rows" in codes
    assert "match_not_bool" in codes


def test_skill_lint_warns_about_unregistered_skill_files() -> None:
    work_dir = Path("workspace") / "tmp_skill_lint_files"
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "orphan_skill.py").write_text("# generated but not registered\n", encoding="utf-8")

    result = lint_skill_registry(include_files=True, skill_dir=work_dir)
    issues = result["issues"]

    assert result["ok"] is True
    assert any(issue["code"] == "unregistered_skill_file" for issue in issues)
