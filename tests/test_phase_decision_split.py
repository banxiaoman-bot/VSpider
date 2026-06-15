"""G3 decision phase split — TDD tests.

Verify that ``make_vlm_decision()`` wraps the VLM call with prompt image
policy and returns decisions.
Pure unit tests; no browser, no VLM, no network.
"""

from __future__ import annotations

import asyncio
import types


class TestMakeVlmDecision:
    def test_returns_decisions_from_vlm(self):
        from visual_web_agent.phases.decision import make_vlm_decision

        expected = [{"action": "click", "target_id": 5}]

        async def _ask(*args, **kwargs):
            return expected

        vlm = types.SimpleNamespace(ask=_ask)

        async def _run():
            return await make_vlm_decision(
                vlm=vlm, screenshot_b64="base64data", goal="click button",
                step=1, input_descriptions="desc", workflow_memory={},
                task_plan=None, max_steps=30, som_elements=None,
                capability_route=None, prompt_images=[],
                prompt_image_policy="off", current_url="https://example.com",
                last_prompt_image_url=None,
            )

        result = asyncio.run(_run())
        assert result.decisions == expected
        assert result.prompt_images_sent is False

    def test_adaptive_policy_sends_images_on_first_step(self):
        from visual_web_agent.phases.decision import make_vlm_decision

        sent_images = []

        async def _ask(*args, **kwargs):
            sent_images.append(kwargs.get("extra_images"))
            return [{"action": "wait"}]

        vlm = types.SimpleNamespace(ask=_ask)

        async def _run():
            return await make_vlm_decision(
                vlm=vlm, screenshot_b64="b64", goal="test",
                step=1, input_descriptions="", workflow_memory={},
                task_plan=None, max_steps=30, som_elements=None,
                capability_route=None, prompt_images=["img1.png"],
                prompt_image_policy="adaptive", current_url="https://example.com",
                last_prompt_image_url=None,
            )

        result = asyncio.run(_run())
        assert result.prompt_images_sent is True
        assert sent_images[0] == ["img1.png"]

    def test_empty_images_never_sends(self):
        from visual_web_agent.phases.decision import make_vlm_decision

        async def _ask(*args, **kwargs):
            return [{"action": "done"}]

        vlm = types.SimpleNamespace(ask=_ask)

        async def _run():
            return await make_vlm_decision(
                vlm=vlm, screenshot_b64="b64", goal="test",
                step=1, input_descriptions="", workflow_memory={},
                task_plan=None, max_steps=30, som_elements=None,
                capability_route=None, prompt_images=[],
                prompt_image_policy="always", current_url="https://example.com",
                last_prompt_image_url=None,
            )

        result = asyncio.run(_run())
        assert result.prompt_images_sent is False
