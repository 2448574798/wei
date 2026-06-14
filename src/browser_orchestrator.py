import os
from contextlib import suppress
from typing import Any

import requests

from src.browser_worker_hub import browser_worker_hub
from src.runtime_config import (
    BROWSER_WORKER_DEFAULT_ID,
    BROWSER_WORKER_ENABLED,
    BROWSER_WORKER_REQUEST_TIMEOUT,
)


class CloudBrowserOrchestrator:
    """Cloud-side task core for local browser work.

    Preferred path:
    Cloud Agent -> this orchestrator -> Browser Worker websocket -> Playwright MCP.

    The HTTP Browser Bridge fallback is kept for local development and older launchers,
    but it is only used when the websocket worker mode is disabled.
    """

    def bridge_url(self) -> str:
        url = os.getenv("BROWSER_BRIDGE_URL", "").strip().rstrip("/")
        if url:
            return url
        host = os.getenv("BROWSER_BRIDGE_HOST", "").strip()
        port = os.getenv("BROWSER_BRIDGE_PORT", "").strip()
        if host and port:
            return f"http://{host}:{port}"
        return ""

    def bridge_timeout(self) -> int:
        return int(os.getenv("BROWSER_BRIDGE_TIMEOUT", "60"))

    def bridge_configured(self) -> bool:
        return bool(self.bridge_url())

    def worker_id(self) -> str:
        configured = os.getenv("BROWSER_WORKER_ID", "").strip()
        return configured or BROWSER_WORKER_DEFAULT_ID

    def worker_enabled(self) -> bool:
        return BROWSER_WORKER_ENABLED

    def request(self, command: str, payload: dict[str, Any] | None = None, *, timeout: int | None = None) -> dict[str, Any]:
        command_name = str(command or "").strip()
        if not command_name:
            raise ValueError("Browser command cannot be empty.")

        timeout_sec = timeout or BROWSER_WORKER_REQUEST_TIMEOUT
        if self.worker_enabled():
            return self._worker_request(command_name, payload or {}, timeout_sec=timeout_sec)

        return self._legacy_bridge_request(command_name, payload or {}, timeout=timeout)

    def start_legacy_job(self, payload: dict[str, Any], *, timeout: int = 20) -> dict[str, Any]:
        if self.worker_enabled():
            raise RuntimeError("Legacy Browser Bridge jobs are disabled while Browser Worker mode is enabled.")
        return self._bridge_post("/jobs/start", payload, timeout=timeout)

    def get_legacy_job(self, job_id: str, *, timeout: int = 15) -> dict[str, Any]:
        return self._bridge_get(f"/jobs/{job_id}", timeout=timeout)

    def cancel_legacy_job(self, job_id: str) -> None:
        with suppress(Exception):
            self._bridge_post(f"/jobs/{job_id}/cancel", {})

    def _worker_request(self, command: str, payload: dict[str, Any], *, timeout_sec: int) -> dict[str, Any]:
        worker_name = self.worker_id()
        if not worker_name:
            raise RuntimeError("BROWSER_WORKER_ID is not configured.")
        try:
            return browser_worker_hub.request_sync(worker_name, command, payload, timeout_sec=timeout_sec)
        except Exception as exc:
            raise RuntimeError(f"Browser worker {command} failed: {exc}") from exc

    def _legacy_bridge_request(self, command: str, payload: dict[str, Any], *, timeout: int | None = None) -> dict[str, Any]:
        if command == "browser.tabs":
            return self._bridge_get("/mcp/tabs", timeout=timeout)
        if command == "browser.navigate":
            return self._bridge_post("/mcp/navigate", {"url": str(payload.get("url") or "").strip()}, timeout=timeout)
        if command == "browser.snapshot":
            return self._bridge_post("/mcp/snapshot", payload, timeout=timeout)
        if command == "browser.interact":
            return self._bridge_post("/mcp/interact", payload, timeout=timeout)
        raise ValueError(f"Unsupported browser command: {command}")

    def _bridge_headers(self) -> dict[str, str]:
        token = os.getenv("BROWSER_BRIDGE_TOKEN", "").strip()
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _bridge_url(self, path: str) -> str:
        base_url = self.bridge_url()
        if not base_url:
            raise RuntimeError("BROWSER_BRIDGE_URL is not configured.")
        return base_url + path

    def _bridge_get(self, path: str, *, timeout: int | None = None) -> dict[str, Any]:
        response = requests.get(
            self._bridge_url(path),
            headers=self._bridge_headers(),
            timeout=timeout or self.bridge_timeout(),
        )
        return self._decode_bridge_response(response, f"Browser Bridge GET {path}")

    def _bridge_post(self, path: str, payload: dict[str, Any], *, timeout: int | None = None) -> dict[str, Any]:
        response = requests.post(
            self._bridge_url(path),
            headers=self._bridge_headers(),
            json=payload,
            timeout=timeout or self.bridge_timeout(),
        )
        return self._decode_bridge_response(response, f"Browser Bridge POST {path}")

    def _decode_bridge_response(self, response: requests.Response, label: str) -> dict[str, Any]:
        if response.ok:
            data = response.json()
            return data if isinstance(data, dict) else {}

        detail = ""
        with suppress(Exception):
            payload = response.json()
            detail = str(payload.get("detail") or "").strip()
        message = detail or response.text.strip() or response.reason or f"HTTP {response.status_code}"
        raise RuntimeError(f"{label} failed: {message}")


browser_orchestrator = CloudBrowserOrchestrator()


def browser_bridge_is_configured() -> bool:
    return browser_orchestrator.bridge_configured()


def browser_worker_is_configured() -> bool:
    return browser_orchestrator.worker_enabled()


def get_browser_bridge_timeout() -> int:
    return browser_orchestrator.bridge_timeout()
