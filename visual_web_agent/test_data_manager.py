import pandas as pd

try:
    from .data_manager import _align_new_columns_to_existing
except ImportError:
    from data_manager import _align_new_columns_to_existing


def test_schema_alignment_does_not_treat_position_as_rank():
    existing = pd.DataFrame([{"rank": 1, "title": "A"}])
    incoming = pd.DataFrame([{"position": "Accountant", "name": "Alice"}])

    aligned = _align_new_columns_to_existing(incoming, existing)

    assert "position" in aligned.columns
    assert "rank" not in aligned.columns


def test_schema_alignment_keeps_real_position_column():
    existing = pd.DataFrame([{"name": "Alice", "position": "Accountant"}])
    incoming = pd.DataFrame([{"name": "Bob", "position": "Director"}])

    aligned = _align_new_columns_to_existing(incoming, existing)

    assert list(aligned.columns) == ["name", "position"]
