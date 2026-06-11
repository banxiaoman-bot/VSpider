"""auto_form macro rich-text writes + fallback shadow reach (FORM-RICHTEXT-2).

Closes two backlog items: the macro's contenteditable branch was a flat
textContent write (desyncs Quill/TinyMCE models), and the component-aware
fallback JS only queried the light DOM.

Also locks the verify fix discovered by the live probe: ``valueOf`` joined
``aria-label`` into the readback, so every labelled control failed verify
('macro quill text Notes' != 'macro quill text'). Live probe (Quill double
+ contenteditable + open-shadow input) PASS after the fix, pre-removal.
"""

from __future__ import annotations

import inspect
import re

import pytest

from visual_web_agent.main import (
    _try_auto_form_fill,
    _try_auto_form_fill_bound_controls,
)

MACRO_SRC = inspect.getsource(_try_auto_form_fill_bound_controls)
FALLBACK_SRC = inspect.getsource(_try_auto_form_fill)


class TestMacroRichText:
    def test_contenteditable_routes_through_rich_text_helper(self) -> None:
        assert "setRichTextValue" in MACRO_SRC
        assert re.search(
            r"if \(el\.isContentEditable\) \{\s*\r?\n\s*setRichTextValue\(el, String\(value \|\| ''\)\);",
            MACRO_SRC,
        )

    def test_flat_textcontent_write_is_gone(self) -> None:
        assert "el.textContent = String(value || '');" not in MACRO_SRC

    def test_editor_api_tiers_present_in_priority_order(self) -> None:
        order = [
            MACRO_SRC.index("'quill_api'"),
            MACRO_SRC.index("'tinymce_api'"),
            MACRO_SRC.index("'ckeditor5_api'"),
            MACRO_SRC.index("'exec_insert_text'"),
            MACRO_SRC.index("'structured_paragraphs'"),
        ]
        assert order == sorted(order)

    def test_paragraph_regexes_survive_python_escaping(self) -> None:
        assert "split(/\\\\n{2,}/)" in MACRO_SRC
        assert "replace(/\\\\n/g, '<br>')" in MACRO_SRC


class TestVerifyValueOf:
    def test_contenteditable_readback_uses_inner_text_only(self) -> None:
        assert "return clean(control.innerText || control.textContent || '');" in MACRO_SRC

    def test_aria_label_no_longer_joined_into_the_value(self) -> None:
        """aria-label is the field's label, not its value."""
        assert (
            "[control.value, control.textContent, control.getAttribute('aria-label')]"
            not in MACRO_SRC
        )
        assert "const direct = [control.value, control.textContent]" in MACRO_SRC

    def test_aria_label_kept_as_empty_value_fallback(self) -> None:
        assert "return clean(control.getAttribute('aria-label') || '');" in MACRO_SRC


class TestFallbackShadowReach:
    def test_component_fallback_descends_open_shadow_roots(self) -> None:
        assert "deepQueryAll" in FALLBACK_SRC
        assert (
            "const allVisible = (selector, root = document) => "
            "deepQueryAll(selector, root).filter(isVisible);" in FALLBACK_SRC
        )

    def test_light_dom_only_one_liner_is_gone(self) -> None:
        assert (
            "const allVisible = (selector, root = document) => "
            "Array.from(root.querySelectorAll(selector)).filter(isVisible);"
            not in FALLBACK_SRC
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
