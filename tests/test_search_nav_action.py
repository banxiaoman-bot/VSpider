"""Stub-frame regression for the SEARCH-NAV capability.

Covers the deterministic core (``open_top_organic_result``) and the ad-redirect
landing helper (``is_ad_redirect_url``) without a real browser: a fake page
exposes ``frames[*].evaluate`` (returns the probed candidate list) and
``goto`` (records a simulated landing URL), so candidate rotation + the
landing-page second pass are exercised end to end.
"""

import asyncio

from visual_web_agent.search_result_guards import (
    is_ad_redirect_url,
    open_top_organic_result,
    _probe_organic_results,
)
from visual_web_agent.search_nav_action import _query_from_url


def _run(coro):
    return asyncio.run(coro)


class _Frame:
    def __init__(self, result):
        self._result = result

    async def evaluate(self, script):
        return self._result


class _Page:
    def __init__(self, candidates, landing_map, browser):
        self.frames = [_Frame(candidates)]
        self._landing_map = landing_map or {}
        self._browser = browser

    def is_closed(self):
        return False

    async def goto(self, url, wait_until=None, timeout=None):
        # Simulate redirects: a candidate href may resolve to a different
        # landing URL (e.g. an ad-redirect wrapper unwinding to doubleclick).
        self._browser.current_url = self._landing_map.get(url, url)


class _Browser:
    def __init__(self, candidates, current_url, landing_map=None):
        self.current_url = current_url
        self._page = _Page(candidates, landing_map, self)
        self.rpa_trail = []

    async def _ensure_active_page(self, reason=""):
        return self._page

    async def _wait_for_page_stable(self):
        return None


# ---------------------------------------------------------------------------
# is_ad_redirect_url
# ---------------------------------------------------------------------------


def test_is_ad_redirect_url_positives():
    assert is_ad_redirect_url("https://www.bing.com/aclick?u=abc")
    assert is_ad_redirect_url("https://doubleclick.net/whatever")
    assert is_ad_redirect_url("https://googleadservices.com/pagead/aclk?adurl=x")
    assert is_ad_redirect_url("https://example.com/p?gclid=abc123")
    assert is_ad_redirect_url("https://example.com/p?msclkid=zzz")
    assert is_ad_redirect_url("https://example.com/p?utm_medium=cpc")


def test_is_ad_redirect_url_negatives():
    assert not is_ad_redirect_url("")
    assert not is_ad_redirect_url(None)
    assert not is_ad_redirect_url("https://docs.python.org/3/tutorial/")
    assert not is_ad_redirect_url("https://news.site/advance")  # /advance != /ad token
    assert not is_ad_redirect_url("https://shop.example.com/products?utm_source=newsletter")


# ---------------------------------------------------------------------------
# _query_from_url
# ---------------------------------------------------------------------------


def test_query_from_serp_url():
    assert _query_from_url("https://www.bing.com/search?q=python%20tutorial") == "python tutorial"
    assert _query_from_url("https://www.baidu.com/s?wd=天气") == "天气"
    assert _query_from_url("https://example.com/no-query") == ""


# ---------------------------------------------------------------------------
# open_top_organic_result — candidate rotation + landing 2nd pass
# ---------------------------------------------------------------------------


def test_skips_ad_href_then_navigates_clean():
    candidates = [
        {"target_id": 1, "text": "Sponsored", "href": "https://www.bing.com/aclick?u=x"},
        {"target_id": 2, "text": "Real Docs", "href": "https://docs.python.org/3/"},
    ]
    browser = _Browser(candidates, "https://www.bing.com/search?q=python")
    res = _run(open_top_organic_result(browser, query="python"))
    assert res is not None
    assert res["url"] == "https://docs.python.org/3/"
    assert res["title"] == "Real Docs"
    assert len(res["skipped"]) == 1
    assert res["skipped"][0]["skipped"] == "ad_redirect_href"
    assert browser.current_url == "https://docs.python.org/3/"


