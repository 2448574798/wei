import logging
import os
import re
import smtplib
import time
from contextlib import suppress
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urlparse
import json

import requests
from bs4 import BeautifulSoup
from websockets.sync.client import connect

from src.execution_context import emit_runtime_event_sync, get_execution_context
from src.human_loop import confirmation_store
from src.local_jobs import local_job_store
from src.research_client import call_online_research_model
from src.runtime_config import ONLINE_RESEARCH_MODEL


logger = logging.getLogger("wei_agent")
SEARXNG_URL = os.getenv("SEARXNG_URL", "http://127.0.0.1:8888")
FETCH_TEXT_LIMIT = int(os.getenv("FETCH_TEXT_LIMIT", "3000"))
SEARCH_RESULT_LIMIT = int(os.getenv("SEARCH_RESULT_LIMIT", "5"))

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.qq.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
DEFAULT_FROM = os.getenv("SMTP_FROM", SMTP_USER)
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,"
        "image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
}
FETCH_BLOCKED_STATUS_CODES = {403, 429}
META_PREFIX = "__WEI_META__:"


def get_open_interpreter_url() -> str:
    return os.getenv("OPEN_INTERPRETER_URL", "").strip().rstrip("/")


def get_open_interpreter_model() -> str:
    return os.getenv("OPEN_INTERPRETER_MODEL", "open-interpreter").strip() or "open-interpreter"


def get_open_interpreter_timeout() -> int:
    return int(os.getenv("OPEN_INTERPRETER_TIMEOUT", "90"))


def get_open_interpreter_auth_key() -> str:
    return os.getenv("OPEN_INTERPRETER_AUTH_KEY", "dummy-api-key").strip() or "dummy-api-key"


def open_interpreter_is_configured() -> bool:
    return bool(get_open_interpreter_url())


def get_browser_bridge_url() -> str:
    return os.getenv("BROWSER_BRIDGE_URL", "").strip().rstrip("/")


def get_browser_bridge_token() -> str:
    return os.getenv("BROWSER_BRIDGE_TOKEN", "").strip()


def get_browser_bridge_timeout() -> int:
    return int(os.getenv("BROWSER_BRIDGE_TIMEOUT", "60"))


def browser_bridge_is_configured() -> bool:
    return bool(get_browser_bridge_url())


def smtp_is_configured() -> bool:
    return bool(SMTP_USER and SMTP_PASSWORD and DEFAULT_FROM)


def is_fetch_error(text: str) -> bool:
    return text.startswith("Fetch failed:")


def get_domain(url: str) -> str:
    return urlparse(url).netloc.lower()


