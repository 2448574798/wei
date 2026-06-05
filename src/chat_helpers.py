import re

from fastapi import HTTPException
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage


def trim_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[Truncated]"


def summarize_text(text: str, limit: int = 80) -> str:
    compact = re.sub(r"\s+", " ", (text or "").strip())
    if len(compact) <= limit:
        return compact
    return compact[:limit].rstrip() + "..."


def detect_tool_status(tool_name: str, content: str) -> str:
    lowered = (content or "").lower()
    error_markers = [
        "failed",
        "error",
        "authentication failed",
        "not configured",
        "不能为空",
        "失败",
        "错误",
        "未配置",
    ]
    if any(marker in lowered for marker in error_markers):
        return "error"
    return "ok"


def default_tool_title(tool_name: str) -> str:
    mapping = {
        "online_research": "联网思考",
        "ask_open_interpreter": "本地解释器",
        "send_email": "发送邮件",
    }
    return mapping.get(tool_name, tool_name or "工具")


def make_trace_entry(
    tool: str,
    content: str,
    *,
    title: str | None = None,
    phase: str | None = None,
    status: str | None = None,
    summary: str | None = None,
    model: str | None = None,
) -> dict:
    body = str(content or "").strip()
    return {
        "tool": tool,
        "title": title or default_tool_title(tool),
        "phase": phase or "",
        "status": status or detect_tool_status(tool, body),
        "summary": summary or summarize_text(body),
        "model": model or "",
        "content": trim_text(body, 800),
    }


def build_tool_trace(messages: list) -> list[dict]:
    last_human_index = -1
    for index, message in enumerate(messages):
        if isinstance(message, HumanMessage):
            last_human_index = index

    trace = []
    scoped_messages = messages[last_human_index + 1 :] if last_human_index >= 0 else messages
    for message in scoped_messages:
        if isinstance(message, ToolMessage):
            tool_name = getattr(message, "name", "") or "tool"
            trace.append(
                make_trace_entry(
                    tool_name,
                    str(message.content),
                    phase="tool_execution",
                    summary=f"{default_tool_title(tool_name)}已执行",
                )
            )
    return trace


def append_tool_trace(existing_entries: list[dict] | None, new_entries: list[dict]) -> list[dict]:
    trace = list(existing_entries or [])
    trace.extend(new_entries)
    return trace


def format_trace_content(title: str, body: str) -> str:
    return trim_text(f"{title}\n\n{body}", 800)


def convert_to_langchain(messages: list[dict]) -> list:
    converted = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content", "")
        if role == "system":
            converted.append(SystemMessage(content=content))
        elif role == "user":
            converted.append(HumanMessage(content=content))
        elif role == "assistant":
            converted.append(AIMessage(content=content))
        elif role == "tool":
            converted.append(ToolMessage(content=content, tool_call_id=msg.get("tool_call_id", "")))
    return converted


def validate_chat_messages(messages: list[dict]) -> None:
    if not messages:
        raise HTTPException(status_code=400, detail="messages cannot be empty.")
    if len(messages) > 50:
        raise HTTPException(status_code=400, detail="Too many messages.")

    for msg in messages:
        if len(msg.get("content", "")) > 2000:
            raise HTTPException(status_code=400, detail="A message is too long.")


def get_latest_user_text(messages: list) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return message.content if isinstance(message.content, str) else str(message.content)
    return ""


def remove_urls(text: str) -> str:
    return re.sub(r"https?://\S+", "", text)


def remove_markdown_links(text: str) -> str:
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1", text)
    text = re.sub(r"\((https?://[^)]+)\)", "", text)
    text = re.sub(r"\(([A-Za-z0-9.-]+\.[A-Za-z]{2,})\)", "", text)
    return text


def clean_online_research_output(text: str) -> str:
    cleaned = remove_markdown_links(text or "")
    cleaned = remove_urls(cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    return cleaned.strip()


def public_planner_decision(decision: dict | None) -> dict:
    data = dict(decision or {})
    data.pop("search_query", None)
    return data
