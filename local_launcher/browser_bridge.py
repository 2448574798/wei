from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import urllib.error
from collections import deque
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18100
DEFAULT_USER_DATA_DIR = Path(r"D:\Download\playwright-user-data\edge-douyin-bridge")
DEFAULT_MCP_PROTOCOL_VERSION = "2025-11-25"


@dataclass
class BrowserJob:
    id: str
    title: str
    job_type: str
    status: str = "running"
    progress: list[str] = field(default_factory=list)
    result: str = ""
    error: str = ""
    cancel_requested: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "job_type": self.job_type,
            "status": self.status,
            "progress": list(self.progress),
            "result": self.result,
            "error": self.error,
            "cancel_requested": self.cancel_requested,
        }


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

    def stop(self) -> None:
        with self._lock:
            process = self._process
            self._process = None
            self._initialized = False
            self._server_info = {}
            self._server_capabilities = {}
            self._tool_cache = []
            self._tool_cache_at = 0.0

        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

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
        self.ensure_ready(refresh_tools=False)
        result = self._request(
            "tools/call",
            {
                "name": tool_name,
                "arguments": arguments or {},
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


def _json_string(value: str) -> str:
    return json.dumps(str(value or ""), ensure_ascii=False)


def _mcp_target_to_selector(target: dict[str, Any], *, default_first: bool) -> str:
    selector = ""
    if str(target.get("selector") or "").strip():
        selector = str(target.get("selector") or "").strip()
    elif str(target.get("text") or "").strip():
        selector = f"text={_json_string(str(target.get('text') or '').strip())}"
    elif str(target.get("label") or "").strip():
        selector = f"label={_json_string(str(target.get('label') or '').strip())}"
    elif str(target.get("placeholder") or "").strip():
        selector = f"placeholder={_json_string(str(target.get('placeholder') or '').strip())}"
    elif str(target.get("role") or "").strip():
        role = str(target.get("role") or "").strip()
        name = str(target.get("name") or "").strip()
        selector = f"role={role}"
        if name:
            selector += f"[name={_json_string(name)}]"
            if bool(target.get("exact")):
                selector += "[exact=true]"
    if not selector:
        return ""

    nth = target.get("nth")
    if nth is not None:
        selector += f" >> nth={int(nth)}"
    elif target.get("last"):
        selector += " >> nth=-1"
    elif default_first:
        selector += " >> nth=0"
    return selector


def _mcp_selector_targets(step: dict[str, Any], *, default_first: bool) -> list[dict[str, Any]]:
    raw_targets = step.get("targets") if isinstance(step.get("targets"), list) else []
    candidates = [item for item in raw_targets if isinstance(item, dict)]
    if not candidates:
        fallback = {
            key: step.get(key)
            for key in ("selector", "text", "role", "label", "placeholder", "name", "exact", "nth", "last")
            if key in step
        }
        if fallback:
            candidates = [fallback]

    resolved: list[dict[str, Any]] = []
    for target in candidates:
        selector = _mcp_target_to_selector(target, default_first=default_first)
        if selector:
            resolved.append({"selector": selector, "raw": target})
    return resolved


def _mcp_css_targets(step: dict[str, Any]) -> list[dict[str, Any]]:
    raw_targets = step.get("targets") if isinstance(step.get("targets"), list) else []
    candidates = [item for item in raw_targets if isinstance(item, dict)]
    if not candidates and str(step.get("selector") or "").strip():
        candidates = [{"selector": str(step.get("selector") or "").strip()}]

    resolved: list[dict[str, Any]] = []
    for target in candidates:
        selector = str(target.get("selector") or "").strip()
        if not selector:
            continue
        nth = target.get("nth")
        last = bool(target.get("last"))
        resolved.append(
            {
                "selector": selector,
                "nth": int(nth) if nth is not None else None,
                "last": last,
                "raw": target,
            }
        )
    return resolved


def _step_has_supported_target(step: dict[str, Any]) -> bool:
    return any(str(step.get(key) or "").strip() for key in ("selector", "text", "role", "label", "placeholder"))


def _step_has_selector_target(step: dict[str, Any]) -> bool:
    if str(step.get("selector") or "").strip():
        return True
    targets = step.get("targets") if isinstance(step.get("targets"), list) else []
    return any(isinstance(item, dict) and str(item.get("selector") or "").strip() for item in targets)


def _steps_prefer_mcp_readonly(steps: list[dict[str, Any]] | None) -> bool:
    readonly_step_types = {"wait", "goto", "click", "click_any", "press", "extract_text", "extract_any_text", "extract_list_text", "snapshot"}
    for step in steps or []:
        if not isinstance(step, dict):
            return False
        step_type = str(step.get("type") or "").strip().lower()
        if step_type not in readonly_step_types:
            return False
        if step_type in {"click", "extract_text"} and not _step_has_supported_target(step):
            return False
        if step_type in {"click_any", "extract_any_text"}:
            targets = step.get("targets") if isinstance(step.get("targets"), list) else []
            if targets:
                if not any(isinstance(item, dict) and _step_has_supported_target(item) for item in targets):
                    return False
            elif not _step_has_supported_target(step):
                return False
        if step_type == "press" and _step_has_supported_target(step):
            return False
        if step_type == "extract_list_text" and not _step_has_selector_target(step):
            return False
    return True


def _with_backend_metadata(
    result: dict[str, Any],
    *,
    backend: str,
) -> dict[str, Any]:
    enriched = dict(result)
    enriched["backend"] = backend
    return enriched


class BrowserBridge:
    def __init__(self) -> None:
        self.host = os.getenv("BROWSER_BRIDGE_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST
        self.port = int(os.getenv("BROWSER_BRIDGE_PORT", str(DEFAULT_PORT)))
        self.token = os.getenv("BROWSER_BRIDGE_TOKEN", "").strip()
        self.user_data_dir = self._resolve_user_data_dir()
        self.playwright_mcp = PlaywrightMcpClient(user_data_dir=self.user_data_dir)

        self._jobs_lock = threading.Lock()
        self._jobs: dict[str, BrowserJob] = {}

    def _resolve_user_data_dir(self) -> Path:
        configured = os.getenv("PLAYWRIGHT_USER_DATA_DIR", "").strip()
        return Path(configured) if configured else DEFAULT_USER_DATA_DIR

    def _append_job_progress(self, job: BrowserJob, message: str) -> None:
        text = re.sub(r"\s+", " ", (message or "").strip())
        if text:
            job.progress.append(text)

    def playwright_mcp_status(self, *, ensure_started: bool = False, refresh_tools: bool = False) -> dict[str, Any]:
        return self.playwright_mcp.status(ensure_started=ensure_started, refresh_tools=refresh_tools)

    def playwright_mcp_tools(self, *, refresh: bool = False) -> dict[str, Any]:
        return self.playwright_mcp.tools(refresh=refresh)

    def restart_playwright_mcp(self) -> dict[str, Any]:
        return self.playwright_mcp.restart()

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

    def _playwright_mcp_wait(self, wait_ms: int) -> None:
        wait_seconds = max(0.0, min(float(wait_ms) / 1000.0, 30.0))
        if wait_seconds <= 0:
            return
        result = self.playwright_mcp.call_tool("browser_wait_for", {"time": wait_seconds})
        if result.get("is_error"):
            raise RuntimeError(str(result.get("text") or "Playwright MCP browser_wait_for failed.").strip())

    def _playwright_mcp_click(self, selector: str) -> tuple[bool, str]:
        click_result = self.playwright_mcp.call_tool("browser_click", {"target": selector})
        if not click_result.get("is_error"):
            return True, ""

        last_error = str(click_result.get("text") or "Playwright MCP browser_click failed.").strip()
        js = (
            "(element) => { "
            "if (!element) return { ok: false, reason: 'not_found' }; "
            "const clickable = element.closest('a,button,[role=\"button\"],[href],[data-e2e]') || element; "
            "clickable.scrollIntoView({ block: 'center', inline: 'center' }); "
            "clickable.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window })); "
            "if (typeof clickable.click === 'function') clickable.click(); "
            "return { ok: true }; }"
        )
        fallback_result = self.playwright_mcp.call_tool("browser_evaluate", {"target": selector, "function": js})
        if fallback_result.get("is_error"):
            return False, last_error
        parsed = _parse_browser_evaluate_result(str(fallback_result.get("text_raw") or fallback_result.get("text") or ""))
        if isinstance(parsed, dict) and parsed.get("ok") is True:
            return True, ""
        return False, last_error

    def _playwright_mcp_extract_text(self, selector: str, *, limit: int) -> str:
        result = self.playwright_mcp.call_tool(
            "browser_evaluate",
            {
                "target": selector,
                "function": "(element) => ((element.innerText || element.textContent || '').trim())",
            },
        )
        if result.get("is_error"):
            raise RuntimeError(str(result.get("text") or "Playwright MCP browser_evaluate failed.").strip())
        parsed = _parse_browser_evaluate_result(str(result.get("text_raw") or result.get("text") or ""))
        if isinstance(parsed, str):
            return _compact_text(parsed, limit)
        return _compact_text(json.dumps(parsed, ensure_ascii=False), limit)

    def _playwright_mcp_extract_list_text(self, selector: str, *, item_limit: int, limit: int, nth: int | None = None, last: bool = False) -> list[str]:
        if nth is not None:
            js = (
                f"() => {{ const items = Array.from(document.querySelectorAll({_json_string(selector)})); "
                f"const match = items[{int(nth)}]; if (!match) return []; "
                f"const text = ((match.innerText || match.textContent || '').replace(/\\s+/g, ' ').trim()).slice(0, {int(limit)}); "
                "return text ? [text] : []; }"
            )
        elif last:
            js = (
                f"() => {{ const items = Array.from(document.querySelectorAll({_json_string(selector)})); "
                "const match = items.length ? items[items.length - 1] : null; if (!match) return []; "
                f"const text = ((match.innerText || match.textContent || '').replace(/\\s+/g, ' ').trim()).slice(0, {int(limit)}); "
                "return text ? [text] : []; }"
            )
        else:
            js = (
                f"() => Array.from(document.querySelectorAll({_json_string(selector)}))"
                f".slice(0, {int(item_limit)})"
                f".map((element) => ((element.innerText || element.textContent || '').replace(/\\s+/g, ' ').trim()).slice(0, {int(limit)}))"
                ".filter(Boolean)"
            )
        result = self.playwright_mcp.call_tool("browser_evaluate", {"function": js})
        if result.get("is_error"):
            raise RuntimeError(str(result.get("text") or "Playwright MCP browser_evaluate failed.").strip())
        parsed = _parse_browser_evaluate_result(str(result.get("text_raw") or result.get("text") or ""))
        if isinstance(parsed, list):
            return [_compact_text(item, limit) for item in parsed if str(item or "").strip()]
        if isinstance(parsed, str):
            cleaned = _compact_text(parsed, limit)
            return [cleaned] if cleaned else []
        if parsed:
            cleaned = _compact_text(json.dumps(parsed, ensure_ascii=False), limit)
            return [cleaned] if cleaned else []
        return []

    def _playwright_mcp_snapshot_text(self, *, target: str = "", limit: int = 3000, depth: int | None = None) -> str:
        args: dict[str, Any] = {}
        if str(target or "").strip():
            args["target"] = str(target).strip()
        if depth is not None:
            args["depth"] = int(depth)
        result = self.playwright_mcp.call_tool("browser_snapshot", args)
        if result.get("is_error"):
            raise RuntimeError(str(result.get("text") or "Playwright MCP browser_snapshot failed.").strip())
        return _compact_text(str(result.get("text_raw") or result.get("text") or ""), limit)

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
        if target_url:
            self.playwright_mcp_navigate(target_url)

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
        resolved_url = str(current_tab.get("url") or target_url or "").strip()
        resolved_title = str(current_tab.get("title") or "").strip()
        return {
            "ok": True,
            "url": resolved_url,
            "title": resolved_title,
            "instruction": str(instruction or "").strip(),
            "text": snapshot_text,
            "tool_result": result,
            "tabs": tabs,
        }

    def playwright_mcp_interact_readonly(
        self,
        *,
        url: str = "",
        instruction: str = "",
        steps: list[dict[str, Any]] | None = None,
        wait_ms: int = 1000,
    ) -> dict[str, Any]:
        target_url = str(url or "").strip()
        interaction_steps = [item for item in (steps or []) if isinstance(item, dict)]
        if target_url:
            self.playwright_mcp_navigate(target_url)
        if wait_ms > 0:
            self._playwright_mcp_wait(wait_ms)

        extracts: list[dict[str, Any]] = []
        step_results: list[dict[str, Any]] = []
        snapshot_text = ""

        for index, step in enumerate(interaction_steps, start=1):
            step_type = str(step.get("type") or "").strip().lower()
            timeout_ms = max(500, min(int(step.get("timeout_ms") or 5000), 60000))
            wait_after = max(0, min(int(step.get("wait_ms") or 0), 60000))
            if step_type == "wait":
                explicit_wait = int(step.get("ms") or step.get("wait_ms") or 1000)
                self._playwright_mcp_wait(explicit_wait)
                step_results.append({"index": index, "type": step_type, "status": "ok"})
                continue

            if step_type == "goto":
                next_url = str(step.get("url") or "").strip()
                if not next_url:
                    raise ValueError(f"Step {index} goto requires a url.")
                self.playwright_mcp_navigate(next_url)
                if wait_after > 0:
                    self._playwright_mcp_wait(wait_after)
                step_results.append({"index": index, "type": step_type, "status": "ok", "url": next_url})
                continue

            if step_type == "press":
                if _step_has_supported_target(step):
                    raise ValueError(f"Step {index} press with element target is not supported in MCP readonly mode.")
                key = str(step.get("key") or "").strip()
                if not key:
                    raise ValueError(f"Step {index} press requires a key.")
                press_result = self.playwright_mcp.call_tool("browser_press_key", {"key": key})
                if press_result.get("is_error"):
                    raise RuntimeError(str(press_result.get("text") or "Playwright MCP browser_press_key failed.").strip())
                if wait_after <= 0:
                    wait_after = min(timeout_ms, 800)
                if wait_after > 0:
                    self._playwright_mcp_wait(wait_after)
                step_results.append({"index": index, "type": step_type, "status": "ok", "key": key})
                continue

            if step_type == "click":
                candidates = _mcp_selector_targets(step, default_first=True)
                if not candidates:
                    raise ValueError(f"Step {index} click requires a supported selector, text, role, label, or placeholder target.")
                selector = str(candidates[0]["selector"])
                click_ok, click_error = self._playwright_mcp_click(selector)
                if not click_ok:
                    if bool(step.get("optional")):
                        step_results.append({"index": index, "type": step_type, "status": "skipped", "target": selector})
                        continue
                    raise RuntimeError(click_error or "Playwright MCP browser_click failed.")
                if wait_after <= 0:
                    wait_after = min(timeout_ms, 1500)
                if wait_after > 0:
                    self._playwright_mcp_wait(wait_after)
                step_results.append({"index": index, "type": step_type, "status": "ok", "target": selector})
                continue

            if step_type == "click_any":
                candidates = _mcp_selector_targets(step, default_first=True)
                if not candidates:
                    raise ValueError(f"Step {index} click_any requires supported targets, selectors, texts, roles, labels, or placeholders.")
                last_error = "No candidates attempted."
                matched_target = ""
                for candidate in candidates:
                    selector = str(candidate["selector"])
                    click_ok, click_error = self._playwright_mcp_click(selector)
                    if not click_ok:
                        last_error = click_error or "Playwright MCP browser_click failed."
                        continue
                    matched_target = selector
                    last_error = ""
                    break
                if last_error:
                    if bool(step.get("optional")):
                        step_results.append({"index": index, "type": step_type, "status": "skipped"})
                        continue
                    raise RuntimeError(f"Step {index} click_any failed: {last_error}")
                if wait_after <= 0:
                    wait_after = min(timeout_ms, 1500)
                if wait_after > 0:
                    self._playwright_mcp_wait(wait_after)
                step_results.append({"index": index, "type": step_type, "status": "ok", "target": matched_target})
                continue

            if step_type == "extract_text":
                candidates = _mcp_selector_targets(step, default_first=True)
                if not candidates:
                    raise ValueError(f"Step {index} extract_text requires a supported selector, text, role, label, or placeholder target.")
                selector = str(candidates[0]["selector"])
                extracted_text = self._playwright_mcp_extract_text(selector, limit=int(step.get("limit") or 1000))
                name = str(step.get("name") or f"extract_{index}").strip()
                extracts.append({"name": name, "text": extracted_text})
                step_results.append({"index": index, "type": step_type, "status": "ok", "name": name, "target": selector})
                continue

            if step_type == "extract_any_text":
                candidates = _mcp_selector_targets(step, default_first=True)
                if not candidates:
                    raise ValueError(f"Step {index} extract_any_text requires supported targets, selectors, texts, roles, labels, or placeholders.")
                last_error = "No candidates attempted."
                extracted_text = ""
                matched_target = ""
                for candidate in candidates:
                    selector = str(candidate["selector"])
                    try:
                        extracted_text = self._playwright_mcp_extract_text(selector, limit=int(step.get("limit") or 1000))
                        if not extracted_text:
                            raise RuntimeError("Matched element was empty.")
                        matched_target = selector
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

            if step_type == "extract_list_text":
                candidates = _mcp_css_targets(step)
                if not candidates:
                    raise ValueError(f"Step {index} extract_list_text requires selector-based targets for MCP readonly mode.")
                item_limit = max(1, min(int(step.get("item_limit") or 8), 50))
                joiner = str(step.get("joiner") or " || ").strip() or " || "
                last_error = "No candidates attempted."
                matched_target = ""
                extracted_items: list[str] = []
                for candidate in candidates:
                    selector = str(candidate["selector"])
                    try:
                        extracted_items = self._playwright_mcp_extract_list_text(
                            selector,
                            item_limit=item_limit,
                            limit=int(step.get("limit") or 400),
                            nth=candidate.get("nth"),
                            last=bool(candidate.get("last")),
                        )
                        if not extracted_items:
                            raise RuntimeError("Matched elements were empty.")
                        matched_target = selector
                        last_error = ""
                        break
                    except Exception as exc:
                        last_error = str(exc)
                if last_error:
                    raise RuntimeError(f"Step {index} extract_list_text failed: {last_error}")
                name = str(step.get("name") or f"extract_{index}").strip()
                extracts.append({"name": name, "text": joiner.join(extracted_items)})
                step_results.append(
                    {
                        "index": index,
                        "type": step_type,
                        "status": "ok",
                        "name": name,
                        "target": matched_target,
                        "count": len(extracted_items),
                    }
                )
                continue

            if step_type == "snapshot":
                candidates = _mcp_selector_targets(step, default_first=True)
                selector = str(candidates[0]["selector"]) if candidates else "body >> nth=0"
                snapshot_text = self._playwright_mcp_snapshot_text(
                    target=selector,
                    limit=int(step.get("limit") or 3000),
                )
                step_results.append({"index": index, "type": step_type, "status": "ok", "target": selector})
                continue

            raise ValueError(f"Unsupported interaction step type for MCP readonly mode: {step_type or '<empty>'}")

        if not snapshot_text:
            snapshot_text = self._playwright_mcp_snapshot_text(target="body >> nth=0", limit=3000)

        tabs = self.playwright_mcp_tabs()
        structured_tabs = tabs.get("structured_content") if isinstance(tabs.get("structured_content"), dict) else {}
        current_tab = _extract_current_tab(structured_tabs)
        current_url = str(current_tab.get("url") or target_url or "").strip()
        current_title = str(current_tab.get("title") or "").strip()
        return {
            "ok": True,
            "title": current_title,
            "url": current_url,
            "instruction": str(instruction or "").strip(),
            "text": snapshot_text,
            "extracts": extracts,
            "step_results": step_results,
            "tabs": tabs,
        }

    def interact_prefer_mcp_readonly(
        self,
        url: str = "",
        *,
        instruction: str = "",
        steps: list[dict[str, Any]] | None = None,
        wait_ms: int = 1000,
        page_index: int | None = None,
    ) -> dict[str, Any]:
        interaction_steps = [item for item in (steps or []) if isinstance(item, dict)]
        if not _steps_prefer_mcp_readonly(interaction_steps):
            raise ValueError(
                "MCP-only Browser Bridge supports readonly steps only: wait, goto, click, click_any, press, "
                "extract_text, extract_any_text, extract_list_text, and snapshot."
            )
        result = self.playwright_mcp_interact_readonly(
            url=url,
            instruction=instruction,
            steps=interaction_steps,
            wait_ms=wait_ms,
        )
        return _with_backend_metadata(result, backend="playwright_mcp")

    def start_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        job_type = str(payload.get("job_type") or "").strip()
        if job_type not in {"watch_text", "interaction_watch"}:
            raise ValueError(f"Unsupported job_type: {job_type or '<empty>'}")

        job = BrowserJob(
            id=str(uuid4()),
            title=str(payload.get("title") or "Local webpage watch").strip() or "Local webpage watch",
            job_type=job_type,
        )
        with self._jobs_lock:
            self._jobs[job.id] = job

        thread = threading.Thread(
            target=self._run_watch_text_job if job_type == "watch_text" else self._run_interaction_watch_job,
            args=(job.id, payload),
            daemon=True,
        )
        thread.start()
        return {"ok": True, "job_id": job.id, "status": job.status}

    def _run_watch_text_job(self, job_id: str, payload: dict[str, Any]) -> None:
        job = self._jobs[job_id]
        url = str(payload.get("url") or "").strip()
        keyword = str(payload.get("keyword") or "").strip()
        rounds = max(1, min(int(payload.get("rounds") or 20), 200))
        interval_sec = max(2, min(int(payload.get("interval_sec") or 8), 300))
        wait_ms = max(1000, min(int(payload.get("wait_ms") or 3000), 15000))

        try:
            if not url:
                raise ValueError("url is required")
            if not keyword:
                raise ValueError("keyword is required")

            lowered_keyword = keyword.lower()
            for index in range(rounds):
                if job.cancel_requested:
                    job.status = "cancelled"
                    self._append_job_progress(job, "Job cancelled.")
                    job.result = "Job cancelled."
                    return

                snapshot = self.snapshot(url, instruction=keyword, wait_ms=wait_ms)
                text = str(snapshot.get("text") or "")
                excerpt = text[:240] or "[No body text extracted]"
                self._append_job_progress(job, f"Round {index + 1}: {excerpt}")

                if lowered_keyword in text.lower():
                    job.status = "completed"
                    job.result = f"Matched keyword: {keyword}\n{text[:2000]}"
                    self._append_job_progress(job, f"Matched keyword: {keyword}")
                    return

                if index < rounds - 1:
                    time.sleep(interval_sec)

            job.status = "completed"
            job.result = f"Keyword not found: {keyword}"
            self._append_job_progress(job, f"Keyword not found: {keyword}")
        except Exception as exc:
            job.status = "failed"
            job.error = str(exc)
            self._append_job_progress(job, f"Execution failed: {exc}")

    def _run_interaction_watch_job(self, job_id: str, payload: dict[str, Any]) -> None:
        job = self._jobs[job_id]
        url = str(payload.get("url") or "").strip()
        keyword = str(payload.get("keyword") or "").strip()
        rounds = max(1, min(int(payload.get("rounds") or 20), 500))
        interval_sec = max(1, min(int(payload.get("interval_sec") or 8), 300))
        wait_ms = max(0, min(int(payload.get("wait_ms") or 1500), 15000))
        steps = payload.get("steps") if isinstance(payload.get("steps"), list) else []
        advance_steps = payload.get("advance_steps") if isinstance(payload.get("advance_steps"), list) else []
        match_extract_names = {
            str(item).strip()
            for item in (payload.get("match_extract_names") or [])
            if str(item).strip()
        }

        def backend_label(result: dict[str, Any]) -> str:
            return str(result.get("backend") or "").strip()

        try:
            if not keyword:
                raise ValueError("keyword is required")
            if not steps:
                raise ValueError("steps are required")

            lowered_keyword = keyword.lower()
            current_url = url
            current_page_index = payload.get("page_index")
            if current_page_index is not None:
                current_page_index = int(current_page_index)
            for index in range(rounds):
                if job.cancel_requested:
                    job.status = "cancelled"
                    self._append_job_progress(job, "Job cancelled.")
                    job.result = "Job cancelled."
                    return

                result = self.interact_prefer_mcp_readonly(
                    current_url,
                    instruction=keyword,
                    steps=steps,
                    wait_ms=wait_ms,
                    page_index=current_page_index,
                )
                current_url = ""
                next_page_index = result.get("page_index")
                if next_page_index is not None:
                    current_page_index = int(next_page_index)

                extracts = result.get("extracts") or []
                snapshot_text = str(result.get("text") or "").strip()
                extract_parts: list[str] = []
                for item in extracts:
                    name = str(item.get("name") or "").strip()
                    text = str(item.get("text") or "").strip()
                    if not text:
                        continue
                    if not match_extract_names or name in match_extract_names:
                        extract_parts.append(text)
                searched_text = "\n".join(extract_parts or [snapshot_text]).strip()
                preview = searched_text[:240] or snapshot_text[:240] or "[No text extracted]"
                result_backend = backend_label(result)
                round_prefix = f"Round {index + 1}"
                if result_backend:
                    round_prefix += f" [{result_backend}]"
                self._append_job_progress(job, f"{round_prefix}: {preview}")

                if lowered_keyword in searched_text.lower():
                    job.status = "completed"
                    job.result = json.dumps(
                        {
                            "matched_keyword": keyword,
                            "url": result.get("url", ""),
                            "title": result.get("title", ""),
                            "backend": str(result.get("backend") or ""),
                            "extracts": extracts,
                            "text": snapshot_text[:3000],
                        },
                        ensure_ascii=False,
                    )
                    self._append_job_progress(job, f"Matched keyword: {keyword}")
                    return

                if index < rounds - 1:
                    if advance_steps:
                        try:
                            advance_result = self.interact_prefer_mcp_readonly(
                                "",
                                instruction="advance",
                                steps=advance_steps,
                                wait_ms=500,
                                page_index=current_page_index,
                            )
                            next_page_index = advance_result.get("page_index")
                            if next_page_index is not None:
                                current_page_index = int(next_page_index)
                            advance_text = str(advance_result.get("text") or "").strip()
                            if advance_text:
                                advance_backend = backend_label(advance_result)
                                advance_prefix = "Advance"
                                if advance_backend:
                                    advance_prefix += f" [{advance_backend}]"
                                self._append_job_progress(job, f"{advance_prefix}: {advance_text[:160]}")
                        except Exception as exc:
                            self._append_job_progress(job, f"Advance failed: {exc}")
                    time.sleep(interval_sec)

            job.status = "completed"
            job.result = f"Keyword not found: {keyword}"
            self._append_job_progress(job, f"Keyword not found: {keyword}")
        except Exception as exc:
            job.status = "failed"
            job.error = str(exc)
            self._append_job_progress(job, f"Execution failed: {exc}")

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            return job.to_dict() if job else None

    def cancel_job(self, job_id: str) -> dict[str, Any] | None:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            job.cancel_requested = True
            if job.status == "running":
                job.status = "cancelling"
            self._append_job_progress(job, "Cancellation requested.")
            return job.to_dict()


bridge = BrowserBridge()


class BrowserBridgeHandler(BaseHTTPRequestHandler):
    server_version = "WeiBrowserBridge/0.3"

    def do_GET(self) -> None:
        try:
            self._authorize()
            if self.path == "/health":
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "status": "ok",
                        "playwright_mcp": bridge.playwright_mcp_status(),
                        "host": bridge.host,
                        "port": bridge.port,
                    },
                )
                return

            if self.path == "/mcp/status":
                self._send_json(HTTPStatus.OK, bridge.playwright_mcp_status(ensure_started=True))
                return

            if self.path == "/mcp/tools":
                self._send_json(HTTPStatus.OK, bridge.playwright_mcp_tools())
                return

            if self.path == "/mcp/tabs":
                self._send_json(HTTPStatus.OK, bridge.playwright_mcp_tabs())
                return

            job_id = self._extract_job_id()
            if job_id:
                job = bridge.get_job(job_id)
                if not job:
                    self._send_json(HTTPStatus.NOT_FOUND, {"detail": "Job not found."})
                    return
                self._send_json(HTTPStatus.OK, job)
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"detail": "Not found."})
        except Exception as exc:
            self._send_error_json(exc)

    def do_POST(self) -> None:
        try:
            self._authorize()
            payload = self._read_json()

            if self.path == "/jobs/start":
                result = bridge.start_job(payload)
                self._send_json(HTTPStatus.OK, result)
                return

            if self.path == "/mcp/restart":
                result = bridge.restart_playwright_mcp()
                self._send_json(HTTPStatus.OK, result)
                return

            if self.path == "/mcp/navigate":
                result = bridge.playwright_mcp_navigate(str(payload.get("url") or "").strip())
                self._send_json(HTTPStatus.OK, result)
                return

            if self.path == "/mcp/snapshot":
                result = bridge.playwright_mcp_snapshot(
                    url=str(payload.get("url") or "").strip(),
                    instruction=str(payload.get("instruction") or "").strip(),
                    wait_ms=int(payload.get("wait_ms") or 3000),
                    target=str(payload.get("target") or "").strip(),
                    depth=int(payload["depth"]) if payload.get("depth") is not None else None,
                )
                self._send_json(HTTPStatus.OK, result)
                return

            if self.path == "/mcp/interact":
                result = bridge.interact_prefer_mcp_readonly(
                    url=str(payload.get("url") or "").strip(),
                    instruction=str(payload.get("instruction") or "").strip(),
                    steps=payload.get("steps") if isinstance(payload.get("steps"), list) else [],
                    wait_ms=int(payload.get("wait_ms") or 1000),
                )
                self._send_json(HTTPStatus.OK, result)
                return

            job_id = self._extract_job_id(suffix="/cancel")
            if job_id:
                job = bridge.cancel_job(job_id)
                if not job:
                    self._send_json(HTTPStatus.NOT_FOUND, {"detail": "Job not found."})
                    return
                self._send_json(HTTPStatus.OK, job)
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"detail": "Not found."})
        except Exception as exc:
            self._send_error_json(exc)

    def _extract_job_id(self, suffix: str = "") -> str | None:
        prefix = "/jobs/"
        if not self.path.startswith(prefix):
            return None
        tail = self.path[len(prefix) :]
        if suffix:
            if not tail.endswith(suffix):
                return None
            tail = tail[: -len(suffix)]
        if not tail or "/" in tail.strip("/"):
            return None
        return tail.strip("/")

    def _authorize(self) -> None:
        if not bridge.token:
            return
        auth = self.headers.get("Authorization", "").strip()
        expected = f"Bearer {bridge.token}"
        if auth != expected:
            raise PermissionError("Unauthorized")

    def _read_json(self) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0") or "0")
        if content_length <= 0:
            return {}
        raw = self.rfile.read(content_length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_error_json(self, exc: Exception) -> None:
        if isinstance(exc, PermissionError):
            self._send_json(HTTPStatus.UNAUTHORIZED, {"detail": str(exc)})
            return
        if isinstance(exc, FileNotFoundError):
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"detail": str(exc)})
            return
        if isinstance(exc, ValueError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"detail": str(exc)})
            return
        if isinstance(exc, subprocess.TimeoutExpired):
            self._send_json(HTTPStatus.GATEWAY_TIMEOUT, {"detail": str(exc)})
            return
        self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"detail": str(exc)})

    def log_message(self, format: str, *args: Any) -> None:
        return


def main() -> int:
    server = ThreadingHTTPServer((bridge.host, bridge.port), BrowserBridgeHandler)
    print(f"Browser Bridge listening on http://{bridge.host}:{bridge.port}")
    print(f"User data dir: {bridge.user_data_dir}")
    print(f"Playwright MCP command: {bridge.playwright_mcp.command}")
    if bridge.token:
        print("Auth token: configured")
    else:
        print("Auth token: not configured")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
