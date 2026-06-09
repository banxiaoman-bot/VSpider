"""Tests for ``visual_web_agent.io_contract``.

All tests here are pure: no Playwright, no FS writes, no network. They cover:

- :class:`InputContract` builder and URL/attachment parsing
- attachment intent inference across mime / suffix / goal combinations
- :class:`OutputContract` inference, including media kinds and overrides
- :class:`Manifest` append/dedupe semantics
"""

from __future__ import annotations

import json

import pytest

from visual_web_agent.io_contract.input_contract import (
    AttachmentSpec,
    EntrySuggestion,
    InputContract,
    UrlSpec,
    ATTACHMENT_INTENTS,
    URL_ROLES,
    build_input_contract,
    infer_attachment_intent,
    infer_urls_from_goal,
    parse_urls_field,
    suggest_entry_url,
)
from visual_web_agent.io_contract import RunOutputProtocol
from visual_web_agent.io_contract.output_contract import (
    CONTAINERS,
    OUTPUT_KINDS,
    OutputPrediction,
    OutputContract,
    default_container_for_kind,
    infer_output_contract,
    normalize_output_contract_dict,
    normalize_output_fields,
    output_contract_fields,
)
from visual_web_agent.io_contract.manifest import (
    Manifest,
    ManifestItem,
    append_item,
    merge_source_url,
    new_manifest,
)


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------


class TestParseUrlsField:
    def test_none_returns_empty(self) -> None:
        assert parse_urls_field(None) == []

    def test_empty_string_returns_empty(self) -> None:
        assert parse_urls_field("") == []
        assert parse_urls_field("   \n   ") == []

    def test_single_string(self) -> None:
        out = parse_urls_field("https://a.example.com")
        assert len(out) == 1
        assert out[0].url == "https://a.example.com"
        assert out[0].role == "start"
        assert out[0].system_id == "auto"

    def test_comma_separated(self) -> None:
        out = parse_urls_field("https://a.com, https://b.com")
        assert [u.url for u in out] == ["https://a.com", "https://b.com"]

    def test_newline_separated(self) -> None:
        out = parse_urls_field("https://a.com\nhttps://b.com\n")
        assert [u.url for u in out] == ["https://a.com", "https://b.com"]

    def test_semicolon_separated(self) -> None:
        out = parse_urls_field("https://a.com;https://b.com")
        assert [u.url for u in out] == ["https://a.com", "https://b.com"]

    def test_json_array_of_strings(self) -> None:
        out = parse_urls_field('["https://a.com", "https://b.com"]')
        assert [u.url for u in out] == ["https://a.com", "https://b.com"]

    def test_json_array_of_dicts(self) -> None:
        out = parse_urls_field(
            '[{"url":"https://a.com","role":"start","system_id":"sys_a"},'
            ' {"url":"https://b.com","role":"reference"}]'
        )
        assert out[0].system_id == "sys_a"
        assert out[1].role == "reference"

    def test_list_of_strings(self) -> None:
        out = parse_urls_field(["https://a.com", "https://b.com"])
        assert len(out) == 2

    def test_list_of_dicts(self) -> None:
        out = parse_urls_field([{"url": "https://a.com", "role": "dataset"}])
        assert out[0].role == "dataset"

    def test_invalid_dict_without_url(self) -> None:
        assert parse_urls_field([{"role": "start"}]) == []


# ---------------------------------------------------------------------------
# Attachment intent
# ---------------------------------------------------------------------------


