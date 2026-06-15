"""Tests for broadcast.py HITL form submit chain (offline, no server)."""

from __future__ import annotations

import threading

from broadcast import (
    _HITL_FORM_EVENT,
    submit_hitl_form,
)


def test_submit_hitl_form_sets_event() -> None:
    _HITL_FORM_EVENT.clear()
    assert not _HITL_FORM_EVENT.is_set()
    submit_hitl_form({"username": "admin"})
    assert _HITL_FORM_EVENT.is_set()


def test_submit_hitl_form_stores_result() -> None:
    import broadcast
    broadcast._HITL_FORM_RESULT = None
    submit_hitl_form({"email": "test@example.com"})
    assert broadcast._HITL_FORM_RESULT == {"email": "test@example.com"}
