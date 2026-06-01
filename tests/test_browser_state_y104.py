from __future__ import annotations

import asyncio

from visual_web_agent.browser_state import (
    BrowserStateSnapshot,
    build_browser_state_v2,
    build_browser_state_v2_from_browser,
)


class _FakePage:
    url = "https://example.com/list"

    async def title(self) -> str:
        return "Example List"

    async def evaluate(self, _script: str) -> str:
        return "Visible row one Visible row two"


class _FakeBrowser:
    current_url = "https://fallback.example"

    def __init__(self) -> None:
        self._page = _FakePage()
        self._last_som_elements = [
            {
                "id": 3,
                "role": "button",
                "name": "Next",
                "state": "enabled",
                "rect": {"x": 10, "y": 20, "width": 80, "height": 30},
            }
        ]
        self.element_mapping = {
            "@e3": {"selector": "button.next", "role": "button", "name": "Next"}
        }

    async def _ensure_active_page(self, reason: str = "") -> _FakePage:
        return self._page

    async def get_tabs_state(self) -> str:
        return "[active] Example List"

    async def get_active_page_summary(self) -> str:
        return "list page with next button"


def test_build_browser_state_v2_unifies_page_perception_interaction_and_data() -> None:
    snapshot = BrowserStateSnapshot(
        step=4,
        url="https://example.com",
        title="Example",
        screenshot_path="screenshots/step_04.png",
        ax_tree_excerpt="button Next",
        ax_line_count=1,
        interactive_count=2,
        dom_shape={"tables": 1, "links": 5},
        visible_text_excerpt="row one row two",
        tabs="[active] Example",
        page_summary="structured listing",
        last_action_result={"success": True, "action": "snapshot"},
        metadata={"run_id": "r1"},
    )
    state = build_browser_state_v2(
        snapshot,
        som_elements=[
            {
                "id": 7,
                "tag": "button",
                "text": "Next",
                "rect": {"x": 1, "y": 2, "width": 3, "height": 4},
            }
        ],
        network_candidates=[{"url": f"/api/{i}"} for i in range(60)],
        runtime_status={"status": "available"},
        extraction={"row_count": 2, "required_fields": ["title"]},
    )

    assert state["version"] == "browser_state.v2"
    assert state["page"]["url"] == "https://example.com"
    assert state["artifacts"]["screenshot_path"] == "screenshots/step_04.png"
    assert state["perception"]["dom_shape"]["tables"] == 1
    assert state["perception"]["som"]["elements"][0]["ref"] == "@e7"
    assert state["perception"]["som"]["elements"][0]["bbox"]["width"] == 3
    assert state["interaction"]["last_action_result"]["action"] == "snapshot"
    assert state["runtime"]["status"] == "available"
    assert state["data"]["network_candidate_count"] == 50
    assert state["data"]["extraction"]["row_count"] == 2
    assert state["metrics"] == {
        "interactive_count": 2,
        "ax_line_count": 1,
        "network_candidate_count": 50,
        "action_ref_count": 0,
    }


def test_build_browser_state_v2_from_browser_uses_cached_som_and_action_refs() -> None:
    state = asyncio.run(build_browser_state_v2_from_browser(
        _FakeBrowser(),
        step=1,
        screenshot_path="screenshots/step_01.png",
        ax_tree_text="@e3 button Next",
        dom_shape={"buttons": 1},
        runtime_status={"status": "available"},
    ))

    assert state["version"] == "browser_state.v2"
    assert state["page"]["url"] == "https://example.com/list"
    assert state["page"]["title"] == "Example List"
    assert state["page"]["summary"] == "list page with next button"
    assert state["perception"]["visible_text_excerpt"] == "Visible row one Visible row two"
    assert state["perception"]["som"]["interactive_count"] == 1
    assert state["perception"]["som"]["elements"][0]["ref"] == "@e3"
    assert state["interaction"]["action_refs"][0]["selector"] == "button.next"
    assert state["navigation"]["tabs"] == "[active] Example List"
    assert state["metrics"]["action_ref_count"] == 1
