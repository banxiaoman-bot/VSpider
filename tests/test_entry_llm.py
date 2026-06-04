"""Unit tests for ``visual_web_agent.io_contract.entry_llm``.

mission §一-B #2: 当 urls 缺失时，planner(semantic LLM) 应从 goal 推断候选
入口。``suggest_entry_url(llm=...)`` 已支持注入一个 ``prompt -> text`` 同步
回调；但 vlm_client 的 semantic 客户端是 *async* 的，无法直接喂给同步的
preflight。本模块提供一个用 semantic 配置构建的 **同步** 单次补全回调，
作为 ``build_preflight(llm=...)`` 的注入体，把语义入口推断真正接进主入口。

测试用伪 ``openai.OpenAI`` 客户端，零网络。
"""

from __future__ import annotations

import pytest


def _install_fake_openai(monkeypatch: pytest.MonkeyPatch, *, reply: str, capture: dict) -> None:
    openai = pytest.importorskip("openai")

    class _Msg:
        content = reply

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    class _Completions:
        def create(self, **kwargs):
            capture["create_kwargs"] = kwargs
            return _Resp()

    class _Chat:
        completions = _Completions()

    class _FakeOpenAI:
        def __init__(self, **kwargs):
            capture["init_kwargs"] = kwargs
            self.chat = _Chat()

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI, raising=True)


class TestMakeEntryLlm:
    def test_none_without_model(self) -> None:
        from visual_web_agent.io_contract.entry_llm import make_entry_llm

        assert make_entry_llm(model="") is None
        assert make_entry_llm(model="   ") is None

    def test_returns_callable_that_completes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        capture: dict = {}
        _install_fake_openai(monkeypatch, reply="https://www.taobao.com", capture=capture)
        from visual_web_agent.io_contract.entry_llm import make_entry_llm

        llm = make_entry_llm(api_base="http://sem", api_key="sk-x", model="qwen")
        assert callable(llm)
        out = llm("帮我在淘宝买机械键盘最合适的起始站是？")
        assert out == "https://www.taobao.com"
        # the model name flows through to the completion call
        assert capture["create_kwargs"]["model"] == "qwen"
        assert capture["init_kwargs"]["base_url"] == "http://sem"

    def test_callable_swallows_errors_to_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        openai = pytest.importorskip("openai")

        class _Boom:
            def __init__(self, **kwargs):
                pass

            class chat:
                class completions:
                    @staticmethod
                    def create(**kwargs):
                        raise RuntimeError("semantic endpoint down")

        monkeypatch.setattr(openai, "OpenAI", _Boom, raising=True)
        from visual_web_agent.io_contract.entry_llm import make_entry_llm

        llm = make_entry_llm(model="qwen")
        assert callable(llm)
        assert llm("anything") == ""

    def test_feeds_suggest_entry_url_as_llm_source(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The callable plugs straight into suggest_entry_url; a returned URL
        is surfaced as an ``llm``-sourced entry suggestion (not the Bing
        search fallback)."""
        capture: dict = {}
        _install_fake_openai(monkeypatch, reply="https://www.zhihu.com", capture=capture)
        from visual_web_agent.io_contract.entry_llm import make_entry_llm
        from visual_web_agent.io_contract import suggest_entry_url

        llm = make_entry_llm(model="qwen")
        sug = suggest_entry_url("到知乎上找答案", llm=llm)
        assert sug is not None
        assert sug.source == "llm"
        assert sug.url == "https://www.zhihu.com"


class TestEntryLlmFromConfig:
    def test_none_when_config_has_no_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = pytest.importorskip("visual_web_agent.config")
        monkeypatch.setattr(cfg, "VLM_SEMANTIC_MODEL_NAME", "", raising=False)
        monkeypatch.setattr(cfg, "VLM_MODEL_NAME", "", raising=False)
        from visual_web_agent.io_contract.entry_llm import entry_llm_from_config

        assert entry_llm_from_config() is None

    def test_builds_from_semantic_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = pytest.importorskip("visual_web_agent.config")
        capture: dict = {}
        _install_fake_openai(monkeypatch, reply="https://example.com", capture=capture)
        monkeypatch.setattr(cfg, "VLM_SEMANTIC_MODEL_NAME", "sem-model", raising=False)
        monkeypatch.setattr(cfg, "VLM_SEMANTIC_API_BASE", "http://sem-base", raising=False)
        monkeypatch.setattr(cfg, "VLM_SEMANTIC_API_KEY", "sk-sem", raising=False)
        from visual_web_agent.io_contract.entry_llm import entry_llm_from_config

        llm = entry_llm_from_config()
        assert callable(llm)
        assert llm("pick a site") == "https://example.com"
        assert capture["create_kwargs"]["model"] == "sem-model"
        assert capture["init_kwargs"]["base_url"] == "http://sem-base"
