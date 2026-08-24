from provider_ledger_proxy import (
    cache_request_hints,
    request_metadata,
    response_error_metadata,
    response_usage_metadata,
    safe_response_metadata,
    usage_from_response,
)


def test_anthropic_usage_stream_is_supported():
    body = b'data: {"message":{"id":"msg-1","usage":{"input_tokens":10,"cache_read_input_tokens":5}}}\n' b'data: {"usage":{"output_tokens":2}}\n'
    response_id, usage = response_usage_metadata(body, "text/event-stream")
    assert response_id == "msg-1"
    assert usage == {
        "input_tokens": 10,
        "cache_read_input_tokens": 5,
        "output_tokens": 2,
    }


def test_extracts_openai_usage_from_json_response():
    body = b'{"id":"response-id","usage":{"prompt_tokens":42,"completion_tokens":7}}'

    assert usage_from_response(body, "application/json") == {
        "prompt_tokens": 42,
        "completion_tokens": 7,
    }


def test_extracts_usage_from_final_stream_chunk():
    body = b"\n".join(
        [
            b'data: {"choices":[{"delta":{"content":"hello"}}]}',
            b'data: {"choices":[],"usage":{"prompt_tokens":42,"completion_tokens":7}}',
            b"data: [DONE]",
        ]
    )

    assert usage_from_response(body, "text/event-stream") == {
        "prompt_tokens": 42,
        "completion_tokens": 7,
    }


def test_extracts_usage_from_responses_completed_event():
    body = b"\n".join(
        [
            b'event: response.output_text.delta',
            b'data: {"type":"response.output_text.delta","delta":"private"}',
            b'event: response.completed',
            b'data: {"type":"response.completed","response":{"id":"resp-123","usage":{"input_tokens":42,"input_tokens_details":{"cached_tokens":30},"output_tokens":7}}}',
        ]
    )

    assert response_usage_metadata(body, "text/event-stream") == (
        "resp-123",
        {
            "input_tokens": 42,
            "input_tokens_details": {"cached_tokens": 30},
            "output_tokens": 7,
        },
    )


def test_merges_anthropic_stream_usage_across_events():
    body = b"\n".join([
        b'data: {"type":"message_start","message":{"id":"msg-1","usage":{"input_tokens":10,"cache_read_input_tokens":30,"cache_creation_input_tokens":2}}}',
        b'data: {"type":"message_delta","usage":{"output_tokens":7}}',
        b"data: [DONE]",
    ])
    assert response_usage_metadata(body, "text/event-stream") == (
        "msg-1",
        {
            "input_tokens": 10,
            "cache_read_input_tokens": 30,
            "cache_creation_input_tokens": 2,
            "output_tokens": 7,
        },
    )


def test_extracts_provider_response_id_without_retaining_response_content():
    body = b'{"id":"chatcmpl-123","choices":[{"message":{"content":"private"}}],"usage":{"prompt_tokens":42}}'

    assert response_usage_metadata(body, "application/json") == (
        "chatcmpl-123",
        {"prompt_tokens": 42},
    )


def test_request_metadata_excludes_message_content():
    metadata = request_metadata(
        b'{"model":"openhands/glm-5.2","stream":true,"messages":[{"role":"user","content":"secret"}],"tools":[{}]}'
    )

    assert metadata["request_json"] is True
    assert metadata["model"] == "openhands/glm-5.2"
    assert metadata["message_count"] == 1
    assert metadata["tool_count"] == 1
    assert metadata["message_shape"] == [{
        "role": "user", "content_bytes": 8, "has_tool_calls": False, "tool_call_count": 0
    }]
    assert "secret" not in repr(metadata)
    assert len(metadata["messages_sha256"]) == 64
    assert len(metadata["tools_sha256"]) == 64


def test_response_metadata_keeps_only_safe_headers_and_no_content():
    metadata = safe_response_metadata(
        {"X-Cache": "HIT", "Authorization": "Bearer secret", "Set-Cookie": "secret"},
        b"private response text",
        12.3456,
    )
    assert metadata["headers"] == {"x-cache": "HIT"}
    assert metadata["response_bytes"] == 21
    assert metadata["elapsed_ms"] == 12.346
    assert "private response text" not in repr(metadata)


def test_cache_request_hints_preserve_structure_not_values():
    hints = cache_request_hints(
        b'{"messages":[{"cache_control":{"type":"ephemeral"}}],"prompt_cache_key":"private-key"}',
        {"anthropic-beta": "prompt-caching-2024-07-31"},
    )
    assert hints == {
        "cache_control_paths": ["$.messages[0].cache_control", "$.prompt_cache_key"],
        "anthropic_beta_present": True,
        "anthropic_beta_mentions_prompt_caching": True,
    }
    assert "private-key" not in repr(hints)


def test_detects_anthropic_error_in_successful_sse_response_without_message():
    body = b"\n".join([
        b"event: error",
        b'data: {"type":"error","error":{"type":"overloaded_error","message":"private provider detail"}}',
    ])

    metadata = response_error_metadata(body, "text/event-stream")

    assert metadata == {
        "detected": True,
        "source": "sse",
        "event_type": "error",
        "error_type": "overloaded_error",
    }
    assert "private provider detail" not in repr(metadata)


def test_detects_openai_stream_error_code_without_message_or_param():
    body = b'data: {"error":{"message":"private","type":"server_error","code":"rate_limit_exceeded","param":"secret"}}\n'

    metadata = response_error_metadata(body, "text/event-stream")

    assert metadata == {
        "detected": True,
        "source": "sse",
        "error_type": "server_error",
        "error_code": "rate_limit_exceeded",
    }
    assert "private" not in repr(metadata)
    assert "secret" not in repr(metadata)


def test_marks_malformed_sse_error_event_without_retaining_payload():
    body = b"event: error\ndata: this is private and is not json\n"

    assert response_error_metadata(body, "text/event-stream") == {
        "detected": True,
        "source": "sse",
        "event_type": "error",
    }


def test_ignores_normal_stream_events():
    body = b'data: {"type":"message_delta","delta":{"text":"private"}}\n'

    assert response_error_metadata(body, "text/event-stream") is None