class TestInferAttachmentIntent:
    def test_xlsx_is_batch_rows(self) -> None:
        assert infer_attachment_intent(filename="data.xlsx") == "batch_rows"
        assert infer_attachment_intent(filename="data.csv") == "batch_rows"
        assert infer_attachment_intent(filename="data.parquet") == "batch_rows"

    def test_pdf_upload_intent(self) -> None:
        assert (
            infer_attachment_intent(
                filename="contract.pdf", goal="请把这个 PDF 上传到附件区"
            )
            == "upload_to_page"
        )

    def test_pdf_read_intent(self) -> None:
        assert (
            infer_attachment_intent(
                filename="report.pdf", goal="请阅读并总结这个 PDF 的要点"
            )
            == "prompt_context"
        )

    def test_pdf_default_upload(self) -> None:
        assert infer_attachment_intent(filename="x.pdf") == "upload_to_page"

    def test_image_upload(self) -> None:
        assert (
            infer_attachment_intent(filename="head.png", goal="upload my avatar")
            == "upload_to_page"
        )

    def test_image_prompt_context_default(self) -> None:
        assert infer_attachment_intent(filename="head.png") == "prompt_context"

    def test_video_default_media_source(self) -> None:
        assert infer_attachment_intent(filename="clip.mp4") == "media_source"

    def test_video_upload(self) -> None:
        assert (
            infer_attachment_intent(filename="clip.mp4", goal="上传该视频到平台")
            == "upload_to_page"
        )

    def test_zip_default_upload(self) -> None:
        assert infer_attachment_intent(filename="bundle.zip") == "upload_to_page"

    def test_txt_is_prompt_context(self) -> None:
        assert infer_attachment_intent(filename="notes.txt") == "prompt_context"
        assert infer_attachment_intent(filename="readme.md") == "prompt_context"

    def test_jsonl_array_sample_is_batch_rows(self) -> None:
        assert (
            infer_attachment_intent(
                filename="rows.jsonl", sample_bytes=b'[{"a":1}]'
            )
            == "batch_rows"
        )

    def test_json_object_is_prompt_context(self) -> None:
        assert (
            infer_attachment_intent(filename="cfg.json", sample_bytes=b'{"a":1}')
            == "prompt_context"
        )

    def test_json_with_columns_is_batch_rows(self) -> None:
        assert (
            infer_attachment_intent(filename="x.json", columns=["a", "b"])
            == "batch_rows"
        )

    def test_unknown_suffix(self) -> None:
        assert infer_attachment_intent(filename="weird.bin") == "unknown"
        assert infer_attachment_intent(filename="") == "unknown"

    def test_mime_overrides_when_filename_blank(self) -> None:
        assert (
            infer_attachment_intent(filename="", mime="application/pdf", goal="上传")
            == "upload_to_page"
        )


# ---------------------------------------------------------------------------
# InputContract builder
# ---------------------------------------------------------------------------


class TestBuildInputContract:
    def test_minimal(self) -> None:
        c = build_input_contract(goal="hello")
        assert c.goal == "hello"
        assert c.urls == []
        assert c.attachments == []
        assert c.version == "input_contract.v1"


