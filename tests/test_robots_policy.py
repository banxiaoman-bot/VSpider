from __future__ import annotations

from fastapi.testclient import TestClient

from visual_web_agent.robots_policy import RobotsPolicyManager, delay_seconds, is_allowed, parse_robots


def test_parse_robots_allow_disallow_and_crawl_delay() -> None:
    rules = parse_robots(
        """
        User-agent: *
        Disallow: /private
        Allow: /private/public
        Crawl-delay: 2.5
        """
    )

    assert is_allowed("/", rules) is True
    assert is_allowed("/private/page", rules) is False
    assert is_allowed("/private/public/page", rules) is True
    assert delay_seconds(rules) == 2.5


def test_parse_robots_request_rate_fallback_delay() -> None:
    rules = parse_robots(
        """
        User-agent: *
        Request-rate: 4/8
        """
    )

    assert rules.request_rate == (4, 8.0)
    assert delay_seconds(rules) == 2.0


def test_robots_policy_manager_check_and_reserve() -> None:
    manager = RobotsPolicyManager()
    manager.set_robots(
        "example.com",
        """
        User-agent: *
        Disallow: /blocked
        Crawl-delay: 3
        """,
    )

    allowed = manager.check_url("https://example.com/open", now=100.0)
    blocked = manager.check_url("https://example.com/blocked/page", now=100.0)
    reserved = manager.reserve_url("https://example.com/open", now=100.0)
    throttled = manager.check_url("https://example.com/next", now=101.0)

    assert allowed["allowed"] is True
    assert blocked["allowed_by_robots"] is False
    assert reserved["reserved"] is True
    assert reserved["delay_seconds"] == 3
    assert throttled["throttled"] is True
    assert throttled["wait_seconds"] == 2.0


def test_robots_policy_api_wiring(monkeypatch) -> None:
    import api_server

    monkeypatch.setattr(api_server, "_robots_policy", RobotsPolicyManager())
    client = TestClient(api_server.app)

    set_resp = client.post(
        "/api/robots/set",
        json={
            "domain": "example.com",
            "robots_txt": "User-agent: *\nDisallow: /private\nCrawl-delay: 1",
        },
    )
    check_allowed = client.post("/api/robots/check", json={"url": "https://example.com/"})
    check_blocked = client.post("/api/robots/check", json={"url": "https://example.com/private/a"})
    reserve = client.post("/api/robots/reserve", json={"url": "https://example.com/"})
    rules = client.get("/api/robots/example.com")

    assert set_resp.status_code == 200
    assert set_resp.json()["result"]["crawl_delay"] == 1.0
    assert check_allowed.json()["result"]["allowed_by_robots"] is True
    assert check_blocked.json()["result"]["allowed_by_robots"] is False
    assert reserve.json()["result"]["reserved"] is True
    assert rules.json()["result"]["domain"] == "example.com"


def test_robots_policy_source_wiring() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    api_src = (root / "api_server.py").read_text(encoding="utf-8")
    policy_src = (root / "visual_web_agent" / "robots_policy.py").read_text(encoding="utf-8")

    assert "from visual_web_agent.robots_policy import RobotsPolicyManager" in api_src
    assert "_robots_policy = RobotsPolicyManager()" in api_src
    assert '@app.post("/api/robots/set"' in api_src
    assert '@app.post("/api/robots/check"' in api_src
    assert '@app.post("/api/robots/reserve"' in api_src
    assert '@app.get("/api/robots/{domain}"' in api_src
    assert "class RobotsPolicyManager:" in policy_src
    assert "def parse_robots(" in policy_src
    assert "def is_allowed(" in policy_src
