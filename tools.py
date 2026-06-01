import os
import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from bs4 import BeautifulSoup


SEARXNG_URL = os.getenv("SEARXNG_URL", "http://127.0.0.1:8888")
FETCH_TEXT_LIMIT = int(os.getenv("FETCH_TEXT_LIMIT", "3000"))

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.qq.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
DEFAULT_FROM = os.getenv("SMTP_FROM", SMTP_USER)


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
    for item in data.get("results", [])[:5]:
        title = item.get("title", "Untitled")
        url = item.get("url", "")
        content = item.get("content", "")
        results.append(f"Title: {title}\nURL: {url}\nSnippet: {content}")

    return "\n\n".join(results) if results else "No relevant results found."


def fetch_webpage(url: str) -> str:
    """Fetch webpage text content and return a cleaned plain-text excerpt."""
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        if response.encoding is None:
            response.encoding = "utf-8"

        soup = BeautifulSoup(response.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()

        text = soup.get_text(separator="\n", strip=True)
        text = re.sub(r"\n\s*\n", "\n\n", text)
        if len(text) > FETCH_TEXT_LIMIT:
            text = text[:FETCH_TEXT_LIMIT] + "...\n[Content truncated]"
        return text
    except Exception as exc:
        return f"Fetch failed: {exc}"


def send_email(to: str, subject: str, body: str) -> str:
    """Send a plain-text email through the configured SMTP server."""
    if not SMTP_USER or not SMTP_PASSWORD:
        return "Email is not configured. Set SMTP_USER and SMTP_PASSWORD."

    try:
        message = MIMEMultipart()
        message["From"] = DEFAULT_FROM
        message["To"] = to
        message["Subject"] = subject
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
        return f"Email sent to {to}"
    except Exception as exc:
        return f"Email send failed: {exc}"
