"""OpenHands Enterprise API client for the benchmark.

Wraps the Replicated instance conversation and sandbox APIs with the
specific operations the benchmark controller needs.
"""
import json
import os
import time
import urllib.request
import urllib.error
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

ENTERPRISE_URL = "https://app.replicated.rajistics.com"
API_KEY = os.environ.get("OPENHANDS_API_KEY_ORG", "")
if not API_KEY:
    # Fall back to the .env file the user keeps credentials in
    env_path = os.path.expanduser("~/Code/install_replicate/.env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                if line.startswith("OPENHANDS_API_KEY_ORG="):
                    API_KEY = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break

if not API_KEY:
    raise RuntimeError("OPENHANDS_API_KEY_ORG not found in env or ~/Code/install_replicate/.env")


def _request(method, path, body=None, timeout=60, retries=3):
    """Make an API request with retry on timeout/connection errors."""
    url = f"{ENTERPRISE_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    last_exc = None
    for attempt in range(retries):
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if not raw:
                    return None
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            if e.code in (502, 503, 504) and attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
                continue
            raise RuntimeError(f"HTTP {e.code} on {method} {path}: {raw[:500]}") from None
        except (TimeoutError, OSError, ConnectionError) as e:
            last_exc = e
            if attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
                continue
    raise RuntimeError(f"Request failed after {retries} retries: {method} {path}: {last_exc}") from last_exc


@dataclass
class Sandbox:
    id: str
    status: str


def create_sandbox() -> Sandbox:
    resp = _request("POST", "/api/v1/sandboxes")
    return Sandbox(id=resp["id"], status=resp.get("status", "STARTING"))


def wait_for_sandbox(sandbox_id: str, timeout=180) -> Sandbox:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        resp = _request("GET", f"/api/v1/sandboxes/search?limit=50")
        for item in resp.get("items", []):
            if item["id"] == sandbox_id:
                status = item["status"]
                if status != last:
                    print(f"  sandbox {sandbox_id}: {status}")
                    last = status
                if status in ("RUNNING", "ERROR", "FAILED"):
                    return Sandbox(id=sandbox_id, status=status)
        time.sleep(3)
    raise TimeoutError(f"sandbox {sandbox_id} not RUNNING after {timeout}s")


def pause_sandbox(sandbox_id: str):
    try:
        _request("POST", f"/api/v1/sandboxes/{sandbox_id}/pause")
    except RuntimeError as e:
        print(f"  pause warning: {e}")


@dataclass
class Conversation:
    id: str
    start_task_id: str
    status: str
    sandbox_id: Optional[str] = None


# PI-GLM-5-2-Smoke profile (ACP-backed, verified on Replicated)
PI_PROFILE = "5b3e8bec-aa25-4082-884d-fb5f762d055a"
# OpenHands native GLM 5.2 agent profile (cached after first lookup)
OH_NATIVE_PROFILE = None


def list_agent_profiles() -> list[dict]:
    """List all agent profiles on the Replicated instance."""
    resp = _request("GET", "/api/agent-profiles")
    return resp.get("profiles", []) if isinstance(resp, dict) else resp


def list_llm_profiles() -> list[dict]:
    """List all LLM profiles on the Replicated instance."""
    resp = _request("GET", "/api/v1/settings/profiles")
    return resp.get("profiles", []) if isinstance(resp, dict) else resp


def find_or_create_native_profile() -> str:
    """Find or create an OpenHands native (kind=openhands) agent profile
    that references the OpenHands-GLM-5.2 LLM profile."""
    global OH_NATIVE_PROFILE
    if OH_NATIVE_PROFILE:
        return OH_NATIVE_PROFILE

    profiles = list_agent_profiles()
    for p in profiles:
        if (p.get("agent_kind") == "openhands"
                and p.get("llm_profile_ref") == "OpenHands-GLM-5.2"):
            OH_NATIVE_PROFILE = p["id"]
            print(f"  found native GLM agent profile: {p['name']} ({p['id']})")
            return OH_NATIVE_PROFILE

    # Verify the LLM profile exists
    llm_profiles = list_llm_profiles()
    llm_ref = None
    for lp in llm_profiles:
        if lp.get("name") == "OpenHands-GLM-5.2":
            llm_ref = lp["name"]
            break
    if not llm_ref:
        raise RuntimeError(
            "OpenHands-GLM-5.2 LLM profile not found. Create it in the UI first."
        )

    # Create the agent profile (POST /api/agent-profiles/{name})
    body = {
        "agent_kind": "openhands",
        "llm_profile_ref": llm_ref,
        "mcp_server_refs": None,
    }
    try:
        resp = _request("POST", f"/api/agent-profiles/OH-Native-GLM-5-2", body=body)
        OH_NATIVE_PROFILE = resp.get("id", "OH-Native-GLM-5-2")
        print(f"  created native GLM agent profile: {OH_NATIVE_PROFILE}")
    except RuntimeError as e:
        if "409" in str(e) or "already" in str(e).lower():
            # Re-list to find it
            for p in list_agent_profiles():
                if p.get("name") == "OH-Native-GLM-5-2":
                    OH_NATIVE_PROFILE = p["id"]
                    print(f"  found existing native GLM agent profile: {OH_NATIVE_PROFILE}")
                    return OH_NATIVE_PROFILE
        raise
    return OH_NATIVE_PROFILE


def start_conversation(
    message: str,
    *,
    profile_id: str,
    sandbox_id: Optional[str] = None,
    title: str = "benchmark",
    max_iterations: int = 500,
    run: bool = True,
    repository: Optional[str] = None,
    branch: Optional[str] = None,
) -> Conversation:
    body = {
        "agent_profile_id": profile_id,
        "title": title,
        "max_iterations": max_iterations,
        "initial_message": {
            "role": "user",
            "run": run,
            "content": [{"type": "text", "text": message}],
        },
    }
    if sandbox_id:
        body["sandbox_id"] = sandbox_id
    if repository:
        body["selected_repository"] = repository
    if branch:
        body["selected_branch"] = branch
    resp = _request("POST", "/api/v1/app-conversations", body=body)
    return Conversation(
        id=resp["id"],
        start_task_id=resp["id"],
        status=resp.get("status", "WORKING"),
        sandbox_id=resp.get("sandbox_id"),
    )


def poll_start_task(conv_id: str, timeout=120) -> dict:
    """Poll the start-task endpoint until READY/ERROR/FAILED.

    Returns the start-task item, which contains `app_conversation_id` once
    READY. The app_conversation_id is the ID to use with wait_for_conversation
    and get_final_response — it differs from the start-task ID.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = _request("GET", f"/api/v1/app-conversations/start-tasks?ids={conv_id}")
        if isinstance(resp, list) and len(resp) > 0:
            item = resp[0]
            if item.get("status") in ("READY", "ERROR", "FAILED"):
                return item
        time.sleep(2)
    raise TimeoutError(f"start task {conv_id} not READY after {timeout}s")


def get_app_conversation_id(start_task_id: str, timeout=120) -> str:
    """Resolve the app_conversation_id from a start-task ID.

    POST /api/v1/app-conversations returns a start-task ID, NOT the app
    conversation ID. The app conversation is created asynchronously and its
    ID appears in the start-task's `app_conversation_id` field once READY.
    """
    item = poll_start_task(start_task_id, timeout=timeout)
    app_conv_id = item.get("app_conversation_id")
    if not app_conv_id:
        raise RuntimeError(
            f"start task {start_task_id} READY but app_conversation_id is null"
        )
    return app_conv_id


def wait_for_conversation(conv_id: str, timeout=14400, poll_interval=10) -> dict:
    """Wait until execution_status is finished/error/stopped/paused.

    conv_id can be either a start-task ID or an app conversation ID.
    If it's a start-task ID, we resolve the app_conversation_id first.
    """
    # Try to resolve app_conversation_id if this is a start-task ID
    try:
        app_conv_id = get_app_conversation_id(conv_id, timeout=30)
        if app_conv_id != conv_id:
            print(f"  resolved app_conversation_id: {app_conv_id}")
            conv_id = app_conv_id
    except (TimeoutError, RuntimeError):
        pass  # might already be an app conversation ID

    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        resp = _request("GET", f"/api/v1/app-conversations?ids={conv_id}")
        item = None
        if isinstance(resp, list) and len(resp) > 0:
            item = resp[0]
        if item is None:
            resp = _request("GET", "/api/v1/app-conversations/search?limit=50")
            search_items = resp.get("items", []) if isinstance(resp, dict) else resp
            for s in search_items:
                if s.get("id") == conv_id:
                    item = s
                    break
        if item is not None:
            status = item.get("execution_status", "unknown")
            if status != last:
                print(f"  conversation {conv_id}: {status}")
                last = status
            if status in ("finished", "error", "stopped", "paused"):
                return item
        time.sleep(poll_interval)
    raise TimeoutError(f"conversation {conv_id} not finished after {timeout}s")


def _resolve_conv_id(conv_id: str) -> str:
    """Resolve a start-task ID to an app conversation ID if needed.

    POST /api/v1/app-conversations returns a start-task ID. Most other
    endpoints (events, send-message, app-conversations?ids=) need the app
    conversation ID. This helper resolves the difference transparently.
    """
    try:
        app_conv_id = get_app_conversation_id(conv_id, timeout=10)
        if app_conv_id != conv_id:
            return app_conv_id
    except (TimeoutError, RuntimeError):
        pass
    return conv_id


def get_final_response(conv_id: str) -> str:
    """Extract the agent's final text response from conversation events.

    Uses TIMESTAMP_DESC to get the latest events first, finds the last
    MessageEvent with role=assistant.
    """
    conv_id = _resolve_conv_id(conv_id)
    resp = _request("GET", f"/api/v1/conversation/{conv_id}/events/search?limit=100&sort_order=TIMESTAMP_DESC")
    items = resp.get("items", []) if isinstance(resp, dict) else []
    for event in items:
        if event.get("kind") == "MessageEvent":
            msg = event.get("llm_message", {})
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                content = msg.get("content", [])
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "text":
                        return c.get("text", "")
    # Fallback: concatenate streaming deltas from latest events
    chunks = []
    for event in items:
        if (event.get("kind") == "StreamingDeltaEvent"
                and event.get("source") == "agent"):
            chunks.append(event.get("content", ""))
    return "".join(chunks)


def grade_in_sandbox(sandbox_id: str, task: str, profile_id: Optional[str] = None) -> dict:
    """Start a grading conversation in the given sandbox to run the evaluator.

    The grading conversation:
    1. Clones the benchmark repo (which contains the evaluator + oracle + cases)
    2. Runs the evaluator against the candidate at /home/openhands/candidate/{task}
    3. Reports the score

    This keeps the hidden suite off the sandbox during the implementer campaign.
    The suite is only materialized when the grading conversation clones the repo.
    """
    grade_prompt = f"""Run this exact sequence of commands and report the output:

```bash
cd /home/openhands
git clone https://github.com/rajshah4/benchmark-harness.git
cd benchmark-harness
chmod +x tasks/{task}/oracle-bin-linux
python3 controller/evaluator.py /home/openhands/candidate/{task} --task-dir tasks/{task}
```

Report the full output, including the line that starts with SCORE:.
"""
    conv = start_conversation(
        grade_prompt,
        profile_id=profile_id or PI_PROFILE,
        sandbox_id=sandbox_id,
        title=f"benchmark-grade-{task}",
        max_iterations=50,
    )
    print(f"  grading conversation: {conv.id}")
    result = wait_for_conversation(conv.id, timeout=1800)
    print(f"  grading status: {result.get('execution_status')}")
    final_text = get_final_response(conv.id)
    return {"final_text": final_text, "conversation_id": conv.id}


def send_message(conv_id: str, text: str, run: bool = True) -> dict:
    """Send a follow-up message to an existing conversation.

    Uses POST /api/v1/app-conversations/{id}/send-message.
    The agent runs automatically if run=True (default).
    """
    conv_id = _resolve_conv_id(conv_id)
    body = {
        "role": "user",
        "run": run,
        "content": [{"type": "text", "text": text}],
    }
    return _request("POST", f"/api/v1/app-conversations/{conv_id}/send-message", body=body)


def read_sandbox_file(sandbox_id: str, file_path: str, profile_id: Optional[str] = None) -> str:
    """Read a file from the sandbox by starting a conversation, then using the file API.

    The file API requires a conversation ID, so we start a throwaway conversation
    on the sandbox to get one, then read the file from it.
    """
    # Start a conversation just to get a conversation_id on this sandbox
    conv = start_conversation(
        "echo ready",
        profile_id=profile_id or PI_PROFILE,
        sandbox_id=sandbox_id,
        title="file-read",
        max_iterations=5,
    )
    app_conv_id = _resolve_conv_id(conv.id)
    # Now read the file via the file API
    encoded_path = urllib.parse.quote(file_path, safe="")
    resp = _request("GET", f"/api/v1/app-conversations/{app_conv_id}/file?file_path={encoded_path}")
    return resp if isinstance(resp, str) else json.dumps(resp)


def run_in_sandbox(sandbox_id: str, command: str, profile_id: Optional[str] = None, timeout: int = 600) -> str:
    """Run a single command in a sandbox and return stdout.

    The command's output is written to /tmp/cmd_output.txt by the agent,
    then read back via the file API. This avoids LLM relay garbling the output.
    """
    prompt = f"""Run this command and save its output to a file:

```bash
{command} > /tmp/cmd_output.txt 2>&1
```

Then print "DONE". Do not print any other output.
"""
    conv = start_conversation(
        prompt,
        profile_id=profile_id or PI_PROFILE,
        sandbox_id=sandbox_id,
        title="orchestrator-probe",
        max_iterations=10,
    )
    wait_for_conversation(conv.id, timeout=timeout)
    # Read the output file via the file API
    app_conv_id = _resolve_conv_id(conv.id)
    encoded_path = urllib.parse.quote("/tmp/cmd_output.txt", safe="")
    resp = _request("GET", f"/api/v1/app-conversations/{app_conv_id}/file?file_path={encoded_path}")
    if isinstance(resp, str):
        return resp
    return json.dumps(resp)




if __name__ == "__main__":
    print(f"Enterprise URL: {ENTERPRISE_URL}")
    print(f"API key present: {bool(API_KEY)}")
    print(f"PI profile: {PI_PROFILE}")
