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
    root = artifact_root().resolve()
    resolved = Path(path).resolve()
    rel = resolved.relative_to(root)
    return "/download/" + quote(rel.as_posix())


def is_inside_artifacts(path: str | Path) -> bool:
    try:
        Path(path).resolve().relative_to(artifact_root().resolve())
        return True
    except Exception:
        return False


def copy_into_artifacts(path: str | Path, subdir: str = "") -> Path:
    source = Path(path).resolve()
    target = resolve_artifact_path(source.name, subdir=subdir)
    if source != target.resolve():
        shutil.copy2(source, target)
    return target


def register_artifact(path: str | Path) -> None:
    try:
        resolved = Path(path).resolve()
        if not is_inside_artifacts(resolved):
            resolved = copy_into_artifacts(resolved)
        from api_server import broadcast_new_artifact

        broadcast_new_artifact(resolved)
    except Exception:
        pass

