import asyncio

from visual_web_agent.targeted_type_fallback import (
    FILL_BEST_INPUT_JS,
    fill_best_text_input,
)


class _Frame:
    def __init__(self, result, url="https://example.test", name=""):
        self._result = result
        self.url = url
        self.name = name
        self.calls = []

    async def evaluate(self, script, arg):
        self.calls.append((script, arg))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _Page:
    def __init__(self, frames):
        self.frames = frames


def _run(coro):
    return asyncio.run(coro)


def test_fill_best_text_input_returns_first_successful_frame() -> None:
    bad = _Frame({"ok": False, "reason": "no_visible_input"})
    good = _Frame({"ok": True, "tag": "input", "value": "Python教程"}, url="https://bing.test", name="main")

    result = _run(fill_best_text_input(_Page([bad, good]), "Python教程"))

    assert result["ok"] is True
    assert result["value"] == "Python教程"
    assert result["frame_url"] == "https://bing.test"
    assert good.calls[0][1] == ["Python教程"]


def test_fill_best_text_input_skips_throwing_frames() -> None:
    throwing = _Frame(RuntimeError("cross-origin-ish"))
    good = _Frame({"ok": True, "tag": "textarea", "value": "VLM Agent"})

    result = _run(fill_best_text_input(_Page([throwing, good]), "VLM Agent"))

    assert result["ok"] is True
    assert result["tag"] == "textarea"


def test_fill_best_text_input_reports_failure() -> None:
    result = _run(fill_best_text_input(_Page([_Frame({"ok": False})]), "x"))

    assert result["ok"] is False
    assert result["reason"] == "no_visible_input"


def test_js_prioritizes_search_and_focused_inputs() -> None:
    assert "document.activeElement" in FILL_BEST_INPUT_JS
    assert "role" in FILL_BEST_INPUT_JS
    assert "searchbox" in FILL_BEST_INPUT_JS
    assert 'input[name="q"]' in FILL_BEST_INPUT_JS
