from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from langchain_core.messages import AIMessage, SystemMessage
from langgraph.checkpoint.redis.aio import AsyncRedisSaver
from langgraph.graph import MessagesState
from langgraph.prebuilt import ToolNode
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from src.auth_store import (
    create_session,
    delete_session,
    ensure_seed_admin,
    get_user_by_username,
    init_auth_db,
    verify_password,
)
from src.chat_helpers import (
    append_tool_trace,
    build_tool_trace,
    convert_to_langchain,
    format_trace_content,
    get_latest_user_text,
    make_trace_entry,
    public_planner_decision,
    remove_urls,
    summarize_text,
    validate_chat_messages,
)
from src.dispatching import (
    PlannerDecision,
    build_default_search_query,
    build_dispatcher_prompt,
    build_heuristic_planner_decision,
    choose_execution_model,
    infer_post_actions,
    is_local_execution_intent,
    is_time_sensitive,
    normalize_planner_decision,
    should_prefer_local_execution,
)
from src.research_client import call_online_research_model
from src.runtime_config import (
    APP_HOST,
    APP_PORT,
    AUTH_COOKIE_NAME,
    AUTH_COOKIE_SECURE,
    DISPATCHER_MODEL,
    EXECUTION_MODEL_ADVANCED,
    EXECUTION_MODEL_SIMPLE,
    EXECUTION_MODEL_STANDARD,
    LOCAL_EXECUTION_MODEL,
    ONE_API_TOKEN,
    ONE_API_URL,
    ONLINE_RESEARCH_MODEL,
    REDIS_URL,
    SYSTEM_PROMPT_TEXT,
    get_llm,
    limiter,
    logger,
)
from src.tools import ask_open_interpreter, online_research, open_interpreter_is_configured, send_email, smtp_is_configured
from src.web_helpers import (
    ChatRequestPayload,
    LoginPayload,
    build_graph_config,
    ensure_thread_id,
    get_current_user,
    parse_json_body,
    parse_payload_model,
    require_authenticated_user,
    serialize_user,
)


class AgentState(MessagesState):
    planner_decision: dict
    research_result: str
    tool_trace: list[dict]


def build_runtime_system_prompt() -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    policy = (
        f"今天日期是 {today}。\n"
        "如果请求依赖当前、最新、会变化的信息，优先依据工具证据而不是模型记忆。\n"
        "如果证据不足，请明确说明无法确认。"
    )
    return f"{SYSTEM_PROMPT_TEXT}\n\n{policy}"


def get_request_context(config=None) -> dict:
    configurable = (config or {}).get("configurable", {})
    return {
        "thread_id": configurable.get("thread_id", ""),
        "username": configurable.get("username", ""),
        "user_id": configurable.get("user_id"),
        "local_execution": bool(configurable.get("local_execution")),
    }


async def planner_node(state: AgentState, config=None):
    request_context = get_request_context(config)
    local_execution = request_context["local_execution"]
    user_text = get_latest_user_text(state["messages"])
    today = datetime.now().strftime("%Y-%m-%d")

    if not user_text:
        return {
            "planner_decision": normalize_planner_decision(PlannerDecision(), user_text, local_execution=local_execution),
            "tool_trace": [],
            "research_result": "",
        }

    if should_prefer_local_execution(user_text, config):
        decision = normalize_planner_decision(
            {
                "route": "agent",
                "reason": "请求涉及本地执行或电脑操作，优先进入执行流程。",
                "complexity": "advanced",
                "search_query": "",
                "answer_mode": "tool_agent",
                "post_actions": infer_post_actions(user_text),
            },
            user_text,
            local_execution=True,
        )
        return {"planner_decision": decision, "tool_trace": [], "research_result": ""}

    dispatcher_prompt = build_dispatcher_prompt(user_text, today, local_execution=local_execution)
    try:
        llm = get_llm(DISPATCHER_MODEL).with_structured_output(PlannerDecision)
        raw_decision = await llm.ainvoke(dispatcher_prompt)
    except Exception as exc:
        logger.warning("Dispatcher model %s failed, using heuristic fallback: %s", DISPATCHER_MODEL, exc)
        raw_decision = build_heuristic_planner_decision(user_text, local_execution=local_execution)

    decision = normalize_planner_decision(raw_decision, user_text, local_execution=local_execution)
    logger.info(
        "Dispatcher thread=%s user=%s route=%s complexity=%s reason=%s query=%s post_actions=%s local_execution=%s",
        request_context["thread_id"],
        request_context["username"],
        decision["route"],
        decision["complexity"],
        decision["reason"],
        decision["search_query"],
        decision["post_actions"],
        local_execution,
    )
    return {"planner_decision": decision, "tool_trace": [], "research_result": ""}


