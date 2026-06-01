"""F3: ``answer_domain`` propagation through done broadcast.

Covers:
* ``_record_run_answer(domain=...)`` writes the per-run state slot.
* ``_reset_run_answer()`` clears it.
* ``_broadcast_done_safe`` reads it and forwards as ``answer_domain``.
* The api_server ``send_done`` builds the correct payload, dropping the
  domain key when irrelevant (file mode / failure / unknown domain).
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

import api_server
from visual_web_agent import main as main_mod


# ════════════════════════════════════════════════════════════════════
# Fixtures
# ════════════════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def _isolate_main_run_state():
    """Reset module-level run state before AND after each test so order
    doesn't matter and one test's domain doesn't leak into the next."""
    main_mod._reset_run_answer()
    yield
    main_mod._reset_run_answer()


@pytest.fixture
def captured_done(monkeypatch):
    """Capture the dict payload that ``manager.send_done`` would broadcast."""
    payloads: list[dict] = []

    def _fake_schedule(coro):
        try:
            frame = inspect.getcoroutinelocals(coro)
            # send_done builds its payload inside the coroutine — peek at locals
            # after stepping once. The simplest way is to drive the coro to
            # completion against a stub ``self.broadcast`` that records arg.
            # But send_done is on the ConnectionManager; we cheated by
            # monkeypatching broadcast below.
        finally:
            coro.close()

    async def _fake_broadcast(self_or_msg, *args, **kwargs):
        # ConnectionManager.broadcast(self, message) — but called as
        # manager.broadcast(payload) so self is bound; positional arg is the
        # payload. Accept both binding styles.
        if isinstance(self_or_msg, dict):
            payloads.append(self_or_msg)
        elif args and isinstance(args[0], dict):
            payloads.append(args[0])

    # Patch broadcast on the manager instance so send_done writes go there
    monkeypatch.setattr(api_server.manager, "broadcast", _fake_broadcast)
    # Drive scheduled coroutines synchronously on a fresh loop each time
    # (avoids DeprecationWarning about no current event loop on Py 3.12+).
    def _drive(coro):
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(coro)
        finally:
            loop.close()

    monkeypatch.setattr(api_server, "_schedule", _drive)
    return payloads


# ════════════════════════════════════════════════════════════════════
# Per-run state
# ════════════════════════════════════════════════════════════════════


class TestRunDomainState:
    def test_initial_state_is_none(self) -> None:
        assert main_mod._RUN_ANSWER_DOMAIN is None

    def test_record_writes_domain(self) -> None:
        main_mod._record_run_answer(domain="weather")
        assert main_mod._RUN_ANSWER_DOMAIN == "weather"

    def test_record_text_does_not_clobber_domain(self) -> None:
        main_mod._record_run_answer(domain="stock")
        main_mod._record_run_answer(text="hello")
        assert main_mod._RUN_ANSWER_DOMAIN == "stock"

    def test_reset_clears_domain(self) -> None:
        main_mod._record_run_answer(domain="recipe")
        main_mod._reset_run_answer()
        assert main_mod._RUN_ANSWER_DOMAIN is None

    def test_mode_and_domain_together(self) -> None:
        main_mod._record_run_answer(mode="answer", domain="flight")
        assert main_mod._RUN_OUTPUT_MODE == "answer"
        assert main_mod._RUN_ANSWER_DOMAIN == "flight"


# ════════════════════════════════════════════════════════════════════
# send_done payload assembly
# ════════════════════════════════════════════════════════════════════


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(coro)
    finally:
        loop.close()