def web_search(query: str) -> str:
    """Search the web with SearXNG and return a compact text summary."""
    try:
        response = requests.get(
            f"{SEARXNG_URL}/search",
            params={"q": query, "format": "json"},
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        return f"Search failed: {exc}"

    results = []
    for item in data.get("results", [])[:SEARCH_RESULT_LIMIT]:
        title = item.get("title", "Untitled")
        url = item.get("url", "")
        content = item.get("content", "")
        results.append(f"Title: {title}\nURL: {url}\nSnippet: {content}")

    return "\n\n".join(results) if results else "No relevant results found."


def fetch_webpage(url: str) -> str:
    """Fetch webpage text content and return a cleaned plain-text excerpt."""
    try:
        response = requests.get(url, headers=REQUEST_HEADERS, timeout=15)
        response.raise_for_status()
        apparent = (response.apparent_encoding or "").strip()
        declared = (response.encoding or "").strip()
        if apparent:
            response.encoding = apparent
        elif not declared:
            response.encoding = "utf-8"

        soup = BeautifulSoup(response.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()

        text = soup.get_text(separator="\n", strip=True)
        text = re.sub(r"\n\s*\n", "\n\n", text)
        if len(text) > FETCH_TEXT_LIMIT:
            text = text[:FETCH_TEXT_LIMIT] + "...\n[Content truncated]"
        return text
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else "unknown"
        if status_code in FETCH_BLOCKED_STATUS_CODES:
            logger.warning("Fetch blocked by %s with status %s", get_domain(url), status_code)
            return (
                f"Fetch failed: source blocked automated access with status {status_code} "
                f"for {get_domain(url)}"
            )
        logger.warning("Fetch HTTP error for %s with status %s", get_domain(url), status_code)
        return f"Fetch failed: HTTP {status_code} for {get_domain(url)}"
    except Exception as exc:
        logger.warning("Fetch failed for %s: %s", get_domain(url), exc)
        return f"Fetch failed: {exc}"


def _to_open_interpreter_ws_url(base_url: str) -> str:
    if base_url.startswith("https://"):
        return "wss://" + base_url[len("https://") :].rstrip("/") + "/"
    if base_url.startswith("http://"):
        return "ws://" + base_url[len("http://") :].rstrip("/") + "/"
    if base_url.startswith("wss://") or base_url.startswith("ws://"):
        return base_url.rstrip("/") + "/"
    return "ws://" + base_url.rstrip("/") + "/"


def _send_open_interpreter_payload(ws, payload: dict) -> None:
    ws.send(json.dumps(payload))


def encode_meta_payload(payload: dict) -> str:
    return META_PREFIX + json.dumps(payload, ensure_ascii=False)


def decode_meta_payload(text: str) -> dict | None:
    content = (text or "").strip()
    if not content.startswith(META_PREFIX):
        return None
    with suppress(Exception):
        return json.loads(content[len(META_PREFIX) :])
    return None


def _format_open_interpreter_result(code: str, output: str) -> str:
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


def _browser_bridge_headers() -> dict[str, str]:
    token = get_browser_bridge_token()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _browser_bridge_url(path: str) -> str:
    base_url = get_browser_bridge_url()
    if not base_url:
        raise RuntimeError("BROWSER_BRIDGE_URL is not configured.")
    return base_url + path


def _browser_bridge_get(path: str, timeout: int | None = None) -> dict:
    response = requests.get(
        _browser_bridge_url(path),
        headers=_browser_bridge_headers(),
        timeout=timeout or get_browser_bridge_timeout(),
    )
    response.raise_for_status()
    return response.json()


def _browser_bridge_post(path: str, payload: dict, timeout: int | None = None) -> dict:
    response = requests.post(
        _browser_bridge_url(path),
        headers=_browser_bridge_headers(),
        json=payload,
        timeout=timeout or get_browser_bridge_timeout(),
    )
    response.raise_for_status()
    return response.json()


def _mirror_browser_bridge_job(local_job_id: str, remote_job_id: str) -> str:
    seen_progress = 0
    cancel_sent = False

    while True:
        if local_job_store.is_cancel_requested(local_job_id) and not cancel_sent:
            cancel_sent = True
            try:
                _browser_bridge_post(f"/jobs/{remote_job_id}/cancel", {})
                local_job_store.append_progress(local_job_id, "Sent a cancellation request to Browser Bridge.")
            except Exception as exc:
                local_job_store.append_progress(local_job_id, f"Failed to send cancellation request: {exc}")

        payload = _browser_bridge_get(f"/jobs/{remote_job_id}", timeout=15)
        progress_items = payload.get("progress", [])
        for item in progress_items[seen_progress:]:
            local_job_store.append_progress(local_job_id, item)
        seen_progress = len(progress_items)

        status = str(payload.get("status", "running"))
        if status == "completed":
            result = str(payload.get("result", "")).strip() or "Local webpage watch completed."
            return result
        if status in {"failed", "cancelled"}:
            detail = str(payload.get("error") or payload.get("result") or status).strip()
            raise RuntimeError(detail or "Browser Bridge job failed.")

        time.sleep(2)

def online_research(question: str) -> str:
    """Research current or changing information on the web and return a concise Chinese answer."""
    text = (question or "").strip()
    if not text:
        return "联网思考问题不能为空。"
    try:
        logger.info("online_research tool start: %s", text[:200])
        return call_online_research_model(text, text, ONLINE_RESEARCH_MODEL)
    except Exception as exc:
        logger.warning("online_research tool failed: %s", exc)
        return f"联网思考失败：{exc}"


def _run_open_interpreter(
    code: str,
    *,
    language: str = "python",
    progress_callback=None,
    cancel_check=None,
) -> str:
    base_url = get_open_interpreter_url()
    if not base_url:
        return "Open Interpreter is not configured. Set OPEN_INTERPRETER_URL."

    code = (code or "").strip()
    if not code:
        return "Open Interpreter code cannot be empty."

    timeout = get_open_interpreter_timeout()
    ws_url = _to_open_interpreter_ws_url(base_url)
    auth_key = get_open_interpreter_auth_key()
    console_chunks: list[str] = []
    server_errors: list[str] = []

    try:
        with connect(ws_url, open_timeout=min(timeout, 15), close_timeout=5) as ws:
            _send_open_interpreter_payload(ws, {"auth": auth_key})

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

            _send_open_interpreter_payload(ws, {"role": "assistant", "start": True})
            _send_open_interpreter_payload(
                ws,
                {
                    "role": "assistant",
                    "type": "code",
                    "format": language,
                    "content": code,
                },
            )
            _send_open_interpreter_payload(ws, {"role": "user", "type": "command", "start": True})
            _send_open_interpreter_payload(ws, {"role": "user", "type": "command", "content": "go"})
            _send_open_interpreter_payload(ws, {"role": "user", "type": "command", "end": True})

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
        return _format_open_interpreter_result(code, output)
    if server_errors:
        logger.warning("ask_open_interpreter server error: %s", server_errors[-1][:300])
        return f"Open Interpreter execution failed: {server_errors[-1][:1200]}"
    return "Open Interpreter returned no output."


def summarize_console_chunk(text: str, limit: int = 160) -> str:
    compact = re.sub(r"\s+", " ", (text or "").strip())
    if len(compact) <= limit:
        return compact
    return compact[:limit].rstrip() + "..."


def ask_open_interpreter(code: str, language: str = "python") -> str:
    """Execute already-prepared code with Open Interpreter.

    Pass runnable code directly, not natural-language instructions.
    For side-effect tasks such as opening apps, launching a browser, or writing files,
    prefer code that prints a short Chinese success message after execution.
    Avoid returning raw booleans like True or False when a clearer status message can be printed.
    """
    logger.info("ask_open_interpreter start: language=%s chars=%s", language, len(code))
    result = _run_open_interpreter(
        code,
        language=language,
        progress_callback=lambda chunk: emit_runtime_event_sync(
            "tool_progress",
            {
                "tool": "ask_open_interpreter",
                "title": "本地解释器",
                "chunk": summarize_console_chunk(chunk),
            },
        ),
    )
    if result and "failed" not in result.lower():
        logger.info("ask_open_interpreter success: output_chars=%s", len(result))
    else:
        logger.info("ask_open_interpreter completed with result: %s", result[:240])
    return result


def request_human_confirmation(question: str, context: str = "") -> str:
    """Pause execution and ask the user for confirmation before continuing."""
    prompt = (question or "").strip()
    if not prompt:
        return "确认问题不能为空。"

    runtime = get_execution_context()
    request_data = confirmation_store.create(
        thread_id=runtime.get("thread_id", ""),
        user_id=runtime.get("user_id"),
        username=runtime.get("username", ""),
        question=prompt,
        context=(context or "").strip(),
        local_execution=bool(runtime.get("local_execution")),
    )
    emit_runtime_event_sync("awaiting_confirmation", {"confirmation": request_data})
    return encode_meta_payload({"kind": "confirmation_request", "confirmation": request_data})


def start_open_interpreter_job(code: str, language: str = "python", title: str = "本地长任务") -> str:
    """Start a long-running local Open Interpreter job and return a job id immediately."""
    runtime = get_execution_context()
    job = local_job_store.create(
        title=title,
        thread_id=runtime.get("thread_id", ""),
        user_id=runtime.get("user_id"),
        username=runtime.get("username", ""),
    )
    job_id = job["id"]

    def progress_callback(chunk: str) -> None:
        summary = summarize_console_chunk(chunk)
        if summary:
            local_job_store.append_progress(job_id, summary)

    local_job_store.run_in_background(
        job_id,
        _run_open_interpreter,
        code,
        language=language,
        progress_callback=progress_callback,
        cancel_check=lambda: local_job_store.is_cancel_requested(job_id),
    )
    emit_runtime_event_sync("job_created", {"job": local_job_store.get(job_id)})
    return encode_meta_payload({"kind": "job_created", "job": local_job_store.get(job_id)})


def send_email(to: str, subject: str, body: str) -> str:
    """Send a plain-text email through the configured SMTP server."""
    if not smtp_is_configured():
        logger.warning("Email requested but SMTP is not configured.")
        return "Email is not configured. Set SMTP_USER and SMTP_PASSWORD."

    try:
        message = MIMEMultipart()
        message["From"] = DEFAULT_FROM
        message["To"] = to
        message["Subject"] = str(Header(subject, "utf-8"))
        message.attach(MIMEText(body, "plain", "utf-8"))

        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.send_message(message)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.starttls()
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.send_message(message)
        logger.info("Email sent to %s", to)
        return f"Email sent to {to}"
    except Exception as exc:
        logger.warning("Email send failed for %s: %s", to, exc)
        return f"Email send failed: {exc}"


def open_local_browser_page(url: str) -> str:
    """Open a webpage through the local Browser Bridge."""
    target_url = (url or "").strip()
    if not target_url:
        return "Local webpage URL cannot be empty."
    if not re.match(r"^https?://", target_url, re.IGNORECASE):
        target_url = "https://" + target_url

    try:
        payload = _browser_bridge_post("/page/open", {"url": target_url}, timeout=20)
    except Exception as exc:
        logger.warning("open_local_browser_page failed: %s", exc)
        return f"Open local webpage failed: {exc}"

    current_url = str(payload.get("url") or target_url).strip()
    title = str(payload.get("title") or "").strip()
    if title:
        return f"Opened local webpage: {current_url}\nPage title: {title}"
    return f"Opened local webpage: {current_url}"


def inspect_local_webpage(url: str, instruction: str = "") -> str:
    """Open a webpage through Browser Bridge and return a text snapshot."""
    target_url = (url or "").strip()
    if not target_url:
        return "Local webpage URL cannot be empty."
    if not re.match(r"^https?://", target_url, re.IGNORECASE):
        target_url = "https://" + target_url

    try:
        payload = _browser_bridge_post(
            "/page/snapshot",
            {
                "url": target_url,
                "instruction": (instruction or "").strip(),
                "wait_ms": 3000,
            },
            timeout=max(30, get_browser_bridge_timeout()),
        )
    except Exception as exc:
        logger.warning("inspect_local_webpage failed: %s", exc)
        return f"Local webpage snapshot failed: {exc}"

    title = str(payload.get("title") or "").strip()
    current_url = str(payload.get("url") or target_url).strip()
    observed = str(payload.get("instruction") or "").strip()
    text = str(payload.get("text") or "").strip() or "[No body text extracted]"
    parts = [f"Page title: {title or '-'}", f"Page URL: {current_url}"]
    if observed:
        parts.append(f"Observation target: {observed}")
    parts.append("Page snapshot:")
    parts.append(text)
    return "\n".join(parts)


def start_local_webpage_monitor(
    url: str,
    keyword: str,
    title: str = "Local webpage watch",
    rounds: int = 20,
    interval_sec: int = 8,
) -> str:
    """Start a Browser Bridge watch job and mirror it into the server-side local job store."""
    target_url = (url or "").strip()
    watch_keyword = (keyword or "").strip()
    if not target_url:
        return "Local webpage URL cannot be empty."
    if not watch_keyword:
        return "Keyword cannot be empty."
    if not re.match(r"^https?://", target_url, re.IGNORECASE):
        target_url = "https://" + target_url

    rounds = max(1, min(int(rounds or 20), 200))
    interval_sec = max(2, min(int(interval_sec or 8), 300))

    runtime = get_execution_context()
    job = local_job_store.create(
        title=title,
        thread_id=runtime.get("thread_id", ""),
        user_id=runtime.get("user_id"),
        username=runtime.get("username", ""),
    )
    local_job_id = job["id"]
    local_job_store.append_progress(local_job_id, f"Created local webpage watch job for keyword: {watch_keyword}")

    try:
        payload = _browser_bridge_post(
            "/jobs/start",
            {
                "job_type": "watch_text",
                "url": target_url,
                "keyword": watch_keyword,
                "rounds": rounds,
                "interval_sec": interval_sec,
                "title": title,
            },
            timeout=20,
        )
    except Exception as exc:
        logger.warning("start_local_webpage_monitor failed: %s", exc)
        local_job_store.fail(local_job_id, str(exc))
        return f"Start local webpage watch failed: {exc}"

    remote_job_id = str(payload.get("job_id") or "").strip()
    if not remote_job_id:
        local_job_store.fail(local_job_id, "Browser Bridge did not return a job id.")
        return "Start local webpage watch failed: Browser Bridge did not return a job id."

    local_job_store.append_progress(local_job_id, f"Browser Bridge job started: {remote_job_id}")
    local_job_store.run_in_background(local_job_id, _mirror_browser_bridge_job, local_job_id, remote_job_id)
    emit_runtime_event_sync("job_created", {"job": local_job_store.get(local_job_id)})
    return encode_meta_payload({"kind": "job_created", "job": local_job_store.get(local_job_id)})
