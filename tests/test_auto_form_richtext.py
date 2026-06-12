"""auto_form macro rich-text writes + fallback shadow reach (FORM-RICHTEXT-2).

Closes two backlog items: the macro's contenteditable branch was a flat
textContent write (desyncs Quill/TinyMCE models), and the component-aware
fallback JS only queried the light DOM.

Also locks the verify fix discovered by the live probe: ``valueOf`` joined
``aria-label`` into the readback, so every labelled control failed verify
('macro quill text Notes' != 'macro quill text'). Live probe (Quill double
+ contenteditable + open-shadow input) PASS after the fix, pre-removal.

FORM-RICHTEXT-4 adds the cross-origin editor iframe chain: the in-macro
write surfaces ``iframe_unreachable`` (instead of a misleading ok:true),
the Python side rescues the field through Playwright's frame tree, then
reruns the macro with the rescued labels preset.
"""

from __future__ import annotations

import asyncio
import inspect
import re

import pytest

from visual_web_agent import main as main_mod
from visual_web_agent.main import (
    _try_auto_form_fill,
    _try_auto_form_fill_bound_controls,
)

MACRO_SRC = inspect.getsource(_try_auto_form_fill_bound_controls)
FALLBACK_SRC = inspect.getsource(_try_auto_form_fill)


def _run(coro):
    return asyncio.run(coro)


