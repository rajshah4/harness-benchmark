from datetime import date

from toyapp.dates import parse_date


def test_parse_iso_date() -> None:
    assert parse_date("2026-06-05") == date(2026, 6, 5)
