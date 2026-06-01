from __future__ import annotations

import shutil
from pathlib import Path
from urllib.parse import quote


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def artifact_root() -> Path:
    root = project_root() / "workspace" / "artifacts"
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve_artifact_path(filename: str | Path, subdir: str = "") -> Path:
    raw = Path(filename)
    root = artifact_root()
    target_dir = root / subdir if subdir else root
    target_dir.mkdir(parents=True, exist_ok=True)

    if raw.is_absolute():
        return raw
    safe_parts = [part for part in raw.parts if part not in ("", ".", "..")]
    if not safe_parts:
        safe_parts = ["artifact.bin"]
    return target_dir.joinpath(*safe_parts)


def artifact_url(path: str | Path) -> str:
    resolved = Path(path).resolve()
    try:
        rel = resolved.relative_to(artifact_root().resolve())
        return "/download/" + quote(rel.as_posix())
    except Exception:
        pass
    run_hit = is_inside_any_run_artifacts(resolved)
    if run_hit is not None:
        run_id, _resolved = run_hit
        rel = _resolved.relative_to(_runs_root().resolve() / run_id / "artifacts")
        return f"/download/runs/{quote(run_id)}/artifacts/{quote(rel.as_posix())}"
    return "/download/" + quote(resolved.name)


def is_inside_artifacts(path: str | Path) -> bool:
    try:
        Path(path).resolve().relative_to(artifact_root().resolve())
        return True
    except Exception:
        return False


def _runs_root() -> Path:
    try:
        from visual_web_agent.io_contract.persistence import default_runs_root

        return default_runs_root()
    except Exception:
        return project_root() / "runs"


def run_artifacts_root(run_id: str, *, base_dir: str | Path | None = None) -> Path:
    from visual_web_agent.data_writers._base import run_artifacts_dir
    from visual_web_agent.io_contract import current_base_dir

    base = base_dir if base_dir is not None else current_base_dir()
    return run_artifacts_dir(str(run_id or "").strip(), base_dir=base)


def is_inside_run_artifacts(
    path: str | Path,
    run_id: str,
    *,
    base_dir: str | Path | None = None,
) -> bool:
    rid = str(run_id or "").strip()
    if not rid:
        return False
    try:
        Path(path).resolve().relative_to(run_artifacts_root(rid, base_dir=base_dir).resolve())
        return True
    except Exception:
        return False


def is_inside_any_run_artifacts(path: str | Path) -> tuple[str, Path] | None:
    resolved = Path(path).resolve()
    runs_root = _runs_root().resolve()
    try:
        rel = resolved.relative_to(runs_root)
    except Exception:
        return None
    parts = rel.parts
    if len(parts) >= 3 and parts[1] == "artifacts":
        return parts[0], resolved
    return None


def copy_into_run_artifacts(
    path: str | Path,
    run_id: str,
    *,
    base_dir: str | Path | None = None,
    subdir: str = "",
) -> Path:
    source = Path(path).resolve()
    target_root = run_artifacts_root(run_id, base_dir=base_dir)
    if subdir:
        target_root = target_root / subdir
        target_root.mkdir(parents=True, exist_ok=True)
    target = target_root / source.name
    if source != target.resolve():
        shutil.copy2(source, target)
    return target


def resolve_output_path(filename: str | Path) -> Path:
    """Prefer ``runs/<run_id>/artifacts/`` when a run context is active."""

    try:
        from visual_web_agent.io_contract import current_base_dir, current_run_id

        rid = current_run_id()
        if rid:
            target = run_artifacts_root(rid, base_dir=current_base_dir()) / Path(filename).name
            target.parent.mkdir(parents=True, exist_ok=True)
            return target
    except Exception:
        pass
    return resolve_artifact_path(filename)


def copy_into_artifacts(path: str | Path, subdir: str = "") -> Path:
    source = Path(path).resolve()
    target = resolve_artifact_path(source.name, subdir=subdir)
    if source != target.resolve():
        shutil.copy2(source, target)
    return target


def register_artifact(
    path: str | Path,
    *,
    run_id: str = "",
    kind: str = "other",
    mime: str = "",
    sha256: str = "",
    size: int = 0,
    source_url: str = "",
    produced_by: str = "",
    step_id: str = "",
) -> None:
    """Broadcast a freshly produced artifact + (optionally) record it in the
    per-run ``manifest.json``.

    Pre-OUT-1 callers pass only ``path`` and we keep the original behaviour
    (copy into ``workspace/artifacts/`` + broadcast to UI + record name for
    final-answer aggregation). New OUT-3 call sites pass ``run_id`` and the
    item metadata; when ``run_id`` is set we additionally land the artifact
    inside the run's ``manifest.json`` so the I/O contract closes cleanly
    even for paths that don't go through ``save_artifact()``.
    """
    rid = str(run_id or "").strip()
    base_dir = None
    if not rid:
        try:
            from visual_web_agent.io_contract import current_run_id

            rid = current_run_id()
        except Exception:
            rid = ""
    try:
        from visual_web_agent.io_contract import current_base_dir

        base_dir = current_base_dir()
    except Exception:
        base_dir = None

    try:
        resolved = Path(path).resolve()
        if rid:
            if not is_inside_run_artifacts(resolved, rid, base_dir=base_dir):
                resolved = copy_into_run_artifacts(resolved, rid, base_dir=base_dir)
        elif not is_inside_artifacts(resolved):
            resolved = copy_into_artifacts(resolved)
        from api_server import broadcast_new_artifact

        broadcast_new_artifact(resolved)
        try:
            from visual_web_agent.main import _record_new_artifact
            _record_new_artifact(resolved.name)
        except Exception:
            pass
        if rid:
            try:
                import hashlib

                from visual_web_agent.io_contract import append_manifest_item

                body_size = int(size or 0) or resolved.stat().st_size
                body_sha = sha256
                if not body_sha and resolved.exists():
                    body_sha = hashlib.sha256(resolved.read_bytes()).hexdigest()
                append_manifest_item(
                    rid,
                    kind=kind or "other",
                    path=str(resolved),
                    size=body_size,
                    sha256=body_sha,
                    mime=mime,
                    source_url=source_url,
                    produced_by=produced_by or "register_artifact",
                    step_id=step_id,
                    base_dir=base_dir,
                )
            except Exception:
                pass
    except Exception:
        pass

