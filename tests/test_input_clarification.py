"""Tests for ``visual_web_agent.io_contract.clarification``.

Pure: builds :class:`InputContract` instances and asserts which structured
clarification requests the deterministic detector raises. No FS / network.
"""

from __future__ import annotations

from visual_web_agent.io_contract import (
    build_input_contract,
    detect_input_clarifications,
    ClarificationRequest,
)


def _codes(contract) -> list[str]:
    return [r.code for r in detect_input_clarifications(contract)]


class TestNoEntryPoint:
    def test_goal_only_blocks(self) -> None:
        c = build_input_contract(goal="帮我抓点东西")
        reqs = detect_input_clarifications(c)
        codes = [r.code for r in reqs]
        assert "no_entry_point" in codes
        block = [r for r in reqs if r.code == "no_entry_point"][0]
        assert block.severity == "block"
        assert block.options  # offers the user a way forward

    def test_explicit_url_satisfies(self) -> None:
        c = build_input_contract(goal="抓取", target_url="https://a.com")
        assert "no_entry_point" not in _codes(c)

    def test_goal_extracted_url_satisfies(self) -> None:
        # goal->URL fallback already supplied an entry, so no clarification
        c = build_input_contract(goal="爬取 https://a.com 的列表数据")
        assert "no_entry_point" not in _codes(c)

    def test_attachment_only_does_not_block_entry(self) -> None:
        c = build_input_contract(
            goal="总结这份 PDF",
            attachments=[{"path": "t/a.pdf", "filename": "a.pdf",
                          "intent": "prompt_context", "size": 100}],
        )
        assert "no_entry_point" not in _codes(c)


class TestAttachmentIntentUnknown:
    def test_unknown_intent_asks(self) -> None:
        c = build_input_contract(
            goal="处理这个", target_url="https://a.com",
            attachments=[{"path": "t/x.bin", "filename": "x.bin",
                          "intent": "unknown", "size": 10}],
        )
        reqs = detect_input_clarifications(c)
        hit = [r for r in reqs if r.code == "attachment_intent_unknown"]
        assert hit
        assert "x.bin" in hit[0].message
        assert hit[0].options  # batch_rows / upload_to_page / prompt_context


class TestBatchRowsUrlColumn:
    def test_no_url_column_warns(self) -> None:
        c = build_input_contract(
            goal="逐行填表", target_url="https://a.com",
            attachments=[{"path": "t/d.xlsx", "filename": "d.xlsx",
                          "intent": "batch_rows", "size": 99,
                          "schema": {"columns": ["name", "price"]}}],
        )
        codes = _codes(c)
        assert "batch_rows_no_url_column" in codes

    def test_url_column_present_ok(self) -> None:
        c = build_input_contract(
            goal="逐行", target_url="https://a.com",
            attachments=[{"path": "t/d.xlsx", "filename": "d.xlsx",
                          "intent": "batch_rows", "size": 99,
                          "schema": {"columns": ["url", "name"]}}],
        )
        assert "batch_rows_no_url_column" not in _codes(c)

    def test_chinese_url_column_present_ok(self) -> None:
        c = build_input_contract(
            goal="逐行", target_url="https://a.com",
            attachments=[{"path": "t/d.xlsx", "filename": "d.xlsx",
                          "intent": "batch_rows", "size": 99,
                          "schema": {"columns": ["网址", "名称"]}}],
        )
        assert "batch_rows_no_url_column" not in _codes(c)


class TestEmptyAttachment:
    def test_zero_size_warns(self) -> None:
        c = build_input_contract(
            goal="读取", target_url="https://a.com",
            attachments=[{"path": "t/e.pdf", "filename": "e.pdf",
                          "intent": "prompt_context", "size": 0}],
        )
        assert "attachment_empty" in _codes(c)


class TestCleanContract:
    def test_no_clarifications(self) -> None:
        c = build_input_contract(goal="抓取列表", target_url="https://a.com")
        assert detect_input_clarifications(c) == []

    def test_request_as_reason_is_human_readable(self) -> None:
        c = build_input_contract(goal="抓点东西")
        req = detect_input_clarifications(c)[0]
        assert isinstance(req, ClarificationRequest)
        reason = req.as_reason()
        assert isinstance(reason, str) and reason.strip()
