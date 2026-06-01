"""Atomic disk persistence for ``input_contract`` / ``output_contract`` / ``manifest``.

Self-contained: importing this module does not touch any other VSpider runtime
state. Callers can persist contracts under ``runs/<run_id>/`` or any custom
``base_dir`` (which makes unit tests trivial with ``tmp_path``).

Layout (under ``base_dir/<run_id>/``):

::

    input_contract.json
    output_contract.json
    manifest.json
    artifacts/

All writes use a temp-file + ``os.replace`` to avoid partial files on crash.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable

from .input_contract import InputContract, build_input_contract
from .output_contract import OutputContract, OutputPrediction
from .manifest import Manifest, ManifestItem, append_item, new_manifest


_RUN_ID_RE = re.compile(r"^[0-9A-Za-z_.-]+$")


INPUT_CONTRACT_FILENAME = "input_contract.json"
OUTPUT_CONTRACT_FILENAME = "output_contract.json"
MANIFEST_FILENAME = "manifest.json"
ARTIFACTS_DIRNAME = "artifacts"


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_runs_root() -> Path:
    """Return ``<project>/runs``. Callers can override via ``base_dir=``."""

    return _project_root() / "runs"


def _safe_run_id(run_id: str) -> str:
    rid = str(run_id or "").strip()
    if not rid or not _RUN_ID_RE.fullmatch(rid):
        raise ValueError(f"invalid run_id: {run_id!r}")
    return rid


def run_dir(run_id: str, *, base_dir: str | Path | None = None) -> Path:
    rid = _safe_run_id(run_id)
    root = Path(base_dir) if base_dir is not None else default_runs_root()
    target = root / rid
    target.mkdir(parents=True, exist_ok=True)
    (target / ARTIFACTS_DIRNAME).mkdir(parents=True, exist_ok=True)
    return target


def _atomic_write_json(target: Path, payload: dict[str, Any]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=target.stem + ".",
        suffix=".tmp",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp_name, target)
    except Exception:
        try:
            Path(tmp_name).unlink(missing_ok=True)
        except Exception:
            pass
        raise


def _read_json(target: Path) -> dict[str, Any] | None:
    if not target.exists() or not target.is_file():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# Input contract
# ---------------------------------------------------------------------------


def write_input_contract(
    run_id: str,
    contract: InputContract | dict[str, Any],
    *,
    base_dir: str | Path | None = None,
) -> Path:
    payload = contract.to_dict() if isinstance(contract, InputContract) else dict(contract or {})
    target = run_dir(run_id, base_dir=base_dir) / INPUT_CONTRACT_FILENAME
    _atomic_write_json(target, payload)
    return target


def read_input_contract(
    run_id: str,
    *,
    base_dir: str | Path | None = None,
) -> dict[str, Any] | None:
    return _read_json(run_dir(run_id, base_dir=base_dir) / INPUT_CONTRACT_FILENAME)


def ensure_input_contract_skeleton(
    run_id: str,
    *,
    goal: str,
    target_url: str = "",
    urls: Any = None,
    file_path: str = "",
    auth_profiles: str | list[str] = "",
    vlm_options: dict[str, Any] | None = None,
    constraints: dict[str, Any] | None = None,
    source: str = "cli",
    base_dir: str | Path | None = None,
) -> Path:
    """Persist ``input_contract.json`` built from ``goal`` IF ABSENT.

    Mirrors :func:`ensure_contract_skeleton` for the input side so every run --
    including ad-hoc CLI / replay paths that skip the smart_batch dispatch
    layer -- drops a real ``input_contract.json``. An already present file is
    left untouched so the richer contract written by
    ``smart_batch_runner._persist_io_contracts_safe`` (full urls / attachments /
    auth) always wins.
    """

    target = run_dir(run_id, base_dir=base_dir) / INPUT_CONTRACT_FILENAME
    if target.exists():
        return target

    attachments: list[dict[str, Any]] = []
    if file_path:
        fp = Path(file_path)
        attachments.append({
            "path": str(fp),
            "filename": fp.name,
            "size": fp.stat().st_size if fp.exists() else 0,
        })

    contract = build_input_contract(
        goal=goal or "",
        target_url=target_url or "",
        urls=urls or [],
        attachments=attachments,
        auth_profiles=auth_profiles or "",
        vlm_options=vlm_options or {},
        constraints=constraints or None,
        source=source,
    )
    _atomic_write_json(target, contract.to_dict())
    return target


# ---------------------------------------------------------------------------
# Output contract
# ---------------------------------------------------------------------------


def write_output_contract(
    run_id: str,
    contract: OutputContract | dict[str, Any],
    *,
    base_dir: str | Path | None = None,
) -> Path:
    payload = contract.to_dict() if isinstance(contract, OutputContract) else dict(contract or {})
    target = run_dir(run_id, base_dir=base_dir) / OUTPUT_CONTRACT_FILENAME
    _atomic_write_json(target, payload)
    return target


def write_output_prediction(
    run_id: str,
    prediction: OutputPrediction | dict[str, Any],
    *,
    base_dir: str | Path | None = None,
) -> Path:
    payload = prediction.to_dict() if isinstance(prediction, OutputPrediction) else dict(prediction or {})
    target = run_dir(run_id, base_dir=base_dir) / "output_prediction.json"
    _atomic_write_json(target, payload)
    return target


def read_output_contract(
    run_id: str,
    *,
    base_dir: str | Path | None = None,
) -> dict[str, Any] | None:
    return _read_json(run_dir(run_id, base_dir=base_dir) / OUTPUT_CONTRACT_FILENAME)


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def read_manifest(
    run_id: str,
    *,
    base_dir: str | Path | None = None,
) -> Manifest:
    """Read existing manifest or return a fresh one for the run."""

    target = run_dir(run_id, base_dir=base_dir) / MANIFEST_FILENAME
    raw = _read_json(target)
    if not raw:
        return new_manifest(_safe_run_id(run_id))
    return Manifest.from_dict(raw)


def write_manifest(
    run_id: str,
    manifest: Manifest,
    *,
    base_dir: str | Path | None = None,
) -> Path:
    target = run_dir(run_id, base_dir=base_dir) / MANIFEST_FILENAME
    _atomic_write_json(target, manifest.to_dict())
    return target


def append_manifest_item(
    run_id: str,
    *,
    kind: str,
    path: str,
    size: int = 0,
    sha256: str = "",
    mime: str = "",
    source_url: str | Iterable[str] = "",
    produced_by: str = "",
    step_id: str = "",
    extra: dict[str, Any] | None = None,
    base_dir: str | Path | None = None,
) -> ManifestItem:
    """Read-modify-write helper. Use sparingly in hot paths; for batch
    appends, prefer :func:`read_manifest` + multiple :func:`append_item`
    + a single :func:`write_manifest`.
    """

    manifest = read_manifest(run_id, base_dir=base_dir)
    item = append_item(
        manifest,
        kind=kind,
        path=path,
        size=size,
        sha256=sha256,
        mime=mime,
        source_url=source_url,
        produced_by=produced_by,
        step_id=step_id,
        extra=extra,
    )
    write_manifest(run_id, manifest, base_dir=base_dir)
    return item


def record_verification_evidence(
    run_id: str,
    *,
    verification: dict[str, Any],
    path: str = "",
    produced_by: str = "verification",
    step_id: str = "",
    base_dir: str | Path | None = None,
) -> ManifestItem:
    """Persist verification as a manifest evidence item."""

    summary = dict(verification or {})
    manifest = read_manifest(run_id, base_dir=base_dir)
    item = append_item(
        manifest,
        kind="log",
        path=path or f"runs/{run_id}/manifest.json#verification",
        produced_by=produced_by,
        step_id=step_id,
        extra={"verification": summary, "verification_summary": summary.get("verification_summary") or {}},
    )
    write_manifest(run_id, manifest, base_dir=base_dir)
    return item


def record_template_experience(
    run_id: str,
    *,
    template: dict[str, Any],
    verification: dict[str, Any],
    path: str = "",
    produced_by: str = "template_experience",
    step_id: str = "",
    base_dir: str | Path | None = None,
) -> ManifestItem:
    """Persist template usage/outcome as manifest evidence."""

    template_data = dict(template or {})
    verification_data = dict(verification or {})
    manifest = read_manifest(run_id, base_dir=base_dir)
    item = append_item(
        manifest,
        kind="log",
        path=path or f"runs/{run_id}/manifest.json#template_experience",
        produced_by=produced_by,
        step_id=step_id,
        extra={
            "template": template_data,
            "verification": verification_data,
            "template_id": str(template_data.get("id") or ""),
            "template_type": str(template_data.get("task_type") or ""),
            "verification_summary": verification_data.get("verification_summary") or {},
        },
    )
    write_manifest(run_id, manifest, base_dir=base_dir)
    return item


def ensure_contract_skeleton(
    run_id: str,
    *,
    base_dir: str | Path | None = None,
) -> tuple[Path, Path]:
    """Create empty ``output_contract.json`` and ``manifest.json`` if absent.

    This gives every run a stable place to record output intent and produced
    artifacts even before the full writer logic is wired in.
    """

    run_path = run_dir(run_id, base_dir=base_dir)
    output_path = run_path / OUTPUT_CONTRACT_FILENAME
    manifest_path = run_path / MANIFEST_FILENAME

    if not output_path.exists():
        default_output = OutputContract()
        _atomic_write_json(output_path, default_output.to_dict())
    if not manifest_path.exists():
        _atomic_write_json(manifest_path, new_manifest(_safe_run_id(run_id)).to_dict())

    return output_path, manifest_path
