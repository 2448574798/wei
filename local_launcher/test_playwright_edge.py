from __future__ import annotations

import time
from pathlib import Path

from playwright.sync_api import sync_playwright


EDGE_EXECUTABLE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
USER_DATA_DIR = Path(r"D:\Download\playwright-user-data\edge-douyin")
DEFAULT_URL = "https://www.douyin.com/"


def main() -> int:
    if not EDGE_EXECUTABLE.exists():
        raise FileNotFoundError(f"Edge executable not found: {EDGE_EXECUTABLE}")

    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            executable_path=str(EDGE_EXECUTABLE),
            headless=False,
            viewport={"width": 1440, "height": 900},
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(DEFAULT_URL, wait_until="domcontentloaded", timeout=30000)
            print(f"title: {page.title()}")
            print(f"url: {page.url}")
            print("Browser is ready. Log in manually if needed, then close the browser window when finished.")
            while context.browser.is_connected():
                time.sleep(1)
        finally:
            if context.browser.is_connected():
                context.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
