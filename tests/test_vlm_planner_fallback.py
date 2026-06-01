import asyncio
import json

from visual_web_agent.vlm_client import VLMClient


class _Message:
    def __init__(self, content: str):
        self.content = content


class _Choice:
    def __init__(self, content: str):
        self.message = _Message(content)


class _Response:
    def __init__(self, content: str):
        self.choices = [_Choice(content)]


class _Completions:
    def __init__(self, *, content: str | None = None, exc: Exception | None = None):
        self.content = content
        self.exc = exc
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        if self.exc:
            raise self.exc
        return _Response(self.content or "{}")


class _Chat:
    def __init__(self, completions: _Completions):
        self.completions = completions


class _Client:
    def __init__(self, completions: _Completions):
        self.chat = _Chat(completions)


def _run(coro):
    return asyncio.run(coro)


def test_generate_plan_retries_primary_client_when_semantic_key_is_invalid() -> None:
    plan_json = json.dumps(
        {
            "goal": "在必应搜索Python教程，然后切回搜索页。",
            "sub_goals": [
                {
                    "id": 1,
                    "description": "搜索Python教程",
                    "exit_criteria": "Bing结果页已加载",
                    "status": "active",
                },
                {
                    "id": 2,
                    "description": "点击第一个结果并新开标签",
                    "exit_criteria": "结果页在新标签打开",
                    "status": "pending",
                },
            ],
            "current_idx": 0,
        },
        ensure_ascii=False,
    )
    semantic_completions = _Completions(
        exc=Exception("AuthenticationError: Error code: 401 - invalid_api_key")
    )
    primary_completions = _Completions(content=plan_json)

    client = VLMClient.__new__(VLMClient)
    client.semantic_client = _Client(semantic_completions)
    client.client = _Client(primary_completions)
    client.semantic_model = "qwen-bad-key"
    client.model = "deepseek-chat"
    client.max_tokens = 4096
    client._use_structured = True

    plan = _run(client.make_plan("在必应搜索Python教程，然后切回搜索页。", "https://www.bing.com"))

    assert semantic_completions.calls == 1
    assert primary_completions.calls == 1
    assert len(plan.sub_goals) == 2
    assert plan.sub_goals[0].description == "搜索Python教程"
