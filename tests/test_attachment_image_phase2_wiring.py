"""Phase 2 TDD: thread prompt_context image attachments into the agent loop.

Covers the wiring Phase 1 deliberately left out (Slice MM-2):
  * ``prompt_image_policy.should_include_prompt_images`` — deterministic
    decision of whether to (re)send the reference image on a given step.
  * ``run_agent`` accepts a ``prompt_images`` parameter.
  * ``smart_batch_runner`` extracts an image attachment's base64 data-URL(s)
    and forwards them to ``run_agent(prompt_images=...)`` (the api path).
  * a non-image (text) attachment yields no ``prompt_images`` (no
    over-injection).
"""

from __future__ import annotations

import asyncio
import base64
import inspect
from pathlib import Path


# Minimal valid 1x1 PNG so the test needs no Pillow to produce an image.
_PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGNgYAAAAAMAASsJTYQAAAAASUVORK5CYII="
)


def _run(coro):
    return asyncio.run(coro)


class TestShouldIncludePromptImages:
    def _fn(self):
        from visual_web_agent.prompt_image_policy import should_include_prompt_images

        return should_include_prompt_images

    def test_always_sends_every_step(self) -> None:
        f = self._fn()
        assert f(5, policy="always", last_sent_url="u", current_url="u") is True

    def test_first_only_first_step(self) -> None:
        f = self._fn()
        assert f(1, policy="first", last_sent_url=None, current_url="u") is True
        assert f(2, policy="first", last_sent_url="u", current_url="u") is False

    def test_off_never_sends(self) -> None:
        f = self._fn()
        assert f(1, policy="off", last_sent_url=None, current_url="u") is False

    def test_adaptive_first_step_sends(self) -> None:
        f = self._fn()
        assert f(1, policy="adaptive", last_sent_url=None, current_url="u") is True

    def test_adaptive_same_page_later_step_skips(self) -> None:
        f = self._fn()
        assert f(3, policy="adaptive", last_sent_url="u", current_url="u") is False

    def test_adaptive_resends_on_page_change(self) -> None:
        f = self._fn()
        assert f(3, policy="adaptive", last_sent_url="old", current_url="new") is True

    def test_adaptive_early_steps_window(self) -> None:
        f = self._fn()
        # early_steps=2 → step 2 still sends even on the same page
        assert (
            f(2, policy="adaptive", last_sent_url="u", current_url="u", early_steps=2)
            is True
        )


class TestRunAgentSignature:
    def test_run_agent_accepts_prompt_images(self) -> None:
        from visual_web_agent.main import run_agent

        assert "prompt_images" in inspect.signature(run_agent).parameters


class TestSmartBatchForwardsImages:
    def _write_png(self, tmp_path: Path) -> str:
        p = tmp_path / "ref.png"
        p.write_bytes(_PNG_1x1)
        return str(p)

    def _write_txt(self, tmp_path: Path) -> str:
        p = tmp_path / "note.txt"
        p.write_text("hello world", encoding="utf-8")
        return str(p)

    def _capture_run_agent(self, monkeypatch) -> dict:
        captured: dict = {}

        async def fake_run_agent(**kwargs):
            captured.update(kwargs)
            return True

        import visual_web_agent.main as main_mod

        monkeypatch.setattr(main_mod, "run_agent", fake_run_agent)
        return captured

    def test_image_attachment_forwarded_as_prompt_images(
        self, tmp_path, monkeypatch
    ) -> None:
        captured = self._capture_run_agent(monkeypatch)
        from smart_batch_runner import run_smart_batch

        _run(
            run_smart_batch(
                "https://example.com",
                "看这张参考图，在页面上找到相同的商品",
                self._write_png(tmp_path),
            )
        )
        imgs = captured.get("prompt_images")
        assert imgs, f"expected prompt_images forwarded, got {imgs!r}"
        assert imgs[0].startswith("data:image/")

    def test_text_attachment_yields_no_prompt_images(
        self, tmp_path, monkeypatch
    ) -> None:
        captured = self._capture_run_agent(monkeypatch)
        from smart_batch_runner import run_smart_batch

        _run(
            run_smart_batch(
                "https://example.com",
                "参考这个说明完成任务",
                self._write_txt(tmp_path),
            )
        )
        assert not captured.get("prompt_images")
