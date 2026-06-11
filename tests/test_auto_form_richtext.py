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


class TestTinyMceClassicIframe:
    """FORM-RICHTEXT-3: TinyMCE classic hides the textarea behind an editor
    iframe (<id>_ifr); the macro binds the iframe host and writes through
    the registry API (or straight into the same-origin body)."""

    def test_editor_iframe_is_a_bindable_control(self) -> None:
        assert 'iframe[id$="_ifr"]' in MACRO_SRC
        assert "iframe.tox-edit-area__iframe" in MACRO_SRC

    def test_for_attr_falls_back_to_the_ifr_stand_in(self) -> None:
        assert "ctrl.id === forId + '_ifr'" in MACRO_SRC

    def test_registry_lookup_strips_the_ifr_suffix(self) -> None:
        assert "el.id?.replace(/_ifr$/, '')" in MACRO_SRC
        assert "ids.map(id => tiny.get(id)).find(Boolean)" in MACRO_SRC

    def test_container_match_reaches_iframe_hosts(self) -> None:
        """getBody() lives in the iframe document - cross-document contains()
        is always false, so the container (main document) must match too."""
        assert "e?.getContainer?.()?.contains?.(el)" in MACRO_SRC

    def test_apiless_iframe_writes_structured_paragraphs_into_body(self) -> None:
        assert "'iframe_structured_paragraphs'" in MACRO_SRC
        assert "'iframe_unreachable'" in MACRO_SRC
        idx_iframe = MACRO_SRC.index("'iframe_structured_paragraphs'")
        idx_exec = MACRO_SRC.index("'exec_insert_text'")
        assert idx_iframe < idx_exec, (
            "the iframe branch must intercept before the main-document "
            "execCommand path, which cannot reach the iframe body"
        )

    def test_set_native_value_routes_iframes_to_rich_text(self) -> None:
        assert re.search(
            r"if \(String\(el\.tagName \|\| ''\)\.toLowerCase\(\) === 'iframe'\) \{\s*\r?\n"
            r"\s*setRichTextValue\(el, String\(value \|\| ''\)\);",
            MACRO_SRC,
        )

    def test_write_gate_admits_iframe_controls(self) -> None:
        assert "tag === 'input' || tag === 'textarea' || tag === 'iframe'" in MACRO_SRC

    def test_verify_reads_the_iframe_body_back(self) -> None:
        assert "control.contentDocument?.body?.innerText" in MACRO_SRC


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
