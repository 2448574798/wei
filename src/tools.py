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
