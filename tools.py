import requests
from bs4 import BeautifulSoup
import re
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

SEARXNG_URL = "http://localhost:8888"

def web_search(query: str) -> str:
    """搜索互联网，返回格式化结果"""
    try:
        resp = requests.get(
            f"{SEARXNG_URL}/search",
            params={"q": query, "format": "json"},
            timeout=10
        )
        data = resp.json()
    except Exception as e:
        return f"搜索失败: {str(e)}"

    results = []
    for r in data.get("results", [])[:5]:
        title = r.get("title", "无标题")
        url = r.get("url", "")
        content = r.get("content", "")
        results.append(f"标题: {title}\n链接: {url}\n摘要: {content}")
    return "\n\n".join(results) if results else "未找到相关结果。"

def fetch_webpage(url: str) -> str:
    """抓取网页文本内容（最多3000字符）"""
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        if resp.encoding is None:
            resp.encoding = 'utf-8'
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        text = re.sub(r"\n\s*\n", "\n\n", text)
        if len(text) > 3000:
            text = text[:3000] + "...\n[内容已截断]"
        return text
    except Exception as e:
        return f"抓取失败: {str(e)}"

# ---------- 邮件配置 ----------
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.qq.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
DEFAULT_FROM = os.getenv("SMTP_FROM", SMTP_USER)

def send_email(to: str, subject: str, body: str) -> str:
    """发送邮件。参数：to（收件人邮箱）、subject（主题）、body（正文）。"""
    if not SMTP_USER or not SMTP_PASSWORD:
        return "邮件服务未配置：请设置 SMTP_USER 和 SMTP_PASSWORD 环境变量。"

    try:
        msg = MIMEMultipart()
        msg["From"] = DEFAULT_FROM
        msg["To"] = to
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))

        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.starttls()
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.send_message(msg)
        return f"邮件已发送至 {to}"
    except Exception as e:
        return f"邮件发送失败: {str(e)}"
