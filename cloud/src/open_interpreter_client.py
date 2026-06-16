from __future__ import annotations

import json
import logging
import os
import re
from typing import Callable

from websockets.sync.client import connect


logger = logging.getLogger("wei_agent")


def get_open_interpreter_ws_url() -> str:
    return os.getenv("OPEN_INTERPRETER_WS_URL", "").strip().rstrip("/")


def get_open_interpreter_model() -> str:
    return os.getenv("OPEN_INTERPRETER_MODEL", "open-interpreter").strip() or "open-interpreter"


def get_open_interpreter_timeout() -> int:
    try:
        return max(5, min(int(os.getenv("OPEN_INTERPRETER_TIMEOUT", "90") or "90"), 600))
    except Exception:
        return 90


def get_open_interpreter_auth_key() -> str:
    return os.getenv("OPEN_INTERPRETER_AUTH_KEY", "").strip()


def open_interpreter_is_configured() -> bool:
    return bool(get_open_interpreter_ws_url() and get_open_interpreter_auth_key())


def send_open_interpreter_payload(ws, payload: dict) -> None:
    ws.send(json.dumps(payload))


def format_open_interpreter_result(code: str, output: str) -> str:
    cleaned = (output or "").strip()
    if not cleaned:
        return "执行完成，但没有产生控制台输出。"

    normalized = cleaned.replace("\r\n", "\n").strip()
    side_effect_code = any(
        keyword in code
        for keyword in [
            "webbrowser.open",
            "os.startfile",
            "subprocess.Popen",
            "subprocess.run",
            "subprocess.call",
            "start ",
        ]
    )
    if normalized in {"True", "False"} and side_effect_code:
        return f"执行完成，动作已触发。原始返回值：{normalized}"
    return normalized


def summarize_console_chunk(text: str, limit: int = 160) -> str:
    compact = re.sub(r"\s+", " ", (text or "").strip())
    if len(compact) <= limit:
        return compact
    return compact[:limit].rstrip() + "..."


def run_open_interpreter(
    code: str,
    *,
    language: str = "python",
    progress_callback: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> str:
    ws_url = get_open_interpreter_ws_url()
    if not ws_url:
        return "Open Interpreter websocket is not configured. Set OPEN_INTERPRETER_WS_URL."
    if not (ws_url.startswith("ws://") or ws_url.startswith("wss://")):
        return "Open Interpreter websocket URL must start with ws:// or wss://."

    code = (code or "").strip()
    if not code:
        return "Open Interpreter code cannot be empty."

    timeout = get_open_interpreter_timeout()
    auth_key = get_open_interpreter_auth_key()
    if not auth_key:
        return "Open Interpreter auth key is not configured. Set OPEN_INTERPRETER_AUTH_KEY."
    console_chunks: list[str] = []
    server_errors: list[str] = []

    try:
        with connect(ws_url, open_timeout=min(timeout, 15), close_timeout=5) as ws:
            send_open_interpreter_payload(ws, {"auth": auth_key})

            authenticated = False
            for _ in range(5):
                raw = ws.recv(timeout=3)
                data = json.loads(raw)
                if data.get("auth") is True:
                    authenticated = True
                    break
                if data.get("type") == "error":
                    server_errors.append(str(data.get("content", "")).strip())
            if not authenticated:
                return "Open Interpreter authentication failed."

            send_open_interpreter_payload(ws, {"role": "assistant", "start": True})
            send_open_interpreter_payload(
                ws,
                {
                    "role": "assistant",
                    "type": "code",
                    "format": language,
                    "content": code,
                },
            )
            send_open_interpreter_payload(ws, {"role": "user", "type": "command", "start": True})
            send_open_interpreter_payload(ws, {"role": "user", "type": "command", "content": "go"})
            send_open_interpreter_payload(ws, {"role": "user", "type": "command", "end": True})

            for _ in range(300):
                if cancel_check and cancel_check():
                    return "本地任务已收到取消请求，执行器正在停止。"

                raw = ws.recv(timeout=8)
                data = json.loads(raw)
                msg_type = data.get("type")
                msg_format = data.get("format")

                if msg_type == "console" and msg_format == "output":
                    text = str(data.get("content", ""))
                    if text:
                        console_chunks.append(text)
                        if progress_callback:
                            progress_callback(text)
                elif msg_type == "error":
                    server_errors.append(str(data.get("content", "")).strip())
                elif msg_type == "console" and msg_format == "active_line" and data.get("content") is None:
                    break
                elif msg_type == "status" and data.get("content") == "complete":
                    break

    except Exception as exc:
        logger.warning("Open Interpreter websocket request failed: %s", exc)
        return f"Open Interpreter request failed: {exc}"

    output = "".join(console_chunks).strip()
    if output:
        return format_open_interpreter_result(code, output)
    if server_errors:
        logger.warning("ask_open_interpreter server error: %s", server_errors[-1][:300])
        return f"Open Interpreter execution failed: {server_errors[-1][:1200]}"
    return "Open Interpreter returned no output."
