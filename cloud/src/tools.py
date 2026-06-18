import logging
import os
import re
import smtplib
from contextlib import suppress
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import json

from src.browser_orchestrator import (
    browser_orchestrator,
    browser_worker_is_configured,
    get_browser_request_timeout,
)
from src.browser_visual_runner import format_browser_diagnostics, run_local_browser_visual_operation
from src.execution_context import emit_runtime_event_sync, get_execution_context
from src.human_loop import confirmation_store
from src.job_store import cloud_job_store
from src.open_interpreter_client import (
    open_interpreter_is_configured,
    run_open_interpreter,
    summarize_console_chunk,
)
from src.research_client import call_online_research_model
from src.runtime_config import (
    BROWSER_VISION_MODEL,
    ONLINE_RESEARCH_MODEL,
)


logger = logging.getLogger("wei_agent")

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.qq.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
DEFAULT_FROM = os.getenv("SMTP_FROM", SMTP_USER)
META_PREFIX = "__WEI_META__:"

VERIFICATION_PAGE_MARKERS = (
    "\u9a8c\u8bc1\u7801\u4e2d\u95f4\u9875",
    "\u8bf7\u5b8c\u6210\u4e0b\u5217\u9a8c\u8bc1\u540e\u7ee7\u7eed",
    "\u62d6\u52a8\u5b8c\u6210\u4e0a\u65b9\u62fc\u56fe",
    "\u6309\u4f4f\u5de6\u8fb9\u6309\u94ae\u62d6\u52a8\u5b8c\u6210\u4e0a\u65b9\u62fc\u56fe",
)


def smtp_is_configured() -> bool:
    return bool(SMTP_USER and SMTP_PASSWORD and DEFAULT_FROM)


def _manual_verification_message(title: str, text: str, url: str = "") -> str:
    combined = "\n".join(part for part in (title, text, url) if part).strip()
    if not combined:
        return ""
    for marker in VERIFICATION_PAGE_MARKERS:
        if marker in combined:
            return "Manual verification required: the current page is blocked by a captcha/verification challenge. Complete it in the local browser, then retry."
    return ""


def encode_meta_payload(payload: dict) -> str:
    return META_PREFIX + json.dumps(payload, ensure_ascii=False)


def decode_meta_payload(text: str) -> dict | None:
    content = (text or "").strip()
    if not content.startswith(META_PREFIX):
        return None
    with suppress(Exception):
        return json.loads(content[len(META_PREFIX) :])
    return None


def _browser_worker_request(command: str, payload: dict, timeout: int | None = None) -> dict:
    return browser_orchestrator.request(command, payload, timeout=timeout)


def online_research(question: str) -> str:
    """Research current or changing information on the web and return a concise Chinese answer."""
    text = (question or "").strip()
    if not text:
        return "Online research question cannot be empty."
    try:
        logger.info("online_research tool start: %s", text[:200])
        return call_online_research_model(text, text, ONLINE_RESEARCH_MODEL)
    except Exception as exc:
        logger.warning("online_research tool failed: %s", exc)
        return f"Online research failed: {exc}"


