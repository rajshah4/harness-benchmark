import json

from usage_ledger import cache_observation, normalize_usage, summarize


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


def test_distinguishes_missing_cache_field_from_reported_zero():
    assert cache_observation({"prompt_tokens_details": {"cached_tokens": 0}}) == {
        "reported": True,
        "tokens": 0,
        "field": "prompt_tokens_details.cached_tokens",
    }
    assert cache_observation({"prompt_tokens": 10}) == {
        "reported": False,
        "tokens": None,
        "field": None,
    }


def test_normalizes_anthropic_cache_usage_without_losing_categories():
    usage = {
        "input_tokens": 100,
        "cache_creation_input_tokens": 20,
        "cache_read_input_tokens": 300,
        "output_tokens": 40,
    }
    assert normalize_usage(usage) == {
        "input_tokens": 420,
        "fresh_input_tokens": 100,
        "cache_read_input_tokens": 300,
        "cache_write_input_tokens": 20,
        "output_tokens": 40,
        "reasoning_tokens": None,
        "provider_total_tokens": None,
    }
    assert cache_observation(usage) == {
        "reported": True,
        "tokens": 300,
        "field": "cache_read_input_tokens",
    }


def test_duplicate_response_id_fails_publishability(tmp_path):
    record = {
        "provider_response_id": "r1",
        "raw_usage": {
            "prompt_tokens": 10,
            "completion_tokens": 2,
            "total_tokens": 12,
            "prompt_tokens_details": {"cached_tokens": 0},
        },
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
