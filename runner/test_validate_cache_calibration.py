import json

from validate_cache_calibration import validate


def test_calibration_requires_reported_and_positive_cache_reads(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    rows = []
    for harness in ("openhands", "pi", "opencode"):
        for turn, cached in enumerate((0, 100, 200), 1):
            rows.append({
                "harness": harness,
                "context": {"run_id": "cal-1", "phase": f"turn-{turn}"},
                "raw_usage": {"prompt_tokens_details": {"cached_tokens": cached}},
                "error_type": None,
            })
    ledger.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    result = validate(ledger, "cal-1", ["openhands", "pi", "opencode"])
    assert result["passed"] is True
    assert result["harnesses"]["openhands"]["cache_read_tokens"] == 300


def test_calibration_fails_when_cache_field_is_missing(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    rows = [{
        "harness": "openhands",
        "context": {"run_id": "cal-2"},
        "raw_usage": {"prompt_tokens": 10},
        "error_type": None,
    }] * 3
    ledger.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    result = validate(ledger, "cal-2", ["openhands"])
    assert result["passed"] is False
    assert result["harnesses"]["openhands"]["cache_field_missing_calls"] == 3
