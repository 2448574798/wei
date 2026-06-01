import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Literal

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.redis.aio import AsyncRedisSaver
from langgraph.graph import MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from src.tools import send_email, smtp_is_configured


SRC_DIR = Path(__file__).resolve().parent
BASE_DIR = SRC_DIR.parent
DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."

TIME_SENSITIVE_PATTERN = re.compile(
    r"(今天|今日|昨天|昨日|明天|后天|现在|当前|目前|最新|最近|刚刚|实时|近况|行情|价格|汇率|股价|新闻|天气|"
    r"版本|更新|发布|文档|政策|法规|公告|比赛|赛程|票房|销量|"
    r"today|yesterday|tomorrow|now|current|latest|recent|price|weather|news|version|release)",
    re.IGNORECASE,
)
SEARCH_ACTION_PATTERN = re.compile(
    r"(搜索|查询|联网|网页|网站|查一下|搜一下|fetch|search|browse|look up)",
    re.IGNORECASE,
)
EMAIL_ACTION_PATTERN = re.compile(
    r"(发送到|发送给|发到|发给|发送|发邮件|邮件|邮箱|email|mail)",
    re.IGNORECASE,
)
EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")


class PlannerDecision(BaseModel):
    route: Literal["research", "agent"] = Field(default="agent")
    reason: str = Field(default="")
    search_query: str = Field(default="")
    answer_mode: Literal["grounded_summary", "tool_agent"] = Field(default="tool_agent")
    post_actions: list[Literal["send_email"]] = Field(default_factory=list)


class AgentState(MessagesState):
    planner_decision: dict
    research_result: str
    tool_trace: list[dict]


def load_dotenv(env_path: Path) -> None:
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def get_path_from_env(name: str, default: Path) -> Path:
    value = os.getenv(name)
    return Path(value) if value else default


