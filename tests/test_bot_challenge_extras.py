"""Tests for cf_clearance helpers and run constraint wiring."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from visual_web_agent.auth_harvester import cf_clearance_profile_hint, inspect_cf_clearance
from visual_web_agent.config import PROXY_SERVER, apply_run_constraints
from visual_web_agent.captcha_solver import solver_enabled
from visual_web_agent.io_contract.input_contract import build_input_contract


def test_inspect_cf_clearance_with_expiry() -> None:
    state = {
        "cookies": [{
            "name": "cf_clearance",
            "domain": ".example.com",
            "expires": 9999999999,
        }]
    }
    has_cf, hours = inspect_cf_clearance(state, "www.example.com")
    assert has_cf is True
    assert hours is not None
    assert hours > 0


def test_cf_clearance_profile_hint() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        auth_dir = Path(tmp)
        profile = auth_dir / "example_com.json"
        profile.write_text(json.dumps({
            "cookies": [{
                "name": "cf_clearance",
                "domain": ".example.com",
                "expires": 9999999999,
            }],
            "origins": [],
        }), encoding="utf-8")
        hint = cf_clearance_profile_hint(host="example.com", auth_dir=auth_dir)
        assert "cf_clearance" in hint


def test_build_input_contract_proxy_fields() -> None:
    contract = build_input_contract(
        goal="test",
        target_url="https://example.com",
        constraints={
            "proxy_server": "http://127.0.0.1:7890",
            "proxy_username": "u",
            "proxy_password": "p",
        },
    )
    assert contract.constraints.proxy_server == "http://127.0.0.1:7890"
    assert contract.constraints.proxy_username == "u"


def test_apply_run_constraints() -> None:
    before = PROXY_SERVER
    try:
        apply_run_constraints({"proxy_server": "http://proxy.test:8080"})
        from visual_web_agent import config as cfg

        assert cfg.PROXY_SERVER == "http://proxy.test:8080"
    finally:
        from visual_web_agent import config as cfg

        cfg.PROXY_SERVER = before


def test_solver_disabled_without_api_key(monkeypatch) -> None:
    monkeypatch.delenv("VSPIDER_CAPTCHA_API_KEY", raising=False)
    assert solver_enabled() is False
