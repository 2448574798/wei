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

from src.browser_orchestrator import (
    browser_bridge_is_configured,
    browser_orchestrator,
    browser_worker_is_configured,
    get_browser_bridge_timeout,
)
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

VERIFICATION_PAGE_MARKERS = (
    "验证码中间页",
    "请完成下列验证后继续",
    "拖动完成上方拼图",
    "按住左边按钮拖动完成上方拼图",
)


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


def smtp_is_configured() -> bool:
    return bool(SMTP_USER and SMTP_PASSWORD and DEFAULT_FROM)


def is_fetch_error(text: str) -> bool:
    return text.startswith("Fetch failed:")


def get_domain(url: str) -> str:
    return urlparse(url).netloc.lower()


def _manual_verification_message(title: str, text: str, url: str = "") -> str:
    combined = "\n".join(part for part in (title, text, url) if part).strip()
    if not combined:
        return ""
    for marker in VERIFICATION_PAGE_MARKERS:
        if marker in combined:
            return "Manual verification required: the current page is blocked by a captcha/verification challenge. Complete it in the local browser, then retry."
    return ""


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


def _browser_worker_request(command: str, payload: dict, timeout: int | None = None) -> dict:
    return browser_orchestrator.request(command, payload, timeout=timeout)


def _run_worker_watch_text_job(
    local_job_id: str,
    *,
    url: str,
    keyword: str,
    rounds: int,
    interval_sec: int,
    wait_ms: int,
) -> str:
    lowered_keyword = keyword.lower()
    for index in range(rounds):
        if local_job_store.is_cancel_requested(local_job_id):
            return "Local webpage watch cancelled."

        snapshot = _browser_worker_request(
            "browser.snapshot",
            {
                "url": url,
                "instruction": keyword,
                "wait_ms": wait_ms,
            },
            timeout=max(30, get_browser_bridge_timeout()),
        )
        title = str(snapshot.get("title") or "").strip()
        current_url = str(snapshot.get("url") or url).strip()
        text = str(snapshot.get("text") or "").strip()
        excerpt = text[:240] or "[No body text extracted]"
        local_job_store.append_progress(local_job_id, f"Round {index + 1}: {excerpt}")

        manual_notice = _manual_verification_message(title, text, current_url)
        if manual_notice:
            raise RuntimeError(f"{manual_notice} Page URL: {current_url}")

        if lowered_keyword in text.lower():
            local_job_store.append_progress(local_job_id, f"Matched keyword: {keyword}")
            return f"Matched keyword: {keyword}\n{text[:2000]}"

        if index < rounds - 1:
            time.sleep(interval_sec)

    local_job_store.append_progress(local_job_id, f"Keyword not found: {keyword}")
    return f"Keyword not found: {keyword}"


def _run_worker_interaction_watch_job(
    local_job_id: str,
    *,
    url: str,
    keyword: str,
    rounds: int,
    interval_sec: int,
    wait_ms: int,
    steps: list[dict],
    advance_steps: list[dict],
    match_extract_names: set[str],
) -> str:
    lowered_keyword = keyword.lower()
    current_url = url

    for index in range(rounds):
        if local_job_store.is_cancel_requested(local_job_id):
            return "Local comment hunt cancelled."

        result = _browser_worker_request(
            "browser.interact",
            {
                "url": current_url,
                "instruction": keyword,
                "steps": steps,
                "wait_ms": wait_ms,
            },
            timeout=max(60, get_browser_bridge_timeout(), len(steps) * 15),
        )
        current_url = ""
        title = str(result.get("title") or "").strip()
        page_url = str(result.get("url") or "").strip()
        snapshot_text = str(result.get("text") or "").strip()
        extracts = result.get("extracts") if isinstance(result.get("extracts"), list) else []

        manual_notice = _manual_verification_message(title, snapshot_text, page_url)
        if manual_notice:
            raise RuntimeError(f"{manual_notice} Page URL: {page_url or '-'}")

        extract_parts: list[str] = []
        for item in extracts:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            if not match_extract_names or name in match_extract_names:
                extract_parts.append(text)
        searched_text = "\n".join(extract_parts or [snapshot_text]).strip()
        preview = searched_text[:240] or snapshot_text[:240] or "[No text extracted]"
        local_job_store.append_progress(local_job_id, f"Round {index + 1} [browser_worker]: {preview}")

        if lowered_keyword in searched_text.lower():
            local_job_store.append_progress(local_job_id, f"Matched keyword: {keyword}")
            return json.dumps(
                {
                    "matched_keyword": keyword,
                    "url": page_url,
                    "title": title,
                    "backend": "browser_worker",
                    "extracts": extracts,
                    "text": snapshot_text[:3000],
                },
                ensure_ascii=False,
            )

        if index < rounds - 1:
            if advance_steps:
                try:
                    advance_result = _browser_worker_request(
                        "browser.interact",
                        {
                            "url": "",
                            "instruction": "advance",
                            "steps": advance_steps,
                            "wait_ms": 500,
                        },
                        timeout=max(60, get_browser_bridge_timeout(), len(advance_steps) * 15),
                    )
                    advance_text = str(advance_result.get("text") or "").strip()
                    if advance_text:
                        local_job_store.append_progress(local_job_id, f"Advance [browser_worker]: {advance_text[:160]}")
                except Exception as exc:
                    local_job_store.append_progress(local_job_id, f"Advance failed: {exc}")
            time.sleep(interval_sec)

    local_job_store.append_progress(local_job_id, f"Keyword not found: {keyword}")
    return f"Keyword not found: {keyword}"