class TestRunOutputProtocol:
    def test_protocol_round_trip(self) -> None:
        input_contract = build_input_contract(goal="下载图片")
        protocol = RunOutputProtocol(
            input_contract=input_contract,
            output_prediction=OutputPrediction(kind="media_image", mode="artifact"),
        )
        payload = protocol.to_dict()
        assert payload["input_contract"]["goal"] == "下载图片"
        assert payload["output_prediction"]["kind"] == "media_image"
        assert payload["output_contract"]["output_kind"] == "media_image"

    def test_legacy_target_url_promoted(self) -> None:
        c = build_input_contract(goal="x", target_url="https://a.com")
        assert len(c.urls) == 1
        assert c.urls[0].url == "https://a.com"
        assert c.urls[0].role == "start"

    def test_urls_field_overrides_target_url(self) -> None:
        c = build_input_contract(
            goal="x",
            target_url="https://legacy.com",
            urls=["https://a.com", "https://b.com"],
        )
        assert [u.url for u in c.urls] == ["https://a.com", "https://b.com"]

    def test_dedup_target_url_already_present(self) -> None:
        c = build_input_contract(
            goal="x",
            target_url="https://a.com",
            urls=["https://a.com", "https://b.com"],
        )
        assert [u.url for u in c.urls] == ["https://a.com", "https://b.com"]

    def test_attachments_intent_inferred(self) -> None:
        c = build_input_contract(
            goal="批量按行填表",
            attachments=[
                {"path": "temp_uploads/abc.xlsx", "filename": "abc.xlsx"},
                {"path": "temp_uploads/photo.png", "filename": "photo.png"},
            ],
        )
        assert c.attachments[0].intent == "batch_rows"
        assert c.attachments[1].intent == "prompt_context"

    def test_attachments_explicit_intent_respected(self) -> None:
        c = build_input_contract(
            goal="x",
            attachments=[
                {"path": "p", "filename": "weird.bin", "intent": "media_source"}
            ],
        )
        assert c.attachments[0].intent == "media_source"

    def test_auth_profiles_string_split(self) -> None:
        c = build_input_contract(goal="x", auth_profiles="a,b;c")
        assert c.auth_profiles == ["a", "b", "c"]

    def test_vlm_options_into_overrides(self) -> None:
        c = build_input_contract(
            goal="x",
            vlm_options={"model": "m1", "semantic_model": "m2", "temperature": 0.1},
        )
        d = c.model_overrides.to_dict()
        assert d["vlm"]["model"] == "m1"
        assert d["semantic"]["model"] == "m2"
        assert d["vlm"]["temperature"] == 0.1

    def test_constraints_preserve_runtime_extras(self) -> None:
        c = build_input_contract(
            goal="x",
            constraints={
                "max_runs": 3,
                "resume": True,
                "proxy_chain": ["http://p1", "http://p2"],
                "proxy_strategy": "round_robin",
            },
        )
        d = c.constraints.to_dict()
        assert d["max_runs"] == 3
        assert d["resume"] is True
        assert d["proxy_chain"] == ["http://p1", "http://p2"]
        assert d["proxy_strategy"] == "round_robin"

    def test_to_dict_stable_keys(self) -> None:
        c = build_input_contract(goal="x", target_url="https://a.com")
        d = c.to_dict()
        for k in (
            "version", "goal", "urls", "attachments", "auth_profiles",
            "model_overrides", "constraints", "source", "created_at",
        ):
            assert k in d

    def test_to_json_round_trip(self) -> None:
        c = build_input_contract(goal="x", target_url="https://a.com")
        s = c.to_json()
        parsed = json.loads(s)
        assert parsed["goal"] == "x"
        assert parsed["urls"][0]["url"] == "https://a.com"

    def test_has_cross_system(self) -> None:
        c = build_input_contract(
            goal="x",
            urls=["https://a.com", "https://b.com"],
        )
        assert c.has_cross_system is True

    def test_no_cross_system_single_host(self) -> None:
        c = build_input_contract(
            goal="x",
            urls=["https://a.com/x", "https://a.com/y"],
        )
        assert c.has_cross_system is False

    def test_primary_start_url(self) -> None:
        c = build_input_contract(
            goal="x",
            urls=[
                {"url": "https://ref.com", "role": "reference"},
                {"url": "https://start.com", "role": "start"},
            ],
        )
        assert c.primary_start_url == "https://start.com"


# ---------------------------------------------------------------------------
# OutputContract inference
# ---------------------------------------------------------------------------


class TestOutputPrediction:
    def test_from_dict_normalizes_aliases(self) -> None:
        p = OutputPrediction.from_dict({"output_kind": "media_image", "output_mode": "artifact", "source": "planner"})
        assert p.kind == "media_image"
        assert p.mode == "artifact"
        assert p.source == "planner"

    def test_from_dataclass_round_trip(self) -> None:
        p = OutputPrediction(kind="media_video", mode="artifact", confidence=0.88)
        assert p.to_dict()["kind"] == "media_video"
        assert p.normalized().kind == "media_video"


