import requests
from bs4 import BeautifulSoup
import re

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
        results.append(
            f"标题: {r['title']}\n链接: {r['url']}\n摘要: {r.get('content', '')}"
        )
    return "\n\n".join(results)

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
