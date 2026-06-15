"""G6 SoM injector split — TDD tests.

Verify that ``SomInjector`` evaluates SoM JS across frames, collects
element maps, handles heavy-page detection, and retries on zero elements.
Pure unit tests; no browser, no network.
"""

from __future__ import annotations

import asyncio
import types


def _make_stub_frame(result=None, raise_err=None):
    """Build a frame stub that returns result from evaluate()."""
    async def _evaluate(js, args=None):
        if raise_err:
            raise raise_err
        return result

    return types.SimpleNamespace(evaluate=_evaluate)


def _make_stub_page(frames=None, is_closed=False, url="https://example.com"):
    page = types.SimpleNamespace()
    page.frames = frames or []
    page.is_closed = lambda: is_closed
    page.url = url
    return page


class TestInjectAcrossFrames:
    def test_single_frame_returns_elements(self):
        from visual_web_agent.som_injector import SomInjector

        injector = SomInjector(som_js="fake_js")
        frame = _make_stub_frame(result={
            "nextId": 6,
            "resultMap": [
                {"id": 1, "role": "button", "name": "Submit"},
                {"id": 2, "role": "link", "name": "Home"},
            ],
        })

        async def _run():
            return await injector.inject_across_frames([frame], step=1)

        result, descs = asyncio.run(_run())
        assert result.total_elements == 2
        assert result.injected_frames == 1
        assert len(result.elements) == 2
        assert result.elements[0]["name"] == "Submit"

    def test_multiple_frames_accumulate(self):
        from visual_web_agent.som_injector import SomInjector

        injector = SomInjector(som_js="fake_js")
        f1 = _make_stub_frame(result={"nextId": 3, "resultMap": [{"id": 1}, {"id": 2}]})
        f2 = _make_stub_frame(result={"nextId": 5, "resultMap": [{"id": 3}, {"id": 4}]})

        async def _run():
            return await injector.inject_across_frames([f1, f2], step=1)

        result, _ = asyncio.run(_run())
        assert result.total_elements == 4
        assert result.injected_frames == 2
        assert len(result.elements) == 4

    def test_frame_error_skipped_gracefully(self):
        from visual_web_agent.som_injector import SomInjector

        injector = SomInjector(som_js="fake_js")
        good = _make_stub_frame(result={"nextId": 2, "resultMap": [{"id": 1}]})
        bad = _make_stub_frame(raise_err=RuntimeError("detached"))

        async def _run():
            return await injector.inject_across_frames([bad, good], step=1)

        result, _ = asyncio.run(_run())
        assert result.total_elements == 1
        assert result.injected_frames == 1

    def test_empty_frames_returns_zero(self):
        from visual_web_agent.som_injector import SomInjector

        injector = SomInjector(som_js="fake_js")

        async def _run():
            return await injector.inject_across_frames([], step=1)

        result, _ = asyncio.run(_run())
        assert result.total_elements == 0
        assert result.injected_frames == 0

    def test_heavy_page_detection(self):
        from visual_web_agent.som_injector import SomInjector

        injector = SomInjector(som_js="fake_js")
        elements = [{"id": i} for i in range(200)]
        frame = _make_stub_frame(result={"nextId": 201, "resultMap": elements})

        async def _run():
            return await injector.inject_across_frames([frame], step=1)

        result, _ = asyncio.run(_run())
        assert result.is_heavy
        assert result.total_elements == 200

    def test_format_element_callback(self):
        from visual_web_agent.som_injector import SomInjector

        injector = SomInjector(som_js="fake_js")
        frame = _make_stub_frame(result={
            "nextId": 2,
            "resultMap": [{"id": 1, "name": "btn"}],
        })

        formatted = []
        def _fmt(el):
            s = f"@e{el['id']}:{el['name']}"
            formatted.append(s)
            return s

        async def _run():
            return await injector.inject_across_frames(
                [frame], step=1, format_element=_fmt
            )

        _, descs = asyncio.run(_run())
        assert descs == ["@e1:btn"]
        assert formatted == ["@e1:btn"]

    def test_scope_passed_to_evaluate(self):
        from visual_web_agent.som_injector import SomInjector

        injector = SomInjector(som_js="fake_js")
        captured = []

        async def _eval(js, args=None):
            captured.append(args)
            return {"nextId": 1, "resultMap": []}

        frame = types.SimpleNamespace(evaluate=_eval)

        async def _run():
            return await injector.inject_across_frames(
                [frame], scope="viewport", step=1
            )

        asyncio.run(_run())
        assert captured[0] == {"startIndex": 1, "scope": "viewport"}


class TestRetryInjection:
    def test_retry_on_closed_page_returns_none(self):
        from visual_web_agent.som_injector import SomInjector

        injector = SomInjector(som_js="fake_js")
        page = _make_stub_page(is_closed=True)

        async def _run():
            return await injector.retry_injection(page, step=1)

        assert asyncio.run(_run()) is None

    def test_retry_on_about_blank_returns_none(self):
        from visual_web_agent.som_injector import SomInjector

        injector = SomInjector(som_js="fake_js")
        page = _make_stub_page(url="about:blank")

        async def _run():
            return await injector.retry_injection(page, step=1)

        assert asyncio.run(_run()) is None


class TestSomResult:
    def test_defaults(self):
        from visual_web_agent.som_injector import SomResult

        r = SomResult()
        assert r.elements == []
        assert r.total_elements == 0
        assert r.injected_frames == 0
        assert r.duration_ms == 0
        assert not r.is_heavy
        assert not r.retried
