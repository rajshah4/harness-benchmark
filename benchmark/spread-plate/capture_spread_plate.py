#!/usr/bin/env python3
"""Capture comparable states from a completed spread plate artifact."""

from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright

from verify_spread_plate import ACTIONS, static_server


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    with static_server(args.workspace.resolve()) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.goto(url, wait_until="networkidle")
        page.screenshot(path=str(output_dir / "01-initial.png"), full_page=True)

        page.evaluate("async () => await window.spreadPlateLab.perform('sample')")
        page.wait_for_timeout(250)
        page.screenshot(path=str(output_dir / "02-invalid-action.png"), full_page=True)

        page.evaluate("() => window.spreadPlateLab.reset()")
        for action in ACTIONS:
            page.evaluate(
                "async action => await window.spreadPlateLab.perform(action)",
                action,
            )
        page.wait_for_timeout(1200)
        page.screenshot(path=str(output_dir / "03-complete.png"), full_page=True)

        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(250)
        page.screenshot(path=str(output_dir / "04-mobile-complete.png"), full_page=True)
        browser.close()
    print(output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
