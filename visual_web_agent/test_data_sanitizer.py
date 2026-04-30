try:
    from .data_sanitizer import extract_tooltip_primary_key, sanitize_extracted_rows
except ImportError:
    from data_sanitizer import extract_tooltip_primary_key, sanitize_extracted_rows


def test_sanitizer_filters_sparse_paragraph_rows_and_dedups():
    source = """
    An AI agent published a hit piece on me 2346 points scottshambaugh 2 months ago 951 comments
    https://theshamblog.com/an-ai-agent-published-a-hit-piece-on-me/
    The stack: two agents on separate boxes.
    Tiered inference: Haiku 4.5 for conversation.
    """
    seen = set()
    rows = [
        {
            "title": "An AI agent published a hit piece on me",
            "url": "https://theshamblog.com/an-ai-agent-published-a-hit-piece-on-me/",
            "author": "scottshambaugh",
            "time": "2 months ago",
            "points": "2346",
            "comments": "951",
        },
        {
            "title": "An AI agent published a hit piece on me",
            "link": "https://theshamblog.com/an-ai-agent-published-a-hit-piece-on-me/",
            "author": "scottshambaugh",
            "time": "2 months ago",
            "points": "2346",
            "comments": "951",
        },
        {"title": "The stack: two agents on separate boxes"},
        {"title": "Tiered inference: Haiku 4.5 for conversation"},
    ]

    result = sanitize_extracted_rows(rows, source, seen)

    assert result.accepted == 1
    assert result.duplicates == 1
    assert result.rejected_sparse == 2


def test_sanitizer_allows_numeric_grid_rows():
    source = "10kV凤岭线01A段 2026-04-25 10:00 10.2 8.7 0.98 36.5"
    seen = set()
    rows = [
        {
            "设备编码": "10kV凤岭线01A段",
            "采集时间": "2026-04-25 10:00",
            "A相电压": "10.2",
            "B相电流": "8.7",
            "功率因数": "0.98",
            "温度": "36.5",
        }
    ]

    result = sanitize_extracted_rows(rows, source, seen)

    assert result.accepted == 1
    assert result.rows[0]["设备编码"] == "10kV凤岭线01A段"


def test_sanitizer_caps_target_remaining():
    source = "repo-a repo-b repo-c"
    seen = set()
    rows = [
        {"name": "repo-a", "stars": "100"},
        {"name": "repo-b", "stars": "90"},
        {"name": "repo-c", "stars": "80"},
    ]

    result = sanitize_extracted_rows(rows, source, seen, target_remaining=2)

    assert result.accepted == 2
    assert len(result.rows) == 2


def test_sanitizer_rejects_rows_grounded_only_by_volatile_fields():
    source = """
    opencode AI coding agent, built for the terminal 319 points indoodad 10 months ago 81 comments
    Launch HN: Undermind (YC S24) - AI agent for discovering scientific papers
    292 points ramette 2 years ago 126 comments
    """
    seen = set()
    rows = [
        {
            "title": "AutoGPT vs BabyAGI vs LangChain: Comparing AI agent frameworks",
            "url": "https://medium.com/@ai_research/auto-gpt-vs-babyagi-vs-langchain-comparison-2025",
            "points": "292",
            "author": "ramette",
            "time": "2 years ago",
            "comments": "126",
        },
        {
            "title": "Launch HN: Undermind (YC S24) - AI agent for discovering scientific papers",
            "url": "https://www.undermind.ai/",
            "points": "292",
            "author": "ramette",
            "time": "2 years ago",
            "comments": "126",
        },
    ]

    result = sanitize_extracted_rows(rows, source, seen)

    assert result.accepted == 1
    assert result.rejected_ungrounded == 1
    assert result.rows[0]["title"].startswith("Launch HN: Undermind")


def test_sanitizer_flattens_wrapped_visible_rows():
    source = """
    Page 1 of an employee table.
    Airi Satou Accountant Tokyo 2008-11-28 $162,700
    Angelica Ramos Chief Executive Officer London 2009-10-09 $1,200,000
    """
    seen = set()
    rows = [
        {
            "page": 1,
            "total_entries": 57,
            "entries_per_page": 10,
            "visible_rows": [
                {
                    "name": "Airi Satou",
                    "position": "Accountant",
                    "office": "Tokyo",
                    "start_date": "2008-11-28",
                    "salary": "$162,700",
                },
                {
                    "name": "Angelica Ramos",
                    "position": "Chief Executive Officer",
                    "office": "London",
                    "start_date": "2009-10-09",
                    "salary": "$1,200,000",
                },
            ],
        }
    ]

    result = sanitize_extracted_rows(rows, source, seen)

    assert result.accepted == 2
    assert result.rows[0]["page"] == 1
    assert result.rows[0]["name"] == "Airi Satou"
    assert "visible_rows" not in result.rows[0]


def test_sanitizer_does_not_dedup_table_rows_by_repeated_short_fields():
    source = """
    Alice Chen Regional Director San Francisco 2020-01-01 $100
    Bob Li Regional Director San Francisco 2020-02-01 $110
    """
    seen = set()
    rows = [
        {
            "name": "Alice Chen",
            "position": "Regional Director",
            "office": "San Francisco",
            "start_date": "2020-01-01",
            "salary": "$100",
        },
        {
            "name": "Bob Li",
            "position": "Regional Director",
            "office": "San Francisco",
            "start_date": "2020-02-01",
            "salary": "$110",
        },
    ]

    result = sanitize_extracted_rows(rows, source, seen)

    assert result.accepted == 2
    assert result.duplicates == 0


def test_sanitizer_dedups_ranked_list_rows_by_rank():
    source = """
    7 星际穿越 Interstellar 9.4 2180674 爱是一种力量，让我们超越时空感知它的存在。
    8 这个杀手不太冷 9.4 2550313 怪蜀黍和小萝莉不得不说的故事。
    """
    seen = set()
    rows = [
        {
            "rank": 7,
            "title": "星际穿越",
            "score": "9.4",
            "reviews": "2180674",
            "summary": "爱是",
        },
        {
            "rank": "7",
            "title": "星际穿越",
            "score": "9.4",
            "reviews": "2180674人评价",
            "summary": "爱是一种力量，让我们超越时空感知它的存在。",
        },
        {
            "rank": 8,
            "title": "这个杀手不太冷",
            "score": "9.4",
            "reviews": "2550313",
            "summary": "怪蜀黍和小萝莉不得不说的故事。",
        },
    ]

    result = sanitize_extracted_rows(rows, source, seen)

    assert result.accepted == 2
    assert result.duplicates == 1
    assert [row["rank"] for row in result.rows] == [7, 8]


def test_tooltip_primary_key_prefers_trigger_fields():
    row = {
        "column_name": "Active Users",
        "tooltip_text": "Number of users active in the last 7 days",
    }

    same_trigger_better_payload = {
        "column_name": "Active Users",
        "tooltip_text": "Users active in the last seven days, excluding disabled accounts",
    }

    assert extract_tooltip_primary_key(row) == extract_tooltip_primary_key(
        same_trigger_better_payload
    )


def test_tooltip_primary_key_falls_back_to_payload_value():
    row = {"hover_result": "Click to save the current configuration"}

    assert extract_tooltip_primary_key(row).startswith("value|")
