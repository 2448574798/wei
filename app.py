import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Literal

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

from tools import fetch_webpage, send_email, web_search


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."
TIME_SENSITIVE_PATTERN = re.compile(
    r"(今天|昨日|昨天|明天|现在|当前|目前|最新|最近|刚刚|实时|近况|行情|价格|汇率|股价|新闻|天气|"
    r"版本|更新|发布|API|SDK|模型|文档|政策|法规|公告|比赛|赛程|票房|销量|"
    r"today|now|current|latest|recent|price|weather|news|version|release|api|sdk|model)",
    re.IGNORECASE,
)
SEARCH_URL_PATTERN = re.compile(r"^URL:\s*(\S+)$", re.MULTILINE)


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


class AgentState(MessagesState):
    planner_decision: dict
    search_result: str
    fetch_result: str


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
    return f"{user_text} {today}"


def extract_urls(search_result: str) -> list[str]:
    return SEARCH_URL_PATTERN.findall(search_result)


def is_time_sensitive(user_text: str) -> bool:
    return bool(TIME_SENSITIVE_PATTERN.search(user_text))


def get_selected_model_name(config) -> str:
    model_name = "gpt-4o"
    if config and "configurable" in config:
        model_name = config["configurable"].get("model", model_name)
    return model_name


load_dotenv(BASE_DIR / ".env")
logger = configure_logger()

REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
ONE_API_URL = os.getenv("ONE_API_URL", "http://127.0.0.1:3000/v1")
ONE_API_TOKEN = os.getenv("ONE_API_TOKEN", "").strip()
APP_HOST = os.getenv("APP_HOST", "127.0.0.1")
APP_PORT = int(os.getenv("APP_PORT", "8000"))
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
    model_name = get_selected_model_name(config)
    llm = get_llm(model_name).with_structured_output(PlannerDecision)
    user_text = get_latest_user_text(state["messages"])
    today = datetime.now().strftime("%Y-%m-%d")

    if not user_text:
        return {"planner_decision": PlannerDecision().model_dump()}

    planner_prompt = [
        SystemMessage(
            content=(
                "You are a planning node for a tool-using assistant.\n"
                "Return only a structured decision.\n"
                "Choose route='research' when the request is time-sensitive, asks for latest/current/recent information, "
                "or depends on news, prices, versions, dates, schedules, policies, weather, or anything likely to change.\n"
                "Choose route='agent' for stable knowledge, simple writing tasks, or cases where normal tool-calling can continue.\n"
                "For research, provide a concrete search query and set answer_mode='grounded_summary'.\n"
                "For agent, set answer_mode='tool_agent'."
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
    decision = await llm.ainvoke(planner_prompt)
    logger.info("Planner route=%s reason=%s query=%s", decision.route, decision.reason, decision.search_query)
    return {"planner_decision": decision.model_dump()}


def route_after_planner(state: AgentState):
    decision = state.get("planner_decision") or {}
    return decision.get("route", "agent")


async def search_node(state: AgentState):
    decision = state.get("planner_decision") or {}
    user_text = get_latest_user_text(state["messages"])
    query = decision.get("search_query") or build_default_search_query(user_text)
    search_result = web_search(query)
    logger.info("Executed search query: %s", query)
    return {"search_result": search_result}


def route_after_search(state: AgentState):
    decision = state.get("planner_decision") or {}
    if decision.get("needs_fetch"):
        return "fetch"
    return "grounded_answer"


async def fetch_node(state: AgentState):
    decision = state.get("planner_decision") or {}
    fetch_url = decision.get("fetch_url", "").strip()
    if not fetch_url:
        urls = extract_urls(state.get("search_result", ""))
        fetch_url = urls[0] if urls else ""

    if not fetch_url:
        return {"fetch_result": ""}

    fetch_result = fetch_webpage(fetch_url)
    logger.info("Fetched webpage for grounded answer: %s", fetch_url)
    return {"fetch_result": fetch_result}


async def grounded_answer_node(state: AgentState, config=None):
    model_name = get_selected_model_name(config)
    llm = get_llm(model_name)
    user_text = get_latest_user_text(state["messages"])
    today = datetime.now().strftime("%Y-%m-%d")
    search_result = state.get("search_result", "")
    fetch_result = state.get("fetch_result", "")

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


async def agent_node(state: AgentState, config=None):
    model_name = get_selected_model_name(config)
    llm = get_llm(model_name)
    llm_with_tools = llm.bind_tools([web_search, fetch_webpage, send_email])
    system_msg = SystemMessage(content=build_runtime_system_prompt())
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
    config = {"configurable": {"thread_id": thread_id, "model": model}}

    try:
        langchain_messages = convert_to_langchain(messages)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid message format.") from exc

    try:
        result = await app.state.graph.ainvoke({"messages": langchain_messages}, config=config)
        final_message = result["messages"][-1]
        reply = final_message.content if hasattr(final_message, "content") else str(final_message)
        reply = remove_urls(reply)
        response = {"reply": reply, "thread_id": thread_id}
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
