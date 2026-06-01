"""G2: ``broadcast_phase`` payload + scheduling contract.

We don't spin up a real FastAPI loop. Instead we hijack the module-level
``_schedule`` helper to capture each scheduled coroutine and inspect the
payload that ``manager.broadcast(...)`` would receive.
"""

from __future__ import annotations

import inspect

import pytest

import api_server


@pytest.fixture
def captured(monkeypatch):
    """Capture broadcast() payloads instead of scheduling them on a loop."""

    payloads: list[dict] = []

    def _fake_schedule(coro):
        # Drive the coroutine just far enough to extract the payload arg.
        try:
            # manager.broadcast(payload) returns a coroutine; awaiting it
            # would push to websockets. We only need the *argument*.
            # Peek at frame locals after a single .send(None) — that hits
            # the first await, where 'message' is in scope.
            frame = inspect.getcoroutinelocals(coro)
            if "message" in frame:
                payloads.append(frame["message"])
            elif "payload" in frame:
                payloads.append(frame["payload"])
        finally:
            coro.close()

    monkeypatch.setattr(api_server, "_schedule", _fake_schedule)
    return payloads


class TestPhaseFunctionExists:
    def test_signature(self) -> None:
        sig = inspect.signature(api_server.broadcast_phase)
        params = sig.parameters
        assert "phase" in params
        assert params["severity"].default == "info"
        assert params["message"].default == ""
        assert params["step"].default is None
        assert params["duration_ms"].default is None
        assert params["extra"].default is None
        # U: notice_severity is a new optional keyword. None = omit field.
        assert "notice_severity" in params
        assert params["notice_severity"].default is None


class TestPhaseDefaults:
    def test_minimal_payload(self, captured) -> None:
        api_server.broadcast_phase("vlm_call")
        assert len(captured) == 1
        p = captured[0]
        assert p["type"] == "phase"
        assert p["phase"] == "vlm_call"
        assert p["severity"] == "info"
        assert p["message"] == ""
        assert "ts" in p and isinstance(p["ts"], float)
        # Optional fields not included when None
        assert "step" not in p
        assert "duration_ms" not in p

    def test_unknown_phase_uses_unknown(self, captured) -> None:
        api_server.broadcast_phase("")
        assert captured[0]["phase"] == "unknown"


class TestSeverity:
    @pytest.mark.parametrize("sev", ["info", "warn", "error"])
    def test_valid_severity_preserved(self, captured, sev) -> None:
        api_server.broadcast_phase("vlm_call", severity=sev)
        assert captured[-1]["severity"] == sev

    def test_invalid_severity_falls_back_to_info(self, captured) -> None:
        api_server.broadcast_phase("vlm_call", severity="critical")
        assert captured[-1]["severity"] == "info"


class TestOptionalFields:
    def test_step_included(self, captured) -> None:
        api_server.broadcast_phase("action", step=7)
        assert captured[-1]["step"] == 7

    def test_duration_ms_included(self, captured) -> None:
        api_server.broadcast_phase("vlm_call", duration_ms=1234)
        assert captured[-1]["duration_ms"] == 1234

    def test_step_coerced_to_int(self, captured) -> None:
        api_server.broadcast_phase("action", step="42")
        assert captured[-1]["step"] == 42

    def test_duration_coerced_to_int(self, captured) -> None:
        api_server.broadcast_phase("vlm_call", duration_ms="500")
        assert captured[-1]["duration_ms"] == 500


class TestExtra:
    def test_extra_keys_merged(self, captured) -> None:
        api_server.broadcast_phase(
            "extract",
            extra={"target_count": 42, "url": "https://example.test"},
        )
        p = captured[-1]
        assert p["target_count"] == 42
        assert p["url"] == "https://example.test"

    def test_extra_cannot_override_reserved(self, captured) -> None:
        api_server.broadcast_phase(
            "vlm_call",
            severity="warn",
            extra={"severity": "error", "phase": "hacked", "type": "evil"},
        )
        p = captured[-1]
        assert p["severity"] == "warn"  # NOT "error" — extra blocked
        assert p["phase"] == "vlm_call"  # NOT "hacked"
        assert p["type"] == "phase"


