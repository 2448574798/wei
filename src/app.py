import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

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

from src.tools import fetch_webpage, is_fetch_error, send_email, smtp_is_configured, web_search


SRC_DIR = Path(__file__).resolve().parent
BASE_DIR = SRC_DIR.parent
DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."
TIME_SENSITIVE_PATTERN = re.compile(
    r"(今天|昨日|昨天|明天|现在|当前|目前|最新|最近|刚刚|实时|近况|行情|价格|汇率|股价|新闻|天气|"
    r"版本|更新|发布|API|SDK|模型|文档|政策|法规|公告|比赛|赛程|票房|销量|"
    r"today|now|current|latest|recent|price|weather|news|version|release|api|sdk|model)",
    re.IGNORECASE,
)
SEARCH_URL_PATTERN = re.compile(r"^URL:\s*(\S+)$", re.MULTILINE)
EMAIL_ACTION_PATTERN = re.compile(
    r"(发送到|发送给|发到|发给|发送|发邮件|邮件|邮箱|email|mail)",
    re.IGNORECASE,
)
SEARCH_ACTION_PATTERN = re.compile(r"(搜索|搜一下|查询|查一下|联网|网页|网站|fetch|search|browse|look up)", re.IGNORECASE)
NON_FETCH_FRIENDLY_DOMAINS = {
    "help.openai.com",
    "support.google.com",
    "docs.github.com",
}


class PlannerDecision(BaseModel):
    route: Literal["research", "agent"] = Field(
        default="agent",
        description="Use research for time-sensitive/current information, otherwise use agent.",
    )
    reason: str = Field(default="", description="Short reason for the decision.")
    search_query: str = Field(default="", description="Search query for research route.")
    needs_fetch: bool = Field(
        default=False,
        description="Whether a webpage should be fetched after search for more detail.",
    )
    fetch_url: str = Field(default="", description="Optional URL to fetch after search.")
    answer_mode: Literal["grounded_summary", "tool_agent"] = Field(
        default="tool_agent",
        description="How the answer should be produced.",
    )
    post_actions: list[Literal["send_email"]] = Field(
        default_factory=list,
        description="Actions that should happen after grounded research is complete.",
    )


class AgentState(MessagesState):
    planner_decision: dict
    search_result: str
    fetch_result: str
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
        f"Today is {today}.\n"
        "If a request depends on current, recent, versioned, scheduled, or otherwise changeable facts, "
        "prefer tool evidence over memory.\n"
        "If evidence is insufficient, clearly say it cannot be confirmed."
    )
    return f"{SYSTEM_PROMPT_TEXT}\n\n{policy}"


