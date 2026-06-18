from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect as websocket_connect


def _default_user_data_dir() -> Path:
    local_appdata = os.getenv("LOCALAPPDATA", "").strip()
    if local_appdata:
        return Path(local_appdata) / "WeiAgent" / "playwright-user-data" / "edge-worker"
    return Path.home() / ".wei-agent" / "playwright-user-data" / "edge-worker"


DEFAULT_USER_DATA_DIR = _default_user_data_dir()
DEFAULT_MCP_PROTOCOL_VERSION = "2025-11-25"
DEFAULT_BROWSER_DIAGNOSTICS_ENABLED = "false"


class PlaywrightMcpClient:
    def __init__(self, *, user_data_dir: Path) -> None:
        self.enabled = os.getenv("PLAYWRIGHT_MCP_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
        self.command = self._resolve_command(user_data_dir)
        self.cwd = self._resolve_cwd()
        self.protocol_version = os.getenv("PLAYWRIGHT_MCP_PROTOCOL_VERSION", DEFAULT_MCP_PROTOCOL_VERSION).strip() or DEFAULT_MCP_PROTOCOL_VERSION
        self.startup_timeout_sec = max(5, min(int(os.getenv("PLAYWRIGHT_MCP_STARTUP_TIMEOUT", "20") or "20"), 120))
        self.request_timeout_sec = max(5, min(int(os.getenv("PLAYWRIGHT_MCP_REQUEST_TIMEOUT", "30") or "30"), 180))

        self._lock = threading.Lock()
        self._io_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[int, dict[str, Any]] = {}
        self._next_id = 1
        self._process: subprocess.Popen[str] | None = None
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._server_info: dict[str, Any] = {}
        self._server_capabilities: dict[str, Any] = {}
        self._tool_cache: list[dict[str, Any]] = []
        self._tool_cache_at = 0.0
        self._initialized = False
        self._last_error = ""
        self._last_started_at = 0.0
        self._stderr_tail: deque[str] = deque(maxlen=40)
        self._last_notification: dict[str, Any] | None = None

    def _resolve_command(self, user_data_dir: Path) -> list[str]:
        command = os.getenv("PLAYWRIGHT_MCP_COMMAND", "").strip()
        args_json = os.getenv("PLAYWRIGHT_MCP_ARGS_JSON", "").strip()
        if command:
            args: list[str] = []
            if args_json:
                parsed = json.loads(args_json)
                if not isinstance(parsed, list):
                    raise ValueError("PLAYWRIGHT_MCP_ARGS_JSON must be a JSON array.")
                args = [str(item) for item in parsed]
            return [command, *args]

        browser_name = os.getenv("PLAYWRIGHT_MCP_BROWSER", "msedge").strip() or "msedge"
        default_args = [
            "@playwright/mcp@latest",
            f"--browser={browser_name}",
            f"--user-data-dir={user_data_dir}",
        ]
        if os.getenv("PLAYWRIGHT_MCP_HEADLESS", "").strip().lower() in {"1", "true", "yes", "on"}:
            default_args.append("--headless")
        return ["npx", *default_args]

    def _resolve_cwd(self) -> Path | None:
        configured = os.getenv("PLAYWRIGHT_MCP_CWD", "").strip()
        if not configured:
            return None
        path = Path(configured)
        return path if path.exists() else None

    def _build_process_env(self) -> dict[str, str]:
        env = dict(os.environ)
        command_path = Path(self.command[0]) if self.command else None
        if command_path and command_path.is_absolute():
            command_dir = str(command_path.parent)
            existing_path = env.get("PATH", "")
            parts = [part for part in existing_path.split(os.pathsep) if part]
            lowered = {part.lower() for part in parts}
            if command_dir.lower() not in lowered:
                env["PATH"] = os.pathsep.join([command_dir, *parts]) if parts else command_dir
        return env

    def _set_last_error(self, message: str) -> None:
        self._last_error = re.sub(r"\s+", " ", (message or "").strip())

    def _stderr_tail_list(self) -> list[str]:
        return list(self._stderr_tail)

    def status(self, *, ensure_started: bool = False, refresh_tools: bool = False) -> dict[str, Any]:
        if ensure_started and self.enabled:
            try:
                self.ensure_ready(refresh_tools=refresh_tools)
            except Exception as exc:
                self._set_last_error(str(exc))
        with self._lock:
            process = self._process
            running = bool(process and process.poll() is None)
            available = running and self._initialized
            return {
                "enabled": self.enabled,
                "available": available,
                "running": running,
                "initialized": self._initialized,
                "command": list(self.command),
                "cwd": str(self.cwd) if self.cwd else "",
                "protocol_version": self.protocol_version,
                "server_info": dict(self._server_info),
                "tool_count": len(self._tool_cache),
                "tool_names": [str(item.get("name") or "").strip() for item in self._tool_cache if str(item.get("name") or "").strip()],
                "last_error": self._last_error,
                "stderr_tail": self._stderr_tail_list(),
                "pid": process.pid if process and process.poll() is None else None,
                "started_at": self._last_started_at or 0.0,
                "last_notification": self._last_notification,
            }

    def restart(self) -> dict[str, Any]:
        self.stop()
        if self.enabled:
            self.ensure_ready(refresh_tools=True)
        return self.status()

    def _terminate_process_tree(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                check=False,
            )
            return

        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def stop(self) -> None:
        with self._lock:
            process = self._process
            self._process = None
            self._initialized = False
            self._server_info = {}
            self._server_capabilities = {}
            self._tool_cache = []
            self._tool_cache_at = 0.0

        if process:
            self._terminate_process_tree(process)

        self._fail_pending("Playwright MCP server stopped.")

    def ensure_ready(self, *, refresh_tools: bool = False) -> None:
        if not self.enabled:
            raise RuntimeError("Playwright MCP integration is disabled.")

        with self._lock:
            process = self._process
            if process and process.poll() is not None:
                self._process = None
                self._initialized = False

            if self._process is None:
                self._start_process_locked()

        if not self._initialized:
            init_result = self._request(
                "initialize",
                {
                    "protocolVersion": self.protocol_version,
                    "capabilities": {},
                    "clientInfo": {"name": "wei-browser-bridge", "version": "0.1"},
                },
                timeout_sec=self.startup_timeout_sec,
            )
            self._server_info = init_result.get("serverInfo") if isinstance(init_result, dict) else {}
            if not isinstance(self._server_info, dict):
                self._server_info = {}
            self._server_capabilities = init_result.get("capabilities") if isinstance(init_result, dict) else {}
            if not isinstance(self._server_capabilities, dict):
                self._server_capabilities = {}
            self._notify("notifications/initialized", {})
            self._initialized = True

        if refresh_tools or not self._tool_cache or (time.time() - self._tool_cache_at) > 300:
            result = self._request("tools/list", {}, timeout_sec=self.request_timeout_sec)
            tools = result.get("tools") if isinstance(result, dict) else []
            if not isinstance(tools, list):
                raise RuntimeError("Playwright MCP tools/list returned an invalid payload.")
            self._tool_cache = [item for item in tools if isinstance(item, dict)]
            self._tool_cache_at = time.time()
        self._set_last_error("")

    def tools(self, *, refresh: bool = False) -> dict[str, Any]:
        self.ensure_ready(refresh_tools=refresh)
        return {
            "server_info": dict(self._server_info),
            "protocol_version": self.protocol_version,
            "tools": list(self._tool_cache),
        }

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        tool_name = str(name or "").strip()
        if not tool_name:
            raise ValueError("Playwright MCP tool name cannot be empty.")
        try:
            return self._call_tool_once(tool_name, arguments or {})
        except Exception as exc:
            if not self._should_restart_after_error(str(exc)):
                raise
            self._set_last_error(f"Restarting Playwright MCP after transport error: {exc}")
            self.restart()
            return self._call_tool_once(tool_name, arguments or {})

    def _should_restart_after_error(self, message: str) -> bool:
        lowered = str(message or "").lower()
        return any(
            token in lowered
            for token in (
                "not running",
                "stdout closed",
                "stdin",
                "timed out",
                "no response",
                "server stopped",
                "broken pipe",
                "connection reset",
            )
        )

    def _call_tool_once(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.ensure_ready(refresh_tools=False)
        result = self._request(
            "tools/call",
            {
                "name": tool_name,
                "arguments": arguments,
            },
            timeout_sec=self.request_timeout_sec,
        )
        content = result.get("content") if isinstance(result, dict) else []
        structured = result.get("structuredContent") if isinstance(result, dict) else {}
        if not isinstance(content, list):
            content = []
        if not isinstance(structured, dict):
            structured = {}
        text_parts: list[str] = []
        raw_text_parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if str(item.get("type") or "").strip() != "text":
                continue
            raw_text = str(item.get("text") or "").strip()
            if raw_text:
                raw_text_parts.append(raw_text)
                text_parts.append(re.sub(r"\s+", " ", raw_text))
        return {
            "tool": tool_name,
            "content": content,
            "structured_content": structured,
            "text": "\n".join(text_parts).strip(),
            "text_raw": "\n".join(raw_text_parts).strip(),
            "is_error": bool(result.get("isError")) if isinstance(result, dict) else False,
            "raw_result": result if isinstance(result, dict) else {"value": result},
        }

    def _start_process_locked(self) -> None:
        self._set_last_error("")
        self._stderr_tail.clear()
        env = self._build_process_env()
        try:
            self._process = subprocess.Popen(
                self.command,
                cwd=str(self.cwd) if self.cwd else None,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
            )
        except Exception as exc:
            self._process = None
            raise RuntimeError(f"Failed to start Playwright MCP server: {exc}") from exc

        self._last_started_at = time.time()
        self._stdout_thread = threading.Thread(target=self._stdout_loop, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread = threading.Thread(target=self._stderr_loop, daemon=True)
        self._stderr_thread.start()

    def _stdout_loop(self) -> None:
        process = self._process
        if not process or not process.stdout:
            return
        try:
            for raw_line in process.stdout:
                line = (raw_line or "").strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except Exception:
                    self._stderr_tail.append(f"stdout(non-json): {line[:400]}")
                    continue
                self._handle_message(message)
        finally:
            self._fail_pending("Playwright MCP server stdout closed.")

    def _stderr_loop(self) -> None:
        process = self._process
        if not process or not process.stderr:
            return
        for raw_line in process.stderr:
            line = re.sub(r"\s+", " ", (raw_line or "").strip())
            if line:
                self._stderr_tail.append(line[:400])

    def _handle_message(self, message: dict[str, Any]) -> None:
        if not isinstance(message, dict):
            return
        if "id" in message:
            request_id = int(message.get("id"))
            with self._pending_lock:
                pending = self._pending.get(request_id)
            if pending is not None:
                pending["response"] = message
                pending["event"].set()
            return
        self._last_notification = message

    def _fail_pending(self, message: str) -> None:
        self._set_last_error(message)
        with self._pending_lock:
            pending_items = list(self._pending.values())
            self._pending.clear()
        for pending in pending_items:
            pending["error"] = message
            pending["event"].set()

    def _request(self, method: str, params: dict[str, Any] | None, *, timeout_sec: int) -> dict[str, Any]:
        process = self._process
        if not process or process.poll() is not None or not process.stdin:
            raise RuntimeError("Playwright MCP server is not running.")

        with self._pending_lock:
            request_id = self._next_id
            self._next_id += 1
            event = threading.Event()
            pending = {"event": event, "response": None, "error": ""}
            self._pending[request_id] = pending

        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        }

        with self._io_lock:
            process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            process.stdin.flush()

        if not event.wait(timeout_sec):
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise RuntimeError(f"Playwright MCP request timed out: {method}")

        error_message = str(pending.get("error") or "").strip()
        if error_message:
            raise RuntimeError(error_message)

        response = pending.get("response")
        with self._pending_lock:
            self._pending.pop(request_id, None)
        if not isinstance(response, dict):
            raise RuntimeError(f"Playwright MCP returned no response for {method}.")
        if response.get("error"):
            error_payload = response.get("error") or {}
            if isinstance(error_payload, dict):
                detail = str(error_payload.get("message") or error_payload)
            else:
                detail = str(error_payload)
            raise RuntimeError(f"Playwright MCP {method} failed: {detail}")
        result = response.get("result")
        return result if isinstance(result, dict) else {"value": result}

    def _notify(self, method: str, params: dict[str, Any] | None) -> None:
        process = self._process
        if not process or process.poll() is not None or not process.stdin:
            raise RuntimeError("Playwright MCP server is not running.")
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        }
        with self._io_lock:
            process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            process.stdin.flush()


def _parse_browser_tabs_text(text: str) -> dict[str, Any]:
    tabs: list[dict[str, Any]] = []
    current_index: int | None = None
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        match = re.match(r"^-\s+(\d+):\s+(.*)$", line)
        if not match:
            continue
        index = int(match.group(1))
        remainder = match.group(2).strip()
        is_current = remainder.startswith("(current)")
        if is_current:
            remainder = remainder[len("(current)") :].strip()
            current_index = index
        title = ""
        url = ""
        markdown_match = re.match(r"^\[(.*?)\]\((.*?)\)$", remainder)
        if markdown_match:
            title = markdown_match.group(1).strip()
            url = markdown_match.group(2).strip()
        else:
            fallback_match = re.match(r"^(.*?)\s+\((https?://.*?)\)$", remainder)
            if fallback_match:
                title = fallback_match.group(1).strip()
                url = fallback_match.group(2).strip()
            else:
                title = remainder.strip()
        tabs.append(
            {
                "index": index,
                "title": title,
                "url": url,
                "is_current": is_current,
            }
        )
    return {
        "tabs": tabs,
        "currentTab": current_index,
    }


def _parse_browser_navigate_metadata(text: str) -> dict[str, str]:
    page_url = ""
    page_title = ""
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if line.startswith("- Page URL:"):
            page_url = line.split(":", 1)[1].strip()
        if line.startswith("- Page Title:"):
            page_title = line.split(":", 1)[1].strip()
    return {
        "url": page_url,
        "title": page_title,
    }


def _extract_current_tab(structured_tabs: dict[str, Any]) -> dict[str, Any]:
    current_index = structured_tabs.get("currentTab") if isinstance(structured_tabs.get("currentTab"), int) else None
    for item in structured_tabs.get("tabs") or []:
        if isinstance(item, dict) and item.get("index") == current_index:
            return item
    return {}


def _is_blank_browser_url(url: str) -> bool:
    value = str(url or "").strip().lower()
    return not value or value in {"about:blank", "chrome://new-tab-page/", "edge://newtab/"}


def _normalised_url_host(url: str) -> str:
    try:
        parsed = urlparse(str(url or "").strip())
    except Exception:
        return ""
    return (parsed.hostname or "").lower().removeprefix("www.")


def _compact_text(text: Any, limit: int) -> str:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    normalized = re.sub(r"[ \t]+\n", "\n", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    if limit > 0 and len(normalized) > limit:
        return normalized[:limit].rstrip() + "..."
    return normalized


def _parse_browser_evaluate_result(text: str) -> Any:
    raw_text = str(text or "").strip()
    if not raw_text:
        return ""
    match = re.search(r"### Result\s*(.*?)\s*### Ran Playwright code", raw_text, re.DOTALL)
    candidate = match.group(1).strip() if match else raw_text
    with_references_removed = re.sub(r"\n### Page.*$", "", candidate, flags=re.DOTALL).strip()
    if not with_references_removed:
        return ""
    try:
        return json.loads(with_references_removed)
    except Exception:
        return with_references_removed


def _extract_image_content(tool_result: dict[str, Any]) -> dict[str, str]:
    raw_result = tool_result.get("raw_result") if isinstance(tool_result.get("raw_result"), dict) else {}
    content = raw_result.get("content") if isinstance(raw_result.get("content"), list) else []
    for item in content:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "").strip() != "image":
            continue
        data = str(item.get("data") or "").strip()
        if not data:
            continue
        mime_type = str(item.get("mimeType") or item.get("mime_type") or "image/png").strip() or "image/png"
        return {"mime_type": mime_type, "data": data}
    return {}


def _optimise_image_content(image: dict[str, str]) -> dict[str, Any]:
    if os.getenv("BROWSER_SCREENSHOT_OPTIMIZE", "true").strip().lower() in {"0", "false", "no", "off"}:
        return dict(image)
    data = str(image.get("data") or "").strip()
    if not data:
        return dict(image)
    try:
        from PIL import Image
    except Exception:
        return dict(image)

    try:
        max_width = max(480, min(int(os.getenv("BROWSER_SCREENSHOT_MAX_WIDTH", "1280") or "1280"), 2560))
    except Exception:
        max_width = 1280
    try:
        quality = max(35, min(int(os.getenv("BROWSER_SCREENSHOT_JPEG_QUALITY", "72") or "72"), 95))
    except Exception:
        quality = 72

    try:
        raw = base64.b64decode(data)
        with Image.open(io.BytesIO(raw)) as source:
            width, height = source.size
            image_rgb = source.convert("RGB")
            if width > max_width:
                next_height = max(1, int(height * (max_width / float(width))))
                image_rgb = image_rgb.resize((max_width, next_height), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            image_rgb.save(output, format="JPEG", quality=quality, optimize=True)
        optimised = base64.b64encode(output.getvalue()).decode("ascii")
        if len(optimised) >= len(data):
            return {**image, "optimized": False, "width": width, "height": height}
        return {
            "mime_type": "image/jpeg",
            "data": optimised,
            "optimized": True,
            "original_mime_type": str(image.get("mime_type") or ""),
            "original_base64_length": len(data),
            "base64_length": len(optimised),
            "width": width,
            "height": height,
        }
    except Exception:
        return dict(image)


def _browser_diagnostics_enabled() -> bool:
    return os.getenv("BROWSER_DIAGNOSTICS_ENABLED", DEFAULT_BROWSER_DIAGNOSTICS_ENABLED).strip().lower() in {"1", "true", "yes", "on"}


def _json_string(value: str) -> str:
    return json.dumps(str(value or ""), ensure_ascii=False)


def _friendly_bridge_error_message(message: str) -> str:
    detail = re.sub(r"\s+", " ", str(message or "").strip())
    if not detail:
        return detail
    if "Target page, context or browser has been closed" in detail and "--user-data-dir=" in detail:
        profile_match = re.search(r"--user-data-dir=([^\s]+)", detail)
        profile_path = profile_match.group(1).strip('"') if profile_match else ""
        if profile_path:
            return (
                "Playwright MCP could not launch the browser profile. "
                f"Close any Chrome windows using {profile_path}, then retry. "
                "Do not manually open that same profile before the local browser worker starts."
            )
        return (
            "Playwright MCP could not launch the browser profile. "
            "Close any Chrome windows using the MCP profile, then retry."
        )
    return detail


class BrowserBridge:
    def __init__(self) -> None:
        self.user_data_dir = self._resolve_user_data_dir()
        self.playwright_mcp = PlaywrightMcpClient(user_data_dir=self.user_data_dir)
        self.worker_enabled = os.getenv("BROWSER_WORKER_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
        self.worker_id = str(os.getenv("BROWSER_WORKER_ID", "").strip() or "default")
        self.worker_token = os.getenv("BROWSER_WORKER_TOKEN", "").strip()
        self.worker_ws_url = self._resolve_worker_ws_url()
        self.worker_reconnect_sec = max(3, min(int(os.getenv("BROWSER_WORKER_RECONNECT_SEC", "5") or "5"), 60))
        self._worker_thread: threading.Thread | None = None

    def _resolve_user_data_dir(self) -> Path:
        configured = os.getenv("PLAYWRIGHT_USER_DATA_DIR", "").strip()
        return Path(configured) if configured else DEFAULT_USER_DATA_DIR

    def _resolve_worker_ws_url(self) -> str:
        configured = os.getenv("BROWSER_WORKER_WS_URL", "").strip()
        if not configured:
            return ""
        if configured.startswith("http://"):
            configured = "ws://" + configured[len("http://") :]
        elif configured.startswith("https://"):
            configured = "wss://" + configured[len("https://") :]
        return configured

    def worker_status(self) -> dict[str, Any]:
        return {
            "enabled": self.worker_enabled,
            "worker_id": self.worker_id,
            "ws_url": self.worker_ws_url,
            "profile_path": str(self.user_data_dir),
            "thread_alive": bool(self._worker_thread and self._worker_thread.is_alive()),
        }

    def playwright_mcp_status(self, *, ensure_started: bool = False, refresh_tools: bool = False) -> dict[str, Any]:
        return self.playwright_mcp.status(ensure_started=ensure_started, refresh_tools=refresh_tools)

    def playwright_mcp_tools(self, *, refresh: bool = False) -> dict[str, Any]:
        return self.playwright_mcp.tools(refresh=refresh)

    def restart_playwright_mcp(self) -> dict[str, Any]:
        return self.playwright_mcp.restart()

    def _playwright_mcp_tool_names(self) -> set[str]:
        try:
            payload = self.playwright_mcp_tools(refresh=False)
        except Exception:
            return set()
        tools = payload.get("tools") if isinstance(payload.get("tools"), list) else []
        return {
            str(item.get("name") or "").strip()
            for item in tools
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        }

    def _available_tool_candidates(self, names: list[str]) -> list[str]:
        available = self._playwright_mcp_tool_names()
        if not available:
            return names
        preferred = [name for name in names if name in available]
        fallback = [name for name in names if name not in preferred]
        return [*preferred, *fallback]

    def handle_worker_command(self, command: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        command_name = str(command or "").strip().lower()
        data = payload if isinstance(payload, dict) else {}
        if command_name == "browser.tabs":
            return self.playwright_mcp_tabs()
        if command_name == "browser.navigate":
            return self.playwright_mcp_navigate(str(data.get("url") or "").strip())
        if command_name == "browser.snapshot":
            return self.playwright_mcp_snapshot(
                url=str(data.get("url") or "").strip(),
                instruction=str(data.get("instruction") or "").strip(),
                wait_ms=int(data.get("wait_ms") or 3000),
                target=str(data.get("target") or "").strip(),
                depth=int(data["depth"]) if data.get("depth") is not None else None,
            )
        if command_name == "browser.screenshot":
            return self.playwright_mcp_screenshot(
                url=str(data.get("url") or "").strip(),
                instruction=str(data.get("instruction") or "").strip(),
                wait_ms=int(data.get("wait_ms") or 1000),
                request_id=str(data.get("request_id") or "").strip(),
                task_id=str(data.get("task_id") or "").strip(),
                action_id=str(data.get("action_id") or "").strip(),
            )
        if command_name == "browser.hit_test":
            return self.playwright_mcp_hit_test(
                x=data.get("x"),
                y=data.get("y"),
                candidate_id=str(data.get("candidate_id") or "").strip(),
                target_description=str(data.get("target_description") or "").strip(),
                task_id=str(data.get("task_id") or "").strip(),
                action_id=str(data.get("action_id") or "").strip(),
            )
        if command_name == "browser.visual_action":
            return self.playwright_mcp_visual_action(data)
        raise ValueError(f"Unsupported browser worker command: {command_name or '<empty>'}")

    def playwright_mcp_tabs(self) -> dict[str, Any]:
        result = self.playwright_mcp.call_tool("browser_tabs", {"action": "list"})
        if result.get("is_error"):
            raise RuntimeError(str(result.get("text") or "Playwright MCP browser_tabs failed.").strip())
        if not result.get("structured_content"):
            result["structured_content"] = _parse_browser_tabs_text(str(result.get("text_raw") or result.get("text") or ""))
        return result

    def playwright_mcp_navigate(self, url: str) -> dict[str, Any]:
        target_url = str(url or "").strip()
        if not target_url:
            raise ValueError("url is required")
        result = self.playwright_mcp.call_tool("browser_navigate", {"url": target_url})
        if result.get("is_error"):
            raise RuntimeError(str(result.get("text") or "Playwright MCP browser_navigate failed.").strip())
        tabs = self.playwright_mcp_tabs()
        structured_tabs = tabs.get("structured_content") if isinstance(tabs.get("structured_content"), dict) else {}
        current_tab = _extract_current_tab(structured_tabs)
        parsed_page = _parse_browser_navigate_metadata(str(result.get("text_raw") or result.get("text") or ""))
        resolved_url = str(current_tab.get("url") or parsed_page.get("url") or target_url).strip()
        resolved_title = str(current_tab.get("title") or parsed_page.get("title") or "").strip()
        return {
            "ok": not bool(result.get("is_error")),
            "url": resolved_url,
            "title": resolved_title,
            "tool_result": result,
            "tabs": tabs,
            "text": result.get("text", ""),
        }

    def _current_tab_info(self) -> dict[str, Any]:
        try:
            tabs = self.playwright_mcp_tabs()
        except Exception:
            return {"tabs": {}, "current_tab": {}}
        structured_tabs = tabs.get("structured_content") if isinstance(tabs.get("structured_content"), dict) else {}
        return {"tabs": tabs, "current_tab": _extract_current_tab(structured_tabs)}

    def _maybe_navigate_for_observation(self, target_url: str) -> dict[str, Any]:
        requested_url = str(target_url or "").strip()
        if not requested_url:
            return {"mode": "reuse_current_page", "reason": "no_url_requested", "url": ""}

        tab_info = self._current_tab_info()
        current_tab = tab_info.get("current_tab") if isinstance(tab_info.get("current_tab"), dict) else {}
        current_url = str(current_tab.get("url") or "").strip()
        target_host = _normalised_url_host(requested_url)
        current_host = _normalised_url_host(current_url)

        if not _is_blank_browser_url(current_url) and current_host and target_host and current_host == target_host:
            return {
                "mode": "reuse_current_page",
                "reason": "same_site",
                "requested_url": requested_url,
                "current_url": current_url,
                "current_title": str(current_tab.get("title") or "").strip(),
            }
        if not _is_blank_browser_url(current_url) and current_url.rstrip("/") == requested_url.rstrip("/"):
            return {
                "mode": "reuse_current_page",
                "reason": "same_url",
                "requested_url": requested_url,
                "current_url": current_url,
                "current_title": str(current_tab.get("title") or "").strip(),
            }

        navigated = self.playwright_mcp_navigate(requested_url)
        return {
            "mode": "navigated",
            "reason": "blank_or_different_site",
            "requested_url": requested_url,
            "current_url": str(navigated.get("url") or requested_url).strip(),
            "current_title": str(navigated.get("title") or "").strip(),
        }

    def _playwright_mcp_wait(self, wait_ms: int) -> None:
        wait_seconds = max(0.0, min(float(wait_ms) / 1000.0, 30.0))
        if wait_seconds <= 0:
            return
        result = self.playwright_mcp.call_tool("browser_wait_for", {"time": wait_seconds})
        if result.get("is_error"):
            raise RuntimeError(str(result.get("text") or "Playwright MCP browser_wait_for failed.").strip())

    def _playwright_mcp_page_diagnostics(self, *, limit: int = 2400) -> dict[str, Any]:
        js = r"""() => {
  const compact = (value, max = 180) => String(value || '').replace(/\s+/g, ' ').trim().slice(0, max);
  const cssEscape = (value) => window.CSS && CSS.escape
    ? CSS.escape(String(value || ''))
    : String(value || '').replace(/[^a-zA-Z0-9_-]/g, (char) => `\\${char}`);
  const attrEscape = (value) => String(value || '').replace(/\\/g, '\\\\').replace(/"/g, '\\"');
  const visible = (element) => {
    if (!element || !(element instanceof Element)) return false;
    const style = window.getComputedStyle(element);
    if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) return false;
    const rect = element.getBoundingClientRect();
    return rect.width > 1 && rect.height > 1 && rect.bottom >= 0 && rect.right >= 0
      && rect.top <= window.innerHeight && rect.left <= window.innerWidth;
  };
  const cssPath = (element) => {
    if (!element || !(element instanceof Element)) return '';
    if (element.id) return `#${cssEscape(element.id)}`;
    const dataE2e = element.getAttribute('data-e2e');
    if (dataE2e) return `[data-e2e="${attrEscape(dataE2e)}"]`;
    const aria = element.getAttribute('aria-label');
    if (aria) return `${element.tagName.toLowerCase()}[aria-label="${attrEscape(aria)}"]`;
    const parts = [];
    let node = element;
    while (node && node instanceof Element && parts.length < 4) {
      let part = node.tagName.toLowerCase();
      const cls = Array.from(node.classList || []).filter(Boolean).slice(0, 2);
      if (cls.length) part += '.' + cls.map((name) => cssEscape(name)).join('.');
      const parent = node.parentElement;
      if (parent) {
        const siblings = Array.from(parent.children).filter((child) => child.tagName === node.tagName);
        if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(node) + 1})`;
      }
      parts.unshift(part);
      node = parent;
    }
    return parts.join(' > ');
  };
  const describe = (element) => {
    const rect = element.getBoundingClientRect();
    const inputValue = ['INPUT', 'TEXTAREA'].includes(element.tagName) ? element.value : '';
    return {
      tag: element.tagName.toLowerCase(),
      role: compact(element.getAttribute('role'), 60),
      text: compact(element.innerText || element.textContent || inputValue, 160),
      ariaLabel: compact(element.getAttribute('aria-label'), 120),
      title: compact(element.getAttribute('title'), 120),
      placeholder: compact(element.getAttribute('placeholder'), 120),
      dataE2e: compact(element.getAttribute('data-e2e'), 80),
      type: compact(element.getAttribute('type'), 40),
      href: compact(element.getAttribute('href'), 160),
      selector: cssPath(element),
      rect: {
        x: Math.round(rect.x), y: Math.round(rect.y),
        width: Math.round(rect.width), height: Math.round(rect.height)
      }
    };
  };
  const controlSelector = [
    'button', 'a[href]', '[role="button"]', '[role="link"]',
    'input', 'textarea', 'select', '[contenteditable="true"]',
    '[aria-label]', '[data-e2e]'
  ].join(',');
  const controls = Array.from(document.querySelectorAll(controlSelector))
    .filter(visible)
    .map(describe)
    .filter((item) => item.text || item.ariaLabel || item.placeholder || item.dataE2e || item.title || item.href)
    .slice(0, 80);
  const headings = Array.from(document.querySelectorAll('h1,h2,h3,[role="heading"]'))
    .filter(visible)
    .map((element) => compact(element.innerText || element.textContent, 160))
    .filter(Boolean)
    .slice(0, 20);
  const dialogs = Array.from(document.querySelectorAll('[role="dialog"], dialog, .modal, [class*="modal"], [class*="popup"], [class*="mask"]'))
    .filter(visible)
    .map(describe)
    .slice(0, 20);
  const bodyText = compact(document.body ? document.body.innerText : '', 1200);
  const active = document.activeElement && document.activeElement !== document.body ? describe(document.activeElement) : null;
  return {
    readyState: document.readyState,
    url: location.href,
    title: document.title,
    viewport: { width: window.innerWidth, height: window.innerHeight, devicePixelRatio: window.devicePixelRatio || 1 },
    scroll: { x: Math.round(window.scrollX), y: Math.round(window.scrollY), height: Math.round(document.documentElement.scrollHeight || 0) },
    visibleText: bodyText,
    headings,
    dialogs,
    controls,
    activeElement: active
  };
}"""
        result = self.playwright_mcp.call_tool("browser_evaluate", {"function": js})
        if result.get("is_error"):
            return {"ok": False, "error": str(result.get("text") or "Playwright MCP browser_evaluate failed.").strip()}
        parsed = _parse_browser_evaluate_result(str(result.get("text_raw") or result.get("text") or ""))
        if not isinstance(parsed, dict):
            return {"ok": False, "raw": _compact_text(parsed, limit)}
        controls = parsed.get("controls") if isinstance(parsed.get("controls"), list) else []
        dialogs = parsed.get("dialogs") if isinstance(parsed.get("dialogs"), list) else []
        headings = parsed.get("headings") if isinstance(parsed.get("headings"), list) else []
        parsed["controls"] = controls[:80]
        parsed["dialogs"] = dialogs[:20]
        parsed["headings"] = headings[:20]
        parsed["visibleText"] = _compact_text(parsed.get("visibleText", ""), limit)
        parsed["ok"] = True
        parsed["controlCount"] = len(controls)
        parsed["dialogCount"] = len(dialogs)
        return parsed

    def _playwright_mcp_page_state(self) -> dict[str, Any]:
        js = r"""() => {
  const compact = (value, max = 4000) => String(value || '').replace(/\s+/g, ' ').trim().slice(0, max);
  return {
    capturedAt: Date.now(),
    readyState: document.readyState,
    url: location.href,
    title: document.title,
    viewport: {
      width: window.innerWidth || 0,
      height: window.innerHeight || 0,
      devicePixelRatio: window.devicePixelRatio || 1
    },
    scroll: {
      x: Math.round(window.scrollX || 0),
      y: Math.round(window.scrollY || 0),
      height: Math.round(document.documentElement.scrollHeight || document.body?.scrollHeight || 0)
    },
    visibleText: compact(document.body ? document.body.innerText : '', 4000),
    activeTag: document.activeElement && document.activeElement.tagName ? document.activeElement.tagName.toLowerCase() : ''
  };
}"""
        try:
            result = self.playwright_mcp.call_tool("browser_evaluate", {"function": js})
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if result.get("is_error"):
            return {"ok": False, "error": str(result.get("text") or "").strip()}
        parsed = _parse_browser_evaluate_result(str(result.get("text_raw") or result.get("text") or ""))
        if not isinstance(parsed, dict):
            return {"ok": False, "raw": _compact_text(parsed, 500)}
        visible_text = str(parsed.pop("visibleText", "") or "")
        text_hash = hashlib.sha256(visible_text.encode("utf-8", errors="ignore")).hexdigest()[:16]
        url = str(parsed.get("url") or "").strip()
        title = str(parsed.get("title") or "").strip()
        viewport = parsed.get("viewport") if isinstance(parsed.get("viewport"), dict) else {}
        scroll = parsed.get("scroll") if isinstance(parsed.get("scroll"), dict) else {}
        signature_payload = {
            "url": url,
            "title": title,
            "viewport": viewport,
            "scroll": scroll,
            "text_hash": text_hash,
        }
        parsed["ok"] = True
        parsed["visibleTextHash"] = text_hash
        parsed["visibleTextLength"] = len(visible_text)
        parsed["visibleTextPreview"] = visible_text[:500]
        parsed["signature"] = hashlib.sha256(
            json.dumps(signature_payload, ensure_ascii=False, sort_keys=True).encode("utf-8", errors="ignore")
        ).hexdigest()[:16]
        return parsed

    def _playwright_mcp_action_candidates(self, *, limit: int = 80) -> list[dict[str, Any]]:
        js = r"""() => {
  const compact = (value, max = 180) => String(value || '').replace(/\s+/g, ' ').trim().slice(0, max);
  const cssEscape = (value) => window.CSS && CSS.escape
    ? CSS.escape(String(value || ''))
    : String(value || '').replace(/[^a-zA-Z0-9_-]/g, (char) => `\\${char}`);
  const attrEscape = (value) => String(value || '').replace(/\\/g, '\\\\').replace(/"/g, '\\"');
  const cssPath = (element) => {
    if (!element || !(element instanceof Element)) return '';
    if (element.id) return `#${cssEscape(element.id)}`;
    const dataE2e = element.getAttribute('data-e2e');
    if (dataE2e) return `[data-e2e="${attrEscape(dataE2e)}"]`;
    const aria = element.getAttribute('aria-label');
    if (aria) return `${element.tagName.toLowerCase()}[aria-label="${attrEscape(aria)}"]`;
    const href = element.getAttribute('href');
    if (href && element.tagName.toLowerCase() === 'a') return `a[href="${attrEscape(href)}"]`;
    const parts = [];
    let node = element;
    while (node && node instanceof Element && parts.length < 5) {
      let part = node.tagName.toLowerCase();
      const cls = Array.from(node.classList || []).filter(Boolean).slice(0, 2);
      if (cls.length) part += '.' + cls.map((name) => cssEscape(name)).join('.');
      const parent = node.parentElement;
      if (parent) {
        const siblings = Array.from(parent.children).filter((child) => child.tagName === node.tagName);
        if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(node) + 1})`;
      }
      parts.unshift(part);
      node = parent;
    }
    return parts.join(' > ');
  };
  const visible = (element) => {
    if (!element || !(element instanceof Element)) return false;
    const style = window.getComputedStyle(element);
    if (!style || style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) return false;
    if (style.pointerEvents === 'none') return false;
    const rect = element.getBoundingClientRect();
    return rect.width >= 12 && rect.height >= 12 && rect.bottom >= 0 && rect.right >= 0
      && rect.top <= window.innerHeight && rect.left <= window.innerWidth;
  };
  const describe = (element) => {
    const rect = element.getBoundingClientRect();
    const centerX = Math.max(0, Math.min(window.innerWidth || 1, rect.left + rect.width / 2));
    const centerY = Math.max(0, Math.min(window.innerHeight || 1, rect.top + rect.height / 2));
    const text = compact(element.innerText || element.textContent || '', 220);
    const ariaLabel = compact(element.getAttribute('aria-label'), 160);
    const title = compact(element.getAttribute('title'), 160);
    const dataE2e = compact(element.getAttribute('data-e2e'), 100);
    const href = compact(element.getAttribute('href'), 220);
    const label = compact(text || ariaLabel || title || dataE2e || href, 220);
    const selector = cssPath(element);
    return {
      tag: element.tagName.toLowerCase(),
      role: compact(element.getAttribute('role'), 60),
      label,
      text,
      ariaLabel,
      title,
      dataE2e,
      href,
      selector,
      className: compact(element.className, 160),
      rect: {
        x: Math.round(rect.x), y: Math.round(rect.y),
        width: Math.round(rect.width), height: Math.round(rect.height)
      },
      center: {
        x: Math.round(centerX),
        y: Math.round(centerY),
        normalizedX: Math.round(centerX * 1000 / Math.max(1, window.innerWidth || 1)),
        normalizedY: Math.round(centerY * 1000 / Math.max(1, window.innerHeight || 1))
      },
      viewport: { width: window.innerWidth || 0, height: window.innerHeight || 0 }
    };
  };
  const selectors = [
    'button',
    'a[href]',
    '[role="button"]',
    '[role="link"]',
    'input',
    'textarea',
    'select',
    '[contenteditable="true"]',
    '[aria-label]',
    '[data-e2e]',
    '[class*="video" i]',
    '[class*="card" i]',
    '[class*="feed" i]',
    '[class*="waterfall" i]',
    '[class*="item" i]'
  ].join(',');
  const seen = new Set();
  const candidates = [];
  for (const element of Array.from(document.querySelectorAll(selectors))) {
    if (!visible(element)) continue;
    const item = describe(element);
    const area = item.rect.width * item.rect.height;
    if (area < 144) continue;
    if (area > Math.max(1, (window.innerWidth || 1) * (window.innerHeight || 1)) * 0.82) continue;
    const key = `${item.selector}|${item.rect.x},${item.rect.y},${item.rect.width},${item.rect.height}|${item.label}`;
    if (seen.has(key)) continue;
    seen.add(key);
    candidates.push(item);
  }
  return candidates.slice(0, __LIMIT__);
}"""
        js = js.replace("__LIMIT__", str(max(1, min(int(limit or 80), 160))))
        try:
            result = self.playwright_mcp.call_tool("browser_evaluate", {"function": js})
        except Exception:
            return []
        if result.get("is_error"):
            return []
        parsed = _parse_browser_evaluate_result(str(result.get("text_raw") or result.get("text") or ""))
        if not isinstance(parsed, list):
            return []
        items: list[dict[str, Any]] = []
        for index, raw_item in enumerate(parsed[: max(1, min(int(limit or 80), 160))], start=1):
            if not isinstance(raw_item, dict):
                continue
            signature_payload = {
                "selector": str(raw_item.get("selector") or ""),
                "label": str(raw_item.get("label") or ""),
                "rect": raw_item.get("rect") if isinstance(raw_item.get("rect"), dict) else {},
                "center": raw_item.get("center") if isinstance(raw_item.get("center"), dict) else {},
            }
            candidate_id = hashlib.sha256(
                json.dumps(signature_payload, ensure_ascii=False, sort_keys=True).encode("utf-8", errors="ignore")
            ).hexdigest()[:12]
            raw_item["candidate_id"] = f"c{index}_{candidate_id}"
            items.append(raw_item)
        return items

    def _find_action_candidate(self, candidate_id: str) -> dict[str, Any]:
        target_id = str(candidate_id or "").strip()
        if not target_id:
            return {}
        target_hash = target_id.rsplit("_", 1)[-1]
        for item in self._playwright_mcp_action_candidates(limit=120):
            item_id = str(item.get("candidate_id") or "").strip()
            item_hash = item_id.rsplit("_", 1)[-1]
            if item_id == target_id or (target_hash and item_hash == target_hash):
                return item
        return {}

    def playwright_mcp_snapshot(
        self,
        *,
        url: str = "",
        instruction: str = "",
        wait_ms: int = 3000,
        target: str = "",
        depth: int | None = None,
    ) -> dict[str, Any]:
        target_url = str(url or "").strip()
        navigation = self._maybe_navigate_for_observation(target_url)

        wait_seconds = max(0.0, min(float(wait_ms) / 1000.0, 30.0))
        if wait_seconds > 0:
            wait_result = self.playwright_mcp.call_tool("browser_wait_for", {"time": wait_seconds})
            if wait_result.get("is_error"):
                raise RuntimeError(str(wait_result.get("text") or "Playwright MCP browser_wait_for failed.").strip())

        snapshot_args: dict[str, Any] = {}
        if str(target or "").strip():
            snapshot_args["target"] = str(target).strip()
        if depth is not None:
            snapshot_args["depth"] = int(depth)
        result = self.playwright_mcp.call_tool("browser_snapshot", snapshot_args)
        if result.get("is_error"):
            raise RuntimeError(str(result.get("text") or "Playwright MCP browser_snapshot failed.").strip())

        tabs = self.playwright_mcp_tabs()
        structured_tabs = tabs.get("structured_content") if isinstance(tabs.get("structured_content"), dict) else {}
        current_tab = _extract_current_tab(structured_tabs)
        snapshot_text = str(result.get("text_raw") or result.get("text") or "").strip()
        diagnostics = self._playwright_mcp_page_diagnostics() if _browser_diagnostics_enabled() else {}
        page_state = self._playwright_mcp_page_state()
        action_candidates = self._playwright_mcp_action_candidates()
        resolved_url = str(current_tab.get("url") or target_url or "").strip()
        resolved_title = str(current_tab.get("title") or "").strip()
        return {
            "ok": True,
            "url": resolved_url,
            "title": resolved_title,
            "instruction": str(instruction or "").strip(),
            "text": snapshot_text,
            "page_state": page_state,
            "action_candidates": action_candidates,
            "diagnostics": diagnostics,
            "navigation": navigation,
            "tool_result": result,
            "tabs": tabs,
        }

    def playwright_mcp_screenshot(
        self,
        *,
        url: str = "",
        instruction: str = "",
        wait_ms: int = 1000,
        request_id: str = "",
        task_id: str = "",
        action_id: str = "",
    ) -> dict[str, Any]:
        target_url = str(url or "").strip()
        navigation = self._maybe_navigate_for_observation(target_url)
        if wait_ms > 0:
            self._playwright_mcp_wait(wait_ms)

        result: dict[str, Any] = {}
        image: dict[str, str] = {}
        screenshot_tool = ""
        last_error = ""
        for tool_name in self._available_tool_candidates(["browser_take_screenshot", "browser_screenshot"]):
            try:
                result = self.playwright_mcp.call_tool(tool_name, {})
            except Exception as exc:
                last_error = str(exc)
                continue
            if result.get("is_error"):
                last_error = str(result.get("text") or f"Playwright MCP {tool_name} failed.").strip()
                continue
            image = _extract_image_content(result)
            if image:
                image = _optimise_image_content(image)
                screenshot_tool = tool_name
                break
        if not image:
            raise RuntimeError(last_error or "Playwright MCP screenshot returned no image content.")

        diagnostics = self._playwright_mcp_page_diagnostics() if _browser_diagnostics_enabled() else {}
        viewport = self._playwright_mcp_viewport_metadata(diagnostics)
        page_state = self._playwright_mcp_page_state()
        action_candidates = self._playwright_mcp_action_candidates()
        tabs = self.playwright_mcp_tabs()
        structured_tabs = tabs.get("structured_content") if isinstance(tabs.get("structured_content"), dict) else {}
        current_tab = _extract_current_tab(structured_tabs)
        return {
            "ok": True,
            "request_id": request_id,
            "task_id": task_id,
            "action_id": action_id,
            "url": str(current_tab.get("url") or target_url or diagnostics.get("url") or "").strip(),
            "title": str(current_tab.get("title") or diagnostics.get("title") or "").strip(),
            "instruction": str(instruction or "").strip(),
            "mime_type": image["mime_type"],
            "image_base64": image["data"],
            "screenshot_backend": screenshot_tool or "playwright_mcp",
            "image_optimized": bool(image.get("optimized")),
            "image_base64_length": int(image.get("base64_length") or len(str(image.get("data") or ""))),
            "original_image_base64_length": int(image.get("original_base64_length") or 0),
            "viewport": viewport,
            "device_pixel_ratio": viewport.get("devicePixelRatio") if isinstance(viewport, dict) else None,
            "page_state": page_state,
            "action_candidates": action_candidates,
            "diagnostics": diagnostics,
            "navigation": navigation,
            "tabs": tabs,
        }

    def _playwright_mcp_viewport_metadata(self, diagnostics: dict[str, Any] | None = None) -> dict[str, Any]:
        diag_viewport = diagnostics.get("viewport") if isinstance(diagnostics, dict) and isinstance(diagnostics.get("viewport"), dict) else {}
        if diag_viewport:
            return {
                "width": int(diag_viewport.get("width") or 0),
                "height": int(diag_viewport.get("height") or 0),
                "devicePixelRatio": float(diag_viewport.get("devicePixelRatio") or diag_viewport.get("dpr") or 1),
            }
        js = "() => ({ width: window.innerWidth || 0, height: window.innerHeight || 0, devicePixelRatio: window.devicePixelRatio || 1 })"
        try:
            result = self.playwright_mcp.call_tool("browser_evaluate", {"function": js})
        except Exception as exc:
            return {"width": 0, "height": 0, "devicePixelRatio": 1, "error": str(exc)}
        if result.get("is_error"):
            return {"width": 0, "height": 0, "devicePixelRatio": 1, "error": str(result.get("text") or "").strip()}
        parsed = _parse_browser_evaluate_result(str(result.get("text_raw") or result.get("text") or ""))
        if not isinstance(parsed, dict):
            return {"width": 0, "height": 0, "devicePixelRatio": 1}
        return {
            "width": int(parsed.get("width") or 0),
            "height": int(parsed.get("height") or 0),
            "devicePixelRatio": float(parsed.get("devicePixelRatio") or parsed.get("dpr") or 1),
        }

    def _normalised_point_to_viewport_js(self, x: Any, y: Any) -> str:
        try:
            norm_x = max(0.0, min(float(x), 1000.0))
            norm_y = max(0.0, min(float(y), 1000.0))
        except Exception as exc:
            raise ValueError("Visual action requires numeric x and y coordinates from 0 to 1000.") from exc
        return (
            "() => { "
            f"const x = Math.round((window.innerWidth || 1) * {norm_x} / 1000); "
            f"const y = Math.round((window.innerHeight || 1) * {norm_y} / 1000); "
            "return { x, y, viewport: { width: window.innerWidth, height: window.innerHeight } }; "
            "}"
        )

    def _click_target_wants_dom_assist(self, target_description: str) -> bool:
        text = str(target_description or "").strip().lower()
        if not text:
            return False
        keywords = (
            "video",
            "card",
            "thumbnail",
            "cover",
            "feed",
            "open",
            "enter",
            "watch",
            "视频",
            "卡片",
            "封面",
            "进入",
            "打开",
            "播放",
        )
        return any(keyword in text for keyword in keywords)

    def _playwright_mcp_dom_coordinate_click(self, css_x: int, css_y: int, *, target_description: str = "") -> dict[str, Any]:
        js = r"""() => {
  const compact = (value, max = 180) => String(value || '').replace(/\s+/g, ' ').trim().slice(0, max);
  const x = __CSS_X__;
  const y = __CSS_Y__;
  const element = document.elementFromPoint(x, y);
  if (!element) return { ok: false, reason: 'no_element', x, y };

  const viewportArea = Math.max(1, (window.innerWidth || 1) * (window.innerHeight || 1));
  const candidateSelectors = [
    'button',
    'a',
    '[role="button"]',
    '[role="link"]',
    'input',
    'textarea',
    'select',
    '[contenteditable="true"]',
    '[data-e2e]',
    '[aria-label]',
    '[class*="video" i]',
    '[class*="card" i]',
    '[class*="feed" i]',
    '[class*="waterfall" i]',
    '[data-e2e*="video" i]',
    '[data-e2e*="card" i]'
  ].join(',');

  const describe = (node) => {
    if (!node || !(node instanceof Element)) return null;
    const rect = node.getBoundingClientRect();
    return {
      tag: node.tagName.toLowerCase(),
      role: compact(node.getAttribute('role'), 60),
      text: compact(node.innerText || node.textContent || '', 220),
      ariaLabel: compact(node.getAttribute('aria-label'), 140),
      title: compact(node.getAttribute('title'), 140),
      dataE2e: compact(node.getAttribute('data-e2e'), 100),
      className: compact(node.className, 180),
      href: compact(node.getAttribute('href'), 180),
      rect: {
        x: Math.round(rect.x), y: Math.round(rect.y),
        width: Math.round(rect.width), height: Math.round(rect.height)
      }
    };
  };

  const isGoodCandidate = (node) => {
    if (!node || !(node instanceof Element)) return false;
    const rect = node.getBoundingClientRect();
    if (rect.width < 16 || rect.height < 16) return false;
    if ((rect.width * rect.height) > viewportArea * 0.78) return false;
    const style = window.getComputedStyle(node);
    if (style && style.pointerEvents === 'none') return false;
    return true;
  };

  let target = element.closest(candidateSelectors);
  if (!isGoodCandidate(target)) {
    target = null;
    let node = element;
    for (let depth = 0; node && depth < 6; depth += 1, node = node.parentElement) {
      if (!(node instanceof Element)) continue;
      const style = window.getComputedStyle(node);
      const hasHandler = Boolean(node.onclick);
      const looksClickable = style.cursor === 'pointer'
        || node.getAttribute('role') === 'button'
        || node.getAttribute('role') === 'link'
        || node.hasAttribute('data-e2e')
        || /video|card|feed|waterfall/i.test(String(node.className || ''));
      if (looksClickable && isGoodCandidate(node)) {
        target = node;
        break;
      }
      if (hasHandler && isGoodCandidate(node)) {
        target = node;
        break;
      }
    }
  }
  if (!target) target = element;

  const beforeUrl = location.href;
  const beforeTitle = document.title;
  try { target.scrollIntoView({ block: 'center', inline: 'center' }); } catch (_) {}
  const rect = target.getBoundingClientRect();
  const clientX = Math.max(Math.round(rect.left + Math.min(Math.max(x - rect.left, 1), Math.max(rect.width - 1, 1))), 0);
  const clientY = Math.max(Math.round(rect.top + Math.min(Math.max(y - rect.top, 1), Math.max(rect.height - 1, 1))), 0);
  for (const type of ['pointerover', 'pointerenter', 'mouseover', 'mouseenter', 'pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
    const event = type.startsWith('pointer')
      ? new PointerEvent(type, { bubbles: true, cancelable: true, view: window, pointerId: 1, pointerType: 'mouse', isPrimary: true, clientX, clientY })
      : new MouseEvent(type, { bubbles: true, cancelable: true, view: window, clientX, clientY });
    target.dispatchEvent(event);
  }
  if (typeof target.click === 'function') target.click();
  return {
    ok: true,
    x,
    y,
    clientX,
    clientY,
    beforeUrl,
    afterUrl: location.href,
    beforeTitle,
    afterTitle: document.title,
    element: describe(element),
    target: describe(target),
    targetDescription: __TARGET_DESCRIPTION__
  };
}"""
        js = (
            js.replace("__CSS_X__", str(int(css_x)))
            .replace("__CSS_Y__", str(int(css_y)))
            .replace("__TARGET_DESCRIPTION__", _json_string(target_description))
        )
        click_result = self.playwright_mcp.call_tool("browser_evaluate", {"function": js})
        if click_result.get("is_error"):
            raise RuntimeError(str(click_result.get("text") or "Playwright MCP coordinate click failed.").strip())
        parsed = _parse_browser_evaluate_result(str(click_result.get("text_raw") or click_result.get("text") or ""))
        if isinstance(parsed, dict) and parsed.get("ok") is True:
            return {"ok": True, "x": css_x, "y": css_y, "backend": "dom_dispatch", "target": parsed}
        raise RuntimeError(f"Coordinate DOM click failed: {parsed}")

    def _playwright_mcp_click_normalised(self, x: Any, y: Any, *, target_description: str = "") -> dict[str, Any]:
        point_result = self.playwright_mcp.call_tool("browser_evaluate", {"function": self._normalised_point_to_viewport_js(x, y)})
        if point_result.get("is_error"):
            raise RuntimeError(str(point_result.get("text") or "Playwright MCP browser_evaluate failed.").strip())
        point = _parse_browser_evaluate_result(str(point_result.get("text_raw") or point_result.get("text") or ""))
        if not isinstance(point, dict):
            raise RuntimeError("Could not resolve visual click coordinates.")
        css_x = int(point.get("x") or 0)
        css_y = int(point.get("y") or 0)
        native_result = self._playwright_mcp_native_coordinate_click(css_x, css_y)
        if native_result.get("ok"):
            if self._click_target_wants_dom_assist(target_description):
                try:
                    dom_result = self._playwright_mcp_dom_coordinate_click(css_x, css_y, target_description=target_description)
                    return {
                        **dom_result,
                        "backend": "native_plus_dom_dispatch",
                        "native": native_result,
                    }
                except Exception as exc:
                    return {
                        **native_result,
                        "dom_assist_error": str(exc),
                        "target_description": str(target_description or "").strip(),
                    }
            return native_result
        return self._playwright_mcp_dom_coordinate_click(css_x, css_y, target_description=target_description)

    def _playwright_mcp_native_coordinate_click(self, css_x: int, css_y: int) -> dict[str, Any]:
        candidates = [
            ("browser_screen_click", {"x": css_x, "y": css_y}),
            ("browser_mouse_click_xy", {"x": css_x, "y": css_y}),
            ("browser_click_xy", {"x": css_x, "y": css_y}),
        ]
        available = self._playwright_mcp_tool_names()
        last_error = ""
        for tool_name, args in candidates:
            if available and tool_name not in available:
                continue
            try:
                result = self.playwright_mcp.call_tool(tool_name, args)
            except Exception as exc:
                last_error = str(exc)
                continue
            if result.get("is_error"):
                last_error = str(result.get("text") or f"Playwright MCP {tool_name} failed.").strip()
                continue
            return {
                "ok": True,
                "x": css_x,
                "y": css_y,
                "backend": tool_name,
                "text": str(result.get("text") or "").strip(),
            }
        return {"ok": False, "error": last_error}

    def playwright_mcp_hit_test(
        self,
        *,
        x: Any,
        y: Any,
        candidate_id: str = "",
        target_description: str = "",
        task_id: str = "",
        action_id: str = "",
    ) -> dict[str, Any]:
        selected_candidate: dict[str, Any] = {}
        candidate_name = str(candidate_id or "").strip()
        if candidate_name:
            selected_candidate = self._find_action_candidate(candidate_name)
            if not selected_candidate:
                raise RuntimeError(f"Action candidate not found: {candidate_name}")
            center = selected_candidate.get("center") if isinstance(selected_candidate.get("center"), dict) else {}
            norm_x = max(0.0, min(float(center.get("normalizedX") or 0), 1000.0))
            norm_y = max(0.0, min(float(center.get("normalizedY") or 0), 1000.0))
        else:
            try:
                norm_x = max(0.0, min(float(x), 1000.0))
                norm_y = max(0.0, min(float(y), 1000.0))
            except Exception as exc:
                raise ValueError("Hit test requires numeric x/y coordinates or candidate_id.") from exc
        js = r"""() => {
  const compact = (value, max = 180) => String(value || '').replace(/\s+/g, ' ').trim().slice(0, max);
  const cssEscape = (value) => window.CSS && CSS.escape
    ? CSS.escape(String(value || ''))
    : String(value || '').replace(/[^a-zA-Z0-9_-]/g, (char) => `\\${char}`);
  const attrEscape = (value) => String(value || '').replace(/\\/g, '\\\\').replace(/"/g, '\\"');
  const cssPath = (element) => {
    if (!element || !(element instanceof Element)) return '';
    if (element.id) return `#${cssEscape(element.id)}`;
    const dataE2e = element.getAttribute('data-e2e');
    if (dataE2e) return `[data-e2e="${attrEscape(dataE2e)}"]`;
    const aria = element.getAttribute('aria-label');
    if (aria) return `${element.tagName.toLowerCase()}[aria-label="${attrEscape(aria)}"]`;
    const parts = [];
    let node = element;
    while (node && node instanceof Element && parts.length < 4) {
      let part = node.tagName.toLowerCase();
      const cls = Array.from(node.classList || []).filter(Boolean).slice(0, 2);
      if (cls.length) part += '.' + cls.map((name) => cssEscape(name)).join('.');
      const parent = node.parentElement;
      if (parent) {
        const siblings = Array.from(parent.children).filter((child) => child.tagName === node.tagName);
        if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(node) + 1})`;
      }
      parts.unshift(part);
      node = parent;
    }
    return parts.join(' > ');
  };
  const describe = (element) => {
    if (!element || !(element instanceof Element)) return null;
    const rect = element.getBoundingClientRect();
    const inputValue = ['INPUT', 'TEXTAREA'].includes(element.tagName) ? element.value : '';
    return {
      tag: element.tagName.toLowerCase(),
      role: compact(element.getAttribute('role'), 60),
      text: compact(element.innerText || element.textContent || inputValue, 180),
      ariaLabel: compact(element.getAttribute('aria-label'), 140),
      title: compact(element.getAttribute('title'), 140),
      placeholder: compact(element.getAttribute('placeholder'), 140),
      dataE2e: compact(element.getAttribute('data-e2e'), 100),
      className: compact(element.className, 180),
      type: compact(element.getAttribute('type'), 40),
      href: compact(element.getAttribute('href'), 180),
      selector: cssPath(element),
      rect: {
        x: Math.round(rect.x), y: Math.round(rect.y),
        width: Math.round(rect.width), height: Math.round(rect.height)
      }
    };
  };
  const x = Math.round((window.innerWidth || 1) * __NORM_X__ / 1000);
  const y = Math.round((window.innerHeight || 1) * __NORM_Y__ / 1000);
  const element = document.elementFromPoint(x, y);
  if (!element) {
    return {
      ok: false,
      reason: 'no_element',
      point: { x, y, normalizedX: __NORM_X__, normalizedY: __NORM_Y__ },
      viewport: { width: window.innerWidth, height: window.innerHeight }
    };
  }
  const target = element.closest('button,a,[role="button"],[role="link"],input,textarea,select,[contenteditable="true"],[data-e2e],[aria-label]') || element;
  const ancestors = [];
  let node = element.parentElement;
  while (node && ancestors.length < 3) {
    ancestors.push(describe(node));
    node = node.parentElement;
  }
  const targetTag = target && target.tagName ? target.tagName.toLowerCase() : '';
  const actionable = Boolean(target && target.matches('button,a,[role="button"],[role="link"],input,textarea,select,[contenteditable="true"],[data-e2e],[aria-label]'));
  return {
    ok: true,
    point: { x, y, normalizedX: __NORM_X__, normalizedY: __NORM_Y__ },
    viewport: { width: window.innerWidth, height: window.innerHeight },
    actionable,
    element: describe(element),
    target: describe(target),
    targetTag,
    ancestors: ancestors.filter(Boolean)
  };
}"""
        js = js.replace("__NORM_X__", str(norm_x)).replace("__NORM_Y__", str(norm_y))
        result = self.playwright_mcp.call_tool("browser_evaluate", {"function": js})
        if result.get("is_error"):
            raise RuntimeError(str(result.get("text") or "Playwright MCP hit test failed.").strip())
        parsed = _parse_browser_evaluate_result(str(result.get("text_raw") or result.get("text") or ""))
        if not isinstance(parsed, dict):
            raise RuntimeError(f"Hit test returned invalid payload: {parsed}")
        parsed["target_description"] = str(target_description or "").strip()
        if selected_candidate:
            parsed["candidate"] = selected_candidate
            parsed["candidate_id"] = candidate_name
        parsed["page_state"] = self._playwright_mcp_page_state()
        if task_id:
            parsed["task_id"] = task_id
        if action_id:
            parsed["action_id"] = action_id
        return parsed

    def _playwright_mcp_scroll(self, delta_y: Any) -> dict[str, Any]:
        try:
            amount = int(float(delta_y))
        except Exception as exc:
            raise ValueError("Visual scroll requires numeric delta_y.") from exc
        amount = max(-5000, min(amount, 5000))
        js = (
            "() => { "
            f"window.scrollBy({{ top: {amount}, left: 0, behavior: 'instant' }}); "
            "return { ok: true, scrollY: Math.round(window.scrollY), deltaY: "
            f"{amount}"
            " }; }"
        )
        result = self.playwright_mcp.call_tool("browser_evaluate", {"function": js})
        if result.get("is_error"):
            raise RuntimeError(str(result.get("text") or "Playwright MCP scroll failed.").strip())
        parsed = _parse_browser_evaluate_result(str(result.get("text_raw") or result.get("text") or ""))
        return parsed if isinstance(parsed, dict) else {"ok": True, "result": parsed}

    def _playwright_mcp_type_text(self, text: str) -> dict[str, Any]:
        content = str(text or "")
        if not content:
            return {"ok": True, "typed": 0}
        type_error = ""
        try:
            result = self.playwright_mcp.call_tool("browser_type", {"text": content})
        except Exception as exc:
            result = {"is_error": True, "text": str(exc)}
        if not result.get("is_error"):
            return {"ok": True, "typed": len(content), "backend": "browser_type"}
        type_error = str(result.get("text") or "").strip()
        js = (
            "() => { "
            f"const value = {_json_string(content)}; "
            "const element = document.activeElement; "
            "if (!element) return { ok: false, reason: 'no_active_element' }; "
            "if ('value' in element) { "
            "  const start = element.selectionStart ?? element.value.length; "
            "  const end = element.selectionEnd ?? element.value.length; "
            "  element.value = element.value.slice(0, start) + value + element.value.slice(end); "
            "  element.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' })); "
            "  element.dispatchEvent(new Event('change', { bubbles: true })); "
            "  return { ok: true, tag: element.tagName }; "
            "} "
            "if (element.isContentEditable) { document.execCommand('insertText', false, value); return { ok: true, tag: element.tagName }; } "
            "return { ok: false, reason: 'active_element_not_editable', tag: element.tagName }; "
            "}"
        )
        fallback = self.playwright_mcp.call_tool("browser_evaluate", {"function": js})
        if fallback.get("is_error"):
            raise RuntimeError(str(type_error or fallback.get("text") or "Playwright MCP type failed.").strip())
        parsed = _parse_browser_evaluate_result(str(fallback.get("text_raw") or fallback.get("text") or ""))
        if isinstance(parsed, dict) and parsed.get("ok") is True:
            return {"ok": True, "typed": len(content), "backend": "evaluate"}
        raise RuntimeError(f"Type text failed: {parsed}")

    def playwright_mcp_visual_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        action = str(payload.get("action") or "").strip().lower()
        task_id = str(payload.get("task_id") or "").strip()
        action_id = str(payload.get("action_id") or "").strip()
        candidate_id = str(payload.get("candidate_id") or "").strip()
        wait_after = max(0, min(int(payload.get("wait_ms") or 800), 30000))
        result: dict[str, Any]
        if action in {"click", "click_xy"}:
            if candidate_id:
                candidate = self._find_action_candidate(candidate_id)
                if not candidate:
                    raise RuntimeError(f"Action candidate not found: {candidate_id}")
                center = candidate.get("center") if isinstance(candidate.get("center"), dict) else {}
                css_x = int(center.get("x") or 0)
                css_y = int(center.get("y") or 0)
                result = self._playwright_mcp_dom_coordinate_click(
                    css_x,
                    css_y,
                    target_description=str(payload.get("target_description") or candidate.get("label") or ""),
                )
                result["candidate"] = candidate
                result["candidate_id"] = candidate_id
            else:
                result = self._playwright_mcp_click_normalised(
                    payload.get("x"),
                    payload.get("y"),
                    target_description=str(payload.get("target_description") or ""),
                )
        elif action == "scroll":
            delta_y = payload.get("delta_y")
            if delta_y is None:
                direction = str(payload.get("direction") or "down").strip().lower()
                delta_y = -700 if direction in {"up", "backward"} else 700
            result = self._playwright_mcp_scroll(delta_y)
        elif action in {"press", "key"}:
            key = str(payload.get("key") or "").strip()
            if not key:
                raise ValueError("Visual press action requires key.")
            press_result = self.playwright_mcp.call_tool("browser_press_key", {"key": key})
            if press_result.get("is_error"):
                raise RuntimeError(str(press_result.get("text") or "Playwright MCP browser_press_key failed.").strip())
            result = {"ok": True, "key": key}
        elif action in {"type", "type_text"}:
            result = self._playwright_mcp_type_text(str(payload.get("text") or ""))
        elif action == "wait":
            result = {"ok": True, "wait_ms": wait_after}
        else:
            raise ValueError(f"Unsupported visual action: {action or '<empty>'}")

        if wait_after > 0:
            self._playwright_mcp_wait(wait_after)
        diagnostics = self._playwright_mcp_page_diagnostics() if _browser_diagnostics_enabled() else {}
        page_state = self._playwright_mcp_page_state()
        return {
            "ok": True,
            "task_id": task_id,
            "action_id": action_id,
            "action": action,
            "result": result,
            "page_state": page_state,
            "diagnostics": diagnostics,
        }

    def start_worker_client(self) -> None:
        if not self.worker_enabled or not self.worker_ws_url:
            return
        if self._worker_thread and self._worker_thread.is_alive():
            return
        self._worker_thread = threading.Thread(target=self._worker_client_loop, daemon=True)
        self._worker_thread.start()

    def _worker_client_loop(self) -> None:
        while True:
            try:
                self._run_worker_client_session()
            except Exception as exc:
                print(f"Browser worker session error: {exc}")
            time.sleep(self.worker_reconnect_sec)

    def _run_worker_client_session(self) -> None:
        headers = {}
        if self.worker_token:
            headers["Authorization"] = f"Bearer {self.worker_token}"
        with websocket_connect(
            self.worker_ws_url,
            additional_headers=headers,
            open_timeout=15,
            close_timeout=5,
            proxy=None,
        ) as websocket:
            websocket.send(
                json.dumps(
                    {
                        "type": "register",
                        "worker_id": self.worker_id,
                        "token": self.worker_token,
                        "profile_path": str(self.user_data_dir),
                        "capabilities": [
                            "browser.tabs",
                            "browser.navigate",
                            "browser.snapshot",
                            "browser.screenshot",
                            "browser.hit_test",
                            "browser.visual_action",
                        ],
                    },
                    ensure_ascii=False,
                )
            )
            registered = json.loads(websocket.recv(timeout=15))
            if str(registered.get("type") or "").strip().lower() != "registered":
                raise RuntimeError(str(registered.get("detail") or "Browser worker registration failed.").strip())
            print(f"Browser worker connected: {self.worker_id} -> {self.worker_ws_url}")

            while True:
                try:
                    raw_message = websocket.recv(timeout=30)
                except TimeoutError:
                    websocket.send(json.dumps({"type": "heartbeat", "at": time.time()}, ensure_ascii=False))
                    continue
                except ConnectionClosed:
                    break

                message = json.loads(raw_message)
                if not isinstance(message, dict):
                    continue
                message_type = str(message.get("type") or "").strip().lower()
                if message_type == "heartbeat_ack":
                    continue
                if message_type != "command":
                    continue

                request_id = str(message.get("request_id") or "").strip()
                command = str(message.get("command") or "").strip()
                payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
                payload = dict(payload)
                payload.setdefault("request_id", request_id)
                try:
                    result = self.handle_worker_command(command, payload)
                    if isinstance(result, dict):
                        result.setdefault("request_id", request_id)
                        if payload.get("action_id"):
                            result.setdefault("action_id", str(payload.get("action_id") or "").strip())
                    response = {"type": "response", "request_id": request_id, "ok": True, "payload": result}
                except Exception as exc:
                    response = {
                        "type": "response",
                        "request_id": request_id,
                        "action_id": str(payload.get("action_id") or "").strip(),
                        "ok": False,
                        "error": _friendly_bridge_error_message(str(exc)),
                    }
                websocket.send(json.dumps(response, ensure_ascii=False))


bridge = BrowserBridge()


def main() -> int:
    bridge.start_worker_client()
    if not bridge.worker_enabled or not bridge.worker_ws_url:
        raise RuntimeError("Browser Worker requires BROWSER_WORKER_ENABLED=true and BROWSER_WORKER_WS_URL.")
    print("Local Thin Browser Worker started in websocket-only mode.")
    print(f"User data dir: {bridge.user_data_dir}")
    print(f"Playwright MCP command: {bridge.playwright_mcp.command}")
    print(f"Browser worker: {bridge.worker_status()}")
    while True:
        time.sleep(3600)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