class TestInferOutputContract:
    def test_pdf_goal_routes_to_media_pdf(self) -> None:
        c = infer_output_contract("帮我把页面上所有 PDF 报告下载下来")
        assert c.output_kind == "media_pdf"
        assert c.container == "files_folder"

    def test_image_goal_routes_to_media_image(self) -> None:
        c = infer_output_contract("保存这一页的所有图片")
        assert c.output_kind == "media_image"
        assert c.container == "files_folder"
        assert "dedup" in c.post_process

    def test_legacy_infer_marks_media_as_structured_artifact(self) -> None:
        c = infer_output_contract("下载所有图片")
        assert c.mode == "artifact"
        assert c.output_kind == "media_image"
        assert c.container == "files_folder"
        assert "goal_requests_structured_rows" in c.reasons

    def test_video_goal_routes_to_media_video(self) -> None:
        c = infer_output_contract("download every video clip on this page")
        assert c.output_kind == "media_video"

    def test_audio_goal_routes_to_media_audio(self) -> None:
        c = infer_output_contract("帮我下载播客的音频")
        assert c.output_kind == "media_audio"

    def test_english_media_terms_are_detected(self) -> None:
        c = infer_output_contract("Please download all png and jpg images")
        assert c.output_kind == "media_image"

    def test_archive_goal_routes_to_media_archive(self) -> None:
        c = infer_output_contract("下载压缩包")
        assert c.output_kind == "media_archive"

    def test_screenshot_goal(self) -> None:
        c = infer_output_contract("帮我截屏保存当前页面")
        assert c.output_kind == "screenshot"

    def test_html_snapshot_goal(self) -> None:
        c = infer_output_contract("save the whole page as offline copy")
        assert c.output_kind == "html_snapshot"
        assert c.container == "html"

    def test_short_answer_goal(self) -> None:
        c = infer_output_contract("yes or no, will it rain tomorrow?")
        assert c.output_kind == "answer_text"
        assert c.container == "inline_text"

    def test_dataset_extraction_default(self) -> None:
        c = infer_output_contract("提取页面上所有商品的价格和标题")
        # extraction triggers artifact mode; falls back to dataset_rows
        assert c.output_kind in {"dataset_rows", "dataset_records", "mixed"}

    def test_user_explicit_kind_wins(self) -> None:
        c = infer_output_contract(
            "下载这些 PDF",
            user_explicit_kind="media_image",
        )
        assert c.output_kind == "media_image"
        assert c.user_explicit is True

    def test_model_kind_refines_mixed_goal(self) -> None:
        c = infer_output_contract(
            "帮我把这页内容处理好",
            model_predicted_kind="media_image",
        )
        assert c.output_kind == "media_image"
        assert "model_refined_mixed_output" in c.reasons

    def test_model_mode_can_tip_mixed_to_answer(self) -> None:
        c = infer_output_contract(
            "打开这个网站看看",
            model_predicted_mode="answer",
        )
        assert c.output_kind == "answer_text"
        assert "model_prefers_answer_mode" in c.reasons

    def test_user_explicit_container_wins(self) -> None:
        c = infer_output_contract(
            "提取数据",
            user_explicit_container="jsonl",
        )
        assert c.container == "jsonl"
        assert c.user_explicit is True

    def test_default_unknown_goal_is_mixed(self) -> None:
        c = infer_output_contract("打开这个网站看看")
        assert c.output_kind in {"mixed", "answer_text"}
        # default-bucket goals should never default to xlsx without dataset cues
        assert c.container != "xlsx"

    def test_ocr_post_process(self) -> None:
        c = infer_output_contract("下载 PDF 并识别文字")
        assert c.output_kind == "media_pdf"
        assert "ocr" in c.post_process

    def test_to_dict_keys(self) -> None:
        c = infer_output_contract("hello")
        d = c.to_dict()
        for k in (
            "version", "mode", "output_kind", "container", "fields",
            "post_process", "user_explicit", "reasons", "created_at",
        ):
            assert k in d
        assert d["version"] == "output_contract.v1"

    def test_multiple_media_kinds_routes_to_mixed(self) -> None:
        c = infer_output_contract("下载所有图片和视频")
        assert c.output_kind == "mixed"
        assert "multiple_media_kinds" in c.reasons


    def test_requested_fields_are_canonical_contract_fields(self) -> None:
        c = infer_output_contract(
            "export data",
            requested_fields=["Title", "title", "price", "url"],
        )
        payload = c.to_dict()
        assert payload["fields"] == ["Title", "price", "url"]
        assert payload["required_fields"] == ["Title", "price", "url"]
        assert payload["requested_fields"] == ["Title", "price", "url"]
        assert output_contract_fields(payload) == ["Title", "price", "url"]

    def test_normalize_output_fields_merges_legacy_aliases(self) -> None:
        payload = normalize_output_contract_dict({
            "output_kind": "dataset_rows",
            "required_fields": ["title", "price"],
            "requested_fields": "price, url",
        })
        assert normalize_output_fields(["title"], "Title", "price, url") == [
            "title",
            "price",
            "url",
        ]
        assert payload["fields"] == ["title", "price", "url"]
        assert payload["required_fields"] == ["title", "price", "url"]
        assert payload["requested_fields"] == ["title", "price", "url"]


