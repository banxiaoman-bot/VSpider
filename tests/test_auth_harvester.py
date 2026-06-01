"""Tests for the post-ask_human storage_state auto-harvester.

The contract (see ``visual_web_agent/auth_harvester.py`` docstring):

  - Filter ``context.storage_state()`` to cookies/origins matching the
    active host before writing — never smear unrelated cookies in.
  - Skip when the filtered count is below MIN_COOKIES_TO_SAVE (login
    incomplete or no relevant auth on the page).
  - Refuse to overwrite an existing profile that has MORE matching
    cookies than the new harvest (don't degrade hand-curated profiles).
  - Auto-name profile from host: ``yiyan.baidu.com`` → ``yiyan_baidu_com``.
  - Never raise — every failure mode returns a HarvestResult with
    ``saved=False`` and a ``reason``.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from visual_web_agent.auth_harvester import (
    HarvestResult,
    MIN_COOKIES_TO_SAVE,
    _derive_profile_name,
    _filter_state_to_host,
    harvest_storage_state,
)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def tmp_auth_dir():
    """Project-local temp dir; avoids pytest tmp_path's %TEMP% permission
    issues on some Windows setups."""
    base = Path(__file__).resolve().parents[1] / "workspace" / "test_auth_harvester_tmp"
    base.mkdir(parents=True, exist_ok=True)
    d = Path(tempfile.mkdtemp(prefix="ah_", dir=str(base)))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _state(*, cookies: list[dict] | None = None, origins: list[dict] | None = None) -> dict:
    return {"cookies": list(cookies or []), "origins": list(origins or [])}


class _FakeContext:
    def __init__(self, state: dict) -> None:
        self._state = state
        self.storage_state = AsyncMock(return_value=state)


# ── Name derivation ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "host,expected",
    [
        ("yiyan.baidu.com", "yiyan_baidu_com"),
        ("chat.openai.com", "chat_openai_com"),
        ("www.zhihu.com", "zhihu_com"),
        ("passport.bilibili.com", "bilibili_com"),
        ("login.example.com", "example_com"),
        ("auth.taobao.com", "taobao_com"),
        ("ChatGPT.com", "chatgpt_com"),  # lowercased
        ("", ""),
        ("   ", ""),
    ],
)
def test_derive_profile_name(host: str, expected: str) -> None:
    assert _derive_profile_name(host) == expected


# ── Host filtering ──────────────────────────────────────────────────────────


def test_filter_keeps_exact_host_cookies() -> None:
    state = _state(
        cookies=[
            {"name": "session", "domain": "yiyan.baidu.com", "value": "abc"},
            {"name": "intruder", "domain": ".other.com", "value": "x"},
        ]
    )
    out, c, o = _filter_state_to_host(state, "yiyan.baidu.com")
    assert c == 1 and o == 0
    assert out["cookies"][0]["name"] == "session"


def test_filter_keeps_parent_domain_cookies() -> None:
    """A cookie set on .baidu.com matches yiyan.baidu.com (subdomain)."""
    state = _state(
        cookies=[
            {"name": "BAIDUID", "domain": ".baidu.com", "value": "x"},
            {"name": "session", "domain": "yiyan.baidu.com", "value": "y"},
            {"name": "outsider", "domain": ".other.com", "value": "z"},
        ]
    )
    out, c, _ = _filter_state_to_host(state, "yiyan.baidu.com")
    names = {ck["name"] for ck in out["cookies"]}
    assert names == {"BAIDUID", "session"}
    assert c == 2


def test_filter_keeps_origin_matching_host() -> None:
    state = _state(
        origins=[
            {"origin": "https://yiyan.baidu.com",
             "localStorage": [{"name": "k1", "value": "v1"}]},
            {"origin": "https://other.com",
             "localStorage": [{"name": "k2", "value": "v2"}]},
        ]
    )
    out, _, o = _filter_state_to_host(state, "yiyan.baidu.com")
    assert o == 1
    assert out["origins"][0]["origin"] == "https://yiyan.baidu.com"


def test_filter_empty_host_returns_empty() -> None:
    out, c, o = _filter_state_to_host(
        _state(cookies=[{"name": "x", "domain": ".any.com"}]), ""
    )
    assert c == 0 and o == 0


# ── End-to-end harvest_storage_state ────────────────────────────────────────


def test_harvest_writes_filtered_profile(tmp_auth_dir: Path) -> None:
    ctx = _FakeContext(_state(
        cookies=[
            {"name": "BAIDUID", "domain": ".baidu.com", "value": "x"},
            {"name": "session", "domain": "yiyan.baidu.com", "value": "y"},
            {"name": "unrelated", "domain": ".other.com", "value": "z"},
        ],
        origins=[
            {"origin": "https://yiyan.baidu.com",
             "localStorage": [{"name": "k", "value": "v"}]},
            {"origin": "https://other.com", "localStorage": []},
        ],
    ))
    result = _run(harvest_storage_state(
        ctx, hint_host="yiyan.baidu.com", auth_dir=tmp_auth_dir,
    ))
    assert result.saved is True
    assert result.cookies_saved == 2
    assert result.origins_saved == 1
    assert result.profile_name == "yiyan_baidu_com"
    written = json.loads((tmp_auth_dir / "yiyan_baidu_com.json").read_text("utf-8"))
    cookies = written["cookies"]
    assert {c["name"] for c in cookies} == {"BAIDUID", "session"}
    # No leak from .other.com
    assert all("other.com" not in c["domain"] for c in cookies)


def test_harvest_skips_below_min_cookies(tmp_auth_dir: Path) -> None:
    ctx = _FakeContext(_state(cookies=[
        {"name": "irrelevant", "domain": ".other.com", "value": "x"},
    ]))
    result = _run(harvest_storage_state(
        ctx, hint_host="yiyan.baidu.com", auth_dir=tmp_auth_dir,
    ))
    assert result.saved is False
    assert "below" in result.reason or "MIN_COOKIES" in result.reason
    assert not list(tmp_auth_dir.glob("*.json"))


def test_harvest_skips_empty_host(tmp_auth_dir: Path) -> None:
    ctx = _FakeContext(_state(cookies=[
        {"name": "x", "domain": ".baidu.com", "value": "y"},
    ]))
    result = _run(harvest_storage_state(
        ctx, hint_host="", auth_dir=tmp_auth_dir,
    ))
    assert result.saved is False
    assert "host" in result.reason.lower()


def test_harvest_refuses_to_downgrade_existing_profile(tmp_auth_dir: Path) -> None:
    # Pre-existing rich profile (5 matching cookies)
    rich = _state(cookies=[
        {"name": f"c{i}", "domain": ".baidu.com", "value": str(i)} for i in range(5)
    ])
    (tmp_auth_dir / "yiyan_baidu_com.json").write_text(
        json.dumps(rich, ensure_ascii=False), encoding="utf-8"
    )
    # New harvest only has 1 cookie
    ctx = _FakeContext(_state(cookies=[
        {"name": "c0", "domain": ".baidu.com", "value": "0"},
    ]))
    result = _run(harvest_storage_state(
        ctx, hint_host="yiyan.baidu.com", auth_dir=tmp_auth_dir,
    ))
    assert result.saved is False
    assert "downgrade" in result.reason.lower()
    # Existing file untouched
    untouched = json.loads((tmp_auth_dir / "yiyan_baidu_com.json").read_text("utf-8"))
    assert len(untouched["cookies"]) == 5


def test_harvest_upgrades_existing_profile(tmp_auth_dir: Path) -> None:
    # Pre-existing profile with 1 matching cookie
    (tmp_auth_dir / "yiyan_baidu_com.json").write_text(json.dumps(_state(
        cookies=[{"name": "old", "domain": ".baidu.com", "value": "x"}]
    ), ensure_ascii=False), encoding="utf-8")
    # Fresh harvest with 3 cookies → richer
    ctx = _FakeContext(_state(cookies=[
        {"name": "new1", "domain": ".baidu.com", "value": "a"},
        {"name": "new2", "domain": "yiyan.baidu.com", "value": "b"},
        {"name": "new3", "domain": ".baidu.com", "value": "c"},
    ]))
    result = _run(harvest_storage_state(
        ctx, hint_host="yiyan.baidu.com", auth_dir=tmp_auth_dir,
    ))
    assert result.saved is True
    assert result.cookies_saved == 3
    written = json.loads((tmp_auth_dir / "yiyan_baidu_com.json").read_text("utf-8"))
    names = {c["name"] for c in written["cookies"]}
    assert names == {"new1", "new2", "new3"}  # old replaced, not merged


def test_harvest_handles_storage_state_failure(tmp_auth_dir: Path) -> None:
    """If context.storage_state() raises, return saved=False with the error."""
    class _BrokenContext:
        async def storage_state(self):
            raise RuntimeError("CDP closed")
    result = _run(harvest_storage_state(
        _BrokenContext(), hint_host="yiyan.baidu.com", auth_dir=tmp_auth_dir,
    ))
    assert result.saved is False
    assert "RuntimeError" in result.reason
    assert not list(tmp_auth_dir.glob("*.json"))


def test_harvest_explicit_profile_name_override(tmp_auth_dir: Path) -> None:
    ctx = _FakeContext(_state(cookies=[
        {"name": "k", "domain": ".baidu.com", "value": "v"},
    ]))
    result = _run(harvest_storage_state(
        ctx, hint_host="yiyan.baidu.com",
        profile_name="my_yiyan_alt", auth_dir=tmp_auth_dir,
    ))
    assert result.saved is True
    assert result.profile_name == "my_yiyan_alt"
    assert (tmp_auth_dir / "my_yiyan_alt.json").exists()


def test_harvest_explicit_name_with_json_suffix(tmp_auth_dir: Path) -> None:
    ctx = _FakeContext(_state(cookies=[
        {"name": "k", "domain": ".baidu.com", "value": "v"},
    ]))
    result = _run(harvest_storage_state(
        ctx, hint_host="yiyan.baidu.com",
        profile_name="my_alt.json", auth_dir=tmp_auth_dir,
    ))
    assert result.saved is True
    assert (tmp_auth_dir / "my_alt.json").exists()


def test_harvest_creates_auth_dir_if_missing(tmp_auth_dir: Path) -> None:
    new_dir = tmp_auth_dir / "deep" / "nested" / "auth"
    ctx = _FakeContext(_state(cookies=[
        {"name": "k", "domain": ".baidu.com", "value": "v"},
    ]))
    result = _run(harvest_storage_state(
        ctx, hint_host="yiyan.baidu.com", auth_dir=new_dir,
    ))
    assert result.saved is True
    assert new_dir.is_dir()
    assert (new_dir / "yiyan_baidu_com.json").exists()


def test_harvest_result_is_serialisable() -> None:
    """to_dict() must produce a JSON-safe object (used by event_stream)."""
    r = HarvestResult(
        saved=True, path=Path("a.json"), profile_name="foo",
        cookies_saved=3, origins_saved=1, reason="ok",
    )
    d = r.to_dict()
    # Path serialised as str
    assert isinstance(d["path"], str)
    json.dumps(d, ensure_ascii=False)  # must not raise


def test_harvest_constants_are_sane() -> None:
    assert MIN_COOKIES_TO_SAVE >= 1
