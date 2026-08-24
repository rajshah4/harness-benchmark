from provider_ledger_proxy import request_metadata, response_usage_metadata, usage_from_response


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

    assert metadata == {
        "request_json": True,
        "model": "openhands/glm-5.2",
        "stream": True,
        "message_count": 1,
        "tool_count": 1,
    }
