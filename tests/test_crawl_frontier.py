"""Unit tests for the ``crawl_frontier`` module (best-first deep crawl).

Pure / deterministic: no network, no browser. Covers keyword normalization,
URL relevance scoring, BFS FIFO parity, best-first pop ordering (score → depth
→ insertion tie-breaks), graceful degradation without keywords, and the
``build_frontier`` factory.
"""

from __future__ import annotations

from visual_web_agent.crawl_frontier import (
    BestFirstFrontier,
    BFSFrontier,
    build_frontier,
    normalize_keywords,
    score_url,
)


class TestNormalizeKeywords:
    def test_string_is_tokenized(self) -> None:
        assert normalize_keywords("Python Tutorial") == ["python", "tutorial"]

    def test_iterable_is_flattened_and_deduped(self) -> None:
        assert normalize_keywords(["A-B", "c", "b"]) == ["a", "b", "c"]

    def test_empty_inputs(self) -> None:
        assert normalize_keywords("") == []
        assert normalize_keywords(None) == []


class TestScoreUrl:
    def test_counts_distinct_keyword_hits_in_path(self) -> None:
        score = score_url("https://x.com/python/tutorial/intro", ["python", "tutorial"])
        assert score == 2.0

    def test_query_tokens_count(self) -> None:
        score = score_url("https://x.com/p?topic=python", ["python"])
        assert score == 1.0

    def test_unrelated_url_scores_zero(self) -> None:
        assert score_url("https://x.com/random/page", ["python"]) == 0.0

    def test_no_keywords_scores_zero(self) -> None:
        assert score_url("https://x.com/python", []) == 0.0

    def test_anchor_text_adds_half_weight(self) -> None:
        assert score_url("https://x.com/p", ["guide"], anchor_text="Read the Guide") == 0.5

    def test_domain_tokens_are_not_scored(self) -> None:
        # keyword only appears in the host, not path/query -> no score
        assert score_url("https://python.com/docs", ["python"]) == 0.0


class TestBFSFrontier:
    def test_is_fifo(self) -> None:
        f = BFSFrontier(["https://x.com/a"])
        f.push("https://x.com/b", 1)
        f.push("https://x.com/c", 1)
        assert len(f) == 3
        assert f.pop() == ("https://x.com/a", 0)
        assert f.pop() == ("https://x.com/b", 1)
        assert f.pop() == ("https://x.com/c", 1)
        assert len(f) == 0


class TestBestFirstFrontier:
    def test_pops_highest_score_first(self) -> None:
        f = BestFirstFrontier(keywords=["python"])
        f.push("https://x.com/random", 1)
        f.push("https://x.com/python/guide", 1)
        f.push("https://x.com/other", 1)
        assert f.pop()[0] == "https://x.com/python/guide"

    def test_depth_breaks_score_ties(self) -> None:
        f = BestFirstFrontier(keywords=["python"])
        f.push("https://x.com/python", 3)
        f.push("https://x.com/python", 1)
        # equal score -> shallower depth first
        assert f.pop() == ("https://x.com/python", 1)

    def test_insertion_order_breaks_remaining_ties(self) -> None:
        f = BestFirstFrontier(keywords=["python"])
        f.push("https://x.com/first", 1)
        f.push("https://x.com/second", 1)
        # equal score (0) + equal depth -> FIFO
        assert f.pop()[0] == "https://x.com/first"
        assert f.pop()[0] == "https://x.com/second"

    def test_degrades_without_keywords(self) -> None:
        f = BestFirstFrontier(seeds=["https://x.com/a"], keywords=[])
        f.push("https://x.com/b", 1)
        assert len(f) == 2
        out = [f.pop()[0], f.pop()[0]]
        # no keywords -> every score is 0; frontier still drains every URL
        assert set(out) == {"https://x.com/a", "https://x.com/b"}
        assert len(f) == 0


class TestBuildFrontier:
    def test_bfs_default(self) -> None:
        assert isinstance(build_frontier("bfs", seeds=["https://x.com/"]), BFSFrontier)

    def test_best_first(self) -> None:
        f = build_frontier("best_first", keywords=["python"], seeds=["https://x.com/python"])
        assert isinstance(f, BestFirstFrontier)
        assert f.pop() == ("https://x.com/python", 0)

    def test_unknown_strategy_falls_back_to_bfs(self) -> None:
        assert isinstance(build_frontier("nonsense", seeds=[]), BFSFrontier)

    def test_seeds_enter_at_depth_zero(self) -> None:
        f = build_frontier("best_first", keywords=["a"], seeds=["https://x.com/a", "https://x.com/b"])
        assert f.pop()[1] == 0
