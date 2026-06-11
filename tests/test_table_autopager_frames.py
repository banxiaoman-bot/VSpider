"""Table autopager scope sweep (Slice EXTRACT-IFRAME-3).

Closes the P2 weakness "iframe tables can be harvested but not paged":
the autopager clicked/verified in the main document only, so iframe-hosted
multi-page tables yielded just their first page.

The autopager and signature helper are closures inside run_agent, so these
are source anchors; the scope semantics (main-doc signature empty while the
frame sees the table, pager click only resolving inside the frame, the
frame-scoped wait verifying page 2) were validated with a live-browser
probe at 3/3 PASS before removal.
"""

from __future__ import annotations

import inspect
import re

import pytest

from visual_web_agent.main import run_agent

SRC = inspect.getsource(run_agent)


class TestSignatureScope:
    def test_signature_helper_accepts_a_scope(self) -> None:
        assert "async def _visible_table_signature(reason: str, scope=None) -> str:" in SRC

    def test_scope_short_circuits_active_page_lookup(self) -> None:
        assert re.search(
            r"_sig_page = scope\s*\r?\n\s*if _sig_page is None:",
            SRC,
        ), "explicit scope must bypass the active-page lookup"


class TestAutopagerSweep:
    def test_pager_js_is_hoisted_and_run_per_scope(self) -> None:
        assert "_pager_js = " in SRC
        assert "_pager_wait_js = " in SRC
        assert "result = await scope.evaluate(_pager_js)" in SRC

    def test_scopes_cover_page_then_child_frames(self) -> None:
        assert "scopes = [_page_for_next]" in SRC
        assert re.search(
            r"for frame in list\(getattr\(_page_for_next, \"frames\", None\) or \[\]\):"
            r"[\s\S]{0,400}?scopes\.append\(frame\)",
            SRC,
        )

    def test_empty_signature_advances_to_next_scope(self) -> None:
        """No table in a scope = keep probing; nothing was clicked yet."""
        assert re.search(
            r'before_sig = await _visible_table_signature\(\s*\r?\n\s*'
            r'"table autopager before", scope=scope\s*\r?\n\s*\)\s*\r?\n'
            r"\s*if not before_sig:\s*\r?\n\s*continue",
            SRC,
        )

    def test_click_in_one_scope_never_probes_further_scopes(self) -> None:
        """After a click the sweep must verify in place, not double-page."""
        assert "probing further scopes after a click risks double-paging" in SRC
        click_zone = SRC.split("probing further scopes after a click", 1)[1][:1400]
        assert "return False" in click_zone and "return True" in click_zone
        assert "continue" not in click_zone.split("return True")[0], (
            "the post-click path must terminate, not continue the sweep"
        )

    def test_wait_and_after_signature_stay_in_the_clicked_scope(self) -> None:
        assert "await scope.wait_for_function(" in SRC
        assert re.search(
            r'"table autopager after", scope=scope',
            SRC,
        )

    def test_legacy_main_document_only_flow_is_gone(self) -> None:
        assert "await _page_for_next.wait_for_function(" not in SRC


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
