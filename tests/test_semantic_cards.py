from visual_web_agent.extraction_engine.cards import extract_semantic_card_rows


def test_extracts_weather_forecast_cards_from_dom_list_source_text():
    rows = [
        {"title": "今天 05月11日"},
        {"title": "明天 05月12日"},
        {"title": "后天 05月13日"},
    ]
    source_text = """
Item 1: 今天 05月11日
今天 05月11日 多云 19℃ 东南风 3-4级

Item 2: 明天 05月12日
明天 05月12日 小雨转中雨 24℃ 11℃ 西北风 5-6级

Item 3: 后天 05月13日
后天 05月13日 大雨转多云 13℃ 7℃ 西北风 5-6级转<3级
"""

    extracted, card_text = extract_semantic_card_rows(
        rows,
        source_text=source_text,
        requested_fields=[
            "day_label",
            "weather",
            "high_temp",
            "low_temp",
            "wind_level",
        ],
        goal="提取今天、明天、后天的天气、最高/最低温度和风力等级",
    )

    assert extracted == [
        {
            "day_label": "今天",
            "weather": "多云",
            "high_temp": "19℃",
            "low_temp": "19℃",
            "wind_level": "3-4级",
        },
        {
            "day_label": "明天",
            "weather": "小雨转中雨",
            "high_temp": "24℃",
            "low_temp": "11℃",
            "wind_level": "5-6级",
        },
        {
            "day_label": "后天",
            "weather": "大雨转多云",
            "high_temp": "13℃",
            "low_temp": "7℃",
            "wind_level": "5-6级转<3级",
        },
    ]
    assert "Card 1" in card_text


def test_semantic_card_extractor_does_not_touch_article_schemas():
    extracted, _ = extract_semantic_card_rows(
        [{"title": "A useful article", "author": "Ada", "points": 12}],
        source_text="Item 1: A useful article by Ada 12 points",
        requested_fields=["title", "author", "points"],
        goal="extract article title author and points",
    )

    assert extracted == []


def test_extracts_forecast_cards_from_continuous_page_text_before_news_items():
    source_text = (
        "首页 预报 今天 7天 "
        "10日（今天） 多云 19℃ 3-4级 "
        "11日（明天） 小雨转中雨 24℃ 11℃ 5-6级 "
        "12日（后天） 大雨转多云 13℃ 7℃ 5-6级转<3级 "
        "13日（周三） 晴 18℃ 4℃ <3级 "
        "天气资讯 今天（5月10日）至13日，南北方气温多波动 30℃"
    )

    extracted, _ = extract_semantic_card_rows(
        [],
        source_text=source_text,
        requested_fields=["day_label", "weather", "high_temp", "low_temp", "wind_level"],
        goal="提取今天、明天、后天的天气现象、最高/最低温度、风力等级",
    )

    assert len(extracted) == 3
    assert extracted[0]["day_label"] == "今天"
    assert extracted[1]["weather"] == "小雨转中雨"
    assert extracted[2]["high_temp"] == "13℃"
    assert extracted[2]["low_temp"] == "7℃"
    assert extracted[2]["wind_level"] == "5-6级转<3级"


def test_single_temperature_can_fill_high_and_low_requested_fields():
    extracted, _ = extract_semantic_card_rows(
        [],
        source_text="Item 1: Today sunny 68°F light wind 2级",
        requested_fields=["day", "condition", "high_temp", "low_temp"],
        goal="extract weather forecast",
    )

    assert extracted == [
        {
            "day": "Today",
            "condition": "sunny",
            "high_temp": "68°",
            "low_temp": "68°",
        }
    ]


def test_temperature_range_with_single_unit_fills_high_and_low():
    extracted, _ = extract_semantic_card_rows(
        [],
        source_text="11日（今天） 小雨转中雨 21/13℃ 3-4级",
        requested_fields=["day_label", "weather", "high_temp", "low_temp", "wind_level"],
        goal="提取今天、明天、后天的天气预报",
    )

    assert extracted == [
        {
            "day_label": "今天",
            "weather": "小雨转中雨",
            "high_temp": "21℃",
            "low_temp": "13℃",
            "wind_level": "3-4级",
        }
    ]


def test_extracts_requested_multi_day_forecast_horizon_and_stops_before_news():
    source_text = (
        "7 day forecast "
        "Today sunny 19°C wind 2级 "
        "Tomorrow cloudy 22°C 14°C wind 3级 "
        "Wednesday rain 18°C 12°C wind 4级 "
        "Thursday sunny 20°C 10°C wind 2级 "
        "Friday cloudy 21°C 11°C wind 2级 "
        "Saturday rain 17°C 9°C wind 3级 "
        "Sunday sunny 23°C 13°C wind 2级 "
        "weather news Today severe weather 30°C wind 8级"
    )

    extracted, _ = extract_semantic_card_rows(
        [],
        source_text=source_text,
        requested_fields=["day", "condition", "high_temp", "low_temp", "wind_level"],
        goal="extract next 7 days weather forecast",
    )

    assert len(extracted) == 7
    assert extracted[0]["day"] == "Today"
    assert extracted[2]["day"] == "Wednesday"
    assert extracted[-1]["day"] == "Sunday"
    assert all(row["condition"] != "severe weather" for row in extracted)


def test_extracts_forecast_from_table_like_multiline_text():
    source_text = """
row 1
05/11
Sunny
21C / 13C
Wind 3
row 2
05/12
Cloudy
18C / 9C
Wind 4
row 3
05/13
Rain
16C / 8C
Wind 5
"""

    extracted, card_text = extract_semantic_card_rows(
        [],
        source_text=source_text,
        requested_fields=["day", "condition", "high_temp", "low_temp", "wind_level"],
        goal="extract 3 days weather forecast",
    )

    assert extracted == [
        {
            "day": "05/11",
            "condition": "Sunny",
            "high_temp": "21℃",
            "low_temp": "13℃",
            "wind_level": "Wind 3",
        },
        {
            "day": "05/12",
            "condition": "Cloudy",
            "high_temp": "18℃",
            "low_temp": "9℃",
            "wind_level": "Wind 4",
        },
        {
            "day": "05/13",
            "condition": "Rain",
            "high_temp": "16℃",
            "low_temp": "8℃",
            "wind_level": "Wind 5",
        },
    ]
    assert "Card 3" in card_text
