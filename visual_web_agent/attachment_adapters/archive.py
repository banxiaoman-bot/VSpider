"""Archive (zip / 7z / rar) attachment adapter (stub).

We deliberately do NOT auto-extract archives because:

- They may contain credentials / very large files
- Extraction policy is task-specific (which entries to keep, OCR them?)

The adapter returns a listing of contained entries so the agent / planner
can decide what to do next.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from visual_web_agent.io_contract.input_contract import AttachmentSpec

from .base import AdapterResult


def adapt(
    spec: AttachmentSpec,
    *,
    goal: str = "",
    options: dict[str, Any] | None = None,
) -> AdapterResult:
    path = Path(spec.path)
    if not path.exists():
        return AdapterResult(
            kind="archive", ok=False, reasons=[f"attachment_not_found:{path}"]
        )

    suffix = path.suffix.lower()
    members: list[str] = []
    try:
        if suffix in {".zip"}:
            import zipfile

            with zipfile.ZipFile(path) as zf:
                members = [info.filename for info in zf.infolist()][:200]
        elif suffix in {".tar", ".gz", ".tgz", ".bz2"}:
            import tarfile

            try:
                with tarfile.open(path) as tf:
                    members = tf.getnames()[:200]
            except Exception:
                members = []
        else:
            members = []
    except Exception as exc:
        return AdapterResult(
            kind="archive",
            ok=True,
            text=f"[archive attachment: {spec.filename or path.name}, listing failed: {exc}]",
            metadata={"filename": spec.filename or path.name, "size": spec.size or path.stat().st_size},
            reasons=["archive_listing_failed"],
        )

    caption = (
        f"[archive attachment: {spec.filename or path.name}, "
        f"{len(members)} entries listed]"
    )
    return AdapterResult(
        kind="archive",
        ok=True,
        text=caption,
        metadata={
            "filename": spec.filename or path.name,
            "size": spec.size or path.stat().st_size,
            "members_preview": members[:50],
            "auto_extract": False,
        },
    )
