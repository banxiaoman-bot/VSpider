"""Rich-text editor structured write (Slice FORM-RICHTEXT-1).

Closes the weakness "rich-text editors only receive a flat textContent
write": the binding JS must write through the editor API (Quill / TinyMCE /
CKEditor 5) when one is present, then fall back to the real input chain
(execCommand insertText) and finally structured paragraph HTML - never a
bare textContent assignment that desyncs the editor model.

Source-level anchors only: the JS execution paths were validated with a
live-browser probe (generic contenteditable / Quill API double / multiline)
at 3/3 PASS before probe removal.
"""

from __future__ import annotations

import inspect
import re

import pytest

from visual_web_agent.actions import _form_set_bound_control_v2

SRC = inspect.getsource(_form_set_bound_control_v2)


class TestRichTextWritePaths:
    def test_rich_text_helper_is_wired_into_set_native_value(self) -> None:
        assert "setRichTextValue" in SRC
        assert re.search(
            r"if \(el\.isContentEditable\) \{\s*\r?\n\s*setRichTextValue\(el, val\);",
            SRC,
        ), "contenteditable branch must route through setRichTextValue"

    def test_flat_textcontent_write_is_gone(self) -> None:
        assert "el.textContent = val;" not in SRC, (
            "regressed to the flat textContent write that desyncs editor models"
        )

    def test_quill_path_uses_editor_api(self) -> None:
        assert ".ql-editor" in SRC
        assert "__quill" in SRC
        assert "quill.setText" in SRC
        assert "'quill_api'" in SRC

    def test_tinymce_path_uses_editor_registry(self) -> None:
        assert "window.tinymce" in SRC
        assert "ed.setContent" in SRC
        assert "'tinymce_api'" in SRC

    def test_ckeditor5_path_uses_instance_api(self) -> None:
        assert "ckeditorInstance" in SRC
        assert "'ckeditor5_api'" in SRC

    def test_generic_path_drives_real_input_chain(self) -> None:
        """ProseMirror/Slate/Lexical listen to beforeinput - execCommand
        insertText fires it; bare DOM writes do not."""
        assert "execCommand('insertText', false, text)" in SRC
        assert "'exec_insert_text'" in SRC

    def test_last_resort_is_structured_paragraphs(self) -> None:
        assert "toParagraphHtml" in SRC
        assert "'structured_paragraphs'" in SRC
        assert "escHtml" in SRC, "paragraph fallback must escape HTML"

    def test_priority_order_api_before_exec_before_html(self) -> None:
        """Editor APIs first, real input chain second, synthetic HTML last."""
        order = [
            SRC.index("'quill_api'"),
            SRC.index("'tinymce_api'"),
            SRC.index("'ckeditor5_api'"),
            SRC.index("'exec_insert_text'"),
            SRC.index("'structured_paragraphs'"),
        ]
        assert order == sorted(order), "rich-text fallback priority order changed"

    def test_paragraph_regexes_survive_python_escaping(self) -> None:
        r"""The JS lives in a non-raw Python string: \n must stay escaped as
        \\n in source so JS receives the regex /\n{2,}/, not a literal
        newline inside a regex (SyntaxError, caught by the live probe)."""
        assert "split(/\\\\n{2,}/)" in SRC
        assert "replace(/\\\\n/g, '<br>')" in SRC


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
