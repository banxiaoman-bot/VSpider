"""Tests for ``visual_web_agent.data_writers`` packaging / zip bundling.

Drives the delivery policy the user asked for:

- ``container == 'zip'`` lands a real ``.zip`` (was previously unregistered
  and raised ``unknown container``).
- run-level packaging bundles downloadable files into one ``bundle.zip``
  when the count strictly exceeds the threshold (default 5); at or below
  the threshold the files stay individually downloadable.
- ``decide_delivery`` recommends inline / files_folder / zip / mixed so the
  answer-only, file, and mixed tasks each get the right shape.

All IO is sandboxed under ``tmp_path`` via ``base_dir=...``.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest


pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _read_manifest(tmp_path: Path, run_id: str) -> dict:
    import json

    from visual_web_agent.io_contract import MANIFEST_FILENAME

    p = tmp_path / run_id / MANIFEST_FILENAME
    assert p.exists(), f"manifest must exist at {p}"
    return json.loads(p.read_text(encoding="utf-8"))


def _image_blobs(n: int) -> list[dict]:
    return [
        {
            "filename": f"photo_{i}.jpg",
            "bytes": f"image-bytes-{i}".encode("utf-8"),
            "mime": "image/jpeg",
            "kind": "media_image",
            "source_url": f"https://x/img/{i}.jpg",
        }
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# zip writer is registered + lands a real archive
# ---------------------------------------------------------------------------

class TestZipWriterRegistered:
    def test_zip_container_is_registered(self) -> None:
        import visual_web_agent.data_writers as dw

        assert "zip" in dw.CONTAINER_TO_WRITER, "zip container must have a writer"

    def test_container_zip_lands_real_archive_and_manifest(self, tmp_path: Path) -> None:
        from visual_web_agent.data_writers import save_artifact

        item = save_artifact(
            _image_blobs(3),
            run_id="run_zip",
            container="zip",
            output_kind="media_archive",
            produced_by="media_harvester",
            base_dir=tmp_path,
        )
        path = Path(item["path"])
        assert path.suffix == ".zip"
        body = path.read_bytes()
        assert body[:2] == b"PK", "must be a real zip (PK magic)"

        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            names = zf.namelist()
        assert len(names) == 3
        assert all(n.endswith(".jpg") for n in names)

        manifest = _read_manifest(tmp_path, "run_zip")
        target = next(it for it in manifest["items"] if it["kind"] == "media_archive")
        assert target["mime"] == "application/zip"
        assert target["extra"]["member_count"] == 3

    def test_zip_is_deterministic(self, tmp_path: Path) -> None:
        from visual_web_agent.data_writers.zip_writer import write_zip

        a = write_zip(_image_blobs(2), run_id="run_det_a", filename_hint="bundle", base_dir=tmp_path)
        b = write_zip(_image_blobs(2), run_id="run_det_b", filename_hint="bundle", base_dir=tmp_path)
        assert a["sha256"] == b["sha256"], "identical inputs must yield identical zip bytes"

    def test_bundle_filename_hint_not_doubled(self, tmp_path: Path) -> None:
        from visual_web_agent.data_writers.zip_writer import write_zip

        item = write_zip(_image_blobs(1), run_id="run_name", filename_hint="bundle", base_dir=tmp_path)
        assert Path(item["path"]).name == "bundle.zip"


# ---------------------------------------------------------------------------
# threshold policy
# ---------------------------------------------------------------------------

class TestThresholdPolicy:
    def test_should_bundle_strictly_above_threshold(self) -> None:
        from visual_web_agent.data_writers.packaging import should_bundle

        assert should_bundle(6, threshold=5) is True
        assert should_bundle(5, threshold=5) is False
        assert should_bundle(0, threshold=5) is False

    def test_default_threshold_is_five(self) -> None:
        from visual_web_agent.data_writers.packaging import FILE_BUNDLE_THRESHOLD

        assert FILE_BUNDLE_THRESHOLD == 5


# ---------------------------------------------------------------------------
# decide_delivery: the "灵活分配" recommendation
# ---------------------------------------------------------------------------

class TestDecideDelivery:
    def test_inline_only_answer(self) -> None:
        from visual_web_agent.data_writers.packaging import decide_delivery

        items = [{"kind": "answer_text", "path": "", "inline": True, "extra": {"inline_text": "42"}}]
        plan = decide_delivery(items)
        assert plan["recommended"] == "inline"
        assert plan["inline_answers"] == ["42"]
        assert plan["file_count"] == 0
        assert plan["bundle"] is False

    def test_few_files_stay_folder(self) -> None:
        from visual_web_agent.data_writers.packaging import decide_delivery

        items = [{"kind": "media_image", "path": f"runs/r/artifacts/p{i}.jpg"} for i in range(3)]
        plan = decide_delivery(items)
        assert plan["recommended"] == "files_folder"
        assert plan["file_count"] == 3
        assert plan["bundle"] is False

    def test_many_files_recommend_zip(self) -> None:
        from visual_web_agent.data_writers.packaging import decide_delivery

        items = [{"kind": "media_image", "path": f"runs/r/artifacts/p{i}.jpg"} for i in range(6)]
        plan = decide_delivery(items)
        assert plan["recommended"] == "zip"
        assert plan["bundle"] is True

    def test_mixed_answer_plus_files(self) -> None:
        from visual_web_agent.data_writers.packaging import decide_delivery

        items = [
            {"kind": "answer_text", "path": "", "inline": True, "extra": {"inline_text": "done"}},
            {"kind": "file_generic", "path": "runs/r/artifacts/a.bin"},
        ]
        plan = decide_delivery(items)
        assert plan["recommended"] == "mixed"
        assert plan["inline_answers"] == ["done"]
        assert plan["file_count"] == 1

    def test_bundle_item_excluded_from_count(self) -> None:
        from visual_web_agent.data_writers.packaging import collect_downloadable_files

        items = [
            {"kind": "media_image", "path": "runs/r/artifacts/p0.jpg"},
            {"kind": "media_archive", "path": "runs/r/artifacts/bundle.zip", "extra": {"bundle": True}},
        ]
        files = collect_downloadable_files(items)
        assert len(files) == 1
        assert files[0]["path"].endswith("p0.jpg")


# ---------------------------------------------------------------------------
# package_run_artifacts: auto-bundle at completion (>5)
# ---------------------------------------------------------------------------

class TestPackageRunArtifacts:
    def _land_files(self, tmp_path: Path, run_id: str, n: int) -> None:
        from visual_web_agent.data_writers import save_artifact

        save_artifact(
            _image_blobs(n),
            run_id=run_id,
            container="files_folder",
            output_kind="media_image",
            base_dir=tmp_path,
        )

    def test_below_threshold_no_bundle(self, tmp_path: Path) -> None:
        from visual_web_agent.data_writers.packaging import package_run_artifacts

        self._land_files(tmp_path, "run_few", 3)
        result = package_run_artifacts("run_few", threshold=5, base_dir=tmp_path)
        assert result["bundled"] is False
        assert result["reason"] == "below_threshold"
        assert not (tmp_path / "run_few" / "artifacts" / "bundle.zip").exists()

    def test_above_threshold_creates_bundle(self, tmp_path: Path) -> None:
        from visual_web_agent.data_writers.packaging import package_run_artifacts

        self._land_files(tmp_path, "run_many", 6)
        result = package_run_artifacts("run_many", threshold=5, base_dir=tmp_path)
        assert result["bundled"] is True
        assert result["member_count"] == 6
        bundle = tmp_path / "run_many" / "artifacts" / "bundle.zip"
        assert bundle.exists()

        manifest = _read_manifest(tmp_path, "run_many")
        bundle_items = [
            it for it in manifest["items"]
            if it.get("extra", {}).get("bundle") is True
        ]
        assert len(bundle_items) == 1
        assert bundle_items[0]["kind"] == "media_archive"

    def test_idempotent_second_call(self, tmp_path: Path) -> None:
        from visual_web_agent.data_writers.packaging import package_run_artifacts

        self._land_files(tmp_path, "run_idem", 6)
        first = package_run_artifacts("run_idem", threshold=5, base_dir=tmp_path)
        assert first["bundled"] is True
        second = package_run_artifacts("run_idem", threshold=5, base_dir=tmp_path)
        assert second["bundled"] is False
        assert second["reason"] == "already_bundled"


# ---------------------------------------------------------------------------
# build_run_bundle: on-demand "download everything" (any count >= 1)
# ---------------------------------------------------------------------------

class TestBuildRunBundle:
    def test_no_files_returns_no_files(self, tmp_path: Path) -> None:
        from visual_web_agent.data_writers.packaging import build_run_bundle

        from visual_web_agent.io_contract.persistence import run_dir

        run_dir("run_empty", base_dir=tmp_path)  # create skeleton, no artifacts
        result = build_run_bundle("run_empty", base_dir=tmp_path)
        assert result["bundled"] is False
        assert result["reason"] == "no_files"

    def test_bundles_even_below_threshold_on_demand(self, tmp_path: Path) -> None:
        from visual_web_agent.data_writers import save_artifact
        from visual_web_agent.data_writers.packaging import build_run_bundle

        save_artifact(
            _image_blobs(2),
            run_id="run_ondemand",
            container="files_folder",
            output_kind="media_image",
            base_dir=tmp_path,
        )
        result = build_run_bundle("run_ondemand", base_dir=tmp_path)
        assert result["bundled"] is True
        assert (tmp_path / "run_ondemand" / "artifacts" / "bundle.zip").exists()