def route_after_planner(state: AgentState):
    decision = state.get("planner_decision") or {}
    user_text = get_latest_user_text(state["messages"])
    if decision.get("route") != "research" and is_time_sensitive(user_text) and not is_local_execution_intent(user_text):
        return "research"
    return decision.get("route", "agent")


async def online_research_node(state: AgentState, config=None):
    request_context = get_request_context(config)
    user_text = get_latest_user_text(state["messages"])
    decision = state.get("planner_decision") or {}
    query = decision.get("search_query") or build_default_search_query(user_text)
    logger.info(
        "Online research start thread=%s user=%s model=%s query=%s",
        request_context["thread_id"],
        request_context["username"],
        ONLINE_RESEARCH_MODEL,
        query,
    )
    answer = call_online_research_model(user_text, query, ONLINE_RESEARCH_MODEL)
    trace_entry = make_trace_entry(
        "online_research",
        format_trace_content(f"联网思考模型：{ONLINE_RESEARCH_MODEL}\n搜索焦点：{query}", answer),
        title="联网思考",
        phase="research",
        model=ONLINE_RESEARCH_MODEL,
        summary=f"已完成联网思考：{query}",
    )
    logger.info(
        "Online research done thread=%s user=%s chars=%s",
        request_context["thread_id"],
        request_context["username"],
        len(answer),
    )
    return {
        "messages": [AIMessage(content=answer)],
        "research_result": answer,
        "tool_trace": append_tool_trace(state.get("tool_trace"), [trace_entry]),
    }


def route_after_online_research(state: AgentState):
    decision = state.get("planner_decision") or {}
    if decision.get("post_actions"):
        return "agent"
    return "__end__"


async def agent_node(state: AgentState, config=None):
    request_context = get_request_context(config)
    local_execution = request_context["local_execution"]
    user_text = get_latest_user_text(state["messages"])
    decision = state.get("planner_decision") or {}
    model_name = choose_execution_model(decision, local_execution=local_execution)

    llm = get_llm(model_name)
    llm_with_tools = llm.bind_tools([online_research, send_email, ask_open_interpreter])

    system_prompt = build_runtime_system_prompt()
    system_prompt += (
        "\n\nTool policy:\n"
        "- Use online_research when the task depends on latest, current, changing, or externally verified information.\n"
        "- If a task combines current information with local actions, first call online_research, then continue with ask_open_interpreter or send_email.\n"
        "- Use ask_open_interpreter only when code execution or local computer actions are actually needed.\n"
        "- Pass runnable code directly to ask_open_interpreter, not natural-language instructions.\n"
        "- For side-effect actions such as opening apps, opening a browser, writing files, or launching programs, make the code print a short Chinese success message after the action completes.\n"
        "- Do not return raw booleans like True or False when a clearer execution message can be printed.\n"
        "- Use send_email only when the user explicitly asks to send an email and a recipient is available.\n"
        f"- The dispatcher selected complexity={decision.get('complexity', 'standard')} and executor model={model_name}. Respect that execution level.\n"
    )
    if local_execution or is_local_execution_intent(user_text):
        system_prompt += (
            "\n\nLocal execution policy:\n"
            "- The user is asking to operate their local computer or run local code.\n"
            "- If the task still needs up-to-date external information, call online_research before local execution.\n"
            "- Strongly prefer ask_open_interpreter for these tasks instead of answering abstractly.\n"
            "- If you call ask_open_interpreter, provide complete runnable code.\n"
            "- When opening local apps, browsers, files, or performing side effects, include a final print statement in Chinese describing what succeeded.\n"
            "- Prefer concise, reliable code over fancy code."
        )
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

    logger.info(
        "Agent start thread=%s user=%s model=%s route=%s complexity=%s local_execution=%s",
        request_context["thread_id"],
        request_context["username"],
        model_name,
        decision.get("route", "agent"),
        decision.get("complexity", "standard"),
        local_execution,
    )
    response = await llm_with_tools.ainvoke(messages)
    tool_calls = getattr(response, "tool_calls", None) or []
    if tool_calls:
        logger.info(
            "Agent tool plan thread=%s user=%s tools=%s",
            request_context["thread_id"],
            request_context["username"],
            [call.get("name", "") for call in tool_calls],
        )
    else:
        logger.info(
            "Agent completed without tool call thread=%s user=%s",
            request_context["thread_id"],
            request_context["username"],
        )
    return {"messages": [response]}


def should_continue_agent(state: AgentState):
    last_message = state["messages"][-1]
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "tools"
    return "__end__"


