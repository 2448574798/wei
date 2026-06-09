from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright


DEFAULT_URL = "https://www.douyin.com/"
DEFAULT_USER_DATA_DIR = Path(r"D:\Download\playwright-user-data\edge-douyin-bridge")
EDGE_CANDIDATES = [
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
]


def resolve_edge_executable() -> Path:
    configured = os.getenv("PLAYWRIGHT_EDGE_EXECUTABLE", "").strip()
    if configured:
        path = Path(configured)
        if path.exists():
            return path
        raise FileNotFoundError(f"PLAYWRIGHT_EDGE_EXECUTABLE not found: {path}")

    for candidate in EDGE_CANDIDATES:
        if candidate.exists():
            return candidate
    raise FileNotFoundError("Microsoft Edge executable not found in common install paths.")


def resolve_user_data_dir() -> Path:
    configured = os.getenv("PLAYWRIGHT_EDGE_USER_DATA_DIR", "").strip()
    return Path(configured) if configured else DEFAULT_USER_DATA_DIR


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Launch Edge or run Browser Bridge CDP helpers.")
    parser.add_argument("--url", default=os.getenv("PLAYWRIGHT_DEFAULT_URL", DEFAULT_URL), help="Page URL to open.")
    parser.add_argument("--width", type=int, default=int(os.getenv("PLAYWRIGHT_BROWSER_WIDTH", "1440")))
    parser.add_argument("--height", type=int, default=int(os.getenv("PLAYWRIGHT_BROWSER_HEIGHT", "900")))
    parser.add_argument(
        "--bridge-stdio",
        action="store_true",
        help="Read a Browser Bridge JSON payload from stdin and emit a JSON result to stdout.",
    )
    return parser.parse_args()


def _resolve_bridge_page(context, reuse_existing_page: bool):
    if reuse_existing_page and context.pages:
        return context.pages[0], False
    return context.new_page(), True


def run_bridge_payload(payload: dict[str, Any]) -> dict[str, Any]:
    cdp_url = str(payload.get("cdp_url") or "").strip()
    action = str(payload.get("action") or "").strip()
    target_url = str(payload.get("url") or "").strip()
    wait_ms = max(0, min(int(payload.get("wait_ms") or 0), 60000))
    selector = str(payload.get("selector") or "body").strip() or "body"
    instruction = str(payload.get("instruction") or "").strip()
    reuse_existing_page = bool(payload.get("reuse_existing_page"))
    close_page = bool(payload.get("close_page"))

    if not cdp_url:
        raise ValueError("cdp_url is required.")
    if action not in {"open", "snapshot"}:
        raise ValueError(f"Unsupported action: {action or '<empty>'}")
    if not target_url:
        raise ValueError("url is required.")

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(cdp_url)
        contexts = browser.contexts
        if not contexts:
            raise RuntimeError("No browser context available via CDP.")

        context = contexts[0]
        page, created_page = _resolve_bridge_page(context, reuse_existing_page)
        try:
            page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
            if wait_ms > 0:
                page.wait_for_timeout(wait_ms)

            result = {
                "ok": True,
                "title": page.title(),
                "url": page.url,
            }
            if action == "snapshot":
                try:
                    text = page.locator(selector).inner_text(timeout=5000)
                except Exception:
                    text = page.locator("body").inner_text(timeout=5000)
                compact = re.sub(r"\s+", " ", (text or "").strip())
                result["instruction"] = instruction
                result["text"] = compact[:3000]
            return result
        finally:
            if close_page and created_page:
                try:
                    page.close()
                except Exception:
                    pass


def run_bridge_stdio() -> int:
    try:
        payload = json.load(sys.stdin)
        result = run_bridge_payload(payload)
        sys.stdout.write(json.dumps(result, ensure_ascii=False))
        sys.stdout.flush()
        os._exit(0)
    except Exception as exc:
        sys.stderr.write(str(exc))
        sys.stderr.flush()
        os._exit(1)


def run_interactive_open(url: str, width: int, height: int) -> int:
    edge_executable = resolve_edge_executable()
    user_data_dir = resolve_user_data_dir()
    user_data_dir.mkdir(parents=True, exist_ok=True)

    print(f"edge_executable: {edge_executable}")
    print(f"user_data_dir: {user_data_dir}")
    print(f"url: {url}")

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            executable_path=str(edge_executable),
            headless=False,
            viewport={"width": width, "height": height},
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            print(f"title: {page.title()}")
            print(f"current_url: {page.url}")
            print("Browser is ready. Close the Edge window when you are done.")
            while context.browser.is_connected():
                time.sleep(1)
        finally:
            if context.browser.is_connected():
                context.close()

    return 0


def main() -> int:
    args = parse_args()
    if args.bridge_stdio:
        return run_bridge_stdio()
    return run_interactive_open(args.url, args.width, args.height)


if __name__ == "__main__":
    raise SystemExit(main())
