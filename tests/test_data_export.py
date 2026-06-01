"""Regression suite for the generic data-export-URL transformer registry.

This replaces the previous site-specific test_google_sheets_skill.py with
a suite that pins:

  * ``find_data_export_url`` correctly classifies + transforms URLs across
    all built-in registered hosts (Sheets / OneDrive / SharePoint)
  * Adding a new ``ExportTransform`` auto-wires it through the public API
    AND the prompt-injection gate (``_url_matches_data_export_registry``)
    — no per-host branching needed
  * The Sheets-specific shim (``sheets_url.convert_google_sheets_url_to_csv``)
    keeps working for backwards-compatibility
  * ``DATA_EXPORT_SKILL`` is registered and injected when triggered

Adding a new data source = one ``register_export_transform`` call +
one new parametrised test row here.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_VWA = Path(__file__).resolve().parents[1] / "visual_web_agent"
sys.path.insert(0, str(_VWA))

from data_export import (  # noqa: E402
    ExportTransform,
    find_data_export_url,
    list_registered_hosts,
    register_export_transform,
    _REGISTRY,
)
from prompt_skills import DATA_EXPORT_SKILL, SKILL_PROMPTS  # noqa: E402
from prompts import build_system_prompt  # noqa: E402
from sheets_url import (  # noqa: E402
    convert_google_sheets_url_to_csv,
    is_google_sheets_url,
)


# ── Test fixtures ─────────────────────────────────────────────────────────

_SHEET_ID = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms"
_SHEET_BASE = f"https://docs.google.com/spreadsheets/d/{_SHEET_ID}"

_ONEDRIVE_XLSX = (
    "https://onedrive.live.com/personal/abc/Documents/report.xlsx"
)
_SHAREPOINT_XLSX = (
    "https://contoso.sharepoint.com/sites/Marketing/Shared%20Documents/sales.xlsx"
)


# ════════════════════════════════════════════════════════════════════════════
# find_data_export_url  — public registry API
# ════════════════════════════════════════════════════════════════════════════

# ── Google Sheets via the generic API ─────────────────────────────────────
@pytest.mark.parametrize(
    "url,expected_export",
    [
        (
            f"{_SHEET_BASE}/edit?gid=42",
            f"{_SHEET_BASE}/export?format=csv&gid=42",
        ),
        (
            f"{_SHEET_BASE}/edit#gid=42",
            f"{_SHEET_BASE}/export?format=csv&gid=42",
        ),
        # User's exact URL shape
        (
            f"{_SHEET_BASE}/edit?gid=0#gid=0",
            f"{_SHEET_BASE}/export?format=csv&gid=0",
        ),
        # No gid → default 0
        (
            f"{_SHEET_BASE}/edit",
            f"{_SHEET_BASE}/export?format=csv&gid=0",
        ),
        # ?gid= with empty value → default 0
        (
            f"{_SHEET_BASE}/edit?gid=",
            f"{_SHEET_BASE}/export?format=csv&gid=0",
        ),
        # Query gid wins over fragment gid
        (
            f"{_SHEET_BASE}/edit?gid=7#gid=99",
            f"{_SHEET_BASE}/export?format=csv&gid=7",
        ),
        # Other Sheets path variants
        (
            f"{_SHEET_BASE}/preview",
            f"{_SHEET_BASE}/export?format=csv&gid=0",
        ),
        (
            f"{_SHEET_BASE}/htmlview",
            f"{_SHEET_BASE}/export?format=csv&gid=0",
        ),
        (
            f"{_SHEET_BASE}/view?gid=11&usp=sharing",
            f"{_SHEET_BASE}/export?format=csv&gid=11",
        ),
    ],
)
def test_registry_transforms_sheets_urls(url, expected_export):
    name, export = find_data_export_url(url)
    assert name == "Google Sheets"
    assert export == expected_export


def test_registry_returns_empty_for_already_export_sheets_url():
    name, export = find_data_export_url(
        f"{_SHEET_BASE}/export?format=csv&gid=0"
    )
    assert (name, export) == ("", "")


# ── OneDrive via the generic API ─────────────────────────────────────────
@pytest.mark.parametrize(
    "url",
    [
        _ONEDRIVE_XLSX,
        "https://1drv.ms/x/s!ABCdef/123/report.xlsx",
        "https://onedrive.live.com/foo/bar.csv",
        "https://onedrive.live.com/foo/bar.xlsm",
    ],
)
def test_registry_appends_download_for_onedrive_file_urls(url):
    name, export = find_data_export_url(url)
    assert name == "OneDrive"
    assert "download=1" in export


def test_onedrive_passthrough_when_download_already_set():
    name, export = find_data_export_url(
        f"{_ONEDRIVE_XLSX}?download=1"
    )
    assert (name, export) == ("", "")


def test_onedrive_does_not_fire_for_non_file_navigation_pages():
    """Plain OneDrive navigation page without a file extension → no transform."""
    name, export = find_data_export_url(
        "https://onedrive.live.com/?cid=ABC&id=root"
    )
    assert (name, export) == ("", "")


# ── SharePoint via the generic API ───────────────────────────────────────
def test_registry_appends_download_for_sharepoint_xlsx():
    name, export = find_data_export_url(_SHAREPOINT_XLSX)
    assert name == "SharePoint"
    assert export.startswith(_SHAREPOINT_XLSX)
    assert "download=1" in export


def test_registry_appends_download_for_sharepoint_xlsx_with_existing_query():
    url = _SHAREPOINT_XLSX + "?web=1"
    name, export = find_data_export_url(url)
    assert name == "SharePoint"
    assert "web=1" in export
    assert "download=1" in export


# ── Negative path ────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "url",
    [
        "",
        None,
        "not a url",
        "ftp://docs.google.com/spreadsheets/d/abc/edit",
        "https://example.com/something",
        "https://docs.google.com/document/d/1abc/edit",  # Docs, not Sheets
        "https://docs.google.com/forms/d/e/1abc/viewform",
        "https://docs.google.com/spreadsheets/",  # missing /d/{id}
    ],
)
def test_registry_returns_empty_for_unrecognised_urls(url):
    assert find_data_export_url(url) == ("", "")


# ════════════════════════════════════════════════════════════════════════════
# Registry extensibility — new transformer auto-wires through public API
# ════════════════════════════════════════════════════════════════════════════
def test_register_new_transform_picks_up_in_find_data_export_url():
    """Demonstrate the registry pattern: one register call gives a new
    host first-class support without touching any other module."""
    original_len = len(_REGISTRY)
    try:
        register_export_transform(
            ExportTransform(
                name="MyCustomTableHost",
                host_pattern=r"^reports\.example\.com$",
                transform=lambda url: url + "?fmt=csv",
            )
        )
        name, export = find_data_export_url(
            "https://reports.example.com/q/123"
        )
        assert name == "MyCustomTableHost"
        assert export.endswith("?fmt=csv")
    finally:
        # Clean up so test order doesn't matter.
        while len(_REGISTRY) > original_len:
            _REGISTRY.pop()


def test_list_registered_hosts_returns_all_built_in_patterns():
    hosts = list_registered_hosts()
    assert any("docs\\.google\\.com" in h for h in hosts)
    assert any("onedrive" in h or "1drv" in h for h in hosts)
    assert any("sharepoint" in h for h in hosts)


def test_transform_exception_does_not_break_registry():
    """A broken transformer must not poison the registry — the next
    one in line should still match if applicable."""
    original_len = len(_REGISTRY)
    try:
        def _boom(_url: str) -> str:
            raise RuntimeError("transformer crashed")

        register_export_transform(
            ExportTransform(
                name="BrokenHost",
                host_pattern=r"^crash\.example\.com$",
                transform=_boom,
            )
        )
        # Broken transformer on a matching URL → falls back to ("", "")
        name, export = find_data_export_url("https://crash.example.com/x")
        assert (name, export) == ("", "")
        # Other URLs still work via earlier-registered transformers
        name, export = find_data_export_url(
            f"{_SHEET_BASE}/edit?gid=0"
        )
        assert name == "Google Sheets"
    finally:
        while len(_REGISTRY) > original_len:
            _REGISTRY.pop()


# ════════════════════════════════════════════════════════════════════════════
# Backwards-compat shim — sheets_url module
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize(
    "url",
    [
        f"{_SHEET_BASE}/edit",
        f"{_SHEET_BASE}/edit?gid=0",
        f"{_SHEET_BASE}/edit#gid=0",
        f"{_SHEET_BASE}/preview",
        f"{_SHEET_BASE}/htmlview",
        f"{_SHEET_BASE}/export?format=csv&gid=0",  # already exported still True
    ],
)
def test_sheets_url_shim_classifier_recognises_sheets(url):
    assert is_google_sheets_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "",
        None,
        "https://example.com/spreadsheets/d/abc/edit",
        "https://docs.google.com/document/d/1abc/edit",
        "ftp://docs.google.com/spreadsheets/d/abc/edit",
    ],
)
def test_sheets_url_shim_classifier_rejects_non_sheets(url):
    assert is_google_sheets_url(url) is False


def test_sheets_url_shim_converter_matches_registry():
    """The Sheets-specific shim must produce the same output as the
    generic registry for Sheets URLs."""
    url = f"{_SHEET_BASE}/edit?gid=11"
    shim_result = convert_google_sheets_url_to_csv(url)
    _, registry_result = find_data_export_url(url)
    assert shim_result == registry_result
    assert shim_result == f"{_SHEET_BASE}/export?format=csv&gid=11"


def test_sheets_url_shim_returns_empty_for_already_export_url():
    assert convert_google_sheets_url_to_csv(
        f"{_SHEET_BASE}/export?format=csv&gid=0"
    ) == ""


# ════════════════════════════════════════════════════════════════════════════
# DATA_EXPORT_SKILL — registration + injection
# ════════════════════════════════════════════════════════════════════════════
def test_skill_is_registered_in_prompt_map():
    assert "data_export" in SKILL_PROMPTS
    assert SKILL_PROMPTS["data_export"] is DATA_EXPORT_SKILL


def test_skill_content_pins_critical_rules():
    body = DATA_EXPORT_SKILL
    # Generic framing — must not be Sheets-only
    assert "canvas" in body.lower()
    assert "Google Sheets" in body
    assert "OneDrive" in body
    assert "SharePoint" in body
    # Registry-pattern reference
    assert "注册" in body or "registry" in body.lower()
    # Must reference the system-injected hint marker (load-bearing contract)
    assert "【数据导出 URL】" in body
    # First action contract
    assert "goto" in body
    # Sheets gid hint kept as one bullet
    assert "gid" in body


def test_skill_injected_via_sheets_url_in_browser_state():
    """The most reliable trigger — a Sheets URL is in browser_state. The
    skill is gated through the generic registry, not a hardcoded host."""
    rendered = build_system_prompt(
        goal="提取表头以及前 20 行数据",
        browser_state=f"current_url: {_SHEET_BASE}/edit?gid=0#gid=0",
    )
    assert DATA_EXPORT_SKILL in rendered


def test_skill_injected_via_onedrive_xlsx_url_in_browser_state():
    """OneDrive .xlsx is a *new* host that the old google_sheets skill
    didn't cover — proves the registry pattern delivers genericity."""
    rendered = build_system_prompt(
        goal="从这个表格里提取数据",
        browser_state=f"current_url: {_ONEDRIVE_XLSX}",
    )
    assert DATA_EXPORT_SKILL in rendered


