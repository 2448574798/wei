from datetime import datetime

import requests

from src.chat_helpers import clean_online_research_output, trim_text
from src.runtime_config import (
    ONE_API_TOKEN,
    ONE_API_URL,
    ONLINE_RESEARCH_MAX_TOKENS,
    ONLINE_RESEARCH_MODEL,
    SYSTEM_PROMPT_TEXT,
    logger,
)


def call_online_research_model(user_text: str, search_query: str, model_name: str | None = None) -> str:
    if not ONE_API_TOKEN:
        raise RuntimeError("ONE_API_TOKEN is not configured.")

    selected_model = (model_name or ONLINE_RESEARCH_MODEL).strip() or ONLINE_RESEARCH_MODEL
    endpoint = f"{ONE_API_URL}/chat/completions"
    today = datetime.now().strftime("%Y-%m-%d")
    system_prompt = (
        f"{SYSTEM_PROMPT_TEXT}\n\n"
        f"今天日期是 {today}。\n"
        "你是一名支持内置联网搜索的研究助手。\n"
        "当问题涉及当前、最新、会变化的信息时，请先联网核实，再给出结论。\n"
        "请输出简洁中文答案；如果证据不足，要明确说明。\n"
        "不要输出 Markdown 链接、括号引用、来源网址或原始搜索引用格式。\n"
        "优先直接给结论和必要要点，控制篇幅，避免长篇展开。"
    )
    user_prompt = (
        f"用户请求：{user_text}\n\n"
        f"建议搜索焦点：{search_query or user_text}\n\n"
        "请先联网搜索并核实，再给出最终回答。"
    )
    payload = {
        "model": selected_model,
        "temperature": 0.2,
        "max_tokens": ONLINE_RESEARCH_MAX_TOKENS,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    fallback_payload = {
        "model": selected_model,
        "max_tokens": ONLINE_RESEARCH_MAX_TOKENS,
        "messages": [{"role": "user", "content": user_text}],
    }
    headers = {
        "Authorization": f"Bearer {ONE_API_TOKEN}",
        "Content-Type": "application/json",
    }

    response = requests.post(endpoint, headers=headers, json=payload, timeout=90)
    if response.status_code >= 400:
        logger.warning(
            "Online research primary request failed: status=%s body=%s",
            response.status_code,
            response.text[:1000],
        )
        response = requests.post(endpoint, headers=headers, json=fallback_payload, timeout=90)

    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        body = response.text[:1000] if response is not None else ""
        raise RuntimeError(f"Online research request failed: HTTP {response.status_code}. {body}") from exc

    data = response.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("Online research model returned no choices.")

    message = choices[0].get("message", {})
    content = message.get("content", "")
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        content = "\n".join(part for part in parts if part)

    content = (content or "").strip()
    if not content:
        raise RuntimeError("Online research model returned empty content.")
    return trim_text(clean_online_research_output(content), 1200)