def configure_logger() -> logging.Logger:
    logger = logging.getLogger("wei_agent")
    if logger.handlers:
        return logger

    log_dir = get_path_from_env("WEI_LOG_DIR", BASE_DIR / "logs")
    log_dir.mkdir(parents=True, exist_ok=True)

    handler = RotatingFileHandler(
        log_dir / "wei_agent.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))

    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def load_system_prompt() -> str:
    prompt_candidates = [
        os.getenv("SYSTEM_PROMPT_PATH"),
        str(BASE_DIR / "config" / "system_prompt.txt"),
    ]
    for candidate in prompt_candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if path.exists():
            return path.read_text(encoding="utf-8")
    return DEFAULT_SYSTEM_PROMPT


def get_latest_user_text(messages: list) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return message.content if isinstance(message.content, str) else str(message.content)
    return ""


def build_runtime_system_prompt() -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    policy = (
        f"今天日期是 {today}。\n"
        "如果请求依赖当前、最近、版本、日期、赛程等可能变化的信息，优先依据工具证据而不是模型记忆。\n"
        "如果证据不足，请明确说明无法确认。"
    )
    return f"{SYSTEM_PROMPT_TEXT}\n\n{policy}"


def format_cn_date(value: datetime) -> str:
    return f"{value.year}年{value.month}月{value.day}日"


def expand_relative_dates(text: str) -> str:
    if not text:
        return text

    now = datetime.now()
    replacements = [
        (r"今天|今日", format_cn_date(now)),
        (r"明天", format_cn_date(now + timedelta(days=1))),
        (r"后天", format_cn_date(now + timedelta(days=2))),
        (r"昨天|昨日", format_cn_date(now - timedelta(days=1))),
        (r"\btoday\b", now.strftime("%Y-%m-%d")),
        (r"\btomorrow\b", (now + timedelta(days=1)).strftime("%Y-%m-%d")),
        (r"\byesterday\b", (now - timedelta(days=1)).strftime("%Y-%m-%d")),
    ]

    expanded = text
    for pattern, replacement in replacements:
        expanded = re.sub(pattern, replacement, expanded, flags=re.IGNORECASE)
    return expanded


def has_relative_date(text: str) -> bool:
    return bool(re.search(r"今天|今日|明天|后天|昨天|昨日|\btoday\b|\btomorrow\b|\byesterday\b", text, re.IGNORECASE))


def has_explicit_date(text: str) -> bool:
    return bool(re.search(r"\d{4}[-/.年]\d{1,2}([-/\.月]\d{1,2})?", text))


def build_default_search_query(user_text: str) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    if not user_text:
        return today

    normalized = EMAIL_PATTERN.sub("", user_text)
    normalized = EMAIL_ACTION_PATTERN.sub(" ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    normalized = expand_relative_dates(normalized or user_text)
    if has_explicit_date(normalized):
        return normalized
    return f"{normalized} {today}"


def normalize_search_query(query: str, user_text: str) -> str:
    if has_relative_date(user_text):
        return build_default_search_query(user_text)
    cleaned = (query or "").strip()
    if not cleaned:
        return build_default_search_query(user_text)
    return expand_relative_dates(cleaned)


def extract_email_targets(user_text: str) -> list[str]:
    return EMAIL_PATTERN.findall(user_text)


def infer_post_actions(user_text: str) -> list[str]:
    emails = extract_email_targets(user_text)
    if emails and EMAIL_ACTION_PATTERN.search(user_text):
        return ["send_email"]
    return []


def is_time_sensitive(user_text: str) -> bool:
    return bool(TIME_SENSITIVE_PATTERN.search(user_text))


def likely_needs_research(user_text: str) -> bool:
    return is_time_sensitive(user_text) or bool(SEARCH_ACTION_PATTERN.search(user_text))


def localize_planner_reason(reason: str, route: str, post_actions: list[str] | None = None) -> str:
    text = (reason or "").strip()
    post_actions = post_actions or []

    if not text:
        if route == "research":
            return "用户请求涉及时效性或需要联网核实的信息。"
        if route == "agent":
            return "用户请求更适合直接进入常规执行流程。"
        return "已根据当前请求选择执行路径。"

    lowered = text.lower()
    if "time-sensitive" in lowered or "current" in lowered or "latest" in lowered or "recent" in lowered:
        return "用户请求涉及时效性或最新信息，适合先思考再回答。"
    if "email" in lowered and post_actions:
        return "需要先整理信息，再继续执行邮件等后续动作。"
    if "search" in lowered and route == "research":
        return "这个请求需要先搜索和核实资料。"
    if re.search(r"[\u4e00-\u9fff]", text):
        return text
    if route == "research":
        return "已判断这个请求更适合先思考，再基于结果回答。"
    if route == "agent":
        return "已判断这个请求可以直接进入常规执行流程。"
    return text


def normalize_planner_decision(decision: PlannerDecision | dict, user_text: str) -> dict:
    data = decision.model_dump() if isinstance(decision, PlannerDecision) else dict(decision)
    if is_time_sensitive(user_text):
        data["route"] = "research"
        data["answer_mode"] = "grounded_summary"
        data["reason"] = data.get("reason") or "命中了时效性规则。"

    if data.get("route") == "research":
        data["search_query"] = normalize_search_query(data.get("search_query", ""), user_text)

    post_actions = list(dict.fromkeys(data.get("post_actions") or infer_post_actions(user_text)))
    data["post_actions"] = post_actions
    if post_actions and data.get("route") != "research" and likely_needs_research(user_text):
        data["route"] = "research"
        data["answer_mode"] = "grounded_summary"
        data["reason"] = f"{data.get('reason', '').strip()} 需要先完成思考，再继续执行后续动作。".strip()
        data["search_query"] = normalize_search_query(data.get("search_query", ""), user_text)

    data["reason"] = localize_planner_reason(data.get("reason", ""), data.get("route", "agent"), post_actions)
    return data


def build_heuristic_planner_decision(user_text: str) -> dict:
    route = "research" if likely_needs_research(user_text) else "agent"
    return normalize_planner_decision(
        {
            "route": route,
            "reason": "本地规则兜底路径。",
            "search_query": build_default_search_query(user_text) if route == "research" else "",
            "answer_mode": "grounded_summary" if route == "research" else "tool_agent",
            "post_actions": infer_post_actions(user_text),
        },
        user_text,
    )


def trim_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[Truncated]"


def build_tool_trace(messages: list) -> list[dict]:
    last_human_index = -1
    for index, message in enumerate(messages):
        if isinstance(message, HumanMessage):
            last_human_index = index

    trace = []
    scoped_messages = messages[last_human_index + 1 :] if last_human_index >= 0 else messages
    for message in scoped_messages:
        if isinstance(message, ToolMessage):
            trace.append(
                {
                    "tool": getattr(message, "name", ""),
                    "content": trim_text(str(message.content), 400),
                }
            )
    return trace


def append_tool_trace(state: AgentState, entries: list[dict]) -> list[dict]:
    existing = list(state.get("tool_trace") or [])
    existing.extend(entries)
    return existing


def format_trace_content(title: str, body: str) -> str:
    return trim_text(f"{title}\n\n{body}", 800)


load_dotenv(BASE_DIR / ".env")
logger = configure_logger()

REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
ONE_API_URL = os.getenv("ONE_API_URL", "http://127.0.0.1:3000/v1").rstrip("/")
ONE_API_TOKEN = os.getenv("ONE_API_TOKEN", "").strip()
APP_HOST = os.getenv("APP_HOST", "127.0.0.1")
APP_PORT = int(os.getenv("APP_PORT", "8000"))
PLANNER_MODEL = os.getenv("PLANNER_MODEL", "gpt-4o-mini")
AGENT_MODEL = os.getenv("AGENT_MODEL", "gpt-4o")
ONLINE_RESEARCH_MODEL = os.getenv("ONLINE_RESEARCH_MODEL", "gpt-4o-mini-search-preview")
ONLINE_RESEARCH_MAX_TOKENS = int(os.getenv("ONLINE_RESEARCH_MAX_TOKENS", "420"))
SYSTEM_PROMPT_TEXT = load_system_prompt()

if not ONE_API_TOKEN:
    logger.warning("ONE_API_TOKEN is not set. Chat requests will fail until it is configured.")

limiter = Limiter(key_func=get_remote_address, storage_uri="memory://")
_llm_cache: dict[tuple[str, str, float], ChatOpenAI] = {}


def get_llm(model_name: str) -> ChatOpenAI:
    if not ONE_API_TOKEN:
        raise RuntimeError("ONE_API_TOKEN is not configured.")

    key = (model_name, ONE_API_URL, 0.2)
    if key not in _llm_cache:
        _llm_cache[key] = ChatOpenAI(
            model=model_name,
            temperature=0.2,
            openai_api_key=ONE_API_TOKEN,
            openai_api_base=ONE_API_URL,
        )
    return _llm_cache[key]


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


def call_online_research_model(user_text: str, search_query: str, model_name: str) -> str:
    if not ONE_API_TOKEN:
        raise RuntimeError("ONE_API_TOKEN is not configured.")

    endpoint = f"{ONE_API_URL}/chat/completions"
    today = datetime.now().strftime("%Y-%m-%d")
    system_prompt = (
        f"{SYSTEM_PROMPT_TEXT}\n\n"
        f"今天日期是 {today}。\n"
        "你是一名支持内置联网搜索的研究助手。\n"
        "当问题涉及当前、最新、会变化的信息时，请先联网核实，再给出结论。\n"
        "请输出简洁中文答案；若证据不足，要明确说明。\n"
        "不要输出 Markdown 链接、括号引用、来源网址或原始搜索引用格式。\n"
        "优先直接给结论和必要要点，控制篇幅，避免冗长展开。"
    )
    user_prompt = (
        f"用户请求：{user_text}\n\n"
        f"建议搜索焦点：{search_query or user_text}\n\n"
        "请先联网搜索并核实，再给出最终回答。"
    )
    payload = {
        "model": model_name,
        "temperature": 0.2,
        "max_tokens": ONLINE_RESEARCH_MAX_TOKENS,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    fallback_payload = {
        "model": model_name,
        "max_tokens": ONLINE_RESEARCH_MAX_TOKENS,
        "messages": [
            {"role": "user", "content": user_text},
        ],
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


async def planner_node(state: AgentState, config=None):
    model_name = PLANNER_MODEL
    if config and "configurable" in config and config["configurable"].get("planner_model"):
        model_name = config["configurable"]["planner_model"]

    user_text = get_latest_user_text(state["messages"])
    today = datetime.now().strftime("%Y-%m-%d")

    if not user_text:
        return {
            "planner_decision": normalize_planner_decision(PlannerDecision(), user_text),
            "tool_trace": [],
            "research_result": "",
        }

    planner_prompt = [
        SystemMessage(
            content=(
                "你是工作流规划节点，只返回结构化决策。\n"
                "请求依赖当前、最新、会变化的信息时，选择 route='research'。\n"
                "稳定知识、普通写作、可直接继续工具流程时，选择 route='agent'。\n"
                "如果选择 research，请给出简洁可用的 search_query。\n"
                "reason 用中文，保持简短。"
            )
        ),
        HumanMessage(
            content=(
                f"今天日期：{today}\n"
                f"用户请求：{user_text}\n"
                f"提示：{'这是明显的时效性问题' if is_time_sensitive(user_text) else '这不是明显的时效性问题'}"
            )
        ),
    ]
    try:
        llm = get_llm(model_name).with_structured_output(PlannerDecision)
        raw_decision = await llm.ainvoke(planner_prompt)
    except Exception as exc:
        logger.warning("Planner model %s failed, using heuristic fallback: %s", model_name, exc)
        raw_decision = build_heuristic_planner_decision(user_text)

    decision = normalize_planner_decision(raw_decision, user_text)
    logger.info(
        "Planner route=%s reason=%s query=%s post_actions=%s",
        decision["route"],
        decision["reason"],
        decision["search_query"],
        decision["post_actions"],
    )
    return {
        "planner_decision": decision,
        "tool_trace": [],
        "research_result": "",
    }


def route_after_planner(state: AgentState):
    decision = state.get("planner_decision") or {}
    if decision.get("route") != "research" and is_time_sensitive(get_latest_user_text(state["messages"])):
        return "research"
    return decision.get("route", "agent")


async def online_research_node(state: AgentState, config=None):
    user_text = get_latest_user_text(state["messages"])
    decision = state.get("planner_decision") or {}
    model_name = ONLINE_RESEARCH_MODEL
    if config and "configurable" in config and config["configurable"].get("online_research_model"):
        model_name = config["configurable"]["online_research_model"]

    query = decision.get("search_query") or build_default_search_query(user_text)
    answer = call_online_research_model(user_text, query, model_name)
    trace_entry = {
        "tool": "online_research",
        "content": trim_text(
            f"联网思考模型：{model_name}\n搜索焦点：{query}\n状态：已完成联网思考并生成回答",
            400,
        ),
    }
    return {
        "messages": [AIMessage(content=answer)],
        "research_result": answer,
        "tool_trace": append_tool_trace(state, [trace_entry]),
    }


def route_after_online_research(state: AgentState):
    decision = state.get("planner_decision") or {}
    if decision.get("post_actions"):
        return "agent"
    return "__end__"


async def agent_node(state: AgentState, config=None):
    model_name = AGENT_MODEL
    if config and "configurable" in config:
        model_name = config["configurable"].get("model", model_name)

    llm = get_llm(model_name)
    llm_with_tools = llm.bind_tools([send_email])
    system_prompt = build_runtime_system_prompt()
    if state.get("research_result"):
        system_prompt += (
            "\n\nA grounded research answer already exists in the conversation. "
            "Use that answer as the source of truth for any follow-up actions."
        )

    messages = state["messages"]
    system_msg = SystemMessage(content=system_prompt)
    if not messages or not isinstance(messages[0], SystemMessage):
        messages = [system_msg] + messages
    else:
        messages = [system_msg] + messages[1:]

    response = await llm_with_tools.ainvoke(messages)
    return {"messages": [response]}


def should_continue_agent(state: AgentState):
    last_message = state["messages"][-1]
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "tools"
    return "__end__"


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with AsyncRedisSaver.from_conn_string(REDIS_URL) as checkpointer:
        workflow = StateGraph(AgentState)
        workflow.add_node("planner", planner_node)
        workflow.add_node("online_research", online_research_node)
        workflow.add_node("agent", agent_node)
        workflow.add_node("tools", ToolNode([send_email]))

        workflow.set_entry_point("planner")
        workflow.add_conditional_edges(
            "planner",
            route_after_planner,
            {"research": "online_research", "agent": "agent"},
        )
        workflow.add_conditional_edges(
            "online_research",
            route_after_online_research,
            {"agent": "agent", "__end__": "__end__"},
        )
        workflow.add_conditional_edges("agent", should_continue_agent, {"tools": "tools", "__end__": "__end__"})
        workflow.add_edge("tools", "agent")

        app.state.graph = workflow.compile(checkpointer=checkpointer)
        yield


app = FastAPI(lifespan=lifespan, title="Wei AI Agent")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "app_host": APP_HOST,
        "app_port": APP_PORT,
        "one_api_url": ONE_API_URL,
        "redis_url": REDIS_URL,
        "one_api_token_configured": bool(ONE_API_TOKEN),
        "planner_model": PLANNER_MODEL,
        "agent_model": AGENT_MODEL,
        "online_research_model": ONLINE_RESEARCH_MODEL,
        "smtp_configured": smtp_is_configured(),
    }


@app.post("/api/chat")
@limiter.limit("10 per minute")
async def chat(request: Request):
    try:
        data = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON request body.") from exc

    if not data:
        raise HTTPException(status_code=400, detail="Request body cannot be empty.")

    messages = data.get("messages", [])
    if not messages:
        raise HTTPException(status_code=400, detail="messages cannot be empty.")
    if len(messages) > 50:
        raise HTTPException(status_code=400, detail="Too many messages.")

    for msg in messages:
        if len(msg.get("content", "")) > 2000:
            raise HTTPException(status_code=400, detail="A message is too long.")

    thread_id = data.get("thread_id")
    new_thread = False
    if not thread_id:
        thread_id = str(uuid.uuid4())
        new_thread = True

    model = data.get("model", AGENT_MODEL)
    planner_model = data.get("planner_model", PLANNER_MODEL)
    online_research_model = data.get("online_research_model", ONLINE_RESEARCH_MODEL)
    include_tool_trace = bool(data.get("include_tool_trace"))
    config = {
        "configurable": {
            "thread_id": thread_id,
            "model": model,
            "planner_model": planner_model,
            "online_research_model": online_research_model,
        }
    }

    try:
        langchain_messages = convert_to_langchain(messages)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid message format.") from exc

    try:
        result = await app.state.graph.ainvoke({"messages": langchain_messages}, config=config)
        final_message = result["messages"][-1]
        reply = final_message.content if hasattr(final_message, "content") else str(final_message)
        reply = remove_urls(reply)
        response = {
            "reply": reply,
            "thread_id": thread_id,
            "planner_decision": result.get("planner_decision", {}),
        }
        if include_tool_trace:
            response["tool_trace"] = (result.get("tool_trace") or []) + build_tool_trace(result["messages"])
        if new_thread:
            response["new_thread"] = True
        return JSONResponse(content=response)
    except RuntimeError as exc:
        logger.exception("Configuration error while handling request.")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Unhandled error while processing request.")
        raise HTTPException(status_code=500, detail="Internal server error.") from exc


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=APP_HOST, port=APP_PORT)
