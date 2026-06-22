"""MC-1: model_config_store 单元测试（spec §9.1-5）。

通过 monkeypatch 模块级 MODEL_CONFIG_PATH 指向 tmp_path 隔离磁盘。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import model_config_store as store


@pytest.fixture()
def cfg_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "model_config.json"
    monkeypatch.setattr(store, "MODEL_CONFIG_PATH", p)
    return p


class TestLoad:
    def test_missing_file_returns_skeleton_no_raise(self, cfg_path: Path) -> None:
        cfg = store.load_model_config()
        assert cfg["version"] == 1
        assert cfg["vlm"]["base_url"] == ""
        assert cfg["semantic"]["model"] == ""

    def test_corrupt_file_returns_skeleton(self, cfg_path: Path) -> None:
        cfg_path.write_text("{not valid json", encoding="utf-8")
        cfg = store.load_model_config()
        assert cfg["vlm"]["api_key"] == ""


class TestSaveLoadRoundTrip:
    def test_roundtrip_and_updated_at(self, cfg_path: Path) -> None:
        store.save_model_config({
            "vlm": {"base_url": "https://vlm/v1", "api_key": "k1", "model": "m1"},
            "semantic": {"base_url": "https://sem/v1", "model": "s1"},
        })
        cfg = store.load_model_config()
        assert cfg["vlm"]["base_url"] == "https://vlm/v1"
        assert cfg["vlm"]["api_key"] == "k1"
        assert cfg["semantic"]["model"] == "s1"
        assert cfg["updated_at"]  # ISO timestamp written

    def test_blank_api_key_preserves_old(self, cfg_path: Path) -> None:
        store.save_model_config({"vlm": {"api_key": "k1"}})
        store.save_model_config({"vlm": {"api_key": "", "model": "m2"}})
        cfg = store.load_model_config()
        assert cfg["vlm"]["api_key"] == "k1"   # preserved
        assert cfg["vlm"]["model"] == "m2"     # updated
        store.save_model_config({"vlm": {"api_key": "k2"}})
        assert store.load_model_config()["vlm"]["api_key"] == "k2"  # explicit overwrite


class TestMasked:
    def test_masks_key_and_flags(self, cfg_path: Path) -> None:
        store.save_model_config({"vlm": {"api_key": "sk-abcdefgh1234"}})
        masked = store.masked_config()
        assert masked["vlm"]["has_api_key"] is True
        assert masked["vlm"]["api_key"] == "sk-****1234"
        assert "abcdefgh" not in json.dumps(masked)  # no plaintext leak

    def test_no_key_flags_false_empty_mask(self, cfg_path: Path) -> None:
        masked = store.masked_config()
        assert masked["vlm"]["has_api_key"] is False
        assert masked["vlm"]["api_key"] == ""


class TestResolvePrecedence:
    def test_form_over_json_over_env_by_omission(self, cfg_path: Path) -> None:
        # json layer present (file exists)
        store.save_model_config({
            "vlm": {"model": "json-model", "base_url": "https://json/v1"},
        })
        resolved = store.resolve_vlm_options({"model": "form-model"})
        assert resolved["model"] == "form-model"         # tier1 form wins
        assert resolved["base_url"] == "https://json/v1"  # tier2 json fills
        assert "api_key" not in resolved                  # tier3: blank both -> omitted -> .env downstream

    def test_no_file_returns_form_only(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(store, "MODEL_CONFIG_PATH", tmp_path / "absent.json")
        resolved = store.resolve_vlm_options({"model": "form-model", "base_url": ""})
        assert resolved == {"model": "form-model"}        # no json injected, blanks dropped
