import logging
import os
import re
import smtplib
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from websockets.sync.client import connect


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
    import json

    ws.send(json.dumps(payload))


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


def ask_open_interpreter(code: str, language: str = "python") -> str:
    """Execute already-prepared code with Open Interpreter.

    Pass runnable code directly, not natural-language instructions.
    For side-effect tasks such as opening apps, launching a browser, or writing files,
    prefer code that prints a short Chinese success message after execution.
    Avoid returning raw booleans like True or False when a clearer status message can be printed.
    """
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
            # Allow a few frames for auth / stale status frames.
            for _ in range(5):
                raw = ws.recv(timeout=3)
                import json

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

            for _ in range(80):
                raw = ws.recv(timeout=8)
                import json

                data = json.loads(raw)
                msg_type = data.get("type")
                msg_format = data.get("format")

                if msg_type == "console" and msg_format == "output":
                    text = str(data.get("content", ""))
                    if text:
                        console_chunks.append(text)
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
        return f"Open Interpreter execution failed: {server_errors[-1][:1200]}"
    return "Open Interpreter returned no output."


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
