from visual_web_agent.data_manager import _normalize_intercept_rows


def test_normalizes_nested_article_feed_rows() -> None:
    rows = [
        {
            "article_id": "123",
            "article_info": {
                "article_id": "123",
                "title": "A useful technical article",
                "digg_count": 42,
                "link_url": "",
            },
            "author_user_info": {"user_name": "Alice"},
            "tags": [{"tag_name": "Python"}, {"tag_name": "Backend"}],
        }
    ]

    normalized = _normalize_intercept_rows(rows)

    assert normalized == [
        {
            "title": "A useful technical article",
            "author": "Alice",
            "digg_count": 42,
            "url": "https://juejin.cn/post/123",
            "article_id": "123",
            "tags": "Python, Backend",
        }
    ]


def test_filters_promoted_nested_article_rows() -> None:
    rows = [
        {
            "article_id": "123",
            "article_info": {"article_id": "123", "title": "Sponsored", "digg_count": 1},
            "author_user_info": {"user_name": "Alice"},
            "tags": [{"tag_name": "推广"}],
        }
    ]

    assert _normalize_intercept_rows(rows) == []


def test_filters_item_info_advertisement_rows() -> None:
    rows = [
        {
            "item_type": 14,
            "item_info": {
                "advertisement_info": {
                    "advert_id": "ad-1",
                    "title": "Sponsored campaign",
                    "digg_count": 0,
                }
            },
        }
    ]

    assert _normalize_intercept_rows(rows) == []


def test_normalizes_item_info_wrapped_article_rows() -> None:
    rows = [
        {
            "item_type": 2,
            "item_info": {
                "article_id": "456",
                "article_info": {
                    "article_id": "456",
                    "title": "Wrapped article",
                    "digg_count": 7,
                },
                "author_user_info": {"user_name": "Bob"},
                "tags": [{"tag_name": "JavaScript"}],
            },
        }
    ]

    normalized = _normalize_intercept_rows(rows)

    assert normalized[0]["title"] == "Wrapped article"
    assert normalized[0]["author"] == "Bob"
    assert normalized[0]["digg_count"] == 7
    assert normalized[0]["url"] == "https://juejin.cn/post/456"


def test_normalizes_content_counter_article_rows() -> None:
    rows = [
        {
            "content": {"content_id": "789", "title": "Rank article"},
            "content_counter": {"like": 11},
            "author": {"name": "Carol"},
        }
    ]

    normalized = _normalize_intercept_rows(rows)

    assert normalized == [
        {
            "title": "Rank article",
            "author": "Carol",
            "digg_count": 11,
            "url": "https://juejin.cn/post/789",
            "article_id": "789",
        }
    ]


def test_filters_author_recommendation_rows_without_articles() -> None:
    rows = [{"user_name": "Only Author", "articles": [], "got_digg_count": 100}]

    assert _normalize_intercept_rows(rows) == []


def test_keeps_plain_rows_unchanged() -> None:
    rows = [{"Name": "Alice", "Office": "Tokyo"}]

    assert _normalize_intercept_rows(rows) == rows