class TestSchedulingContract:
    def test_no_loop_silent_close(self, monkeypatch) -> None:
        """When _API_LOOP is None (CLI standalone), the coro must close cleanly."""
        monkeypatch.setattr(api_server, "_API_LOOP", None)
        # Should not raise and should not warn about unawaited coroutine
        api_server.broadcast_phase("vlm_call", message="cli mode")


# ────────────────────────────────────────────────────────────────────
# U: notice_severity — agent-visible notice level snapshot at emit
# ────────────────────────────────────────────────────────────────────


class TestNoticeSeverity:
    """``notice_severity`` carries the BrowserEnv ``_last_notice_severity``
    at emit time. Frontend uses max(severity, notice_severity) for chip
    colors. ``None`` (default) must omit the field from the payload to
    keep messages lean and back-compat with older WS consumers."""

    def test_default_omitted(self, captured) -> None:
        api_server.broadcast_phase("vlm_call", severity="info")
        assert "notice_severity" not in captured[-1]

    def test_none_explicit_omitted(self, captured) -> None:
        api_server.broadcast_phase("vlm_call", notice_severity=None)
        assert "notice_severity" not in captured[-1]

    @pytest.mark.parametrize("sev", ["info", "warn", "error"])
    def test_valid_values_preserved(self, captured, sev) -> None:
        api_server.broadcast_phase("action", notice_severity=sev)
        assert captured[-1]["notice_severity"] == sev

    def test_invalid_falls_back_to_info(self, captured) -> None:
        api_server.broadcast_phase("action", notice_severity="critical")
        assert captured[-1]["notice_severity"] == "info"

    def test_independent_of_severity(self, captured) -> None:
        """severity and notice_severity are decoupled — a successful action
        (severity=info) may still carry a warn notice_severity from a
        prior set_tab_notice call. Both fields must survive."""
        api_server.broadcast_phase(
            "action",
            severity="info",
            notice_severity="warn",
        )
        p = captured[-1]
        assert p["severity"] == "info"
        assert p["notice_severity"] == "warn"

    def test_extra_does_not_override_when_set(self, captured) -> None:
        """When notice_severity is provided, extra keys can't clobber it.

        (When notice_severity is omitted via the default None, the field is
        not in payload yet, so extra{"notice_severity": ...} *would* set it.
        We deliberately don't test that path because it's an explicit
        caller-side foot-gun, not a contract guarantee.)
        """
        api_server.broadcast_phase(
            "action",
            notice_severity="warn",
            extra={"notice_severity": "info"},
        )
        assert captured[-1]["notice_severity"] == "warn"

    def test_persisted_to_jsonl(self, monkeypatch, captured) -> None:
        """When phase log is open, notice_severity must round-trip through
        the per-run jsonl persistence layer (so offline scanners can
        reconstruct chip colors after the run ends).

        Uses a project-local tmp dir to dodge the Windows
        ``%LOCALAPPDATA%\\Temp\\pytest-of-<user>`` permission denials we
        hit with the default ``tmp_path`` fixture on this host.
        """
        import json
        import shutil
        import tempfile
        from pathlib import Path

        base = Path(__file__).resolve().parent / ".tmp_u_persist"
        base.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix="u_persist_", dir=str(base)))
        try:
            jsonl = tmp / "phase_u.jsonl"
            # Point the module-level path at our temp file (go direct
            # rather than through set_phase_log_run_id which builds its
            # own logs/phase_<run_id>.jsonl path).
            monkeypatch.setattr(api_server, "_PHASE_LOG_PATH", jsonl)
            api_server.broadcast_phase(
                "action",
                severity="info",
                notice_severity="warn",
                message="row removed",
            )
            assert jsonl.exists()
            line = jsonl.read_text(encoding="utf-8").strip().splitlines()[-1]
            rec = json.loads(line)
            assert rec["notice_severity"] == "warn"
            assert rec["severity"] == "info"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