def _mirror_browser_bridge_job(local_job_id: str, remote_job_id: str) -> str:
    seen_progress = 0
    cancel_sent = False

    while True:
        if local_job_store.is_cancel_requested(local_job_id) and not cancel_sent:
            cancel_sent = True
            browser_orchestrator.cancel_legacy_job(remote_job_id)
            local_job_store.append_progress(local_job_id, "Sent a cancellation request to Browser Bridge.")

        payload = browser_orchestrator.get_legacy_job(remote_job_id, timeout=15)
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
    """Open a webpage through the cloud browser orchestrator."""
    target_url = (url or "").strip()
    if not target_url:
        return "Local webpage URL cannot be empty."
    if not re.match(r"^https?://", target_url, re.IGNORECASE):
        target_url = "https://" + target_url

    try:
        payload = browser_orchestrator.request("browser.navigate", {"url": target_url}, timeout=30)
    except Exception as exc:
        logger.warning("open_local_browser_page failed: %s", exc)
        return f"Open local webpage failed: {exc}"

    current_url = str(payload.get("url") or target_url).strip()
    title = str(payload.get("title") or "").strip()
    if not title:
        tabs = payload.get("tabs") if isinstance(payload.get("tabs"), dict) else {}
        structured = tabs.get("structured_content") if isinstance(tabs.get("structured_content"), dict) else {}
        current_index = structured.get("currentTab") if isinstance(structured.get("currentTab"), int) else None
        for item in structured.get("tabs") or []:
            if isinstance(item, dict) and item.get("index") == current_index:
                title = str(item.get("title") or "").strip()
                if not current_url or current_url == target_url:
                    current_url = str(item.get("url") or current_url or target_url).strip()
                break
    if not title:
        tabs = payload.get("tabs") if isinstance(payload.get("tabs"), dict) else {}
        tab_text = str(tabs.get("text") or "").strip()
        if tab_text:
            title = tab_text.splitlines()[0][:160]
    manual_notice = _manual_verification_message(title, str(payload.get("text") or "").strip(), current_url)
    if manual_notice:
        return f"{manual_notice}\nPage URL: {current_url}\nPage title: {title or '-'}"
    if title:
        return f"Opened local webpage: {current_url}\nPage title: {title}"
    return f"Opened local webpage: {current_url}"


