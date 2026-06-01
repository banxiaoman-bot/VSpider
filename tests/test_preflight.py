"""Tests for ``visual_web_agent.io_contract.preflight.build_preflight``.

Pure: the LLM is injected as a ``prompt -> text`` callable; only ``tmp_path``
files touch disk. Verifies decision-A behavior: a missing entry URL is
auto-resolved (never blocks), while a truly ambiguous attachment blocks.
"""

from __future__ import annotations

from pathlib import Path

from visual_web_agent.io_contract import build_preflight, Preflight


class TestEntryResolution:
    def test_explicit_url_wins(self) -> None:
        p = build_preflight("抓取列表", start_url="https://a.com")
        assert isinstance(p, Preflight)
        assert p.resolved_start_url == "https://a.com"
        assert p.entry_suggestion is None
        assert p.blocking == []

    def test_goal_extracted_url(self) -> None:
        p = build_preflight("爬取 https://b.com 的列表")
        assert p.resolved_start_url == "https://b.com"
        assert p.entry_suggestion is None

    def test_no_url_search_fallback_does_not_block(self) -> None:
        p = build_preflight("查一下今天的金价")
        assert p.entry_suggestion is not None
        assert p.entry_suggestion.source == "search_fallback"
        assert p.resolved_start_url.startswith("https://")
        assert all(r.code != "no_entry_point" for r in p.blocking)
        assert not p.needs_human

    def test_no_url_llm_suggestion(self) -> None:
        p = build_preflight("在淘宝买机械键盘", llm=lambda _p: "https://www.taobao.com")
        assert p.entry_suggestion is not None
        assert p.entry_suggestion.source == "llm"
        assert p.resolved_start_url == "https://www.taobao.com"
        assert not p.needs_human


class TestBlockingClarifications:
    def test_unknown_attachment_blocks(self, tmp_path: Path) -> None:
        f = tmp_path / "weird.bin"
        f.write_bytes(b"\x00\x01\x02\x03")
        p = build_preflight(
            "处理这个", start_url="https://a.com", upload_file=str(f),
        )
        assert p.needs_human
        assert any(r.code == "attachment_intent_unknown" for r in p.blocking)
        assert p.blocking_reason().strip()

    def test_empty_attachment_is_warning_not_block(self, tmp_path: Path) -> None:
        f = tmp_path / "note.txt"
        f.write_text("", encoding="utf-8")  # size 0
        p = build_preflight(
            "读取这个", start_url="https://a.com", upload_file=str(f),
        )
        assert not p.needs_human
        assert any(r.code == "attachment_empty" for r in p.warnings)


class TestClean:
    def test_clean_contract_no_human(self) -> None:
        p = build_preflight("抓取列表", start_url="https://a.com")
        assert not p.needs_human
        assert p.warnings == []
        assert p.contract.goal == "抓取列表"
