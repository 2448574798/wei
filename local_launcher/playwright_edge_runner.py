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


def configure_stdio_utf8() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


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
        return context.pages[-1], False
    return context.new_page(), True


def _compact_text(text: str, limit: int = 3000) -> str:
    compact = re.sub(r"\s+", " ", (text or "").strip())
    return compact[:limit]


def _resolve_locator(page, target: dict[str, Any]):
    selector = str(target.get("selector") or "").strip()
    text = str(target.get("text") or "").strip()
    role = str(target.get("role") or "").strip()
    name = str(target.get("name") or target.get("text") or "").strip()
    label = str(target.get("label") or "").strip()
    placeholder = str(target.get("placeholder") or "").strip()
    exact = bool(target.get("exact"))

    if selector:
        locator = page.locator(selector)
    elif text:
        locator = page.get_by_text(text, exact=exact)
    elif role:
        locator = page.get_by_role(role, name=name or None, exact=exact)
    elif label:
        locator = page.get_by_label(label, exact=exact)
    elif placeholder:
        locator = page.get_by_placeholder(placeholder, exact=exact)
    else:
        raise ValueError("Each interaction step must provide selector, text, role, label, or placeholder.")

    if target.get("last"):
        return locator.last
    if "nth" in target and target.get("nth") is not None:
        return locator.nth(int(target.get("nth")))
    return locator.first


def _expand_targets(step: dict[str, Any]) -> list[dict[str, Any]]:
    targets = step.get("targets")
    if isinstance(targets, list) and targets:
        return [item for item in targets if isinstance(item, dict)]

    expanded: list[dict[str, Any]] = []
    selectors = step.get("selectors")
    if isinstance(selectors, list):
        expanded.extend({"selector": value} for value in selectors if str(value or "").strip())
    texts = step.get("texts")
    if isinstance(texts, list):
        expanded.extend({"text": value} for value in texts if str(value or "").strip())
    roles = step.get("roles")
    if isinstance(roles, list):
        for item in roles:
            if isinstance(item, dict):
                expanded.append(item)
    return expanded


def _extract_text(locator, timeout: int) -> str:
    try:
        return locator.inner_text(timeout=timeout)
    except Exception:
        return locator.text_content(timeout=timeout) or ""


