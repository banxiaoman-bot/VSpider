"""Regression tests for OUT-2.

``ActionResult`` must carry ``output_kind`` / ``output_mime`` /
``output_size`` / ``output_sha256`` / ``output_container`` so the agent
loop can decide what to do with an artifact without re-sniffing the
filesystem after every action.
"""

from __future__ import annotations

from visual_web_agent.action_result import ActionResult, action_result_evidence_parts


class TestOutputKindFields:
    def test_defaults_are_empty_or_zero(self) -> None:
        ar = ActionResult()
        assert ar.output_kind == ""
        assert ar.output_mime == ""
        assert ar.output_size == 0
        assert ar.output_sha256 == ""
        assert ar.output_container == ""

    def test_from_action_extracts_metadata_keys(self) -> None:
        ar = ActionResult.from_action(
            {"action": "extract", "target_id": 0},
            metadata={
                "output_file": "/tmp/run/artifacts/foo.csv",
                "output_kind": "dataset_rows",
                "output_mime": "text/csv",
                "output_size": 4096,
                "output_sha256": "abc123",
                "output_container": "csv",
                "extracted_rows": 12,
            },
        )
        assert ar.action == "extract"
        assert ar.output_file == "/tmp/run/artifacts/foo.csv"
        assert ar.output_kind == "dataset_rows"
        assert ar.output_mime == "text/csv"
        assert ar.output_size == 4096
        assert ar.output_sha256 == "abc123"
        assert ar.output_container == "csv"
        assert ar.extracted_rows == 12
        # metadata keys we popped should not still be in metadata
        for key in (
            "output_file", "output_kind", "output_mime",
            "output_size", "output_sha256", "output_container",
            "extracted_rows",
        ):
            assert key not in ar.metadata

    def test_legacy_callers_without_new_fields_still_work(self) -> None:
        ar = ActionResult.from_action(
            {"action": "click", "target_id": 7},
            success=True,
            metadata={"clicked_text": "Submit"},
        )
        assert ar.output_kind == ""
        assert ar.output_size == 0
        assert ar.clicked_text == "Submit"

    def test_to_dict_includes_new_fields(self) -> None:
        ar = ActionResult(
            action="extract",
            output_kind="media_image",
            output_container="files_folder",
            output_mime="image/png",
            output_size=2048,
            output_sha256="def456",
        )
        payload = ar.to_dict()
        assert payload["output_kind"] == "media_image"
        assert payload["output_container"] == "files_folder"
        assert payload["output_mime"] == "image/png"
        assert payload["output_size"] == 2048
        assert payload["output_sha256"] == "def456"


class TestHistoryEvidenceSummary:
    def test_summarizes_artifact_output_for_vlm_history(self) -> None:
        ar = ActionResult(
            action="extract",
            extracted_rows=12,
            output_file="runs/run_1/artifacts/items.jsonl",
            output_kind="dataset_records",
            output_container="jsonl",
            output_size=4096,
        )

        parts = action_result_evidence_parts(ar)

        assert "rows=12" in parts
        assert "artifact path=runs/run_1/artifacts/items.jsonl" in parts
        assert "artifact kind=dataset_records" in parts
        assert "container=jsonl" in parts
        assert "size=4096" in parts

    def test_summarizes_download_completion_for_vlm_history(self) -> None:
        parts = action_result_evidence_parts(
            {"action": "click", "success": True},
            download_path=r"D:\pythontest\VSpider\runs\run_1\artifacts\sampleFile.jpeg",
            download_name="sampleFile.jpeg",
        )

        assert any(part.startswith("download_completed path=") for part in parts)
        assert any("name=sampleFile.jpeg" in part for part in parts)

    def test_uses_metadata_artifact_path_when_output_file_absent(self) -> None:
        parts = action_result_evidence_parts(
            {
                "action": "download_image",
                "success": True,
                "metadata": {"artifact_path": "runs/run_1/artifacts/photo.jpg"},
            }
        )

        assert "artifact path=runs/run_1/artifacts/photo.jpg" in parts
