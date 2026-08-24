import json

from usage_ledger import normalize_usage, summarize


def test_normalizes_cached_input_without_double_counting():
    usage = {
        "prompt_tokens": 5219,
        "completion_tokens": 102,
        "total_tokens": 5321,
        "prompt_tokens_details": {"cached_tokens": 4436},
        "completion_tokens_details": {"reasoning_tokens": 11},
    }
    assert normalize_usage(usage) == {
        "input_tokens": 5219,
        "fresh_input_tokens": 783,
        "cache_read_input_tokens": 4436,
        "cache_write_input_tokens": None,
        "output_tokens": 102,
        "reasoning_tokens": 11,
        "provider_total_tokens": 5321,
    }


def test_missing_usage_fails_publishability(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(json.dumps({"provider_response_id": "r1"}) + "\n")
    result = summarize(ledger)
    assert result["publishable"] is False
    assert result["errors"] == [{"row": 1, "error": "missing raw provider usage"}]


def test_duplicate_response_id_fails_publishability(tmp_path):
    record = {
        "provider_response_id": "r1",
        "raw_usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("\n".join([json.dumps(record), json.dumps(record)]) + "\n")
    result = summarize(ledger)
    assert result["publishable"] is False
    assert {error["error"] for error in result["errors"]} == {"duplicate provider response ID"}


def test_summarizes_provider_records_by_task_and_phase(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(
        json.dumps(
            {
                "provider_response_id": "r1",
                "context": {
                    "run_id": "run-1",
                    "task_id": "task-1",
                    "phase": "primary",
                },
                "raw_usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                    "prompt_tokens_details": {"cached_tokens": 80},
                },
            }
        )
        + "\n"
    )

    result = summarize(ledger)

    assert result["publishable"] is True
    assert result["by_context"] == {
        "unlabeled/run-1/task-1/primary": {
            "input_tokens": 100,
            "fresh_input_tokens": 20,
            "cache_read_input_tokens": 80,
            "output_tokens": 20,
            "provider_total_tokens": 120,
        }
    }
