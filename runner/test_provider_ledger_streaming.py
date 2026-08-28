import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from provider_ledger_proxy import Ledger, LedgerProxyServer


class StreamingUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        return

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(b'data: {"id":"response-1","delta":"first"}\n\n')
        self.wfile.flush()
        time.sleep(0.4)
        self.wfile.write(
            b'data: {"id":"response-1","usage":{"prompt_tokens":3,'
            b'"completion_tokens":2,"total_tokens":5,'
            b'"prompt_tokens_details":{"cached_tokens":0}}}\n\n'
        )
        self.wfile.flush()
        self.close_connection = True


def test_proxy_relays_sse_before_upstream_finishes(tmp_path):
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), StreamingUpstream)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    ledger_path = tmp_path / "ledger.jsonl"
    proxy = LedgerProxyServer(
        ("127.0.0.1", 0),
        f"http://127.0.0.1:{upstream.server_port}",
        Ledger(ledger_path),
        5,
    )
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()

    started = time.monotonic()
    request = urllib.request.Request(
        f"http://127.0.0.1:{proxy.server_port}/v1/test/chat/completions",
        data=json.dumps({"model": "claude-sonnet-4-6", "messages": []}).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            first_line = response.readline()
            first_elapsed = time.monotonic() - started
            remainder = response.read()
    finally:
        proxy.shutdown()
        upstream.shutdown()

    assert b'"delta":"first"' in first_line
    assert first_elapsed < 0.3
    assert b'"usage"' in remainder
    record = json.loads(ledger_path.read_text().strip())
    assert record["provider_response_id"] == "response-1"
    assert record["raw_usage"]["total_tokens"] == 5
