from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo


def parse_date(value: str) -> date:
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unsupported date format: {value!r}")


def parse_date_range(start: str, end: str) -> tuple[date, date]:
    start_date = parse_date(start)
    end_date = parse_date(end)
    if end_date < start_date:
        raise ValueError("end date must be on or after start date")
    return start_date, end_date


def normalize_timezone(name: str) -> str:
    if name.upper() in {"UTC", "Z"}:
        return "UTC"
    return ZoneInfo(name).key


def as_utc_midnight(value: str) -> datetime:
    parsed = parse_date(value)
    return datetime(parsed.year, parsed.month, parsed.day, tzinfo=timezone.utc)