def test_skill_injected_via_sharepoint_url_in_browser_state():
    rendered = build_system_prompt(
        goal="提取所有销售记录",
        browser_state=f"current_url: {_SHAREPOINT_XLSX}",
    )
    assert DATA_EXPORT_SKILL in rendered


@pytest.mark.parametrize(
    "goal",
    [
        "请打开 Google Sheets 提取数据",
        "Open the Google Sheet and grab all rows",
        "从 google 表格里提取 20 条",
        "Extract data from the google spreadsheet",
        "导出 csv 文件",
        "下载 excel",
        "从 OneDrive 上的报表里抽取数据",
        "open the sharepoint .xlsx and extract rows",
    ],
)
def test_skill_injected_via_goal_vocabulary(goal):
    rendered = build_system_prompt(goal=goal, browser_state="")
    assert DATA_EXPORT_SKILL in rendered, (
        f"data_export skill not injected for goal={goal!r}"
    )


def test_skill_not_injected_for_non_data_extract():
    """Plain extract goal on an unrelated URL must not pull this skill."""
    rendered = build_system_prompt(
        goal="Extract top 30 employee rows",
        browser_state="current_url: https://datatables.net/",
    )
    assert DATA_EXPORT_SKILL not in rendered


def test_skill_not_injected_for_pure_dom_extract():
    rendered = build_system_prompt(
        goal="抓取这个页面前 50 条文章",
        browser_state="current_url: https://juejin.cn/",
    )
    assert DATA_EXPORT_SKILL not in rendered


def test_new_registered_host_auto_triggers_skill():
    """The strongest property: registering a new transform should make the
    skill fire for that host **without touching prompts.py or main.py**."""
    original_len = len(_REGISTRY)
    try:
        register_export_transform(
            ExportTransform(
                name="ExampleReports",
                host_pattern=r"^reports\.example\.org$",
                transform=lambda url: url + "?fmt=csv",
            )
        )
        rendered = build_system_prompt(
            goal="extract the table",
            browser_state="current_url: https://reports.example.org/q/1",
        )
        assert DATA_EXPORT_SKILL in rendered
    finally:
        while len(_REGISTRY) > original_len:
            _REGISTRY.pop()
