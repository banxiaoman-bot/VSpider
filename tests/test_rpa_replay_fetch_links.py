from __future__ import annotations

import asyncio

from visual_web_agent.main import _replay_rpa


class _FakeReplayPage:
    url = "https://origin.example/"

    def __init__(self) -> None:
        self._closed = False

    def is_closed(self) -> bool:
        return self._closed


class _FakeReplayBrowser:
    def __init__(self) -> None:
        self._page = _FakeReplayPage()
        self.current_url = self._page.url
        self.calls: list[dict] = []

    async def execute_action(self, action_payload, workflow_memory=None):
        self.calls.append(dict(action_payload))
        key = action_payload.get("memory_key") or "missing"
        if workflow_memory is not None:
            if action_payload["action"] == "fetch_links_batch":
                workflow_memory[key] = [
                    {
                        "ok": True,
                        "url": "https://one.example/",
                        "title": "One",
                        "content": "First",
                    }
                ]
            else:
                workflow_memory[key] = {
                    "url": "https://one.example/",
                    "title": "One",
                    "content": "First",
                }
        return self._page


def _run(coro):
    return asyncio.run(coro)


def test_replay_dispatches_fetch_link_content_through_browser_action() -> None:
    browser = _FakeReplayBrowser()
    memory = {"seed_url": "https://article.example/"}

    ok = _run(_replay_rpa(
        browser,
        [{
            "action": "fetch_link_content",
            "type_value": "{{seed_url}}",
            "memory_key": "article",
        }],
        workflow_memory=memory,
    ))

    assert ok is True
    assert browser.calls == [{
        "progress_review": "cached fetch replay",
        "thought": "Replay cached fetch_link_content without VLM.",
        "current_state": "RPA replay",
        "action": "fetch_link_content",
        "target_id": 0,
        "type_value": "https://article.example/",
        "memory_key": "article",
        "extracted_data": None,
        "status": "pending",
    }]
    assert memory["article"]["content"] == "First"


def test_replay_dispatches_fetch_links_batch_and_preserves_json_options() -> None:
    browser = _FakeReplayBrowser()
    memory: dict = {}
    type_value = (
        '{"urls":["https://one.example/"],'
        '"mode":"ax","selectors":["article","main"],"concurrency":2}'
    )

    ok = _run(_replay_rpa(
        browser,
        [{
            "action": "fetch_links_batch",
            "type_value": type_value,
            "memory_key": "links",
        }],
        workflow_memory=memory,
    ))

    assert ok is True
    assert browser.calls[0]["action"] == "fetch_links_batch"
    assert browser.calls[0]["type_value"] == type_value
    assert browser.calls[0]["memory_key"] == "links"
    assert memory["links"][0]["title"] == "One"


def test_replay_fetch_generates_memory_key_when_cache_step_omits_it() -> None:
    browser = _FakeReplayBrowser()
    memory: dict = {}

    ok = _run(_replay_rpa(
        browser,
        [{
            "action": "fetch_link_content",
            "url": "https://article.example/",
        }],
        workflow_memory=memory,
    ))

    assert ok is True
    assert browser.calls[0]["memory_key"] == "fetched_1"
    assert memory["fetched_1"]["url"] == "https://one.example/"