def test_skips_landing_ad_redirect_then_next():
    candidates = [
        {"target_id": 1, "text": "Looks Organic", "href": "https://example.com/go"},
        {"target_id": 2, "text": "Real", "href": "https://realsite.org/page"},
    ]
    landing_map = {"https://example.com/go": "https://doubleclick.net/aclk?adurl=zzz"}
    browser = _Browser(candidates, "https://www.bing.com/search?q=q", landing_map)
    res = _run(open_top_organic_result(browser, query="q"))
    assert res["url"] == "https://realsite.org/page"
    assert len(res["skipped"]) == 1
    assert res["skipped"][0]["skipped"] == "ad_or_search_host"


def test_skips_landing_same_search_host():
    # An href that stays on the search engine (failed redirect) is a dead end.
    candidates = [
        {"target_id": 1, "text": "Stuck", "href": "https://www.bing.com/search?q=again"},
        {"target_id": 2, "text": "Real", "href": "https://realsite.org/x"},
    ]
    browser = _Browser(candidates, "https://www.bing.com/search?q=q")
    res = _run(open_top_organic_result(browser, query="q"))
    assert res["url"] == "https://realsite.org/x"
    assert res["skipped"][0]["skipped"] == "ad_or_search_host"


def test_all_candidates_ads_fails():
    candidates = [
        {"target_id": 1, "text": "Ad1", "href": "https://www.bing.com/aclick?u=1"},
        {"target_id": 2, "text": "Ad2", "href": "https://googleadservices.com/pagead/aclk"},
    ]
    browser = _Browser(candidates, "https://www.bing.com/search?q=q")
    res = _run(open_top_organic_result(browser, query="q"))
    assert res is not None
    assert res.get("failed") is True
    assert res["url"] == ""
    assert len(res["skipped"]) == 2


def test_no_candidates_returns_none():
    browser = _Browser([], "https://www.bing.com/search?q=q")
    res = _run(open_top_organic_result(browser, query="q"))
    assert res is None


def test_probe_organic_results_returns_list():
    candidates = [
        {"target_id": 1, "text": "A", "href": "https://a.com/"},
        {"target_id": 2, "text": "B", "href": "https://b.com/"},
    ]
    browser = _Browser(candidates, "https://www.bing.com/search?q=q")
    out = _run(_probe_organic_results(browser, "q", limit=6))
    assert isinstance(out, list)
    assert len(out) == 2
    assert out[0]["href"] == "https://a.com/"


def test_browse_mode_surfaces_top_n_clean_results():
    # want>=2: surface the top-N clean organic results (ad href dropped) while
    # still navigating to only the first clean one ("看好几个但不点击太多").
    candidates = [
        {"target_id": 1, "text": "Ad", "href": "https://www.bing.com/aclick?u=1"},
        {"target_id": 2, "text": "First", "href": "https://a.org/1"},
        {"target_id": 3, "text": "Second", "href": "https://b.org/2"},
        {"target_id": 4, "text": "Third", "href": "https://c.org/3"},
    ]
    browser = _Browser(candidates, "https://www.bing.com/search?q=q")
    res = _run(open_top_organic_result(browser, query="q", want=3))
    assert res["url"] == "https://a.org/1"  # navigated to first clean
    assert [r["title"] for r in res["results"]] == ["First", "Second", "Third"]
    assert [r["rank"] for r in res["results"]] == [1, 2, 3]
    assert all("bing.com/aclick" not in r["url"] for r in res["results"])


def test_browse_count_capped_at_max():
    candidates = [
        {"target_id": i, "text": f"T{i}", "href": f"https://s{i}.org/"}
        for i in range(1, 9)
    ]
    browser = _Browser(candidates, "https://www.bing.com/search?q=q")
    res = _run(open_top_organic_result(browser, query="q", want=99))
    assert len(res["results"]) == 5  # _MAX_BROWSE hard cap


def test_default_want_is_single_result():
    candidates = [
        {"target_id": 1, "text": "First", "href": "https://a.org/1"},
        {"target_id": 2, "text": "Second", "href": "https://b.org/2"},
    ]
    browser = _Browser(candidates, "https://www.bing.com/search?q=q")
    res = _run(open_top_organic_result(browser, query="q"))
    assert res["url"] == "https://a.org/1"
    assert len(res["results"]) == 1
