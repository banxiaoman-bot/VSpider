"""Finalization phase — extracted from ``main.py`` (slice G5).

Houses ``finalize_run()`` which performs all run-end cleanup: io_contract
clear, checkpoint finish, resume recording, event_stream.run_end,
html_logger.finalize, browser release, session router release.

Behavior is a straight lift-and-delegate from run_agent finally block;
no logic changes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("visual_web_agent.phases.finalization")


@dataclass
class RunEndMetadata:
    """Metadata emitted with the run_end event."""
    html_log: str = ""
    output_mode: str = "default"
    vlm_output: str | None = None
    xhr_output: str | None = None


def _clear_io_contract() -> None:
    """Clear the global current-run marker. Swallows all errors."""
    try:
        try:
            from ..io_contract import clear_current_run
        except ImportError:
            from io_contract import clear_current_run  # type: ignore[no-redef]
        clear_current_run()
    except Exception:
        pass


def _clear_phase_log() -> None:
    """Reset the broadcast phase log run_id. Swallows all errors."""
    try:
        from api_server import set_phase_log_run_id
        set_phase_log_run_id(None)
    except Exception:
        pass


def _finish_checkpoint(run_ckpt: Any | None, succeeded: bool) -> None:
    """Finalize the run checkpoint (inert unless resume). Swallows errors."""
    try:
        if run_ckpt is not None:
            run_ckpt.finish(succeeded)
    except Exception:
        pass


def _record_resume(
    *,
    goal: str,
    start_url: str,
    run_ts: str,
    total_extracted_rows: int,
    succeeded: bool,
    run_constraints: dict | None,
) -> None:
    """Write resume-index entry (inert unless constraints.resume)."""
    if not bool((run_constraints or {}).get("resume")):
        return
    try:
        try:
            from ..resume_seed import record_run_for_resume
        except ImportError:
            from resume_seed import record_run_for_resume  # type: ignore[no-redef]
        record_run_for_resume(
            goal,
            start_url,
            run_ts,
            item_count=total_extracted_rows,
            status="completed" if succeeded else "failed",
        )
    except Exception:
        pass


def build_run_end_metadata(
    *,
    html_logger: Any,
    output_mode: str,
    vlm_output: str,
    xhr_output: str,
    enable_xhr: bool,
    xhr_pattern: str,
    resolve_artifact_path: Any,
) -> dict:
    """Assemble the metadata dict for event_stream.run_end().

    Lifted from run_agent lines 18303-18318.
    """
    metadata: dict = {
        "html_log": str(getattr(html_logger, "path", "") or ""),
        "output_mode": output_mode,
    }
    try:
        vlm_exists = resolve_artifact_path(vlm_output).exists()
    except Exception:
        vlm_exists = False
    try:
        xhr_exists = resolve_artifact_path(xhr_output).exists()
    except Exception:
        xhr_exists = False

    if output_mode != "answer" or vlm_exists:
        metadata["vlm_output"] = vlm_output
    if enable_xhr or xhr_pattern or xhr_exists:
        metadata["xhr_output"] = xhr_output

    return metadata


async def finalize_run(
    *,
    run_ckpt: Any | None,
    succeeded: bool,
    goal: str,
    start_url: str,
    run_ts: str,
    total_extracted_rows: int,
    run_constraints: dict | None,
    event_stream: Any,
    html_logger: Any,
    output_mode: str,
    vlm_output: str,
    xhr_output: str,
    enable_xhr: bool,
    xhr_pattern: str,
    resolve_artifact_path: Any,
    registry_record_owned: bool,
    stop_event: Any | None,
    browser_lease: Any,
    release_browser: Any,
    session_router: Any | None = None,
    xsys_enabled: Any | None = None,
    complete_run_registry: Any | None = None,
) -> None:
    """Run-end cleanup. Called from the finally block of run_agent.

    Lifted from run_agent lines 18262-18352. Every sub-step swallows
    its own exceptions so one failure doesn't prevent subsequent cleanup.
    """
    _clear_io_contract()
    _clear_phase_log()
    _finish_checkpoint(run_ckpt, succeeded)

    _record_resume(
        goal=goal,
        start_url=start_url,
        run_ts=run_ts,
        total_extracted_rows=total_extracted_rows,
        succeeded=succeeded,
        run_constraints=run_constraints,
    )

    metadata = build_run_end_metadata(
        html_logger=html_logger,
        output_mode=output_mode,
        vlm_output=vlm_output,
        xhr_output=xhr_output,
        enable_xhr=enable_xhr,
        xhr_pattern=xhr_pattern,
        resolve_artifact_path=resolve_artifact_path,
    )
    event_stream.run_end(
        success=succeeded,
        reason="completed" if succeeded else "stopped_or_failed",
        metadata=metadata,
    )

    if complete_run_registry is not None:
        complete_run_registry(
            run_ts,
            owned=registry_record_owned,
            success=succeeded,
            stopped=bool(stop_event and stop_event.is_set()),
        )

    html_logger.finalize()

    await release_browser(
        browser_lease,
        error="" if succeeded else "stopped_or_failed",
    )

    if session_router is not None and xsys_enabled is not None:
        try:
            if xsys_enabled():
                released = await session_router.release_all(
                    error="" if succeeded else "stopped_or_failed",
                )
                if released:
                    logger.info(
                        "[SESSION ROUTER] released %d pooled session(s)",
                        len(released),
                    )
        except Exception as err:
            logger.debug("[SESSION ROUTER] release_all skipped: %s", err)
