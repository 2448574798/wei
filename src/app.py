import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
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
    default_tool_title,
    format_trace_content,
    get_latest_user_text,
    make_trace_entry,
    public_planner_decision,
    remove_urls,
    summarize_text,
    validate_chat_messages,
)
from src.execution_context import bind_execution_context, emit_runtime_event
from src.dispatching import (
    PlannerDecision,
    build_default_search_query,
    build_dispatcher_prompt,
    build_heuristic_planner_decision,
    collect_dispatch_signals,
    choose_execution_model,
    infer_post_actions,
    is_local_execution_intent,
    is_time_sensitive,
    should_prefer_local_execution,
    validate_dispatch_decision,
)
from src.human_loop import confirmation_store
from src.local_jobs import local_job_store
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
from src.tools import (
    ask_open_interpreter,
    browser_bridge_is_configured,
    decode_meta_payload,
    inspect_local_webpage,
    online_research,
    open_local_browser_page,
    open_interpreter_is_configured,
    request_human_confirmation,
    send_email,
    smtp_is_configured,
    start_local_webpage_monitor,
    start_open_interpreter_job,
)
from src.web_helpers import (
    ChatRequestPayload,
    ConfirmationPayload,
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
    dispatch_signals: dict
    research_result: str
    tool_trace: list[dict]
    awaiting_confirmation: dict | None
    pending_job: dict | None


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


def email_tool_allowed(decision: dict | None) -> bool:
    post_actions = (decision or {}).get("post_actions") or []
    return "send_email" in post_actions


def get_allowed_tools(decision: dict | None, local_execution: bool) -> list:
    route = (decision or {}).get("route", "agent")
    allow_email = email_tool_allowed(decision)
    if local_execution:
        tools = [
            ask_open_interpreter,
            start_open_interpreter_job,
            open_local_browser_page,
            inspect_local_webpage,
            start_local_webpage_monitor,
            request_human_confirmation,
        ]
        if allow_email:
            tools.insert(0, send_email)
        return tools

    complexity = (decision or {}).get("complexity", "standard")
    if route == "research":
        return [send_email] if allow_email else []
    if complexity == "simple":
        return [send_email] if allow_email else []
    if complexity == "advanced":
        tools = [
            ask_open_interpreter,
            start_open_interpreter_job,
            open_local_browser_page,
            inspect_local_webpage,
            start_local_webpage_monitor,
            request_human_confirmation,
        ]
        if allow_email:
            tools.insert(0, send_email)
        return tools
    tools = [request_human_confirmation]
    if allow_email:
        tools.insert(0, send_email)
    return tools


def get_allowed_tool_names(decision: dict | None, local_execution: bool) -> list[str]:
    return [tool.__name__ for tool in get_allowed_tools(decision, local_execution)]


async def emit_agent_event(event_type: str, **payload) -> None:
    await emit_runtime_event(event_type, payload)


def sanitize_final_reply(result: dict) -> str:
    awaiting_confirmation = result.get("awaiting_confirmation")
    pending_job = result.get("pending_job")
    final_message = result["messages"][-1]
    raw_reply = final_message.content if hasattr(final_message, "content") else str(final_message)
    if isinstance(raw_reply, str) and decode_meta_payload(raw_reply):
        if awaiting_confirmation:
            return f"需要你的确认后我再继续。\n\n{awaiting_confirmation.get('question', '')}"
        if pending_job:
            return f"已创建本地长任务：{pending_job.get('title', '本地长任务')}。任务编号：{pending_job.get('id', '')}"
    return remove_urls(raw_reply)


def collect_response_payload(result: dict, *, thread_id: str, new_thread: bool, include_tool_trace: bool) -> dict:
    tool_trace_entries = (result.get("tool_trace") or []) + build_tool_trace(result["messages"])
    reply = sanitize_final_reply(result)
    response = {
        "reply": reply,
        "thread_id": thread_id,
        "planner_decision": public_planner_decision(result.get("planner_decision", {})),
        "awaiting_confirmation": result.get("awaiting_confirmation"),
        "pending_job": result.get("pending_job"),
    }
    if include_tool_trace:
        response["tool_trace"] = tool_trace_entries
    if new_thread:
        response["new_thread"] = True
    return response


async def process_graph_run(
    *,
    langchain_messages: list,
    config: dict,
    current_user: dict,
    thread_id: str,
    include_tool_trace: bool,
    new_thread: bool,
    event_async_emitter=None,
    event_sync_emitter=None,
) -> dict:
    context = {
        "thread_id": thread_id,
        "user_id": current_user["id"],
        "username": current_user["username"],
        "local_execution": bool(config.get("configurable", {}).get("local_execution")),
    }
    with bind_execution_context(context, async_emitter=event_async_emitter, sync_emitter=event_sync_emitter):
        result = await app.state.graph.ainvoke({"messages": langchain_messages}, config=config)

    response = collect_response_payload(
        result,
        thread_id=thread_id,
        new_thread=new_thread,
        include_tool_trace=include_tool_trace,
    )
    tool_trace_entries = response.get("tool_trace", [])
    logger.info(
        "Chat completed thread=%s user=%s route=%s complexity=%s tools=%s reply=%s",
        thread_id,
        current_user["username"],
        (result.get("planner_decision") or {}).get("route", "agent"),
        (result.get("planner_decision") or {}).get("complexity", "standard"),
        [entry.get("tool") for entry in tool_trace_entries],
        summarize_text(response["reply"], 120),
    )
    return response


async def planner_node(state: AgentState, config=None):
    request_context = get_request_context(config)
    local_execution = request_context["local_execution"]
    user_text = get_latest_user_text(state["messages"])
    today = datetime.now().strftime("%Y-%m-%d")
    signals = collect_dispatch_signals(user_text, local_execution=local_execution)
    await emit_agent_event("planner_started", message="正在规划", user_text=user_text, local_execution=local_execution)

    if not user_text:
        decision = validate_dispatch_decision(PlannerDecision(), signals, user_text, local_execution=local_execution)
        return {
            "planner_decision": decision,
            "dispatch_signals": signals,
            "tool_trace": [],
            "research_result": "",
        }

    if should_prefer_local_execution(user_text, config):
        decision = validate_dispatch_decision(
            {
                "route": "agent",
                "reason": "请求涉及本地执行或电脑操作，优先进入执行流程。",
                "complexity": "advanced",
                "search_query": "",
                "answer_mode": "tool_agent",
                "post_actions": infer_post_actions(user_text),
            },
            signals,
            user_text,
            local_execution=True,
        )
        return {
            "planner_decision": decision,
            "dispatch_signals": signals,
            "tool_trace": [],
            "research_result": "",
        }

    dispatcher_prompt = build_dispatcher_prompt(user_text, today, signals, local_execution=local_execution)
    try:
        llm = get_llm(DISPATCHER_MODEL).with_structured_output(PlannerDecision)
        raw_decision = await llm.ainvoke(dispatcher_prompt)
    except Exception as exc:
        logger.warning("Dispatcher model %s failed, using heuristic fallback: %s", DISPATCHER_MODEL, exc)
        raw_decision = build_heuristic_planner_decision(user_text, local_execution=local_execution)

    raw_decision_payload = raw_decision.model_dump() if isinstance(raw_decision, PlannerDecision) else dict(raw_decision)
    decision = validate_dispatch_decision(raw_decision, signals, user_text, local_execution=local_execution)
    logger.info(
        "Dispatcher thread=%s user=%s local_execution=%s signals=%s raw_decision=%s validated_decision=%s",
        request_context["thread_id"],
        request_context["username"],
        local_execution,
        signals,
        raw_decision_payload,
        decision,
    )
    await emit_agent_event(
        "planner_finished",
        planner_decision=public_planner_decision(decision),
        dispatch_signals=signals,
    )
    return {
        "planner_decision": decision,
        "dispatch_signals": signals,
        "tool_trace": [],
        "research_result": "",
    }


def route_after_planner(state: AgentState):
    decision = state.get("planner_decision") or {}
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
    await emit_agent_event(
        "research_started",
        model=ONLINE_RESEARCH_MODEL,
        query=query,
        title="联网思考",
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
    await emit_agent_event(
        "research_finished",
        model=ONLINE_RESEARCH_MODEL,
        query=query,
        summary=summarize_text(answer, 180),
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
    allowed_tools = get_allowed_tools(decision, local_execution)
    allowed_tool_names = [tool.__name__ for tool in allowed_tools]

    llm = get_llm(model_name)
    llm_with_tools = llm.bind_tools(allowed_tools) if allowed_tools else llm

    system_prompt = build_runtime_system_prompt()
    system_prompt += (
        "\n\nTool policy:\n"
        "- Respect the dispatcher's single-executor decision for this run.\n"
        "- If the dispatcher selected agent, do not try to switch back to online_research in the middle of execution.\n"
        "- If the dispatcher selected research, answer from grounded research and only do explicit post-actions such as send_email when allowed.\n"
        "- Use ask_open_interpreter only when code execution or local computer actions are actually needed.\n"
        "- Use open_local_browser_page when the main task is simply to open a webpage locally in Edge.\n"
        "- Use inspect_local_webpage when you need a local logged-in webpage snapshot before deciding next actions.\n"
        "- Use start_local_webpage_monitor for longer local webpage observation tasks such as watching for specific visible text.\n"
        "- Pass runnable code directly to ask_open_interpreter, not natural-language instructions.\n"
        "- For side-effect actions such as opening apps, opening a browser, writing files, or launching programs, make the code print a short Chinese success message after the action completes.\n"
        "- Do not return raw booleans like True or False when a clearer execution message can be printed.\n"
        "- Use send_email only when the user explicitly asks to send an email and a recipient is available.\n"
        f"- Allowed tools for this run: {', '.join(allowed_tool_names) or 'none'}.\n"
        f"- The dispatcher selected complexity={decision.get('complexity', 'standard')} and executor model={model_name}. Respect that execution level.\n"
    )
    if local_execution or is_local_execution_intent(user_text):
        system_prompt += (
            "\n\nLocal execution policy:\n"
            "- The user is asking to operate their local computer or run local code.\n"
            "- Strongly prefer ask_open_interpreter for these tasks instead of answering abstractly.\n"
            "- For webpage tasks on the local machine, prefer open_local_browser_page, inspect_local_webpage, or start_local_webpage_monitor before falling back to generic code execution.\n"
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
        "Agent start thread=%s user=%s model=%s route=%s complexity=%s local_execution=%s allowed_tools=%s",
        request_context["thread_id"],
        request_context["username"],
        model_name,
        decision.get("route", "agent"),
        decision.get("complexity", "standard"),
        local_execution,
        allowed_tool_names,
    )
    await emit_agent_event(
        "agent_started",
        model=model_name,
        route=decision.get("route", "agent"),
        complexity=decision.get("complexity", "standard"),
        allowed_tools=allowed_tool_names,
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
        await emit_agent_event(
            "agent_tool_plan",
            model=model_name,
            tools=[call.get("name", "") for call in tool_calls],
        )
    else:
        logger.info(
            "Agent completed without tool call thread=%s user=%s",
            request_context["thread_id"],
            request_context["username"],
        )
        await emit_agent_event("agent_finished", model=model_name, summary=summarize_text(response.content if hasattr(response, "content") else str(response), 160))
    return {"messages": [response]}


def should_continue_agent(state: AgentState):
    last_message = state["messages"][-1]
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "tools"
    return "__end__"


async def tools_node(state: AgentState, config=None):
    request_context = get_request_context(config)
    local_execution = request_context["local_execution"]
    decision = state.get("planner_decision") or {}
    allowed_tools = get_allowed_tools(decision, local_execution)
    allowed_tool_names = [tool.__name__ for tool in allowed_tools]
    last_message = state["messages"][-1]
    requested_tools = [call.get("name", "") for call in (getattr(last_message, "tool_calls", None) or [])]

    logger.info(
        "Tools node thread=%s user=%s requested=%s allowed=%s",
        request_context["thread_id"],
        request_context["username"],
        requested_tools,
        allowed_tool_names,
    )
    for name in requested_tools:
        await emit_agent_event("tool_started", tool=name, title=default_tool_title(name))

    unauthorized = [name for name in requested_tools if name and name not in allowed_tool_names]
    if unauthorized:
        logger.warning(
            "Blocked unauthorized tools thread=%s user=%s unauthorized=%s allowed=%s",
            request_context["thread_id"],
            request_context["username"],
            unauthorized,
            allowed_tool_names,
        )
        unauthorized_text = ", ".join(unauthorized)
        unauthorized_tool_messages = []
        for call in (getattr(last_message, "tool_calls", None) or []):
            name = call.get("name", "")
            if not name or name not in unauthorized:
                continue
            unauthorized_tool_messages.append(
                ToolMessage(
                    content=(
                        f"Tool call blocked: {name}.\n"
                        f"This run does not allow that tool. Allowed tools: {', '.join(allowed_tool_names) or 'none'}."
                    ),
                    tool_call_id=call.get("id", ""),
                    name=name,
                )
            )
        return {
            "messages": unauthorized_tool_messages,
            "tool_trace": append_tool_trace(
                state.get("tool_trace"),
                [
                    make_trace_entry(
                        "tool_guard",
                        f"Blocked unauthorized tool calls: {unauthorized_text}\nAllowed tools: {', '.join(allowed_tool_names) or 'none'}",
                        title="Tool Guard",
                        phase="tool_guard",
                        status="error",
                        summary=f"Blocked unauthorized tools: {unauthorized_text}",
                    )
                ],
            ),
        }

    result = await ToolNode(allowed_tools).ainvoke(state, config=config)
    new_messages = result.get("messages") or []
    extra_trace_entries = []
    awaiting_confirmation = None
    pending_job = None

    for message in new_messages:
        tool_name = getattr(message, "name", "") or "tool"
        content = str(getattr(message, "content", "") or "")
        meta = decode_meta_payload(content)
        status = "error" if any(token in content.lower() for token in ["failed", "error", "not configured"]) else "ok"

        if meta and meta.get("kind") == "confirmation_request":
            awaiting_confirmation = meta.get("confirmation")
            extra_trace_entries.append(
                make_trace_entry(
                    tool_name,
                    f"确认问题：{awaiting_confirmation.get('question', '')}\n补充上下文：{awaiting_confirmation.get('context', '')}",
                    title="等待人工确认",
                    phase="human_loop",
                    status="pending",
                    summary=awaiting_confirmation.get("question", "等待人工确认"),
                )
            )
            await emit_agent_event("awaiting_confirmation", confirmation=awaiting_confirmation)
            await emit_agent_event("tool_finished", tool=tool_name, title="等待人工确认", status="pending")
            continue

        if meta and meta.get("kind") == "job_created":
            pending_job = meta.get("job")
            extra_trace_entries.append(
                make_trace_entry(
                    tool_name,
                    f"任务标题：{pending_job.get('title', '')}\n任务编号：{pending_job.get('id', '')}",
                    title="本地长任务",
                    phase="local_job",
                    status="pending",
                    summary=f"已创建本地长任务：{pending_job.get('title', '')}",
                )
            )
            await emit_agent_event("job_created", job=pending_job)
            await emit_agent_event("tool_finished", tool=tool_name, title="本地长任务", status="pending")
            continue

        await emit_agent_event(
            "tool_finished" if status == "ok" else "tool_error",
            tool=tool_name,
            title=default_tool_title(tool_name),
            status=status,
            summary=summarize_text(content, 160),
        )

    tool_trace = state.get("tool_trace")
    if extra_trace_entries:
        tool_trace = append_tool_trace(tool_trace, extra_trace_entries)

    return {
        **result,
        "tool_trace": tool_trace,
        "awaiting_confirmation": awaiting_confirmation,
        "pending_job": pending_job,
    }


def route_after_tools(state: AgentState):
    if state.get("awaiting_confirmation") or state.get("pending_job"):
        return "__end__"
    return "agent"


def build_agent_graph(checkpointer):
    from langgraph.graph import StateGraph

    workflow = StateGraph(AgentState)
    workflow.add_node("planner", planner_node)
    workflow.add_node("online_research", online_research_node)
    workflow.add_node("agent", agent_node)
    workflow.add_node("tools", tools_node)

    workflow.set_entry_point("planner")
    workflow.add_conditional_edges("planner", route_after_planner, {"research": "online_research", "agent": "agent"})
    workflow.add_conditional_edges("online_research", route_after_online_research, {"agent": "agent", "__end__": "__end__"})
    workflow.add_conditional_edges("agent", should_continue_agent, {"tools": "tools", "__end__": "__end__"})
    workflow.add_conditional_edges("tools", route_after_tools, {"agent": "agent", "__end__": "__end__"})
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
        "browser_bridge_configured": browser_bridge_is_configured(),
    }


def build_sse_event(event_type: str, payload: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def stream_graph_run(
    *,
    langchain_messages: list,
    config: dict,
    current_user: dict,
    thread_id: str,
    include_tool_trace: bool,
    new_thread: bool,
):
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()
    finished = asyncio.Event()

    async def async_emitter(event_type: str, payload: dict):
        await queue.put({"type": event_type, "payload": payload})

    def sync_emitter(event_type: str, payload: dict):
        loop.call_soon_threadsafe(queue.put_nowait, {"type": event_type, "payload": payload})

    async def runner():
        try:
            response = await process_graph_run(
                langchain_messages=langchain_messages,
                config=config,
                current_user=current_user,
                thread_id=thread_id,
                include_tool_trace=include_tool_trace,
                new_thread=new_thread,
                event_async_emitter=async_emitter,
                event_sync_emitter=sync_emitter,
            )
            await queue.put({"type": "final_answer", "payload": response})
            await queue.put({"type": "run_completed", "payload": {"thread_id": thread_id}})
        except RuntimeError as exc:
            await queue.put({"type": "run_failed", "payload": {"detail": str(exc), "status": 503}})
        except Exception as exc:
            logger.exception("Unhandled error while processing stream request.")
            await queue.put({"type": "run_failed", "payload": {"detail": str(exc), "status": 500}})
        finally:
            finished.set()

    task = asyncio.create_task(runner())

    async def event_generator():
        try:
            while True:
                if finished.is_set() and queue.empty():
                    break
                event = await queue.get()
                yield build_sse_event(event["type"], event["payload"])
        finally:
            task.cancel()

    return StreamingResponse(event_generator(), media_type="text/event-stream")


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
        response = await process_graph_run(
            langchain_messages=langchain_messages,
            config=config,
            current_user=current_user,
            thread_id=thread_id,
            include_tool_trace=payload.include_tool_trace,
            new_thread=new_thread,
        )
        return JSONResponse(content=response)
    except RuntimeError as exc:
        logger.exception("Configuration error while handling request.")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Unhandled error while processing request.")
        raise HTTPException(status_code=500, detail="Internal server error.") from exc


@app.post("/api/chat/stream")
@limiter.limit("10 per minute")
async def chat_stream(request: Request):
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

    return await stream_graph_run(
        langchain_messages=langchain_messages,
        config=config,
        current_user=current_user,
        thread_id=thread_id,
        include_tool_trace=payload.include_tool_trace,
        new_thread=new_thread,
    )


@app.post("/api/chat/confirm")
@limiter.limit("10 per minute")
async def chat_confirm(request: Request):
    current_user = require_authenticated_user(request)
    data = await parse_json_body(request)
    payload = parse_payload_model(data, ConfirmationPayload, "Invalid confirmation payload.")

    confirmation = confirmation_store.get(payload.confirmation_id)
    if not confirmation:
        raise HTTPException(status_code=404, detail="Confirmation request not found.")
    if confirmation["thread_id"] != payload.thread_id:
        raise HTTPException(status_code=400, detail="Confirmation request thread mismatch.")
    if confirmation["user_id"] and int(confirmation["user_id"]) != int(current_user["id"]):
        raise HTTPException(status_code=403, detail="You cannot resolve this confirmation request.")

    confirmation_store.resolve(
        payload.confirmation_id,
        approved=payload.approved,
        answer=payload.response_text,
    )

    confirmation_text = "已确认继续。" if payload.approved else "已拒绝继续。"
    if payload.response_text.strip():
        confirmation_text += f" 用户补充：{payload.response_text.strip()}"

    config = build_graph_config(payload.thread_id, current_user, payload.local_execution or confirmation["local_execution"])
    langchain_messages = convert_to_langchain([{"role": "user", "content": confirmation_text}])
    response = await process_graph_run(
        langchain_messages=langchain_messages,
        config=config,
        current_user=current_user,
        thread_id=payload.thread_id,
        include_tool_trace=payload.include_tool_trace,
        new_thread=False,
    )
    return JSONResponse(content=response)


@app.post("/api/chat/confirm/stream")
@limiter.limit("10 per minute")
async def chat_confirm_stream(request: Request):
    current_user = require_authenticated_user(request)
    data = await parse_json_body(request)
    payload = parse_payload_model(data, ConfirmationPayload, "Invalid confirmation payload.")

    confirmation = confirmation_store.get(payload.confirmation_id)
    if not confirmation:
        raise HTTPException(status_code=404, detail="Confirmation request not found.")
    if confirmation["thread_id"] != payload.thread_id:
        raise HTTPException(status_code=400, detail="Confirmation request thread mismatch.")
    if confirmation["user_id"] and int(confirmation["user_id"]) != int(current_user["id"]):
        raise HTTPException(status_code=403, detail="You cannot resolve this confirmation request.")

    confirmation_store.resolve(
        payload.confirmation_id,
        approved=payload.approved,
        answer=payload.response_text,
    )

    confirmation_text = "已确认继续。" if payload.approved else "已拒绝继续。"
    if payload.response_text.strip():
        confirmation_text += f" 用户补充：{payload.response_text.strip()}"

    config = build_graph_config(payload.thread_id, current_user, payload.local_execution or confirmation["local_execution"])
    langchain_messages = convert_to_langchain([{"role": "user", "content": confirmation_text}])
    return await stream_graph_run(
        langchain_messages=langchain_messages,
        config=config,
        current_user=current_user,
        thread_id=payload.thread_id,
        include_tool_trace=payload.include_tool_trace,
        new_thread=False,
    )


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str, request: Request):
    current_user = require_authenticated_user(request)
    job = local_job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job["user_id"] and int(job["user_id"]) != int(current_user["id"]):
        raise HTTPException(status_code=403, detail="You cannot access this job.")
    return job


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, request: Request):
    current_user = require_authenticated_user(request)
    job = local_job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job["user_id"] and int(job["user_id"]) != int(current_user["id"]):
        raise HTTPException(status_code=403, detail="You cannot cancel this job.")
    updated = local_job_store.cancel(job_id)
    return {"ok": True, "job": updated}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=APP_HOST, port=APP_PORT)
