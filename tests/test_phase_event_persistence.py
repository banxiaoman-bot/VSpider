"""L: per-run phase event persistence to ``logs/phase_<run_id>.jsonl``.

When ``set_phase_log_run_id(run_id)`` is called, every subsequent
``broadcast_phase`` call must:

1. **Append** one JSON Lines row to ``logs/phase_<run_id>.jsonl``.
2. **Preserve** the same payload shape that goes to the WebSocket.
3. **Continue working** when the file write fails (best-effort — the live
   broadcast path must never crash because of a logging issue).
4. **Stay quiet** when no run_id is set (CLI-only / tests don't litter
   the workspace with stray jsonl files).

These tests run with the broadcast manager stubbed out so we don't need an
event loop.
"""

from __future__ import annotations

import importlib
import json
import shutil
import tempfile
from pathlib import Path

import pytest


# Re-import for a clean module state per test session
api_server = importlib.import_module("api_server")


@pytest.fixture
def quiet_broadcast(monkeypatch):
    """Stub out the WebSocket broadcast so tests don't need an event loop.

    We must close() the never-awaited coroutine to silence ``RuntimeWarning:
    coroutine '...' was never awaited`` — the broadcast call site builds the
    coro before passing it in.
    """
    def _drop(coro):
        try:
            coro.close()
        except Exception:
            pass

    monkeypatch.setattr(api_server, "_schedule", _drop)
    yield


@pytest.fixture
def tmp_logs(monkeypatch):
    """Run tests inside an isolated cwd so phase jsonl writes don't pollute
    the real ``logs/`` directory.

    Uses ``tempfile.mkdtemp`` (under the workspace) instead of pytest's
    ``tmp_path`` fixture because the latter writes into
    ``%LOCALAPPDATA%\\Temp\\pytest-of-<user>``, which is occasionally
    permission-denied on this Windows host.
    """
    # Use a project-local tmp dir to avoid both:
    #   - %LOCALAPPDATA%\Temp\pytest-of-<user>  (permission denied)
    #   - workspace/pytest-tmp                  (permission denied)
    base = Path(__file__).resolve().parent.parent / ".tmp_phase_tests"
    base.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="phase_persist_", dir=str(base)))
    monkeypatch.chdir(tmp)
    try:
        yield tmp
    finally:
        # Reset module-level path so other tests don't see leftover state
        api_server.set_phase_log_run_id(None)
        # Best-effort cleanup
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════════════
# Setter contract
# ════════════════════════════════════════════════════════════════════════


class TestSetterContract:
    def test_creates_logs_dir_and_file(self, tmp_logs, quiet_broadcast):
        api_server.set_phase_log_run_id("20260601_120000")
        path = tmp_logs / "logs" / "phase_20260601_120000.jsonl"
        assert path.exists(), "setter must touch the file"
        assert path.read_text(encoding="utf-8") == "", "newly-touched file is empty"

    def test_none_disables_persistence(self, tmp_logs, quiet_broadcast):
        api_server.set_phase_log_run_id("20260601_120000")
        api_server.set_phase_log_run_id(None)
        # subsequent broadcast must NOT create or touch any file
        api_server.broadcast_phase("test_phase", message="should-not-persist")
        jsonls = list((tmp_logs / "logs").glob("*.jsonl"))
        # The original file still exists (touched earlier) but stays empty
        # since persistence was disabled before the broadcast.
        assert all(p.stat().st_size == 0 for p in jsonls)

    def test_empty_string_disables_persistence(self, tmp_logs, quiet_broadcast):
        api_server.set_phase_log_run_id("")
        api_server.broadcast_phase("test_phase", message="x")
        assert not (tmp_logs / "logs" / "phase_.jsonl").exists()

    def test_idempotent_reopen_preserves_content(self, tmp_logs, quiet_broadcast):
        api_server.set_phase_log_run_id("20260601_120000")
        api_server.broadcast_phase("first")
        api_server.set_phase_log_run_id("20260601_120000")
        api_server.broadcast_phase("second")
        path = tmp_logs / "logs" / "phase_20260601_120000.jsonl"
        rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 2
        assert rows[0]["phase"] == "first"
        assert rows[1]["phase"] == "second"


