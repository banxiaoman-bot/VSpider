from visual_web_agent.debug_cli import (
    _cmd_tools,
    _summarize_som_elements,
    build_parser,
)


def test_parser_accepts_tools_command() -> None:
    parser = build_parser()
    args = parser.parse_args(["tools", "--goal", "点击 Next 翻页"])
    assert args.command == "tools"
    assert args.goal == "点击 Next 翻页"


def test_parser_accepts_probe_command() -> None:
    parser = build_parser()
    args = parser.parse_args([
        "probe",
        "--url",
        "https://example.com",
        "--goal",
        "find search input",
        "--kind",
        "input",
    ])
    assert args.command == "probe"
    assert args.goal == "find search input"
    assert args.kind == ["input"]


def test_summarizes_som_elements() -> None:
    rows = _summarize_som_elements(
        [
            {
                "id": 7,
                "role": "button",
                "name": "Next",
                "state": "enabled",
                "rect": {"x": 10, "y": 20, "width": 80, "height": 30},
            }
        ]
    )
    assert rows == [
        {
            "id": 7,
            "role": "button",
            "name": "Next",
            "state": "enabled",
            "input_desc": "",
            "x": 10,
            "y": 20,
            "width": 80,
            "height": 30,
        }
    ]


def test_tools_command_prints_selected_tool(capsys) -> None:
    parser = build_parser()
    args = parser.parse_args(["tools", "--goal", "点击 Next 翻页"])
    assert _cmd_tools(args) == 0
    output = capsys.readouterr().out
    assert "next_page" in output
