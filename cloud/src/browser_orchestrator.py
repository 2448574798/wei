from typing import Any

from src.browser_worker_hub import browser_worker_hub
from src.runtime_config import (
    BROWSER_WORKER_DEFAULT_ID,
    BROWSER_WORKER_ENABLED,
    BROWSER_WORKER_REQUEST_TIMEOUT,
)


class CloudBrowserOrchestrator:
    """Cloud-side task core for local browser work.

    Cloud Agent -> this orchestrator -> Browser Worker websocket -> Playwright MCP.
    """

    def worker_id(self) -> str:
        return BROWSER_WORKER_DEFAULT_ID

    def worker_enabled(self) -> bool:
        return BROWSER_WORKER_ENABLED

    def request_timeout(self) -> int:
        return BROWSER_WORKER_REQUEST_TIMEOUT

    def request(self, command: str, payload: dict[str, Any] | None = None, *, timeout: int | None = None) -> dict[str, Any]:
        command_name = str(command or "").strip()
        if not command_name:
            raise ValueError("Browser command cannot be empty.")
        if not self.worker_enabled():
            raise RuntimeError("Browser Worker websocket mode is disabled. Set BROWSER_WORKER_ENABLED=true.")
        timeout_sec = timeout or BROWSER_WORKER_REQUEST_TIMEOUT
        return self._worker_request(command_name, payload or {}, timeout_sec=timeout_sec)

    def _worker_request(self, command: str, payload: dict[str, Any], *, timeout_sec: int) -> dict[str, Any]:
        worker_name = self.worker_id()
        if not worker_name:
            raise RuntimeError("BROWSER_WORKER_ID is not configured.")
        try:
            return browser_worker_hub.request_sync(worker_name, command, payload, timeout_sec=timeout_sec)
        except Exception as exc:
            raise RuntimeError(f"Browser worker {command} failed: {exc}") from exc


browser_orchestrator = CloudBrowserOrchestrator()


def browser_worker_is_configured() -> bool:
    return browser_orchestrator.worker_enabled()


def get_browser_request_timeout() -> int:
    return browser_orchestrator.request_timeout()