# ════════════════════════════════════════════════════════════════════════
# Persistence content contract
# ════════════════════════════════════════════════════════════════════════


class TestPayloadShape:
    def test_appends_one_jsonl_row_per_call(self, tmp_logs, quiet_broadcast):
        api_server.set_phase_log_run_id("20260601_120000")
        for i in range(3):
            api_server.broadcast_phase("test_phase", step=i)
        path = tmp_logs / "logs" / "phase_20260601_120000.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3
        # Each line is valid JSON
        rows = [json.loads(l) for l in lines]
        assert [r["step"] for r in rows] == [0, 1, 2]

    def test_payload_matches_websocket_broadcast(self, tmp_logs, monkeypatch):
        """The jsonl row must be the SAME dict shape as the WebSocket payload."""
        captured: list[dict] = []

        def _capture(coro):
            # extract the payload from the broadcast coro
            # broadcast accepts a dict — we'll capture via monkeypatching manager.broadcast
            pass

        sent: list[dict] = []

        async def _fake_broadcast(payload):
            sent.append(payload)

        monkeypatch.setattr(api_server.manager, "broadcast", _fake_broadcast)
        # _schedule will try to run the coroutine; for the capture above to
        # fire we need _schedule to actually drive the coroutine. Use a
        # one-shot loop per call (DeprecationWarning-free on Py3.12+).
        import asyncio

        def _schedule_sync(coro):
            asyncio.new_event_loop().run_until_complete(coro)

        monkeypatch.setattr(api_server, "_schedule", _schedule_sync)

        api_server.set_phase_log_run_id("20260601_120000")
        api_server.broadcast_phase(
            "vlm_call",
            severity="warn",
            message="slow",
            step=7,
            duration_ms=4321,
            extra={"model": "qwen-vl"},
        )

        path = tmp_logs / "logs" / "phase_20260601_120000.jsonl"
        jsonl_row = json.loads(path.read_text(encoding="utf-8").strip())
        assert sent, "broadcast manager must still receive the payload"
        ws_payload = sent[-1]

        # File row and WebSocket payload are the same shape
        assert jsonl_row == ws_payload
        # Spot-check expected fields
        assert jsonl_row["type"] == "phase"
        assert jsonl_row["phase"] == "vlm_call"
        assert jsonl_row["severity"] == "warn"
        assert jsonl_row["step"] == 7
        assert jsonl_row["duration_ms"] == 4321
        assert jsonl_row["model"] == "qwen-vl"  # extra fields flattened

    def test_jsonl_is_utf8_no_ascii_escape(self, tmp_logs, quiet_broadcast):
        """Chinese characters should be readable, not \\uXXXX escaped."""
        api_server.set_phase_log_run_id("20260601_120000")
        api_server.broadcast_phase("test", message="中文消息")
        path = tmp_logs / "logs" / "phase_20260601_120000.jsonl"
        content = path.read_text(encoding="utf-8")
        assert "中文消息" in content, (
            "phase jsonl must be human-readable UTF-8, not \\uXXXX escaped"
        )


# ════════════════════════════════════════════════════════════════════════
# WS-EGRESS-REDACT: mask secrets before disk / WebSocket egress
# ════════════════════════════════════════════════════════════════════════