class TestDefaultContainerForKind:
    def test_answer_text(self) -> None:
        assert default_container_for_kind("answer_text") == "inline_text"

    def test_dataset_rows_small(self) -> None:
        assert default_container_for_kind("dataset_rows", row_count=10) == "xlsx"

    def test_dataset_rows_large(self) -> None:
        assert default_container_for_kind("dataset_rows", row_count=10_000) == "jsonl"

    def test_media_kinds_to_files_folder(self) -> None:
        for kind in ("media_image", "media_video", "media_audio", "media_pdf", "media_archive"):
            assert default_container_for_kind(kind) == "files_folder"

    def test_html_snapshot(self) -> None:
        assert default_container_for_kind("html_snapshot") == "html"

    def test_unknown_kind_safe_default(self) -> None:
        assert default_container_for_kind("definitely_not_a_kind") == "files_folder"


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


class TestManifest:
    def test_new_manifest(self) -> None:
        m = new_manifest("run_1")
        assert m.run_id == "run_1"
        assert m.items == []
        assert m.version == "manifest.v1"

    def test_append_item_basic(self) -> None:
        m = new_manifest("run_1")
        item = append_item(
            m,
            kind="media_image",
            path="runs/run_1/artifacts/abc.png",
            size=1024,
            sha256="aaa",
            source_url="https://example.com/abc.png",
        )
        assert len(m.items) == 1
        assert item.path == "runs/run_1/artifacts/abc.png"
        assert item.source_url == ["https://example.com/abc.png"]

    def test_append_dedup_by_sha(self) -> None:
        m = new_manifest("run_1")
        append_item(
            m, kind="media_image", path="p1", sha256="same",
            source_url="https://a.com/x.png",
        )
        append_item(
            m, kind="media_image", path="p1", sha256="same",
            source_url="https://b.com/x.png",
        )
        assert len(m.items) == 1
        assert m.items[0].source_url == [
            "https://a.com/x.png",
            "https://b.com/x.png",
        ]

    def test_append_multiple_distinct_items(self) -> None:
        m = new_manifest("run_1")
        append_item(m, kind="media_image", path="p1", sha256="a")
        append_item(m, kind="media_pdf", path="p2", sha256="b")
        assert len(m.items) == 2

    def test_append_without_sha_does_not_dedupe(self) -> None:
        m = new_manifest("run_1")
        append_item(m, kind="media_image", path="p1")
        append_item(m, kind="media_image", path="p1")
        assert len(m.items) == 2

    def test_kind_normalized_to_other_when_unknown(self) -> None:
        m = new_manifest("run_1")
        item = append_item(m, kind="not_a_kind", path="p")
        assert item.to_dict()["kind"] == "other"

    def test_merge_source_url(self) -> None:
        item = ManifestItem(kind="media_image", path="p", source_url=["a"])
        merge_source_url(item, ["b", "a", "c"])
        assert item.source_url == ["a", "b", "c"]

    def test_to_dict_and_from_dict_round_trip(self) -> None:
        m = new_manifest("run_1")
        append_item(m, kind="media_image", path="p1", sha256="x")
        round_trip = Manifest.from_dict(m.to_dict())
        assert round_trip.run_id == "run_1"
        assert len(round_trip.items) == 1
        assert round_trip.items[0].sha256 == "x"

    def test_to_json_parses(self) -> None:
        m = new_manifest("run_1")
        append_item(m, kind="media_image", path="p1")
        parsed = json.loads(m.to_json())
        assert parsed["version"] == "manifest.v1"

    def test_find_by_sha_returns_none(self) -> None:
        m = new_manifest("run_1")
        assert m.find_by_sha("nothing") is None
        assert m.find_by_sha("") is None

    def test_from_dict_accepts_legacy_string_source(self) -> None:
        payload = {
            "version": "manifest.v1",
            "run_id": "r1",
            "items": [
                {
                    "kind": "media_image",
                    "path": "p",
                    "source_url": "https://a.com/x.png",
                }
            ],
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
        m = Manifest.from_dict(payload)
        assert m.items[0].source_url == ["https://a.com/x.png"]


# ---------------------------------------------------------------------------
# Cross-cutting constants
# ---------------------------------------------------------------------------


def test_constants_well_known() -> None:
    assert "batch_rows" in ATTACHMENT_INTENTS
    assert "upload_to_page" in ATTACHMENT_INTENTS
    assert "media_image" in OUTPUT_KINDS
    assert "media_pdf" in OUTPUT_KINDS
    assert "files_folder" in CONTAINERS
    assert "jsonl" in CONTAINERS
    assert "start" in URL_ROLES


class TestInferUrlsFromGoal:
    """Deterministic extraction of URLs the user literally wrote in the goal."""

    def test_extracts_explicit_https(self) -> None:
        urls = infer_urls_from_goal("帮我去 https://example.com/list 抓取所有商品")
        assert [u.url for u in urls] == ["https://example.com/list"]
        assert urls[0].role == "start"

    def test_extracts_www_and_normalizes_scheme(self) -> None:
        urls = infer_urls_from_goal("crawl www.example.com for prices")
        assert [u.url for u in urls] == ["https://www.example.com"]

    def test_extracts_bare_domain_chinese_goal(self) -> None:
        urls = infer_urls_from_goal("爬取 example.com 的列表数据")
        assert [u.url for u in urls] == ["https://example.com"]

    def test_no_url_returns_empty(self) -> None:
        assert infer_urls_from_goal("下载页面上所有 PDF 报告") == []
        assert infer_urls_from_goal("") == []

    def test_ignores_code_like_tokens(self) -> None:
        # filenames / module paths must not be mistaken for domains
        assert infer_urls_from_goal("修复 main.py 里的 bug") == []
        assert infer_urls_from_goal("读取 data.csv 并汇总") == []
        assert infer_urls_from_goal("调用 scipy.io 模块时报错") == []

    def test_dedup_and_trailing_punctuation(self) -> None:
        urls = infer_urls_from_goal("去 https://a.com，再看 https://a.com 一次")
        assert [u.url for u in urls] == ["https://a.com"]


class TestBuildInputContractGoalUrlFallback:
    """``build_input_contract`` falls back to goal-extracted URLs only when the
    caller supplied none; explicit urls / target_url always win."""

    def test_goal_url_used_when_no_explicit(self) -> None:
        c = build_input_contract(goal="爬取 https://shop.example.com/items 的全部商品")
        assert c.urls
        assert c.urls[0].url == "https://shop.example.com/items"
        assert c.urls[0].role == "start"

    def test_explicit_target_url_wins_over_goal(self) -> None:
        c = build_input_contract(
            goal="顺便看看 https://other.com",
            target_url="https://primary.com",
        )
        assert c.urls[0].url == "https://primary.com"
        assert all(u.url != "https://other.com" for u in c.urls)

    def test_no_url_anywhere_stays_empty(self) -> None:
        c = build_input_contract(goal="只回答一个数字")
        assert c.urls == []


class TestSuggestEntryUrl:
    """LLM-assisted default entry suggestion for goals with no literal URL.

    The LLM is injected as a simple ``prompt -> text`` callable so the unit
    tests stay deterministic; a search-engine URL is the always-available
    deterministic fallback.
    """

    def test_empty_goal_returns_none(self) -> None:
        assert suggest_entry_url("") is None
        assert suggest_entry_url("   ") is None

    def test_no_llm_uses_search_fallback(self) -> None:
        s = suggest_entry_url("查一下今天的金价")
        assert isinstance(s, EntrySuggestion)
        assert s.source == "search_fallback"
        assert s.url.startswith("https://")
        assert "search" in s.url
        assert s.needs_confirmation is True

    def test_llm_known_site_used(self) -> None:
        s = suggest_entry_url(
            "在淘宝搜罗一款机械键盘",
            llm=lambda prompt: "https://www.taobao.com",
        )
        assert s.source == "llm"
        assert s.url == "https://www.taobao.com"
        assert s.needs_confirmation is True

    def test_llm_bare_domain_answer_normalized(self) -> None:
        s = suggest_entry_url("查商品", llm=lambda prompt: "taobao.com")
        assert s.source == "llm"
        assert s.url == "https://taobao.com"

    def test_llm_none_answer_falls_back_to_search(self) -> None:
        s = suggest_entry_url("帮我做点研究", llm=lambda prompt: "NONE")
        assert s.source == "search_fallback"

    def test_llm_exception_falls_back_to_search(self) -> None:
        def boom(prompt: str) -> str:
            raise RuntimeError("model down")

        s = suggest_entry_url("随便看看", llm=boom)
        assert s.source == "search_fallback"

    def test_to_url_spec_role_start(self) -> None:
        s = suggest_entry_url("查金价")
        spec = s.to_url_spec()
        assert spec.role == "start"
        assert spec.url == s.url

    def test_search_url_encodes_goal(self) -> None:
        s = suggest_entry_url("机械 键盘 推荐")
        assert " " not in s.url
