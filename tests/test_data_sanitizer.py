from visual_web_agent.data_sanitizer import sanitize_extracted_rows


def test_sanitizer_accepts_grounded_short_semantic_card_rows():
    rows = [
        {
            "day_label": "今天",
            "weather": "多云",
            "high_temp": "19℃",
            "low_temp": "19℃",
            "wind_level": "3-4级",
        }
    ]

    result = sanitize_extracted_rows(
        rows,
        source_text="Card 1: day_label=今天; weather=多云; high_temp=19℃; low_temp=19℃; wind_level=3-4级",
        seen_fingerprints=set(),
    )

    assert result.accepted == 1
    assert result.rejected_total == 0


def test_sanitizer_still_rejects_ungrounded_short_rows():
    rows = [
        {
            "day_label": "今天",
            "weather": "多云",
            "high_temp": "19℃",
            "low_temp": "19℃",
            "wind_level": "3-4级",
        }
    ]

    result = sanitize_extracted_rows(
        rows,
        source_text="unrelated page text",
        seen_fingerprints=set(),
    )

    assert result.accepted == 0
    assert result.rejected_total == 1