class TestMacroRichText:
    def test_contenteditable_routes_through_rich_text_helper(self) -> None:
        assert "setRichTextValue" in MACRO_SRC
        assert re.search(
            r"if \(el\.isContentEditable\) \{\s*\r?\n"
            r"\s*const method = setRichTextValue\(el, String\(value \|\| ''\)\);",
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
            r"\s*return setRichTextValue\(el, String\(value \|\| ''\)\);",
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


class TestUnreachableIframeEvidence:
    """FORM-RICHTEXT-4 (evidence): the cross-origin iframe write used to push
    ok:true into results while verify failed on the same field. The write
    method must flow back so the failure is surfaced with the frame
    coordinates the Python-side rescue pass needs."""

    def test_set_native_value_returns_the_write_method(self) -> None:
        assert "return 'native_value';" in MACRO_SRC

    def test_write_method_is_checked_before_pushing_ok(self) -> None:
        assert "const writeMethod = setNativeValue(control, expected);" in MACRO_SRC
        assert "writeMethod === 'iframe_unreachable'" in MACRO_SRC

    def test_unreachable_iframe_write_pushes_a_failing_result(self) -> None:
        assert "reason: 'iframe_unreachable'" in MACRO_SRC

    def test_failing_result_carries_frame_coordinates(self) -> None:
        assert "frameId: control.id || ''" in MACRO_SRC
        assert "frameSrc: control.src || ''" in MACRO_SRC


class TestPresetLabels:
    """FORM-RICHTEXT-4 (rerun): fields already written at frame level must
    skip the in-macro write+verify (a cross-origin body is unreachable from
    the main document) but still bind, so label->control matching stays
    exclusive for the remaining fields."""

    def test_macro_accepts_preset_labels(self) -> None:
        assert "preset_labels: list[str] | None = None" in MACRO_SRC
        assert '"presetLabels": list(preset_labels or [])' in MACRO_SRC

    def test_preset_fields_bind_but_skip_write_and_verify(self) -> None:
        assert "presetSet.has(norm(label))" in MACRO_SRC
        assert "'preset_frame_write'" in MACRO_SRC
        used_idx = MACRO_SRC.index("used.add(binding.control);")
        preset_idx = MACRO_SRC.index("presetSet.has(norm(label))")
        bindings_idx = MACRO_SRC.index("bindings.push([label, value, binding]);")
        assert used_idx < preset_idx < bindings_idx, (
            "preset short-circuit must sit between control reservation and "
            "the write/verify pipeline"
        )

    def test_preset_hits_satisfy_the_verification_gate(self) -> None:
        assert "(verifications.length > 0 || presetHits > 0)" in MACRO_SRC


class _StubElementHandle:
    def __init__(self, frame) -> None:
        self._frame = frame

    async def content_frame(self):
        return self._frame


class _StubEditorFrame:
    def __init__(self, readback: str = "macro tiny text", raise_on_evaluate: bool = False) -> None:
        self.url = "https://cross.example/editor"
        self.evaluate_calls: list[tuple[str, object]] = []
        self._readback = readback
        self._raise = raise_on_evaluate

    async def evaluate(self, js, arg=None):
        if self._raise:
            raise RuntimeError("frame detached")
        self.evaluate_calls.append((js, arg))
        return self._readback


class _StubPage:
    def __init__(self, handle=None, frames=(), main_frame=None) -> None:
        self._handle = handle
        self.frames = list(frames)
        self.main_frame = main_frame
        self.query_calls: list[str] = []

    async def query_selector(self, selector):
        self.query_calls.append(selector)
        return self._handle


class _StubHostFrame:
    """The same-origin child frame that hosts the form (not the editor)."""

    def __init__(self, url: str = "https://host.example/form_frame.html") -> None:
        self.url = url

    def is_detached(self) -> bool:
        return False


FIELDS = {"Title": "hello title", "Notes": "macro tiny text"}


def _unreachable_result() -> dict:
    return {
        "ok": False,
        "results": [
            {"label": "Title", "ok": True, "mode": "input", "method": "for_attr"},
            {
                "label": "Notes",
                "ok": False,
                "reason": "iframe_unreachable",
                "method": "for_attr",
                "frameId": "notes_ifr",
                "frameSrc": "https://cross.example/editor",
            },
        ],
        "verifications": [
            {"label": "Title", "ok": True},
            {"label": "Notes", "ok": False, "observed": ""},
        ],
    }


class TestFrameLevelRescue:
    """FORM-RICHTEXT-4 (rescue): Playwright reaches cross-origin editor
    frames that main-document JS cannot - write there, verify the readback,
    then rerun the macro with the rescued labels preset."""

    def test_rescue_is_wired_into_the_frames_wrapper(self) -> None:
        src = inspect.getsource(main_mod._auto_form_fill_bound_controls_with_frames)
        assert "_auto_form_rescue_unreachable_iframes" in src

    def test_ok_result_passes_through(self) -> None:
        page = _StubPage()
        ok_result = {"ok": True, "results": []}
        out = _run(
            main_mod._auto_form_rescue_unreachable_iframes(
                page, ok_result, scope_title="", fields={}, require_submit=False
            )
        )
        assert out is ok_result
        assert page.query_calls == []

    def test_failures_without_unreachable_pass_through(self) -> None:
        page = _StubPage()
        miss = {
            "ok": False,
            "results": [{"label": "Title", "ok": False, "reason": "field_not_found"}],
        }
        out = _run(
            main_mod._auto_form_rescue_unreachable_iframes(
                page, miss, scope_title="", fields=dict(FIELDS), require_submit=False
            )
        )
        assert out is miss
        assert page.query_calls == []

    def test_frame_write_success_reruns_with_preset_labels(self, monkeypatch) -> None:
        frame = _StubEditorFrame(readback="macro tiny text")
        page = _StubPage(handle=_StubElementHandle(frame))
        rerun_calls: list[list[str]] = []

        async def fake_macro(p, *, scope_title, fields, require_submit, preset_labels=None):
            rerun_calls.append(list(preset_labels or []))
            return {"ok": True, "submitted": True, "results": [], "verifications": []}

        monkeypatch.setattr(main_mod, "_try_auto_form_fill_bound_controls", fake_macro)
        out = _run(
            main_mod._auto_form_rescue_unreachable_iframes(
                page,
                _unreachable_result(),
                scope_title="Ticket",
                fields=dict(FIELDS),
                require_submit=True,
            )
        )
        assert rerun_calls == [["Notes"]]
        assert out["ok"] is True
        assert out["frame_level_writes"] == [
            {
                "label": "Notes",
                "frame_url": "https://cross.example/editor",
                "readback_ok": True,
            }
        ]
        assert frame.evaluate_calls, "the rescue must write through frame.evaluate"

    def test_frame_resolved_by_src_when_id_lookup_fails(self, monkeypatch) -> None:
        frame = _StubEditorFrame(readback="macro tiny text")
        page = _StubPage(handle=None, frames=[frame])

        async def fake_macro(p, **kwargs):
            return {"ok": True, "results": [], "verifications": []}

        monkeypatch.setattr(main_mod, "_try_auto_form_fill_bound_controls", fake_macro)
        out = _run(
            main_mod._auto_form_rescue_unreachable_iframes(
                page,
                _unreachable_result(),
                scope_title="Ticket",
                fields=dict(FIELDS),
                require_submit=False,
            )
        )
        assert out["ok"] is True
        assert frame.evaluate_calls

    def test_frame_evaluate_failure_keeps_original_result(self, monkeypatch) -> None:
        frame = _StubEditorFrame(raise_on_evaluate=True)
        page = _StubPage(handle=_StubElementHandle(frame))
        rerun_calls: list[dict] = []

        async def fake_macro(p, **kwargs):
            rerun_calls.append(kwargs)
            return {"ok": True}

        monkeypatch.setattr(main_mod, "_try_auto_form_fill_bound_controls", fake_macro)
        original = _unreachable_result()
        out = _run(
            main_mod._auto_form_rescue_unreachable_iframes(
                page,
                original,
                scope_title="Ticket",
                fields=dict(FIELDS),
                require_submit=False,
            )
        )
        assert out is original
        assert rerun_calls == []

    def test_readback_mismatch_keeps_original_result(self, monkeypatch) -> None:
        frame = _StubEditorFrame(readback="something else entirely")
        page = _StubPage(handle=_StubElementHandle(frame))
        rerun_calls: list[dict] = []

        async def fake_macro(p, **kwargs):
            rerun_calls.append(kwargs)
            return {"ok": True}

        monkeypatch.setattr(main_mod, "_try_auto_form_fill_bound_controls", fake_macro)
        original = _unreachable_result()
        out = _run(
            main_mod._auto_form_rescue_unreachable_iframes(
                page,
                original,
                scope_title="Ticket",
                fields=dict(FIELDS),
                require_submit=False,
            )
        )
        assert out is original
        assert rerun_calls == []

    def test_missing_frame_keeps_original_result(self) -> None:
        page = _StubPage(handle=None, frames=[])
        original = _unreachable_result()
        out = _run(
            main_mod._auto_form_rescue_unreachable_iframes(
                page,
                original,
                scope_title="Ticket",
                fields=dict(FIELDS),
                require_submit=False,
            )
        )
        assert out is original

    def test_label_missing_from_fields_keeps_original_result(self) -> None:
        frame = _StubEditorFrame()
        page = _StubPage(handle=_StubElementHandle(frame))
        original = _unreachable_result()
        out = _run(
            main_mod._auto_form_rescue_unreachable_iframes(
                page,
                original,
                scope_title="Ticket",
                fields={"Title": "hello title"},
                require_submit=False,
            )
        )
        assert out is original
        assert frame.evaluate_calls == []


class TestNestedFrameRescue:
    """FORM-RICHTEXT-5: editor iframes nested inside child frames. The sweep
    path returns the host frame's result without rescuing, and the rerun must
    happen on that host frame (bindings, sibling fields and submit live
    there), while frame resolution stays on the page whose .frames flattens
    every nesting level."""

    def test_sweep_hits_route_through_the_rescue(self) -> None:
        src = inspect.getsource(main_mod._auto_form_fill_bound_controls_with_frames)
        assert src.count("_auto_form_rescue_unreachable_iframes") >= 2, (
            "both the main-document path and the sweep-hit path must rescue"
        )
        assert "rerun_scope=frame" in src

    def test_rescue_reruns_on_the_given_scope(self, monkeypatch) -> None:
        editor = _StubEditorFrame(readback="macro tiny text")
        page = _StubPage(handle=_StubElementHandle(editor))
        host = _StubHostFrame()
        rerun_scopes: list[object] = []

        async def fake_macro(scope, **kwargs):
            rerun_scopes.append(scope)
            return {"ok": True, "results": [], "verifications": []}

        monkeypatch.setattr(main_mod, "_try_auto_form_fill_bound_controls", fake_macro)
        out = _run(
            main_mod._auto_form_rescue_unreachable_iframes(
                page,
                _unreachable_result(),
                scope_title="Ticket",
                fields=dict(FIELDS),
                require_submit=False,
                rerun_scope=host,
            )
        )
        assert rerun_scopes == [host]
        assert out["ok"] is True

    def test_rerun_defaults_to_the_page(self, monkeypatch) -> None:
        editor = _StubEditorFrame(readback="macro tiny text")
        page = _StubPage(handle=_StubElementHandle(editor))
        rerun_scopes: list[object] = []

        async def fake_macro(scope, **kwargs):
            rerun_scopes.append(scope)
            return {"ok": True, "results": [], "verifications": []}

        monkeypatch.setattr(main_mod, "_try_auto_form_fill_bound_controls", fake_macro)
        _run(
            main_mod._auto_form_rescue_unreachable_iframes(
                page,
                _unreachable_result(),
                scope_title="Ticket",
                fields=dict(FIELDS),
                require_submit=False,
            )
        )
        assert rerun_scopes == [page]

    def test_rescue_inherits_frame_url_evidence(self, monkeypatch) -> None:
        editor = _StubEditorFrame(readback="macro tiny text")
        page = _StubPage(handle=_StubElementHandle(editor))
        host = _StubHostFrame()

        async def fake_macro(scope, **kwargs):
            return {"ok": True, "results": [], "verifications": []}

        monkeypatch.setattr(main_mod, "_try_auto_form_fill_bound_controls", fake_macro)
        original = _unreachable_result()
        original["frame_url"] = host.url
        out = _run(
            main_mod._auto_form_rescue_unreachable_iframes(
                page,
                original,
                scope_title="Ticket",
                fields=dict(FIELDS),
                require_submit=False,
                rerun_scope=host,
            )
        )
        assert out["frame_url"] == host.url

    def test_wrapper_rescues_sweep_hits_end_to_end(self, monkeypatch) -> None:
        editor = _StubEditorFrame(readback="macro tiny text")
        host = _StubHostFrame()
        main_frame = object()
        page = _StubPage(
            handle=_StubElementHandle(editor),
            frames=[main_frame, host],
            main_frame=main_frame,
        )
        calls: list[tuple[object, list[str]]] = []

        async def fake_macro(scope, *, preset_labels=None, **kwargs):
            calls.append((scope, list(preset_labels or [])))
            if scope is page:
                return {
                    "ok": False,
                    "results": [
                        {"label": "Title", "ok": False, "reason": "field_not_found"},
                        {"label": "Notes", "ok": False, "reason": "field_not_found"},
                    ],
                }
            if preset_labels:
                return {"ok": True, "results": [], "verifications": []}
            return _unreachable_result()

        monkeypatch.setattr(main_mod, "_try_auto_form_fill_bound_controls", fake_macro)
        out = _run(
            main_mod._auto_form_fill_bound_controls_with_frames(
                page,
                scope_title="Ticket",
                fields=dict(FIELDS),
                require_submit=False,
            )
        )
        assert out["ok"] is True
        assert out["frame_level_writes"][0]["label"] == "Notes"
        assert out["frame_url"] == host.url
        assert calls == [(page, []), (host, []), (host, ["Notes"])]
        assert editor.evaluate_calls, "the rescue must write through the editor frame"

    def test_wrapper_keeps_ok_sweep_hits_untouched(self, monkeypatch) -> None:
        """Same-origin nested editors already succeed inside the host frame -
        the rescue must stay a no-op on that path."""
        host = _StubHostFrame()
        main_frame = object()
        page = _StubPage(frames=[main_frame, host], main_frame=main_frame)
        ok_hit = {"ok": True, "results": [{"label": "Notes", "ok": True}]}

        async def fake_macro(scope, *, preset_labels=None, **kwargs):
            if scope is page:
                return {
                    "ok": False,
                    "results": [
                        {"label": "Notes", "ok": False, "reason": "field_not_found"}
                    ],
                }
            return ok_hit

        monkeypatch.setattr(main_mod, "_try_auto_form_fill_bound_controls", fake_macro)
        out = _run(
            main_mod._auto_form_fill_bound_controls_with_frames(
                page,
                scope_title="Ticket",
                fields=dict(FIELDS),
                require_submit=False,
            )
        )
        assert out is ok_hit
        assert out["frame_url"] == host.url
        assert page.query_calls == []


class _StubQueryFrame:
    """A host frame document that can be queried for the tagged iframe."""

    def __init__(self, handle=None, url: str = "https://host.example/inner.html") -> None:
        self._handle = handle
        self.url = url
        self.query_calls: list[str] = []

    def is_detached(self) -> bool:
        return False

    async def query_selector(self, selector):
        self.query_calls.append(selector)
        return self._handle


def _srcdoc_unreachable_result(token: str = "vsp-test-1") -> dict:
    return {
        "ok": False,
        "results": [
            {"label": "Title", "ok": True, "mode": "input", "method": "for_attr"},
            {
                "label": "Notes",
                "ok": False,
                "reason": "iframe_unreachable",
                "method": "for_attr",
                "frameId": "",
                "frameSrc": "",
                "frameToken": token,
                "frameSrcdoc": True,
            },
        ],
        "verifications": [
            {"label": "Title", "ok": True},
            {"label": "Notes", "ok": False, "observed": ""},
        ],
    }


class TestSrcdocFrameRescue:
    """FORM-RICHTEXT-6: srcdoc editor iframes carry no src and every one of
    them reports the about:srcdoc URL, so the id/src coordinates can never
    locate the Playwright frame. The macro tags the host element with a
    one-shot token attribute; the rescue resolves that token through the
    page first, then the flat frame list (nested hosts included)."""

    def test_srcdoc_iframes_are_bindable_controls(self) -> None:
        """A generic srcdoc editor (no _ifr id, no tox class) must enter the
        control candidate pool, or the unreachable report never fires."""
        assert 'iframe[srcdoc]' in MACRO_SRC

    def test_macro_tags_unreachable_iframes_with_a_token(self) -> None:
        assert "data-vspider-frame-token" in MACRO_SRC
        assert "frameToken" in MACRO_SRC

    def test_macro_reports_the_srcdoc_flag(self) -> None:
        assert "frameSrcdoc" in MACRO_SRC

    def test_token_reuse_is_idempotent(self) -> None:
        """A retried macro run must not stack a second token on the host."""
        get_idx = MACRO_SRC.index("getAttribute('data-vspider-frame-token')")
        set_idx = MACRO_SRC.index("setAttribute('data-vspider-frame-token'")
        assert get_idx < set_idx

    def test_token_resolves_via_page_lookup(self, monkeypatch) -> None:
        frame = _StubEditorFrame(readback="macro tiny text")
        frame.url = "about:srcdoc"
        page = _StubPage(handle=_StubElementHandle(frame))

        async def fake_macro(p, *, preset_labels=None, **kwargs):
            return {"ok": True, "results": [], "verifications": []}

        monkeypatch.setattr(main_mod, "_try_auto_form_fill_bound_controls", fake_macro)
        out = _run(
            main_mod._auto_form_rescue_unreachable_iframes(
                page,
                _srcdoc_unreachable_result(),
                scope_title="Ticket",
                fields=dict(FIELDS),
                require_submit=False,
            )
        )
        assert out["ok"] is True
        assert out["frame_level_writes"] == [
            {"label": "Notes", "frame_url": "about:srcdoc", "readback_ok": True}
        ]
        assert any("data-vspider-frame-token" in sel for sel in page.query_calls)
        assert frame.evaluate_calls, "the rescue must write through the srcdoc frame"

    def test_token_resolves_through_nested_host_frames(self, monkeypatch) -> None:
        """The tagged iframe element lives inside a child frame document -
        the page-level lookup misses and the flat frame walk must find it."""
        editor = _StubEditorFrame(readback="macro tiny text")
        editor.url = "about:srcdoc"
        host = _StubQueryFrame(handle=_StubElementHandle(editor))
        page = _StubPage(handle=None, frames=[host])

        async def fake_macro(p, *, preset_labels=None, **kwargs):
            return {"ok": True, "results": [], "verifications": []}

        monkeypatch.setattr(main_mod, "_try_auto_form_fill_bound_controls", fake_macro)
        out = _run(
            main_mod._auto_form_rescue_unreachable_iframes(
                page,
                _srcdoc_unreachable_result(),
                scope_title="Ticket",
                fields=dict(FIELDS),
                require_submit=False,
            )
        )
        assert out["ok"] is True
        assert any("data-vspider-frame-token" in sel for sel in host.query_calls)
        assert editor.evaluate_calls

    def test_token_lookup_miss_falls_back_to_src(self, monkeypatch) -> None:
        """A stale token must not break the existing src resolution."""
        frame = _StubEditorFrame(readback="macro tiny text")
        page = _StubPage(handle=None, frames=[frame])

        async def fake_macro(p, *, preset_labels=None, **kwargs):
            return {"ok": True, "results": [], "verifications": []}

        monkeypatch.setattr(main_mod, "_try_auto_form_fill_bound_controls", fake_macro)
        result = _unreachable_result()
        result["results"][1]["frameToken"] = "vsp-stale-token"
        out = _run(
            main_mod._auto_form_rescue_unreachable_iframes(
                page,
                result,
                scope_title="Ticket",
                fields=dict(FIELDS),
                require_submit=False,
            )
        )
        assert out["ok"] is True
        assert frame.evaluate_calls

    def test_missing_token_keeps_legacy_lookups_untouched(self, monkeypatch) -> None:
        """Old fixtures without frameToken must never emit a token query."""
        frame = _StubEditorFrame(readback="macro tiny text")
        page = _StubPage(handle=_StubElementHandle(frame))

        async def fake_macro(p, *, preset_labels=None, **kwargs):
            return {"ok": True, "results": [], "verifications": []}

        monkeypatch.setattr(main_mod, "_try_auto_form_fill_bound_controls", fake_macro)
        out = _run(
            main_mod._auto_form_rescue_unreachable_iframes(
                page,
                _unreachable_result(),
                scope_title="Ticket",
                fields=dict(FIELDS),
                require_submit=False,
            )
        )
        assert out["ok"] is True
        assert all("data-vspider-frame-token" not in sel for sel in page.query_calls)

    def test_unresolvable_srcdoc_keeps_original_result(self) -> None:
        """Token misses everywhere and there is no id/src to fall back to."""
        page = _StubPage(handle=None, frames=[])
        original = _srcdoc_unreachable_result()
        out = _run(
            main_mod._auto_form_rescue_unreachable_iframes(
                page,
                original,
                scope_title="Ticket",
                fields=dict(FIELDS),
                require_submit=False,
            )
        )
        assert out is original


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
