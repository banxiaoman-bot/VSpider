"""AUTOPAGER-SHADOW-1: the table autopager reaches open shadow roots.

EXTRACT-SHADOW-1 taught the harvest and the before-signature to walk open
shadow roots, but the autopager itself stayed light-DOM: the DataTables
API sweep and the Next-button / numeric-pager candidate scan never saw a
shadow-hosted pager, and _pager_wait_js (the page-flip confirmation)
queried the light DOM only - so even a successfully clicked shadow pager
could never be confirmed (the wait always timed out and the autopager
reported failure on a page that actually flipped).

Source-anchor tests lock the wiring; the click + confirm loop was
validated against a real shadow-root DataTables-style pager with a
live-browser probe before commit.
"""

from __future__ import annotations

import inspect
import re

from visual_web_agent.main import run_agent
from visual_web_agent.extraction_engine.runtime import ExtractRuntime


def _pager_js_block() -> str:
    src = inspect.getsource(run_agent) + "\n" + inspect.getsource(ExtractRuntime)
    m = re.search(r'_pager_js = """\(\) => \{.*?\}"""', src, re.S)
    assert m, "_pager_js not found in run_agent source"
    return m.group(0)


def _pager_wait_js_block() -> str:
    src = inspect.getsource(run_agent) + "\n" + inspect.getsource(ExtractRuntime)
    m = re.search(r'_pager_wait_js = """\(before\) => \{.*?\}"""', src, re.S)
    assert m, "_pager_wait_js not found in run_agent source"
    return m.group(0)


class TestAutopagerShadowWiring:
    def test_datatables_sweep_walks_shadow_roots(self) -> None:
        block = _pager_js_block()
        assert "const tables = deepQueryAll('table')" in block
        assert "document.querySelectorAll('table')" not in block

    def test_pager_candidates_walk_shadow_roots(self) -> None:
        block = _pager_js_block()
        assert "const candidates = deepQueryAll(" in block
        assert 'document.querySelectorAll(\'button,a,[role="button"],[role="link"]\')' not in block

    def test_flip_confirmation_keeps_parity_with_the_deep_signature(self) -> None:
        block = _pager_wait_js_block()
        assert "const table = deepQueryAll('table')" in block, (
            "the after-flip check must see the same (shadow) table the "
            "before-signature hashed, or shadow flips are never confirmed"
        )

    def test_both_scripts_carry_their_own_walker(self) -> None:
        walker = "if (host.shadowRoot) out.push(...deepQueryAll(selector, host.shadowRoot));"
        assert walker in _pager_js_block()
        assert walker in _pager_wait_js_block()