class TestSecretRedaction:
    """Secrets carried in phase ``extra`` / broadcast payloads must be masked
    before they reach disk (``_persist_phase_event``) or the WebSocket
    (``ConnectionManager.broadcast``). Opaque blobs like screenshot ``data``
    must pass through untouched so the hot path stays cheap."""

    def test_phase_event_persist_masks_secrets(self, tmp_logs, quiet_broadcast):
        api_server.set_phase_log_run_id("20260601_120000")
        api_server.broadcast_phase(
            "vlm_call",
            message="auth",
            extra={
                "vlm_api_key": "supersecret-key",
                "endpoint": "https://u-user:u-pass@api.example.com/v1",
                "model": "qwen-vl",
            },
        )
        path = tmp_logs / "logs" / "phase_20260601_120000.jsonl"
        raw = path.read_text(encoding="utf-8")
        row = json.loads(raw.strip())
        # secret masked on disk
        assert "supersecret-key" not in raw
        assert row["vlm_api_key"] == "***"
        # url-embedded credentials stripped, endpoint survives
        assert "u-user" not in raw
        assert "u-pass" not in raw
        assert row["endpoint"] == "https://api.example.com/v1"
        # non-secret fields preserved
        assert row["model"] == "qwen-vl"
        assert row["phase"] == "vlm_call"

    def test_ws_broadcast_masks_secrets_keeps_blob(self):
        import asyncio

        class _FakeWS:
            def __init__(self):
                self.sent: list[str] = []

            async def send_text(self, text):
                self.sent.append(text)

        mgr = api_server.ConnectionManager()
        ws = _FakeWS()
        mgr.active.append(ws)
        blob = "B" * 5000  # screenshot-sized opaque payload
        payload = {
            "type": "screenshot",
            "vlm_api_key": "supersecret-key",
            "data": blob,
            "ok": 1,
        }
        asyncio.new_event_loop().run_until_complete(mgr.broadcast(payload))
        assert ws.sent
        text = ws.sent[-1]
        # secret masked on the wire
        assert "supersecret-key" not in text
        assert '"vlm_api_key"' in text and "***" in text
        # opaque blob preserved verbatim (not walked / not corrupted)
        assert blob in text
        assert '"ok"' in text


# ════════════════════════════════════════════════════════════════════════
# Safety: write failures must not break the live broadcast
# ════════════════════════════════════════════════════════════════════════


class TestSafety:
    def test_unwritable_path_does_not_raise(self, tmp_logs, quiet_broadcast, monkeypatch):
        api_server.set_phase_log_run_id("20260601_120000")

        # Force _persist_phase_event to hit an IO error
        def _boom(*_a, **_kw):
            raise PermissionError("simulated disk full")

        path_obj = api_server._PHASE_LOG_PATH
        assert path_obj is not None
        monkeypatch.setattr(type(path_obj), "open", _boom, raising=False)
        # Should NOT raise — best-effort persistence
        api_server.broadcast_phase("test_phase", message="should-not-crash")

    def test_no_run_id_no_file_write(self, tmp_logs, quiet_broadcast):
        # Persistence disabled (default state after the fixture cleanup)
        api_server.set_phase_log_run_id(None)
        api_server.broadcast_phase("test_phase", message="should-not-create")
        # No files created in logs/
        log_dir = tmp_logs / "logs"
        if log_dir.exists():
            assert not list(log_dir.glob("*.jsonl"))


# ════════════════════════════════════════════════════════════════════════
# Wiring contract: main.py calls set_phase_log_run_id at run start
# ════════════════════════════════════════════════════════════════════════


class TestMainWiring:
    def test_main_calls_setter_after_run_ts(self) -> None:
        main_src = Path(__file__).resolve().parent.parent / "visual_web_agent" / "main.py"
        src = main_src.read_text(encoding="utf-8")
        # G1: _run_ts now comes from prepare_run_identity delegation
        ts_idx = src.find("_run_ts = _run_ctx.run_ts")
        if ts_idx == -1:
            ts_idx = src.find("_run_ts = datetime.now().strftime")
        assert ts_idx != -1, "main.py must set _run_ts (direct or via RunContext)"
        setter_idx = src.find("set_phase_log_run_id", ts_idx)
        assert setter_idx != -1, "main.py must call set_phase_log_run_id"
        between = src[ts_idx:setter_idx]
        assert between.count("\n") < 15, (
            "set_phase_log_run_id must be called within ~15 lines of _run_ts"
        )
