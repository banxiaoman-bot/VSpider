"""EXTRACT-SHADOW-1: the bulk table harvest reaches open shadow roots.

Component-library tables (Lit / Stencil / vanilla custom elements) render
inside open shadow roots, which plain ``document.querySelectorAll('table')``
never sees - the pre-extract DOM_TABLE candidate and the autopager's
table-signature verification were both blind there. The harvest and the
signature JS now walk open shadow roots via the same ``deepQueryAll``
pattern that the form fallback already uses (actions.deepQueryAll).

Source-anchor tests lock the wiring; the JS itself was validated against a
real shadow-root table with a live-browser probe before commit.
"""

from __future__ import annotations

import inspect
import re

from visual_web_agent.main import run_agent
from visual_web_agent.extraction_engine.runtime import ExtractRuntime


def _run_agent_src() -> str:
    return inspect.getsource(run_agent) + "\n" + inspect.getsource(ExtractRuntime)


class TestShadowTableWiring:
    def test_table_harvest_uses_deep_query(self) -> None:
        src = _run_agent_src()
        assert "const tables = deepQueryAll('table');" in src, (
            "the DOM_TABLE harvest no longer walks open shadow roots"
        )
        assert "const tables = Array.from(document.querySelectorAll('table'));" not in src

    def test_table_signature_uses_deep_query(self) -> None:
        src = _run_agent_src()
        assert "const table = deepQueryAll('table')" in src, (
            "the autopager table signature no longer walks open shadow roots"
        )

    def test_both_scripts_define_the_same_walker(self) -> None:
        src = _run_agent_src()
        assert src.count("if (host.shadowRoot) out.push(...deepQueryAll(selector, host.shadowRoot));") >= 2, (
            "harvest and signature must each carry the shadow walker "
            "(they evaluate as separate scripts)"
        )

    def test_walker_recurses_from_the_document_root(self) -> None:
        pattern = re.compile(
            r"const deepQueryAll = \(selector, root = document\) => \{"
        )
        assert len(pattern.findall(_run_agent_src())) >= 2
