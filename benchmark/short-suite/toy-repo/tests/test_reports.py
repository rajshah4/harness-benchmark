from toyapp.reports import build_monthly_report


ROWS = [
    {
        "month": "2026-05",
        "account": "acme",
        "region": "na",
        "product": "support",
        "revenue": "100",
        "cost": "40",
        "tickets": 3,
        "satisfaction": "4.5",
    },
    {
        "month": "2026-05",
        "account": "beta",
        "region": "eu",
        "product": "support",
        "revenue": "80",
        "cost": "90",
        "tickets": 12,
        "satisfaction": "3.5",
    },
    {
        "month": "2026-04",
        "account": "old",
        "region": "na",
        "product": "archive",
        "revenue": "999",
        "cost": "1",
        "tickets": 1,
        "satisfaction": "5",
    },
]


def test_build_monthly_report() -> None:
    report = build_monthly_report(ROWS, "2026-05")
    assert report["row_count"] == 2
    assert report["totals"]["revenue"] == "$180.00"
    assert report["totals"]["cost"] == "$130.00"
    assert report["totals"]["margin"] == "$50.00"
    assert "beta has negative margin" in report["alerts"]
    assert "beta has high support volume" in report["alerts"]
    assert "Revenue: $180.00" in report["narrative"]
