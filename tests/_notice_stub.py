"""Shared test helper: install ``set_tab_notice`` / ``clear_tab_notice``
methods on a SimpleNamespace browser stub so handlers migrated by the
J batch (which call ``browser.set_tab_notice(...)`` instead of writing
``browser._tab_switch_notice = ...`` directly) work against the lightweight
stubs used across the test suite.

Behavioural mimic of ``BrowserEnv.set_tab_notice``:

* ``text`` is stripped; empty → no-op.
* Severity must be ``info``/``warn``/``error``; falls back to ``info``.
* ``coalesce=False`` clobbers any prior notice; ``coalesce=True`` appends
  with a blank-line separator. Resulting severity = max(prior, incoming).
* The leading-emoji re-stamp logic is **omitted** — tests only assert on
  notice text content, not on the severity tag prefix. Skipping the stamp
  keeps existing test assertions (e.g. ``"TYPE NO-OP" in notice``) intact.

Usage::

    from _notice_stub import install_notice_stub
    browser = SimpleNamespace(...)
    install_notice_stub(browser)
"""

from __future__ import annotations

from typing import Any

_SEVERITY_RANK = {"info": 0, "warn": 1, "error": 2}


def install_notice_stub(browser: Any) -> None:
    """Attach ``set_tab_notice`` and ``clear_tab_notice`` callables to
    ``browser`` (a SimpleNamespace or any mutable object).

    Ensures the supporting attributes exist (idempotent):
    ``_tab_switch_notice`` (str | None) and ``_last_notice_severity`` (str).
    """
    if not hasattr(browser, "_tab_switch_notice"):
        browser._tab_switch_notice = None
    if not hasattr(browser, "_last_notice_severity"):
        browser._last_notice_severity = "info"

    def _set_tab_notice(
        text: str,
        *,
        severity: str = "info",
        coalesce: bool = True,
    ) -> None:
        text = (text or "").strip()
        if not text:
            return
        if severity not in _SEVERITY_RANK:
            severity = "info"
        prior = browser._tab_switch_notice
        if not prior:
            browser._tab_switch_notice = text
            browser._last_notice_severity = severity
            return
        if not coalesce:
            browser._tab_switch_notice = text
            browser._last_notice_severity = severity
            return
        browser._tab_switch_notice = prior + "\n\n" + text
        prior_rank = _SEVERITY_RANK.get(browser._last_notice_severity, 0)
        new_rank = _SEVERITY_RANK.get(severity, 0)
        if new_rank > prior_rank:
            browser._last_notice_severity = severity

    def _clear_tab_notice() -> None:
        browser._tab_switch_notice = None
        browser._last_notice_severity = "info"

    browser.set_tab_notice = _set_tab_notice
    browser.clear_tab_notice = _clear_tab_notice
