"""Image-as-prompt-context adapter.

Turns an image attachment into a base64 data-URL the agent can feed to the
VLM as an extra multimodal image, plus a short human-readable caption that
stays usable on the text-only fallback path. Large images are downscaled
(when Pillow is available) to keep request token cost bounded.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any

from visual_web_agent.io_contract.input_contract import AttachmentSpec

from .base import AdapterResult


_DEFAULT_MAX_BYTES = 4_000_000      # encode raw below this; downscale above
_DEFAULT_MAX_DIM = 1536             # longest edge after downscale
_DEFAULT_JPEG_QUALITY = 80


def _guess_mime(spec: AttachmentSpec, path: Path) -> str:
    if spec.mime and spec.mime.lower().startswith("image/"):
        return spec.mime.lower()
    suffix = path.suffix.lower().lstrip(".")
    if suffix in {"jpg", "jpeg"}:
        return "image/jpeg"
    if suffix:
        return f"image/{suffix}"
    return "image/jpeg"


def _encode_data_url(path: Path, mime: str, max_bytes: int) -> str:
    data = path.read_bytes()
    if len(data) > max_bytes:
        try:
            from PIL import Image  # type: ignore

            with Image.open(path) as img:
                img = img.convert("RGB")
                img.thumbnail((_DEFAULT_MAX_DIM, _DEFAULT_MAX_DIM))
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=_DEFAULT_JPEG_QUALITY)
                data = buf.getvalue()
                mime = "image/jpeg"
        except Exception:
            pass
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def adapt(
    spec: AttachmentSpec,
    *,
    goal: str = "",
    options: dict[str, Any] | None = None,
) -> AdapterResult:
    path = Path(spec.path)
    if not path.exists():
        return AdapterResult(
            kind="image", ok=False, reasons=[f"attachment_not_found:{path}"]
        )

    options = options or {}
    max_bytes = int(options.get("max_image_bytes", _DEFAULT_MAX_BYTES))

    metadata: dict[str, Any] = {
        "filename": spec.filename or path.name,
        "mime": spec.mime,
        "size": spec.size or path.stat().st_size,
    }
    width = height = None
    try:
        from PIL import Image  # type: ignore

        with Image.open(path) as img:
            width, height = img.size
            metadata["mode"] = img.mode
    except Exception:
        pass
    if width is not None:
        metadata["width"] = int(width)
        metadata["height"] = int(height or 0)

    mime = _guess_mime(spec, path)
    try:
        data_url = _encode_data_url(path, mime, max_bytes)
    except Exception as exc:
        return AdapterResult(
            kind="image",
            ok=False,
            reasons=[f"image_encode_failed:{exc}"],
            metadata=metadata,
        )

    caption = (
        f"[image attachment: {metadata['filename']}, "
        f"{metadata.get('width', '?')}x{metadata.get('height', '?')}]"
    )
    return AdapterResult(
        kind="image",
        ok=True,
        text=caption,
        image_b64=[data_url],
        metadata=metadata,
        reasons=["multimodal_image_ready"],
    )