def ask_open_interpreter(code: str, language: str = "python") -> str:
    """Execute already-prepared code with Open Interpreter.

    Pass runnable code directly, not natural-language instructions.
    For side-effect tasks such as opening apps, launching a browser, or writing files,
    prefer code that prints a short success message after execution.
    Avoid returning raw booleans like True or False when a clearer status message can be printed.
    """
    logger.info("ask_open_interpreter start: language=%s chars=%s", language, len(code))
    result = run_open_interpreter(
        code,
        language=language,
        progress_callback=lambda chunk: emit_runtime_event_sync(
            "tool_progress",
            {
                "tool": "ask_open_interpreter",
                "title": "Local Interpreter",
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
        return "Confirmation question cannot be empty."

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


def start_open_interpreter_job(code: str, language: str = "python", title: str = "Local long task") -> str:
    """Start a long-running local Open Interpreter job and return a job id immediately."""
    runtime = get_execution_context()
    job = cloud_job_store.create(
        title=title,
        thread_id=runtime.get("thread_id", ""),
        user_id=runtime.get("user_id"),
        username=runtime.get("username", ""),
    )
    job_id = job["id"]

    def progress_callback(chunk: str) -> None:
        summary = summarize_console_chunk(chunk)
        if summary:
            cloud_job_store.append_progress(job_id, summary)

    cloud_job_store.run_in_background(
        job_id,
        run_open_interpreter,
        code,
        language=language,
        progress_callback=progress_callback,
        cancel_check=lambda: cloud_job_store.is_cancel_requested(job_id),
    )
    emit_runtime_event_sync("job_created", {"job": cloud_job_store.get(job_id)})
    return encode_meta_payload({"kind": "job_created", "job": cloud_job_store.get(job_id)})


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
        payload = browser_orchestrator.request("browser.snapshot", request_payload, timeout=max(30, get_browser_request_timeout()))
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
    diagnostic_lines = format_browser_diagnostics(payload)
    if diagnostic_lines:
        parts.extend(diagnostic_lines)
    parts.append("Page snapshot:")
    parts.append(text)
    return "\n".join(parts)


def operate_local_browser_visual(url: str = "", instruction: str = "", rounds: int = 3) -> str:
    """Operate the local browser with screenshot-based visual recognition.

    Cloud orchestrator owns the loop:
    screenshot -> vision decision -> preflight -> single worker action -> verification.
    The local thin worker only executes the one-step action sent over websocket.
    """
    return run_local_browser_visual_operation(url=url, instruction=instruction, rounds=rounds)


def start_cloud_browser_visual_job(
    instruction: str,
    url: str = "",
    title: str = "Cloud browser visual task",
    rounds: int = 6,
) -> str:
    """Start a cloud-owned Redis job for screenshot-based local browser operation."""
    task = (instruction or "").strip()
    if not task:
        return "Visual browser instruction cannot be empty."
    target_url = (url or "").strip()
    if target_url and not re.match(r"^https?://", target_url, re.IGNORECASE):
        target_url = "https://" + target_url
    max_rounds = max(1, min(int(rounds or 6), 20))

    runtime = get_execution_context()
    try:
        job = cloud_job_store.create(
            title=title,
            thread_id=runtime.get("thread_id", ""),
            user_id=runtime.get("user_id"),
            username=runtime.get("username", ""),
        )
    except Exception as exc:
        logger.warning("start_cloud_browser_visual_job creation failed: %s", exc)
        return f"Start cloud browser visual job failed: {exc}"

    job_id = job["id"]
    cloud_job_store.append_progress(job_id, f"Created cloud visual browser job: rounds={max_rounds}; model={BROWSER_VISION_MODEL}")
    if not browser_worker_is_configured():
        cloud_job_store.fail(job_id, "Browser Worker websocket mode is disabled.")
        return "Start cloud browser visual job failed: Browser Worker websocket mode is disabled."

    cloud_job_store.run_in_background(
        job_id,
        run_local_browser_visual_operation,
        url=target_url,
        instruction=task,
        rounds=max_rounds,
        max_round_cap=20,
        task_id=job_id,
        progress_callback=lambda message: cloud_job_store.append_progress(job_id, message),
        cancel_check=lambda: cloud_job_store.is_cancel_requested(job_id),
        artifact_callback=lambda artifact: cloud_job_store.append_artifact(job_id, artifact),
    )
    cloud_job_store.append_progress(job_id, "Cloud orchestrator started visual browser loop.")
    emit_runtime_event_sync("job_created", {"job": cloud_job_store.get(job_id)})
    return encode_meta_payload({"kind": "job_created", "job": cloud_job_store.get(job_id)})
