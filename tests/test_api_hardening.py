from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def api():
    import api_server

    return api_server


class TestCorsHardening:
    def test_default_has_no_wildcard_but_keeps_credentials(self, api) -> None:
        kw = api._build_cors_kwargs("")
        assert "*" not in kw["allow_origins"]
        assert kw["allow_credentials"] is True
        assert "http://localhost:5173" in kw["allow_origins"]

    def test_explicit_allowlist_is_parsed(self, api) -> None:
        kw = api._build_cors_kwargs("https://a.example , https://b.example")
        assert kw["allow_origins"] == ["https://a.example", "https://b.example"]
        assert kw["allow_credentials"] is True

    def test_wildcard_optin_forces_credentials_off(self, api) -> None:
        # Browsers reject Access-Control-Allow-Origin "*" together with credentials,
        # so the wildcard opt-in must never ship credentials.
        kw = api._build_cors_kwargs("*")
        assert kw["allow_origins"] == ["*"]
        assert kw["allow_credentials"] is False


class TestBindHardening:
    def test_default_binds_localhost(self, api, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VSPIDER_API_HOST", raising=False)
        monkeypatch.delenv("VSPIDER_API_PORT", raising=False)
        assert api._resolve_api_bind() == ("127.0.0.1", 8000)

    def test_env_overrides_host_and_port(self, api, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VSPIDER_API_HOST", "0.0.0.0")
        monkeypatch.setenv("VSPIDER_API_PORT", "9001")
        assert api._resolve_api_bind() == ("0.0.0.0", 9001)

    def test_bad_port_falls_back_to_default(self, api, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VSPIDER_API_PORT", "not-a-port")
        _, port = api._resolve_api_bind()
        assert port == 8000


class TestQueueStateSecretScrub:
    def test_execution_queue_masks_api_keys(self) -> None:
        from visual_web_agent import queue_state

        snap = queue_state.sanitize_snapshot({
            "queue": [{
                "task_id": "t1",
                "status": "queued",
                "vlm_options": {"api_key": "secret", "semantic_api_key": "s2", "model": "gpt"},
            }],
        })
        opts = snap["execution_queue"][0]["vlm_options"]
        assert opts["api_key"] == "***"
        assert opts["semantic_api_key"] == "***"
        assert opts["model"] == "gpt"

    def test_raw_secret_never_serialized(self, tmp_path) -> None:
        from visual_web_agent import queue_state

        state_path = tmp_path / "state.json"
        queue_state.save_snapshot(
            {
                "queue": [{
                    "task_id": "t1",
                    "status": "queued",
                    "vlm_options": {"api_key": "super-secret-token"},
                }],
            },
            base_dir=tmp_path,
        )
        # save_snapshot writes to queue_root(base_dir)/state.json
        written = (queue_state.queue_state_path(tmp_path)).read_text(encoding="utf-8")
        assert "super-secret-token" not in written
        assert "***" in written

    def test_empty_api_key_not_masked(self) -> None:
        from visual_web_agent import queue_state

        snap = queue_state.sanitize_snapshot({
            "queue": [{"task_id": "t1", "status": "queued", "vlm_options": {"api_key": ""}}],
        })
        opts = snap["execution_queue"][0]["vlm_options"]
        assert opts.get("api_key", "") == ""


class TestWsOriginGuard:
    def test_default_allows_localhost_blocks_evil(self, api, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VSPIDER_CORS_ORIGINS", raising=False)
        assert api._ws_origin_allowed("http://localhost:5173") is True
        assert api._ws_origin_allowed("http://127.0.0.1:8000") is True
        assert api._ws_origin_allowed("http://evil.example") is False

    def test_missing_origin_allowed(self, api, monkeypatch: pytest.MonkeyPatch) -> None:
        # non-browser clients (tests / native ws) send no Origin header
        monkeypatch.delenv("VSPIDER_CORS_ORIGINS", raising=False)
        assert api._ws_origin_allowed("") is True

    def test_wildcard_env_allows_any(self, api, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VSPIDER_CORS_ORIGINS", "*")
        assert api._ws_origin_allowed("http://evil.example") is True

    def test_explicit_allowlist_env(self, api, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VSPIDER_CORS_ORIGINS", "https://app.example")
        assert api._ws_origin_allowed("https://app.example") is True
        assert api._ws_origin_allowed("http://localhost:5173") is False

    def test_ws_logs_wiring_pinned(self) -> None:
        # Pin the wiring so a refactor cannot silently drop the origin gate.
        src = Path("api_server.py").read_text(encoding="utf-8")
        assert "_ws_origin_allowed(origin)" in src
        assert "websocket.close(code=1008)" in src


class TestTrustedHostGuard:
    def test_default_allows_local_and_testserver(self, api, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VSPIDER_ALLOWED_HOSTS", raising=False)
        hosts = api._build_trusted_hosts("")
        for expected in ("localhost", "127.0.0.1", "::1", "testserver"):
            assert expected in hosts
        assert "*" not in hosts

    def test_wildcard_optin_disables_check(self, api) -> None:
        assert api._build_trusted_hosts("*") == ["*"]

    def test_explicit_allowlist_parsed(self, api) -> None:
        hosts = api._build_trusted_hosts("vspider.lan , 10.0.0.5")
        assert hosts == ["vspider.lan", "10.0.0.5"]

    def test_rejects_dns_rebinding_host(self, api) -> None:
        # TrustedHost runs before routing: a forged Host header (DNS rebinding
        # lands "attacker.com" on 127.0.0.1) must be rejected with 400 while a
        # legit local Host reaches routing (404 for an unknown path).
        from fastapi.testclient import TestClient

        client = TestClient(api.app)
        ok = client.get("/__host_guard_probe__")
        assert ok.status_code == 404
        evil = client.get("/__host_guard_probe__", headers={"Host": "evil.example"})
        assert evil.status_code == 400
