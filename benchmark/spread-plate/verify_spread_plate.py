#!/usr/bin/env python3
"""Instructor-owned browser verifier for the spread plate artifact task."""

from __future__ import annotations

import argparse
import contextlib
import functools
import http.server
import json
import socketserver
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright


EQUIPMENT = (
    "yeast-broth",
    "cotton-plug",
    "agar-plate",
    "inoculation-loop",
    "alcohol-lamp",
    "clean-bench",
)
ACTIONS = ("sterilize", "cool", "sample", "inoculate", "spread", "incubate")
NEXT_STEPS = ("cool", "sample", "inoculate", "spread", "incubate", "complete")


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


@contextlib.contextmanager
def static_server(root: Path):
    handler = functools.partial(QuietHandler, directory=str(root))
    with socketserver.TCPServer(("127.0.0.1", 0), handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}/index.html"
        finally:
            server.shutdown()
            thread.join(timeout=2)


def require(condition: bool, message: str, failures: list[str]) -> None:
    if not condition:
        failures.append(message)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--screenshot", type=Path)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    index = workspace / "index.html"
    failures: list[str] = []
    require(index.is_file(), "index.html is missing", failures)
    if failures:
        print("FAIL\n" + "\n".join(f"- {item}" for item in failures))
        return 1

    source = index.read_text(encoding="utf-8", errors="replace").lower()
    require("<title" in source, "index.html has no document title", failures)
    remote_markers = (
        'src="http://',
        "src='http://",
        'src="https://',
        "src='https://",
        'href="http://',
        "href='http://",
        'href="https://',
        "href='https://",
    )
    require(
        not any(marker in source for marker in remote_markers),
        "remote assets or external dependencies are not allowed",
        failures,
    )

    console_errors: list[str] = []
    page_errors: list[str] = []
    with static_server(workspace) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on(
            "console",
            lambda message: console_errors.append(message.text)
            if message.type == "error"
            else None,
        )
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        response = page.goto(url, wait_until="networkidle")
        require(response is not None and response.ok, "index.html did not load successfully", failures)

        root = page.locator('[data-testid="lab-root"]')
        require(root.count() == 1, "expected one data-testid=lab-root", failures)
        for item in EQUIPMENT:
            locator = page.locator(f'[data-testid="equipment-{item}"]')
            require(locator.count() >= 1, f"missing equipment marker: {item}", failures)
            if locator.count():
                require(locator.first.get_attribute("draggable") == "true", f"{item} is not draggable", failures)
                require(locator.first.is_visible(), f"{item} is not visible", failures)

        for marker in ("step-instruction", "feedback", "progress"):
            locator = page.locator(f'[data-testid="{marker}"]')
            require(locator.count() == 1, f"expected one data-testid={marker}", failures)
            if locator.count():
                require(locator.is_visible(), f"{marker} is not visible", failures)

        for action in ACTIONS + ("reset",):
            control = page.locator(f'[data-action="{action}"]')
            require(control.count() >= 1, f"missing interactive control for action: {action}", failures)

        contract = page.evaluate(
            """() => ({
              exists: typeof window.spreadPlateLab === 'object' && window.spreadPlateLab !== null,
              getState: typeof window.spreadPlateLab?.getState === 'function',
              perform: typeof window.spreadPlateLab?.perform === 'function',
              reset: typeof window.spreadPlateLab?.reset === 'function'
            })"""
        )
        require(all(contract.values()), f"behavior contract is incomplete: {json.dumps(contract)}", failures)

        if all(contract.values()):
            initial = page.evaluate("() => window.spreadPlateLab.getState()")
            require(initial.get("step") == "sterilize", "initial step must be sterilize", failures)
            require(initial.get("completed") is False, "initial completed must be false", failures)
            require(initial.get("history") == [], "initial history must be empty", failures)

            invalid = page.evaluate("async () => await window.spreadPlateLab.perform('sample')")
            after_invalid = page.evaluate("() => window.spreadPlateLab.getState()")
            require(invalid.get("ok") is False, "out-of-order sample action must return ok=false", failures)
            require(after_invalid.get("step") == "sterilize", "invalid action advanced the experiment", failures)
            feedback = page.locator('[data-testid="feedback"]')
            if feedback.count():
                require(bool(feedback.inner_text().strip()), "invalid action did not show visible feedback", failures)

            for action, next_step in zip(ACTIONS, NEXT_STEPS):
                result = page.evaluate(
                    "async action => await window.spreadPlateLab.perform(action)",
                    action,
                )
                state = page.evaluate("() => window.spreadPlateLab.getState()")
                require(result.get("ok") is True, f"valid action {action} did not return ok=true", failures)
                require(state.get("step") == next_step, f"after {action}, expected step {next_step}, got {state.get('step')}", failures)
                require(state.get("history") == list(ACTIONS[: ACTIONS.index(action) + 1]), f"history is incorrect after {action}", failures)

            complete = page.evaluate("() => window.spreadPlateLab.getState()")
            require(complete.get("completed") is True, "completed must be true after incubation", failures)
            if root.count():
                require(root.get_attribute("data-step") == "complete", "lab-root data-step did not reach complete", failures)

            page.evaluate("() => window.spreadPlateLab.reset()")
            reset = page.evaluate("() => window.spreadPlateLab.getState()")
            require(reset.get("step") == "sterilize", "reset did not restore sterilize step", failures)
            require(reset.get("history") == [], "reset did not clear history", failures)

        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(200)
        if root.count():
            require(root.is_visible(), "lab is not visible at a phone-sized viewport", failures)
            box = root.bounding_box()
            require(box is not None and box["width"] <= 410, "lab overflows the phone viewport", failures)

        if args.screenshot:
            args.screenshot.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(args.screenshot), full_page=True)
        browser.close()

    require(not console_errors, f"browser console errors: {console_errors}", failures)
    require(not page_errors, f"uncaught browser errors: {page_errors}", failures)
    if failures:
        print("FAIL\n" + "\n".join(f"- {item}" for item in failures))
        return 1
    print("PASS: spread plate lab behavior contract satisfied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