def _run_interaction_steps(page, steps: list[dict[str, Any]], instruction: str) -> dict[str, Any]:
    extracts: list[dict[str, str]] = []
    step_results: list[dict[str, Any]] = []
    snapshot_text = ""

    for index, raw_step in enumerate(steps, start=1):
        step = raw_step if isinstance(raw_step, dict) else {}
        step_type = str(step.get("type") or "").strip().lower()
        if not step_type:
            raise ValueError(f"Step {index} is missing a type.")

        timeout = max(1000, min(int(step.get("timeout_ms") or 10000), 60000))
        wait_after = max(0, min(int(step.get("wait_ms") or 0), 60000))

        if step_type == "wait":
            page.wait_for_timeout(max(0, min(int(step.get("ms") or step.get("wait_ms") or 1000), 60000)))
            step_results.append({"index": index, "type": step_type, "status": "ok"})
            continue

        if step_type == "goto":
            target_url = str(step.get("url") or "").strip()
            if not target_url:
                raise ValueError(f"Step {index} goto requires a url.")
            page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
            if wait_after > 0:
                page.wait_for_timeout(wait_after)
            step_results.append({"index": index, "type": step_type, "status": "ok", "url": page.url})
            continue

        if step_type == "click":
            locator = _resolve_locator(page, step)
            locator.wait_for(state="visible", timeout=timeout)
            locator.click(timeout=timeout, force=bool(step.get("force")))
            if wait_after > 0:
                page.wait_for_timeout(wait_after)
            step_results.append({"index": index, "type": step_type, "status": "ok"})
            continue

        if step_type == "click_any":
            targets = _expand_targets(step)
            if not targets:
                raise ValueError(f"Step {index} click_any requires targets, selectors, texts, or roles.")
            last_error = "No candidates attempted."
            clicked_target = ""
            for target in targets:
                try:
                    locator = _resolve_locator(page, target)
                    locator.wait_for(state="visible", timeout=timeout)
                    locator.click(timeout=timeout, force=bool(step.get("force")))
                    clicked_target = json.dumps(target, ensure_ascii=False)
                    last_error = ""
                    break
                except Exception as exc:
                    last_error = str(exc)
            if last_error:
                raise RuntimeError(f"Step {index} click_any failed: {last_error}")
            if wait_after > 0:
                page.wait_for_timeout(wait_after)
            step_results.append({"index": index, "type": step_type, "status": "ok", "target": clicked_target})
            continue

        if step_type == "press":
            key = str(step.get("key") or "").strip()
            if not key:
                raise ValueError(f"Step {index} press requires a key.")
            if any(step.get(field) for field in ("selector", "text", "role", "label", "placeholder")):
                locator = _resolve_locator(page, step)
                locator.press(key, timeout=timeout)
            else:
                page.keyboard.press(key)
            if wait_after > 0:
                page.wait_for_timeout(wait_after)
            step_results.append({"index": index, "type": step_type, "status": "ok", "key": key})
            continue

        if step_type == "fill":
            value = str(step.get("value") or "").strip()
            locator = _resolve_locator(page, step)
            locator.wait_for(state="visible", timeout=timeout)
            locator.fill(value, timeout=timeout)
            if wait_after > 0:
                page.wait_for_timeout(wait_after)
            step_results.append({"index": index, "type": step_type, "status": "ok"})
            continue

        if step_type == "extract_text":
            locator = _resolve_locator(page, step)
            locator.wait_for(state="visible", timeout=timeout)
            text = _compact_text(_extract_text(locator, timeout), int(step.get("limit") or 1000))
            name = str(step.get("name") or f"extract_{index}").strip()
            extracts.append({"name": name, "text": text})
            step_results.append({"index": index, "type": step_type, "status": "ok", "name": name})
            continue

        if step_type == "extract_any_text":
            targets = _expand_targets(step)
            if not targets:
                raise ValueError(f"Step {index} extract_any_text requires targets, selectors, texts, or roles.")
            last_error = "No candidates attempted."
            extracted_text = ""
            matched_target = ""
            for target in targets:
                try:
                    locator = _resolve_locator(page, target)
                    locator.wait_for(state="visible", timeout=timeout)
                    extracted_text = _compact_text(_extract_text(locator, timeout), int(step.get("limit") or 1000))
                    matched_target = json.dumps(target, ensure_ascii=False)
                    last_error = ""
                    break
                except Exception as exc:
                    last_error = str(exc)
            if last_error:
                raise RuntimeError(f"Step {index} extract_any_text failed: {last_error}")
            name = str(step.get("name") or f"extract_{index}").strip()
            extracts.append({"name": name, "text": extracted_text})
            step_results.append({"index": index, "type": step_type, "status": "ok", "name": name, "target": matched_target})
            continue

        if step_type == "snapshot":
            selector = str(step.get("selector") or "body").strip() or "body"
            locator = page.locator(selector).first
            try:
                locator.wait_for(state="visible", timeout=timeout)
                snapshot_text = _compact_text(_extract_text(locator, timeout), int(step.get("limit") or 3000))
            except Exception:
                snapshot_text = _compact_text(_extract_text(page.locator("body").first, timeout), int(step.get("limit") or 3000))
            step_results.append({"index": index, "type": step_type, "status": "ok", "selector": selector})
            continue

        raise ValueError(f"Unsupported interaction step type: {step_type}")

    if not snapshot_text:
        try:
            snapshot_text = _compact_text(_extract_text(page.locator("body").first, 5000), 3000)
        except Exception:
            snapshot_text = ""

    return {
        "instruction": instruction,
        "text": snapshot_text,
        "extracts": extracts,
        "step_results": step_results,
    }


def run_bridge_payload(payload: dict[str, Any]) -> dict[str, Any]:
    cdp_url = str(payload.get("cdp_url") or "").strip()
    action = str(payload.get("action") or "").strip()
    target_url = str(payload.get("url") or "").strip()
    wait_ms = max(0, min(int(payload.get("wait_ms") or 0), 60000))
    selector = str(payload.get("selector") or "body").strip() or "body"
    instruction = str(payload.get("instruction") or "").strip()
    reuse_existing_page = bool(payload.get("reuse_existing_page"))
    close_page = bool(payload.get("close_page"))
    steps = payload.get("steps") or []

    if not cdp_url:
        raise ValueError("cdp_url is required.")
    if action not in {"open", "snapshot", "interact"}:
        raise ValueError(f"Unsupported action: {action or '<empty>'}")
    if action in {"open", "snapshot"} and not target_url:
        raise ValueError("url is required.")
    if action == "interact" and not isinstance(steps, list):
        raise ValueError("steps must be a list for interact actions.")

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(cdp_url)
        contexts = browser.contexts
        if not contexts:
            raise RuntimeError("No browser context available via CDP.")

        context = contexts[0]
        page, created_page = _resolve_bridge_page(context, reuse_existing_page)
        try:
            if target_url:
                page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
            if wait_ms > 0 and action in {"open", "snapshot", "interact"}:
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
                result["instruction"] = instruction
                result["text"] = _compact_text(text, 3000)
            if action == "interact":
                result.update(_run_interaction_steps(page, steps, instruction))
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
    configure_stdio_utf8()
    args = parse_args()
    if args.bridge_stdio:
        return run_bridge_stdio()
    return run_interactive_open(args.url, args.width, args.height)


if __name__ == "__main__":
    raise SystemExit(main())
