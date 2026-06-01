"""Regression tests for OUT-2.

``ActionResult`` must carry ``output_kind`` / ``output_mime`` /
``output_size`` / ``output_sha256`` / ``output_container`` so the agent
loop can decide what to do with an artifact without re-sniffing the
filesystem after every action.
"""

from __future__ import annotations

from visual_web_agent.action_result import ActionResult


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
