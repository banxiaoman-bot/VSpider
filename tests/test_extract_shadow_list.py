"""EXTRACT-SHADOW-2: the list/card harvest reaches open shadow roots.

EXTRACT-SHADOW-1 gave the bulk TABLE harvest a shadow walker; the two
list/card scripts (the structured DOM_LIST candidate and the compact
list-text feed) still queried the light DOM only, so card components
rendered inside open shadow roots (Lit / Stencil / vanilla custom
elements) produced zero list candidates and the page fell through to
screenshot extraction.

Source-anchor tests lock the wiring; the JS itself was validated against a
real shadow-root card list with a live-browser probe before commit.
"""

from __future__ import annotations

import inspect

from visual_web_agent.main import run_agent


def _src() -> str:
    return inspect.getsource(run_agent)


class TestShadowListWiring:
    def test_direct_selector_sweep_uses_deep_query(self) -> None:
        src = _src()
        assert src.count("for (const el of deepQueryAll(directSelectors.join(',')))") == 2, (
            "both list scripts (structured rows + compact text) must walk shadow roots"
        )
        assert "document.querySelectorAll(directSelectors.join(','))" not in src

    def test_container_sweep_uses_deep_query(self) -> None:
        src = _src()
        assert src.count("for (const root of deepQueryAll(containerSelectors.join(',')))") == 2
        assert "document.querySelectorAll(containerSelectors.join(','))" not in src

    def test_every_harvest_script_carries_its_own_walker(self) -> None:
        # table harvest + table signature (SHADOW-1) + two list scripts
        # (SHADOW-2) - each evaluates separately, so each needs the walker.
        src = _src()
        assert src.count(
            "if (host.shadowRoot) out.push(...deepQueryAll(selector, host.shadowRoot));"
        ) >= 4
