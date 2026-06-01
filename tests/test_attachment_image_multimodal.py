"""Phase 1 TDD: image attachment -> base64 + VLM multimodal payload assembly.

Covers the reusable primitives only (no run_agent wiring yet):
  * ``AdapterResult`` carries an ``image_b64`` list + reports ``image_count``.
  * ``attachment_adapters.image.adapt`` returns a base64 data-URL (not just a
    caption) and drops the ``multimodal_feeding_not_yet_wired`` stub reason.
  * ``VLMClient._build_user_content`` assembles OpenAI/Qwen-VL multimodal
    content: screenshot first, then attachment ``extra_images``, then text;
    graceful text-only fallback when no image is present; data-URL
    normalization for raw base64.
  * ``VLMClient.ask`` accepts an ``extra_images`` parameter.
"""

from __future__ import annotations

import base64
import inspect
from pathlib import Path

from visual_web_agent.attachment_adapters import image as image_adapter
from visual_web_agent.attachment_adapters.base import AdapterResult
from visual_web_agent.io_contract.input_contract import AttachmentSpec
from visual_web_agent.vlm_client import VLMClient


# Minimal valid 1x1 PNG so the test needs no Pillow to produce an image.
_PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGNgYAAAAAMAASsJTYQAAAAASUVORK5CYII="
)


def _write_png(tmp_path: Path, name: str = "pic.png") -> Path:
    p = tmp_path / name
    p.write_bytes(_PNG_1x1)
    return p


def _spec(path: Path) -> AttachmentSpec:
    return AttachmentSpec(
        path=str(path),
        filename=path.name,
        mime="image/png",
        intent="prompt_context",
    )


class TestAdapterResultImageField:
    def test_image_b64_defaults_empty(self) -> None:
        assert AdapterResult(kind="image").image_b64 == []

    def test_to_dict_reports_image_count(self) -> None:
        r = AdapterResult(kind="image", image_b64=["data:image/png;base64,AAAA"])
        assert r.to_dict()["image_count"] == 1


class TestImageAdapter:
    def test_returns_base64_data_url(self, tmp_path: Path) -> None:
        res = image_adapter.adapt(_spec(_write_png(tmp_path)))
        assert res.ok is True
        assert res.kind == "image"
        assert len(res.image_b64) == 1
        assert res.image_b64[0].startswith("data:image/")
        assert ";base64," in res.image_b64[0]

    def test_no_longer_reports_stub_reason(self, tmp_path: Path) -> None:
        res = image_adapter.adapt(_spec(_write_png(tmp_path)))
        assert "multimodal_feeding_not_yet_wired" not in res.reasons

    def test_caption_still_present(self, tmp_path: Path) -> None:
        # A human-readable caption is retained for the text-only fallback path.
        assert image_adapter.adapt(_spec(_write_png(tmp_path))).text

    def test_missing_file_is_not_ok(self, tmp_path: Path) -> None:
        assert image_adapter.adapt(_spec(tmp_path / "nope.png")).ok is False


class TestBuildUserContent:
    def test_no_images_returns_plain_text(self) -> None:
        assert VLMClient._build_user_content(None, "hello", None) == "hello"

    def test_screenshot_then_text(self) -> None:
        out = VLMClient._build_user_content("AAAA", "hi", None)
        assert isinstance(out, list)
        assert out[0]["type"] == "image_url"
        assert out[0]["image_url"]["url"] == "data:image/jpeg;base64,AAAA"
        assert out[-1] == {"type": "text", "text": "hi"}

    def test_extra_images_appended_after_screenshot(self) -> None:
        out = VLMClient._build_user_content(
            "SHOT", "hi", ["data:image/png;base64,IMG1", "IMG2"]
        )
        urls = [c["image_url"]["url"] for c in out if c["type"] == "image_url"]
        assert urls == [
            "data:image/jpeg;base64,SHOT",
            "data:image/png;base64,IMG1",
            "data:image/jpeg;base64,IMG2",
        ]
        assert out[-1]["type"] == "text"

    def test_extra_images_without_screenshot_still_multimodal(self) -> None:
        out = VLMClient._build_user_content(None, "hi", ["IMG1"])
        assert isinstance(out, list)
        assert out[0]["image_url"]["url"] == "data:image/jpeg;base64,IMG1"

    def test_blank_extra_images_skipped(self) -> None:
        assert VLMClient._build_user_content(None, "hi", ["", None]) == "hi"


class TestAskSignature:
    def test_ask_accepts_extra_images(self) -> None:
        assert "extra_images" in inspect.signature(VLMClient.ask).parameters