class TestSendDonePayload:
    def test_weather_text_includes_domain(self, captured_done) -> None:
        _run(api_server.manager.send_done(
            True,
            "ok",
            answer_type="text",
            answer="北京晴，25°C",
            answer_domain="weather",
        ))
        assert len(captured_done) == 1
        p = captured_done[0]
        assert p["type"] == "done"
        assert p["success"] is True
        assert p["answer_type"] == "text"
        assert p["answer_domain"] == "weather"

    def test_generic_domain_dropped(self, captured_done) -> None:
        """'generic' is not useful to frontend; backend should strip it."""
        _run(api_server.manager.send_done(
            True,
            "ok",
            answer_type="text",
            answer="hello",
            answer_domain="generic",
        ))
        assert "answer_domain" not in captured_done[0]

    def test_no_domain_when_omitted(self, captured_done) -> None:
        _run(api_server.manager.send_done(True, "ok", answer_type="text", answer="x"))
        assert "answer_domain" not in captured_done[0]

    def test_empty_string_domain_dropped(self, captured_done) -> None:
        _run(api_server.manager.send_done(
            True, "ok", answer_type="text", answer="x", answer_domain=""
        ))
        assert "answer_domain" not in captured_done[0]


# ════════════════════════════════════════════════════════════════════
# _broadcast_done_safe end-to-end
# ════════════════════════════════════════════════════════════════════


def _only_done(payloads: list[dict]) -> list[dict]:
    """``_broadcast_done_safe`` now also emits a finalize phase event (I3),
    so the capture list contains both ``type=done`` and ``type=phase`` rows.
    The done-payload tests below operate on the done rows only."""
    return [p for p in payloads if p.get("type") == "done"]


class TestBroadcastDoneSafe:
    def test_reads_domain_from_run_state(self, captured_done) -> None:
        main_mod._record_run_answer(
            mode="answer", text="北京晴，25°C", domain="weather",
        )
        main_mod._broadcast_done_safe(True, "completed")
        dones = _only_done(captured_done)
        assert len(dones) == 1
        assert dones[0]["answer_domain"] == "weather"

    def test_explicit_arg_overrides_run_state(self, captured_done) -> None:
        main_mod._record_run_answer(mode="answer", text="hi", domain="recipe")
        main_mod._broadcast_done_safe(
            True, "completed", answer_domain="stock",
        )
        assert _only_done(captured_done)[0]["answer_domain"] == "stock"

    def test_file_mode_strips_domain(self, captured_done) -> None:
        """domain is only meaningful for text answers; file-mode runs must
        not surface a card hint to the frontend."""
        main_mod._record_run_answer(mode="artifact", domain="weather")
        main_mod._broadcast_done_safe(True, "completed")
        p = _only_done(captured_done)[0]
        assert p["answer_type"] == "file"
        assert "answer_domain" not in p

    def test_failure_strips_domain(self, captured_done) -> None:
        main_mod._record_run_answer(mode="answer", text="x", domain="stock")
        main_mod._broadcast_done_safe(False, "failed")
        p = _only_done(captured_done)[0]
        assert p["success"] is False
        assert "answer_domain" not in p
        assert "answer_type" not in p
        assert "answer" not in p

    def test_no_domain_recorded_no_domain_emitted(self, captured_done) -> None:
        main_mod._record_run_answer(mode="answer", text="plain text")
        main_mod._broadcast_done_safe(True, "ok")
        assert "answer_domain" not in _only_done(captured_done)[0]


# ════════════════════════════════════════════════════════════════════
# Signature contract
# ════════════════════════════════════════════════════════════════════


class TestSignatures:
    def test_record_run_answer_accepts_domain_kw(self) -> None:
        sig = inspect.signature(main_mod._record_run_answer)
        assert "domain" in sig.parameters
        assert sig.parameters["domain"].default is None

    def test_broadcast_done_safe_accepts_domain_kw(self) -> None:
        sig = inspect.signature(main_mod._broadcast_done_safe)
        assert "answer_domain" in sig.parameters

    def test_api_broadcast_done_accepts_domain_kw(self) -> None:
        sig = inspect.signature(api_server.broadcast_done)
        assert "answer_domain" in sig.parameters

    def test_api_send_done_accepts_domain_kw(self) -> None:
        sig = inspect.signature(api_server.ConnectionManager.send_done)
        assert "answer_domain" in sig.parameters