def build_default_search_query(user_text: str) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    if not user_text:
        return today
    normalized = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "", user_text)
    normalized = EMAIL_ACTION_PATTERN.sub(" ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return f"{normalized or user_text} {today}"


def extract_urls(search_result: str) -> list[str]:
    return SEARCH_URL_PATTERN.findall(search_result)


def is_time_sensitive(user_text: str) -> bool:
    return bool(TIME_SENSITIVE_PATTERN.search(user_text))


def get_agent_model_name(config) -> str:
    model_name = AGENT_MODEL
    if config and "configurable" in config:
        model_name = config["configurable"].get("model", model_name)
    return model_name


def get_planner_model_name(config) -> str:
    if config and "configurable" in config and config["configurable"].get("planner_model"):
        return config["configurable"]["planner_model"]
    return PLANNER_MODEL


def get_grounded_answer_model_name(config) -> str:
    if config and "configurable" in config and config["configurable"].get("grounded_model"):
        return config["configurable"]["grounded_model"]
    return GROUNDED_ANSWER_MODEL or get_agent_model_name(config)


def extract_email_targets(user_text: str) -> list[str]:
    return re.findall(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", user_text)


def infer_post_actions(user_text: str) -> list[str]:
    emails = extract_email_targets(user_text)
    if emails and EMAIL_ACTION_PATTERN.search(user_text):
        return ["send_email"]
    return []


def should_force_research(user_text: str) -> bool:
    return bool(user_text and is_time_sensitive(user_text))


def likely_needs_research(user_text: str) -> bool:
    return should_force_research(user_text) or bool(SEARCH_ACTION_PATTERN.search(user_text))


def localize_planner_reason(reason: str, route: str, user_text: str, post_actions: list[str] | None = None) -> str:
    text = (reason or "").strip()
    post_actions = post_actions or []

    if not text:
        if route == "research":
            return "用户请求涉及时效性或需要联网核实的信息。"
        if route == "agent":
            return "用户请求更适合直接进入常规执行流程。"
        return "已根据当前请求选择执行路径。"

    lowered = text.lower()

    if "heuristic fallback route" in lowered:
        return "模型规划不可用，已使用本地规则选择执行路径。"
    if "matched time-sensitive heuristic" in lowered:
        return "命中了时效性规则，因此优先走调研路径。"
    if "research is required before completing follow-up actions" in lowered:
        return "需要先完成调研，再继续执行后续动作。"
    if "time-sensitive" in lowered or "current" in lowered or "latest" in lowered or "recent" in lowered:
        return "用户请求涉及时效性或最新信息，适合先调研再回答。"
    if "stable knowledge" in lowered or "does not require current information" in lowered:
        return "用户请求更偏稳定知识，不需要先联网调研。"
    if "email" in lowered and post_actions:
        return "需要先整理信息，再继续执行邮件等后续动作。"
    if "search" in lowered and route == "research":
        return "这个请求需要先搜索和核实资料。"

    if route == "research":
        return "已判断这个请求更适合先调研，再基于结果回答。"
    if route == "agent":
        return "已判断这个请求可以直接进入常规执行流程。"
    return text


def normalize_planner_decision(decision: PlannerDecision | dict, user_text: str) -> dict:
    data = decision.model_dump() if isinstance(decision, PlannerDecision) else dict(decision)
    forced_research = should_force_research(user_text)
    if forced_research:
        data["route"] = "research"
        data["answer_mode"] = "grounded_summary"
        data["reason"] = data.get("reason") or "Matched time-sensitive heuristic."
        data["search_query"] = data.get("search_query") or build_default_search_query(user_text)
    if data.get("route") == "research":
        data["search_query"] = data.get("search_query") or build_default_search_query(user_text)
    post_actions = list(dict.fromkeys(data.get("post_actions") or infer_post_actions(user_text)))
    data["post_actions"] = post_actions
    if post_actions and data.get("route") != "research" and likely_needs_research(user_text):
        data["route"] = "research"
        data["answer_mode"] = "grounded_summary"
        data["reason"] = (
            f"{data.get('reason', '').strip()} Research is required before completing follow-up actions."
        ).strip()
        data["search_query"] = data.get("search_query") or build_default_search_query(user_text)
    data["reason"] = localize_planner_reason(data.get("reason", ""), data.get("route", "agent"), user_text, post_actions)
    return data


def build_heuristic_planner_decision(user_text: str) -> dict:
    route = "research" if likely_needs_research(user_text) else "agent"
    reason = "Heuristic fallback route."
    return normalize_planner_decision(
        {
            "route": route,
            "reason": reason,
            "search_query": build_default_search_query(user_text) if route == "research" else "",
            "needs_fetch": route == "research",
            "fetch_url": "",
            "answer_mode": "grounded_summary" if route == "research" else "tool_agent",
            "post_actions": infer_post_actions(user_text),
        },
        user_text,
    )


def score_fetchable_url(url: str) -> int:
    domain = urlparse(url).netloc.lower()
    if not domain:
        return -100
    score = 0
    if domain.startswith("www."):
        domain = domain[4:]
    if domain in NON_FETCH_FRIENDLY_DOMAINS:
        score -= 50
    if any(token in domain for token in ("docs", "developer", "python.org", "wikipedia.org", "mozilla.org")):
        score += 20
    if any(token in domain for token in ("github.com", "github.io", "medium.com")):
        score -= 5
    return score


def rank_fetch_urls(urls: list[str]) -> list[str]:
    return sorted(dict.fromkeys(urls), key=score_fetchable_url, reverse=True)


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
    return trim_text(f"{title}\n\n{body}", 400)


load_dotenv(BASE_DIR / ".env")
logger = configure_logger()

REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
ONE_API_URL = os.getenv("ONE_API_URL", "http://127.0.0.1:3000/v1")
ONE_API_TOKEN = os.getenv("ONE_API_TOKEN", "").strip()
APP_HOST = os.getenv("APP_HOST", "127.0.0.1")
APP_PORT = int(os.getenv("APP_PORT", "8000"))
PLANNER_MODEL = os.getenv("PLANNER_MODEL", "gpt-4o-mini")
AGENT_MODEL = os.getenv("AGENT_MODEL", "gpt-4o")
GROUNDED_ANSWER_MODEL = os.getenv("GROUNDED_ANSWER_MODEL", "").strip()
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


async def planner_node(state: AgentState, config=None):
    model_name = get_planner_model_name(config)
    user_text = get_latest_user_text(state["messages"])
    today = datetime.now().strftime("%Y-%m-%d")

    if not user_text:
        return {
            "planner_decision": normalize_planner_decision(PlannerDecision(), user_text),
            "tool_trace": [],
            "search_result": "",
            "fetch_result": "",
        }

    planner_prompt = [
        SystemMessage(
            content=(
                "You are a planning node for a tool-using assistant.\n"
                "Return only a structured decision.\n"
                "Choose route='research' when the request is time-sensitive, asks for latest/current/recent information, "
                "or depends on news, prices, versions, dates, schedules, policies, weather, or anything likely to change.\n"
                "Choose route='agent' for stable knowledge, simple writing tasks, or cases where normal tool-calling can continue.\n"
                "If the user asks to email/search/report after gathering current information, set post_actions=['send_email'] when email sending remains after research.\n"
                "For research, provide a concrete search query and set answer_mode='grounded_summary'.\n"
                "Keep reasons short and write the reason in Chinese."
            )
        ),
        HumanMessage(
            content=(
                f"Today: {today}\n"
                f"User request: {user_text}\n"
                f"Hint: {'time-sensitive' if is_time_sensitive(user_text) else 'not obviously time-sensitive'}"
            )
        ),
    ]
    try:
        llm = get_llm(model_name).with_structured_output(PlannerDecision)
        raw_decision = await llm.ainvoke(planner_prompt)
    except Exception as exc:
        fallback_model = get_agent_model_name(config)
        if fallback_model == model_name:
            logger.warning("Planner model %s failed, using heuristic fallback: %s", model_name, exc)
            raw_decision = build_heuristic_planner_decision(user_text)
        else:
            logger.warning("Planner model %s failed, falling back to %s: %s", model_name, fallback_model, exc)
            try:
                llm = get_llm(fallback_model).with_structured_output(PlannerDecision)
                raw_decision = await llm.ainvoke(planner_prompt)
            except Exception as fallback_exc:
                logger.warning("Planner fallback model %s failed, using heuristic fallback: %s", fallback_model, fallback_exc)
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
        "search_result": "",
        "fetch_result": "",
    }


def route_after_planner(state: AgentState):
    decision = state.get("planner_decision") or {}
    if decision.get("route") != "research" and should_force_research(get_latest_user_text(state["messages"])):
        return "research"
    return decision.get("route", "agent")


async def search_node(state: AgentState):
    decision = state.get("planner_decision") or {}
    user_text = get_latest_user_text(state["messages"])
    query = decision.get("search_query") or build_default_search_query(user_text)
    search_result = web_search(query)
    logger.info("Executed search query: %s", query)
    trace_entry = {
        "tool": "web_search",
        "content": format_trace_content(f"搜索关键词：{query}", search_result),
    }
    return {
        "search_result": search_result,
        "tool_trace": append_tool_trace(state, [trace_entry]),
    }


def route_after_search(state: AgentState):
    decision = state.get("planner_decision") or {}
    search_result = state.get("search_result", "")
    if search_result.startswith("Search failed:") or not extract_urls(search_result):
        return "grounded_answer"
    if decision.get("needs_fetch"):
        return "fetch"
    return "grounded_answer"


async def fetch_node(state: AgentState):
    decision = state.get("planner_decision") or {}
    fetch_url = decision.get("fetch_url", "").strip()
    candidate_urls = [fetch_url] if fetch_url else rank_fetch_urls(extract_urls(state.get("search_result", "")))
    if not candidate_urls:
        return {"fetch_result": ""}
    attempts = []
    for candidate_url in candidate_urls[:3]:
        fetch_result = fetch_webpage(candidate_url)
        attempts.append(candidate_url)
        if not is_fetch_error(fetch_result):
            logger.info("Fetched webpage for grounded answer: %s", candidate_url)
            trace_entry = {
                "tool": "fetch_webpage",
                "content": format_trace_content(f"抓取页面：{candidate_url}", fetch_result),
            }
            return {
                "fetch_result": fetch_result,
                "tool_trace": append_tool_trace(state, [trace_entry]),
            }
        logger.warning("Fetch attempt failed for %s: %s", candidate_url, fetch_result)
    final_fetch_result = f"{fetch_result}\nTried URLs: {', '.join(attempts)}"
    trace_entry = {
        "tool": "fetch_webpage",
        "content": format_trace_content(f"抓取尝试：{', '.join(attempts)}", final_fetch_result),
    }
    return {
        "fetch_result": final_fetch_result,
        "tool_trace": append_tool_trace(state, [trace_entry]),
    }


async def grounded_answer_node(state: AgentState, config=None):
    model_name = get_grounded_answer_model_name(config)
    llm = get_llm(model_name)
    user_text = get_latest_user_text(state["messages"])
    today = datetime.now().strftime("%Y-%m-%d")
    search_result = trim_text(state.get("search_result", ""), 1800)
    fetch_result = trim_text(state.get("fetch_result", ""), 2200)

    grounded_messages = [
        SystemMessage(
            content=(
                f"{SYSTEM_PROMPT_TEXT}\n\n"
                f"Today is {today}.\n"
                "You must answer using the provided research evidence first.\n"
                "Do not use stale memory to fill missing facts.\n"
                "If the evidence is insufficient, explicitly say so.\n"
                "If dates appear in the evidence, preserve them exactly."
            )
        ),
        HumanMessage(
            content=(
                f"User request:\n{user_text}\n\n"
                f"Search results:\n{search_result}\n\n"
                f"Fetched webpage content:\n{fetch_result or '[none]'}\n\n"
                "Write a concise Chinese answer grounded in the evidence above."
            )
        ),
    ]
    response = await llm.ainvoke(grounded_messages)
    return {"messages": [response]}


def route_after_grounded_answer(state: AgentState):
    decision = state.get("planner_decision") or {}
    if decision.get("post_actions"):
        return "agent"
    return "__end__"


async def agent_node(state: AgentState, config=None):
    model_name = get_agent_model_name(config)
    llm = get_llm(model_name)
    llm_with_tools = llm.bind_tools([web_search, fetch_webpage, send_email])
    system_prompt = build_runtime_system_prompt()
    decision = state.get("planner_decision") or {}
    if decision.get("post_actions") and state.get("search_result"):
        system_prompt += (
            "\n\nA grounded research answer already exists in the conversation."
            " Use that answer as the source of truth for any follow-up actions."
            " Avoid repeating web_search or fetch_webpage unless the current evidence is clearly insufficient."
        )
    system_msg = SystemMessage(content=system_prompt)
    messages = state["messages"]

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
        workflow.add_node("search", search_node)
        workflow.add_node("fetch", fetch_node)
        workflow.add_node("grounded_answer", grounded_answer_node)
        workflow.add_node("agent", agent_node)
        workflow.add_node("tools", ToolNode([web_search, fetch_webpage, send_email]))

        workflow.set_entry_point("planner")
        workflow.add_conditional_edges(
            "planner",
            route_after_planner,
            {"research": "search", "agent": "agent"},
        )
        workflow.add_conditional_edges(
            "search",
            route_after_search,
            {"fetch": "fetch", "grounded_answer": "grounded_answer"},
        )
        workflow.add_edge("fetch", "grounded_answer")
        workflow.add_conditional_edges(
            "grounded_answer",
            route_after_grounded_answer,
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
        "grounded_answer_model": GROUNDED_ANSWER_MODEL or AGENT_MODEL,
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

    model = data.get("model", "gpt-4o")
    planner_model = data.get("planner_model", PLANNER_MODEL)
    grounded_model = data.get("grounded_model", GROUNDED_ANSWER_MODEL or model)
    include_tool_trace = bool(data.get("include_tool_trace"))
    config = {
        "configurable": {
            "thread_id": thread_id,
            "model": model,
            "planner_model": planner_model,
            "grounded_model": grounded_model,
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
