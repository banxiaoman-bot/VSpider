"""Regression: FEED_AD_FILTER_SKILL + ``is_ad_like_text`` helper.

Background: the user's Juejin task says "提取信息流中最新的20篇文章...必须跳过
带有'广告'或'推广'标签的条目，只提取真实的技术文章". Without dedicated
guidance the VLM tends to count ads against the N=20 quota, leaving the user
with ~17 real articles. This test pins:

  * the prompt skill is registered + injected on the right triggers
  * the Python ``is_ad_like_text`` helper recognises the same vocabulary as
    the in-page first-result picker JS regex (so candidates filtered by one
    layer never leak through the other)
  * the filter is conservative — empty / non-ad text never gets dropped
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_VWA = Path(__file__).resolve().parents[1] / "visual_web_agent"
sys.path.insert(0, str(_VWA))

from prompt_skills import FEED_AD_FILTER_SKILL, SKILL_PROMPTS  # noqa: E402
from prompts import build_system_prompt  # noqa: E402
from search_result_guards import is_ad_like_text  # noqa: E402


# ── Skill registration ────────────────────────────────────────────────────
def test_skill_is_registered_in_prompt_map():
    assert "feed_ad_filter" in SKILL_PROMPTS
    assert SKILL_PROMPTS["feed_ad_filter"] is FEED_AD_FILTER_SKILL


def test_skill_content_pins_critical_rules():
    body = FEED_AD_FILTER_SKILL
    # Must reference the keyword filter
    assert "广告" in body and "推广" in body
    assert "sponsored" in body.lower()
    # Must warn against counting ads in the N quota
    assert "数量" in body or "quota" in body.lower() or "前 N" in body
    # Must instruct candidate scanning > N to absorb drops
    assert "25" in body or "30" in body or "更多" in body


# ── Trigger keyword coverage ──────────────────────────────────────────────
@pytest.mark.parametrize(
    "goal",
    [
        "提取信息流前20篇文章，必须跳过广告",
        "抓取最新20条，跳过推广",
        "排除广告条目",
        "过滤广告之后再提取",
        "请只提取真实的技术文章",
        "Only extract real articles, skip ads",
        "Skip sponsored cards in the feed",
        "Filter out ad entries before counting",
    ],
)
def test_skill_injected_when_user_asks_to_skip_ads(goal):
    rendered = build_system_prompt(goal=goal, browser_state="")
    assert FEED_AD_FILTER_SKILL in rendered, (
        f"feed_ad_filter not injected for goal={goal!r}"
    )


@pytest.mark.parametrize(
    "goal",
    [
        # Plain extract — ads not mentioned, must NOT inject (avoid scaring
        # the agent off legitimate cards on goals like "前 30 条 employee data")
        "提取员工表格前30行",
        "Top 50 hot HN posts about AI Agent",
        "豆瓣Top250前26部电影",
        "",
    ],
)
def test_skill_not_injected_for_plain_extract_goal(goal):
    rendered = build_system_prompt(goal=goal, browser_state="")
    assert FEED_AD_FILTER_SKILL not in rendered, (
        f"feed_ad_filter UNEXPECTEDLY injected for plain extract goal={goal!r}"
    )


# ── is_ad_like_text helper ────────────────────────────────────────────────
@pytest.mark.parametrize(
    "snippet",
    [
        "广告",
        "推广",
        "赞助",
        "廣告",  # traditional
        "贊助",
        "推廣",
        "Sponsored",
        "SPONSORED",
        "promoted",
        "Promotion",
        "ads",
        "ad",  # bare token
        "[Ad]",
        "[赞助]",
        "Brand partnership",
        # Realistic feed snippets
        "Vue 3 Composition API 详解 | 广告",
        "Promoted: Try our new IDE",
        "[品牌广告] iPhone 16 全新登场",
    ],
)
def test_is_ad_like_text_detects_ad_tokens(snippet):
    assert is_ad_like_text(snippet) is True, f"missed ad token in {snippet!r}"


@pytest.mark.parametrize(
    "snippet",
    [
        # Real article titles — must NOT be flagged
        "Vue 3 Composition API 详解",
        "How to optimize React useState",
        "深入理解 V8 隐藏类",
        "TypeScript 5.0 新特性",
        # Empty / blank
        "",
        "   ",
        None,
        # Words that contain ad-substrings but aren't ads
        "Adobe 推出新产品",         # contains "Ad" but not as standalone token
        "head bad mad sad lad",     # word boundary check
        "广东省",                    # 广 as prefix, not 广告
        "广州",
    ],
)
def test_is_ad_like_text_does_not_false_positive(snippet):
    assert is_ad_like_text(snippet) is False, (
        f"false positive for {snippet!r}"
    )


def test_is_ad_like_text_handles_long_snippet():
    """Performance / robustness: long card body containing one ad token in
    the middle still gets caught."""
    body = (
        "Vue 3 是一个新的 JavaScript 框架，由尤雨溪开发，"
        "采用了 Composition API ... 这条内容来自广告投放，"
        "请勿混淆为技术文章 ... 详细请阅读官方文档。"
    )
    assert is_ad_like_text(body) is True