def list_local_browser_tabs() -> str:
    """List local browser tabs through the cloud browser orchestrator."""
    try:
        payload = browser_orchestrator.request("browser.tabs", {}, timeout=20)
    except Exception as exc:
        logger.warning("list_local_browser_tabs failed: %s", exc)
        return f"List local browser tabs failed: {exc}"

    structured = payload.get("structured_content") if isinstance(payload.get("structured_content"), dict) else {}
    tabs = structured.get("tabs") if isinstance(structured.get("tabs"), list) else []
    current = structured.get("currentTab") if isinstance(structured.get("currentTab"), int) else None
    if tabs:
        lines = ["Local browser tabs:"]
        for index, item in enumerate(tabs[:20]):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "-").strip() or "-"
            tab_url = str(item.get("url") or "-").strip() or "-"
            prefix = "*" if current is not None and index == current else "-"
            lines.append(f"{prefix} [{index}] {title} | {tab_url}")
        return "\n".join(lines)

    text = str(payload.get("text") or "").strip()
    if text:
        return f"Local browser tabs:\n{text}"
    return "Local browser tabs: [No tabs returned]"


def inspect_local_webpage(url: str, instruction: str = "") -> str:
    """Open a webpage through the cloud browser orchestrator and return a text snapshot."""
    target_url = (url or "").strip()
    if not target_url:
        return "Local webpage URL cannot be empty."
    if not re.match(r"^https?://", target_url, re.IGNORECASE):
        target_url = "https://" + target_url

    try:
        request_payload = {
            "url": target_url,
            "instruction": (instruction or "").strip(),
            "wait_ms": 3000,
        }
        payload = browser_orchestrator.request("browser.snapshot", request_payload, timeout=max(30, get_browser_bridge_timeout()))
    except Exception as exc:
        logger.warning("inspect_local_webpage failed: %s", exc)
        return f"Local webpage snapshot failed: {exc}"

    title = str(payload.get("title") or "").strip()
    current_url = str(payload.get("url") or target_url).strip()
    observed = str(payload.get("instruction") or "").strip()
    text = str(payload.get("text") or "").strip() or "[No body text extracted]"
    parts = [f"Page title: {title or '-'}", f"Page URL: {current_url}"]
    manual_notice = _manual_verification_message(title, text, current_url)
    if manual_notice:
        parts.insert(0, manual_notice)
    if observed:
        parts.append(f"Observation target: {observed}")
    parts.append("Page snapshot:")
    parts.append(text)
    return "\n".join(parts)


def _step_has_supported_target(step: dict) -> bool:
    return any(str(step.get(key) or "").strip() for key in ("selector", "text", "role", "label", "placeholder"))


def _step_has_selector_target(step: dict) -> bool:
    if str(step.get("selector") or "").strip():
        return True
    targets = step.get("targets") if isinstance(step.get("targets"), list) else []
    return any(isinstance(item, dict) and str(item.get("selector") or "").strip() for item in targets)


def _steps_prefer_mcp_readonly(steps: list[dict]) -> bool:
    readonly_step_types = {"wait", "goto", "click", "click_any", "press", "extract_text", "extract_any_text", "extract_list_text", "snapshot"}
    for step in steps:
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


