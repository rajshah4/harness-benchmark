from export_traces import redact_text, sanitize


def test_structured_credentials_are_redacted() -> None:
    value = {
        "api_key": "actual-key-value",
        "nested": {"authorization": "Bearer actual-key-value"},
        "usage": {"prompt_tokens": 123, "cache_read_tokens": 100},
    }

    assert sanitize(value) == {
        "api_key": "[REDACTED]",
        "nested": {"authorization": "[REDACTED]"},
        "usage": {"prompt_tokens": 123, "cache_read_tokens": 100},
    }


def test_secret_assignments_and_headers_are_redacted() -> None:
    text = (
        "LLM_API_KEY=actual-key-value "
        "Authorization: Bearer actual-bearer-value "
        "X-Session-API-Key: actual-session-value"
    )

    redacted = redact_text(text)
    assert "actual-key-value" not in redacted
    assert "actual-bearer-value" not in redacted
    assert "actual-session-value" not in redacted
    assert redacted.count("[REDACTED]") == 3


def test_common_token_formats_and_paths_are_redacted() -> None:
    text = (
        "sk-abcdefghijklmnopqrstuvwx "
        "ghp_abcdefghijklmnopqrstuvwxyz123456 "
        "lmnr_abcdefghijklmnopqrstuvwxyz "
        "/home/ubuntu/private/workspace/file.py"
    )

    redacted = redact_text(text)
    assert "sk-" not in redacted
    assert "ghp_" not in redacted
    assert "lmnr_" not in redacted
    assert "/home/ubuntu" not in redacted
    assert "<local-workspace>" in redacted


def test_placeholders_are_not_mistaken_for_secret_values() -> None:
    text = "Use $LLM_API_KEY and X-Session-API-Key: $SESSION_KEY"
    assert redact_text(text) == text

