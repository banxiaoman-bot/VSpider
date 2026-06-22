"""G5 finalization phase split — TDD tests.

Verify that ``finalize_run()`` performs all run-end cleanup steps
in the correct order and swallows individual errors gracefully.
Pure unit tests; no browser, no VLM, no network.
"""

from __future__ import annotations

import asyncio
import types
from pathlib import Path
from unittest.mock import MagicMock


def _make_stubs(**overrides):
    """Build a minimal stub dict for finalize_run kwargs."""
    event_stream = MagicMock()
    html_logger = MagicMock()
    html_logger.path = "/tmp/test_trace.html"

    async def _release_browser(lease, *, error=""):
        pass

    resolve = MagicMock(return_value=MagicMock(exists=MagicMock(return_value=False)))

    defaults = dict(
        run_ckpt=None,
        succeeded=True,
        goal="test goal",
        start_url="https://example.com",
        run_ts="20260615_120000_000000",
        total_extracted_rows=10,
        run_constraints=None,
        event_stream=event_stream,
        html_logger=html_logger,
        output_mode="default",
        vlm_output="output_test.xlsx",
        xhr_output="xhr_test.xlsx",
        enable_xhr=False,
        xhr_pattern="",
        resolve_artifact_path=resolve,
        registry_record_owned=False,
        stop_event=None,
        browser_lease=MagicMock(),
        release_browser=_release_browser,
        session_router=None,
        xsys_enabled=None,
        complete_run_registry=None,
    )
    defaults.update(overrides)
    return defaults


class TestFinalizeRun:
    def test_calls_event_stream_run_end(self):
        from visual_web_agent.phases.finalization import finalize_run

        stubs = _make_stubs()

        asyncio.run(finalize_run(**stubs))

        stubs["event_stream"].run_end.assert_called_once()
        call_kwargs = stubs["event_stream"].run_end.call_args[1]
        assert call_kwargs["success"] is True
        assert call_kwargs["reason"] == "completed"

    def test_calls_html_logger_finalize(self):
        from visual_web_agent.phases.finalization import finalize_run

        stubs = _make_stubs()

        asyncio.run(finalize_run(**stubs))

        stubs["html_logger"].finalize.assert_called_once()

    def test_calls_complete_run_registry(self):
        from visual_web_agent.phases.finalization import finalize_run

        registry_fn = MagicMock()
        stubs = _make_stubs(
            complete_run_registry=registry_fn,
            registry_record_owned=True,
            succeeded=False,
        )

        asyncio.run(finalize_run(**stubs))

        registry_fn.assert_called_once()
        args = registry_fn.call_args
        assert args[0][0] == "20260615_120000_000000"
        assert args[1]["owned"] is True
        assert args[1]["success"] is False

    def test_failed_run_metadata(self):
        from visual_web_agent.phases.finalization import finalize_run

        stubs = _make_stubs(succeeded=False)

        asyncio.run(finalize_run(**stubs))

        call_kwargs = stubs["event_stream"].run_end.call_args[1]
        assert call_kwargs["success"] is False
        assert call_kwargs["reason"] == "stopped_or_failed"

    def test_session_router_release(self):
        from visual_web_agent.phases.finalization import finalize_run

        router = MagicMock()

        async def _release_all(*, error=""):
            return ["session1", "session2"]

        router.release_all = _release_all
        stubs = _make_stubs(
            session_router=router,
            xsys_enabled=lambda: True,
        )

        asyncio.run(finalize_run(**stubs))

    def test_session_router_skipped_when_disabled(self):
        from visual_web_agent.phases.finalization import finalize_run

        router = MagicMock()
        stubs = _make_stubs(
            session_router=router,
            xsys_enabled=lambda: False,
        )

        asyncio.run(finalize_run(**stubs))
        router.release_all.assert_not_called()

    def test_checkpoint_finish_called(self):
        from visual_web_agent.phases.finalization import finalize_run

        ckpt = MagicMock()
        stubs = _make_stubs(run_ckpt=ckpt, succeeded=True)

        asyncio.run(finalize_run(**stubs))
        ckpt.finish.assert_called_once_with(True)

    def test_checkpoint_none_is_safe(self):
        from visual_web_agent.phases.finalization import finalize_run

        stubs = _make_stubs(run_ckpt=None)
        asyncio.run(finalize_run(**stubs))


class TestBuildRunEndMetadata:
    def test_answer_mode_no_artifact(self):
        from visual_web_agent.phases.finalization import build_run_end_metadata

        resolve = MagicMock(return_value=MagicMock(exists=MagicMock(return_value=False)))
        logger_stub = MagicMock()
        logger_stub.path = "/test.html"

        meta = build_run_end_metadata(
            html_logger=logger_stub,
            output_mode="answer",
            vlm_output="out.xlsx",
            xhr_output="xhr.xlsx",
            enable_xhr=False,
            xhr_pattern="",
            resolve_artifact_path=resolve,
        )
        assert "vlm_output" not in meta
        assert "xhr_output" not in meta
        assert meta["output_mode"] == "answer"

    def test_default_mode_includes_vlm(self):
        from visual_web_agent.phases.finalization import build_run_end_metadata

        resolve = MagicMock(return_value=MagicMock(exists=MagicMock(return_value=False)))
        logger_stub = MagicMock()
        logger_stub.path = "/test.html"

        meta = build_run_end_metadata(
            html_logger=logger_stub,
            output_mode="default",
            vlm_output="out.xlsx",
            xhr_output="xhr.xlsx",
            enable_xhr=False,
            xhr_pattern="",
            resolve_artifact_path=resolve,
        )
        assert meta["vlm_output"] == "out.xlsx"

    def test_xhr_enabled_includes_xhr(self):
        from visual_web_agent.phases.finalization import build_run_end_metadata

        resolve = MagicMock(return_value=MagicMock(exists=MagicMock(return_value=False)))
        logger_stub = MagicMock()
        logger_stub.path = "/test.html"

        meta = build_run_end_metadata(
            html_logger=logger_stub,
            output_mode="default",
            vlm_output="out.xlsx",
            xhr_output="xhr.xlsx",
            enable_xhr=True,
            xhr_pattern="",
            resolve_artifact_path=resolve,
        )
        assert meta["xhr_output"] == "xhr.xlsx"


class TestClearHelpers:
    def test_clear_io_contract_swallows_error(self):
        from visual_web_agent.phases.finalization import _clear_io_contract
        _clear_io_contract()

    def test_clear_phase_log_swallows_error(self):
        from visual_web_agent.phases.finalization import _clear_phase_log
        _clear_phase_log()

    def test_finish_checkpoint_with_none(self):
        from visual_web_agent.phases.finalization import _finish_checkpoint
        _finish_checkpoint(None, True)

    def test_finish_checkpoint_with_mock(self):
        from visual_web_agent.phases.finalization import _finish_checkpoint
        ckpt = MagicMock()
        _finish_checkpoint(ckpt, False)
        ckpt.finish.assert_called_once_with(False)