def interact_local_webpage(url: str = "", steps_json: str = "", instruction: str = "") -> str:
    """Interact with a local webpage through the cloud browser orchestrator using a JSON array of steps.

    Supported step types include click, click_any, wait, fill, press, extract_text,
    extract_any_text, snapshot, and goto. A step can target selector, text, role,
    label, or placeholder. Example steps_json:
    [
      {"type":"click_any","targets":[{"text":"评论"},{"selector":"button:has-text('评论')"}],"wait_ms":2000},
      {"type":"extract_any_text","name":"first_comment","targets":[{"selector":"[class*=comment-item]"}],"limit":300},
      {"type":"snapshot","selector":"body","limit":1200}
    ]
    """
    target_url = (url or "").strip()
    if target_url and not re.match(r"^https?://", target_url, re.IGNORECASE):
        target_url = "https://" + target_url

    raw_steps = (steps_json or "").strip()
    if not raw_steps:
        return "steps_json cannot be empty. Pass a JSON array of interaction steps."

    try:
        steps = json.loads(raw_steps)
    except Exception as exc:
        return f"Invalid steps_json: {exc}"

    if not isinstance(steps, list) or not steps:
        return "steps_json must be a non-empty JSON array."

    if not _steps_prefer_mcp_readonly(steps):
        return (
            "Local webpage interaction failed: MCP-only Browser Bridge currently supports readonly steps only: "
            "wait, goto, click, click_any, press, extract_text, extract_any_text, extract_list_text, and snapshot."
        )

    try:
        request_payload = {
            "url": target_url,
            "instruction": (instruction or "").strip(),
            "steps": steps,
            "wait_ms": 1000,
        }
        payload = browser_orchestrator.request("browser.interact", request_payload, timeout=max(60, get_browser_bridge_timeout(), len(steps) * 15))
    except Exception as exc:
        logger.warning("interact_local_webpage failed: %s", exc)
        return f"Local webpage interaction failed: {exc}"

    title = str(payload.get("title") or "").strip()
    current_url = str(payload.get("url") or target_url or "").strip()
    observed = str(payload.get("instruction") or "").strip()
    text = str(payload.get("text") or "").strip() or "[No body text extracted]"
    extracts = payload.get("extracts") or []
    step_results = payload.get("step_results") or []
    backend = str(payload.get("backend") or "").strip()

    parts = [f"Page title: {title or '-'}", f"Page URL: {current_url or '-'}"]
    manual_notice = _manual_verification_message(title, text, current_url)
    if manual_notice:
        parts.insert(0, manual_notice)
    if observed:
        parts.append(f"Observation target: {observed}")
    if backend:
        parts.append(f"Execution backend: {backend}")
    if step_results:
        parts.append("Interaction steps:")
        for item in step_results[:12]:
            step_type = str(item.get("type") or "step").strip()
            status = str(item.get("status") or "ok").strip()
            name = str(item.get("name") or "").strip()
            detail = f"{step_type} [{status}]"
            if name:
                detail += f" {name}"
            parts.append(detail)
    if extracts:
        parts.append("Extracted text:")
        for item in extracts[:8]:
            name = str(item.get("name") or "extract").strip()
            extracted = str(item.get("text") or "").strip() or "[Empty]"
            parts.append(f"{name}: {extracted}")
    parts.append("Page snapshot:")
    parts.append(text)
    return "\n".join(parts)