def build_agent_graph(checkpointer):
    from langgraph.graph import StateGraph

    workflow = StateGraph(AgentState)
    workflow.add_node("planner", planner_node)
    workflow.add_node("online_research", online_research_node)
    workflow.add_node("agent", agent_node)
    workflow.add_node("tools", ToolNode([online_research, send_email, ask_open_interpreter]))

    workflow.set_entry_point("planner")
    workflow.add_conditional_edges("planner", route_after_planner, {"research": "online_research", "agent": "agent"})
    workflow.add_conditional_edges("online_research", route_after_online_research, {"agent": "agent", "__end__": "__end__"})
    workflow.add_conditional_edges("agent", should_continue_agent, {"tools": "tools", "__end__": "__end__"})
    workflow.add_edge("tools", "agent")
    return workflow.compile(checkpointer=checkpointer)


def build_health_payload() -> dict:
    return {
        "status": "ok",
        "app_host": APP_HOST,
        "app_port": APP_PORT,
        "one_api_url": ONE_API_URL,
        "redis_url": REDIS_URL,
        "one_api_token_configured": bool(ONE_API_TOKEN),
        "dispatcher_model": DISPATCHER_MODEL,
        "execution_model_simple": EXECUTION_MODEL_SIMPLE,
        "execution_model_standard": EXECUTION_MODEL_STANDARD,
        "execution_model_advanced": EXECUTION_MODEL_ADVANCED,
        "local_execution_model": LOCAL_EXECUTION_MODEL,
        "online_research_model": ONLINE_RESEARCH_MODEL,
        "smtp_configured": smtp_is_configured(),
        "open_interpreter_configured": open_interpreter_is_configured(),
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_auth_db()
    ensure_seed_admin()
    async with AsyncRedisSaver.from_conn_string(REDIS_URL) as checkpointer:
        app.state.graph = build_agent_graph(checkpointer)
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
    return build_health_payload()


@app.get("/api/auth/me")
async def auth_me(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not logged in.")
    return {"authenticated": True, "user": serialize_user(user, include_bindings=True)}


@app.post("/api/auth/login")
async def auth_login(request: Request):
    data = await parse_json_body(request)
    payload = parse_payload_model(data, LoginPayload, "Invalid login payload.")
    username = payload.username.strip()
    password = payload.password.strip()
    if not username or not password:
        raise HTTPException(status_code=400, detail="Username and password are required.")

    user = get_user_by_username(username)
    if not user or not verify_password(password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid username or password.")

    session_id, expires_at = create_session(int(user["id"]))
    response = JSONResponse(content={"authenticated": True, "user": serialize_user(user, include_bindings=False)})
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=session_id,
        httponly=True,
        samesite="lax",
        secure=AUTH_COOKIE_SECURE,
        expires=expires_at.strftime("%a, %d %b %Y %H:%M:%S GMT"),
        path="/",
    )
    return response


@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    session_id = request.cookies.get(AUTH_COOKIE_NAME, "").strip()
    if session_id:
        delete_session(session_id)
    response = JSONResponse(content={"ok": True})
    response.delete_cookie(AUTH_COOKIE_NAME, path="/")
    return response


@app.post("/api/chat")
@limiter.limit("10 per minute")
async def chat(request: Request):
    current_user = require_authenticated_user(request)
    data = await parse_json_body(request)
    payload = parse_payload_model(data, ChatRequestPayload, "Invalid chat payload.")
    validate_chat_messages(payload.messages)

    thread_id, new_thread = ensure_thread_id(payload.thread_id)
    config = build_graph_config(thread_id, current_user, payload.local_execution)

    try:
        langchain_messages = convert_to_langchain(payload.messages)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid message format.") from exc

    try:
        result = await app.state.graph.ainvoke({"messages": langchain_messages}, config=config)
        final_message = result["messages"][-1]
        reply = final_message.content if hasattr(final_message, "content") else str(final_message)
        reply = remove_urls(reply)
        tool_trace_entries = (result.get("tool_trace") or []) + build_tool_trace(result["messages"])

        logger.info(
            "Chat completed thread=%s user=%s route=%s complexity=%s tools=%s reply=%s",
            thread_id,
            current_user["username"],
            (result.get("planner_decision") or {}).get("route", "agent"),
            (result.get("planner_decision") or {}).get("complexity", "standard"),
            [entry.get("tool") for entry in tool_trace_entries],
            summarize_text(reply, 120),
        )

        response = {
            "reply": reply,
            "thread_id": thread_id,
            "planner_decision": public_planner_decision(result.get("planner_decision", {})),
        }
        if payload.include_tool_trace:
            response["tool_trace"] = tool_trace_entries
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