def start_local_comment_hunt(
    keyword: str,
    url: str = "https://www.douyin.com/jingxuan",
    title: str = "Douyin comment hunt",
    rounds: int = 30,
    interval_sec: int = 5,
    steps_json: str = "",
    advance_steps_json: str = "",
) -> str:
    """Start a read-only local comment hunt job on a Douyin page.

    The default workflow is tuned for Douyin 精选:
    it resets out of any prior modal, opens the first visible recommendation card,
    opens the right-side comment panel, extracts visible comments, and then
    advances the feed with keyboard navigation. It does not reply to comments or
    send messages.
    """
    watch_keyword = (keyword or "").strip()
    target_url = (url or "").strip()
    if not watch_keyword:
        return "Keyword cannot be empty."
    if target_url and not re.match(r"^https?://", target_url, re.IGNORECASE):
        target_url = "https://" + target_url

    default_steps = [
        {
            "type": "press",
            "key": "Escape",
            "wait_ms": 300,
        },
        {
            "type": "click",
            "selector": ".waterfall-videoCardContainer.jingxuanVideoCard",
            "timeout_ms": 8000,
            "wait_ms": 2200,
        },
        {
            "type": "click",
            "selector": "#douyin-web-recommend-guide-mask button.semi-button",
            "timeout_ms": 2500,
            "wait_ms": 600,
            "optional": True,
        },
        {
            "type": "click",
            "selector": "#dy-modal-video-container-waterFall [data-e2e='feed-comment-icon']",
            "timeout_ms": 8000,
            "wait_ms": 1800,
        },
        {
            "type": "extract_any_text",
            "name": "comment_header",
            "targets": [
                {"selector": ".comment-header-inner-container"},
                {"selector": "[data-e2e='comment-list']"},
            ],
            "limit": 300,
        },
        {
            "type": "extract_list_text",
            "name": "visible_comments",
            "targets": [
                {"selector": "#dy-modal-video-container-waterFall [data-e2e='comment-item']"},
            ],
            "item_limit": 12,
            "limit": 280,
        },
        {
            "type": "snapshot",
            "selector": "#dy-modal-video-container-waterFall",
            "limit": 1800,
        },
    ]
    default_advance_steps = [
        {"type": "press", "key": "Escape", "wait_ms": 400},
        {"type": "press", "key": "Escape", "wait_ms": 400},
        {"type": "press", "key": "PageDown", "wait_ms": 1800},
        {"type": "snapshot", "selector": "body", "limit": 400},
    ]

    steps = default_steps
    if (steps_json or "").strip():
        try:
            parsed = json.loads(steps_json)
        except Exception as exc:
            return f"Invalid steps_json: {exc}"
        if not isinstance(parsed, list) or not parsed:
            return "steps_json must be a non-empty JSON array."
        steps = parsed

    advance_steps = default_advance_steps
    if (advance_steps_json or "").strip():
        try:
            parsed = json.loads(advance_steps_json)
        except Exception as exc:
            return f"Invalid advance_steps_json: {exc}"
        if not isinstance(parsed, list):
            return "advance_steps_json must be a JSON array."
        advance_steps = parsed

    rounds = max(1, min(int(rounds or 30), 300))
    interval_sec = max(1, min(int(interval_sec or 5), 120))

    runtime = get_execution_context()
    try:
        job = local_job_store.create(
            title=title,
            thread_id=runtime.get("thread_id", ""),
            user_id=runtime.get("user_id"),
            username=runtime.get("username", ""),
        )
    except Exception as exc:
        logger.warning("start_local_comment_hunt job creation failed: %s", exc)
        return f"Start local comment hunt failed: {exc}"
    local_job_id = job["id"]
    local_job_store.append_progress(local_job_id, f"Created local comment hunt job for keyword: {watch_keyword}")

    if browser_worker_is_configured():
        local_job_store.run_in_background(
            local_job_id,
            _run_worker_interaction_watch_job,
            local_job_id,
            url=target_url,
            keyword=watch_keyword,
            rounds=rounds,
            interval_sec=interval_sec,
            wait_ms=1200,
            steps=steps,
            advance_steps=advance_steps,
            match_extract_names={"visible_comments"},
        )
        local_job_store.append_progress(local_job_id, "Browser worker interaction watch started.")
        emit_runtime_event_sync("job_created", {"job": local_job_store.get(local_job_id)})
        return encode_meta_payload({"kind": "job_created", "job": local_job_store.get(local_job_id)})

    try:
        payload = browser_orchestrator.start_legacy_job(
            {
                "job_type": "interaction_watch",
                "url": target_url,
                "keyword": watch_keyword,
                "rounds": rounds,
                "interval_sec": interval_sec,
                "wait_ms": 1200,
                "steps": steps,
                "advance_steps": advance_steps,
                "match_extract_names": ["visible_comments"],
                "title": title,
            },
            timeout=20,
        )
    except Exception as exc:
        logger.warning("start_local_comment_hunt failed: %s", exc)
        local_job_store.fail(local_job_id, str(exc))
        return f"Start local comment hunt failed: {exc}"

    remote_job_id = str(payload.get("job_id") or "").strip()
    if not remote_job_id:
        local_job_store.fail(local_job_id, "Browser Bridge did not return a job id.")
        return "Start local comment hunt failed: Browser Bridge did not return a job id."

    local_job_store.append_progress(local_job_id, f"Browser Bridge job started: {remote_job_id}")
    local_job_store.run_in_background(local_job_id, _mirror_browser_bridge_job, local_job_id, remote_job_id)
    emit_runtime_event_sync("job_created", {"job": local_job_store.get(local_job_id)})
    return encode_meta_payload({"kind": "job_created", "job": local_job_store.get(local_job_id)})


def start_local_webpage_monitor(
    url: str,
    keyword: str,
    title: str = "Local webpage watch",
    rounds: int = 20,
    interval_sec: int = 8,
) -> str:
    """Start a local browser watch job and mirror it into the server-side local job store."""
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

    if browser_worker_is_configured():
        local_job_store.run_in_background(
            local_job_id,
            _run_worker_watch_text_job,
            local_job_id,
            url=target_url,
            keyword=watch_keyword,
            rounds=rounds,
            interval_sec=interval_sec,
            wait_ms=3000,
        )
        local_job_store.append_progress(local_job_id, "Browser worker text watch started.")
        emit_runtime_event_sync("job_created", {"job": local_job_store.get(local_job_id)})
        return encode_meta_payload({"kind": "job_created", "job": local_job_store.get(local_job_id)})

    try:
        payload = browser_orchestrator.start_legacy_job(
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
