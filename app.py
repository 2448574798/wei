import os
import json
import re
import uuid
import logging
from logging.handlers import RotatingFileHandler
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from langgraph.graph import StateGraph, MessagesState
from langgraph.checkpoint.redis.aio import AsyncRedisSaver
from langgraph.prebuilt import ToolNode
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage

from tools import web_search, fetch_webpage, send_email

# ---------- 日志 ----------
handler = RotatingFileHandler('/var/log/wei_agent.log', maxBytes=10*1024*1024, backupCount=5)
handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
handler.setLevel(logging.INFO)
logger = logging.getLogger("wei_agent")
logger.addHandler(handler)
logger.setLevel(logging.INFO)

# ---------- 环境变量 ----------
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
ONE_API_URL = os.getenv("ONE_API_URL", "http://127.0.0.1:3000/v1")
ONE_API_TOKEN = os.getenv("ONE_API_TOKEN")
if not ONE_API_TOKEN:
    logger.error("ONE_API_TOKEN 未设置")
    raise RuntimeError("ONE_API_TOKEN 未设置")

# ---------- 限流器 ----------
limiter = Limiter(key_func=get_remote_address, storage_uri="memory://")

# ---------- LLM 缓存 ----------
_llm_cache = {}
def get_llm(model_name: str):
    key = (model_name, ONE_API_URL, 0.2)
    if key not in _llm_cache:
        _llm_cache[key] = ChatOpenAI(
            model=model_name,
            temperature=0.2,
            openai_api_key=ONE_API_TOKEN,
            openai_api_base=ONE_API_URL,
        )
    return _llm_cache[key]

# ---------- 系统提示词 ----------
SYSTEM_PROMPT_TEXT = "你是一个智能助手。"
try:
    with open('/opt/wei/config/system_prompt.txt', 'r', encoding='utf-8') as f:
        SYSTEM_PROMPT_TEXT = f.read()
except FileNotFoundError:
    pass

# ---------- 消息转换 ----------
def convert_to_langchain(messages):
    msgs = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content", "")
        if role == "system":
            msgs.append(SystemMessage(content=content))
        elif role == "user":
            msgs.append(HumanMessage(content=content))
        elif role == "assistant":
            msgs.append(AIMessage(content=content))
        elif role == "tool":
            msgs.append(ToolMessage(content=content, tool_call_id=msg.get("tool_call_id", "")))
    return msgs

def remove_urls(text):
    return re.sub(r'https?://\S+', '', text)

# ---------- 工作流节点 ----------
async def call_model(state: MessagesState, config=None):
    model_name = "gpt-4o"
    if config and "configurable" in config:
        model_name = config["configurable"].get("model", model_name)
    llm = get_llm(model_name)
    llm_with_tools = llm.bind_tools([web_search, fetch_webpage, send_email])
    system_msg = SystemMessage(content=SYSTEM_PROMPT_TEXT)
    messages = state["messages"]
    if not messages or not isinstance(messages[0], SystemMessage):
        messages = [system_msg] + messages
    else:
        messages = [system_msg] + messages[1:]
    response = await llm_with_tools.ainvoke(messages)
    return {"messages": [response]}

def should_continue(state: MessagesState):
    last_message = state["messages"][-1]
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "tools"
    return "__end__"

# ---------- 生命周期：使用 AsyncRedisSaver ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    async with AsyncRedisSaver.from_conn_string(REDIS_URL) as checkpointer:
        workflow = StateGraph(MessagesState)
        workflow.add_node("agent", call_model)
        workflow.add_node("tools", ToolNode([web_search, fetch_webpage, send_email]))
        workflow.set_entry_point("agent")
        workflow.add_conditional_edges("agent", should_continue, {"tools": "tools", "__end__": "__end__"})
        workflow.add_edge("tools", "agent")
        graph = workflow.compile(checkpointer=checkpointer)
        app.state.graph = graph
        yield

# ---------- FastAPI 应用 ----------
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

@app.post('/api/chat')
@limiter.limit("10 per minute")
async def chat(request: Request):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="无效的请求数据")
    if not data:
        raise HTTPException(status_code=400, detail="请求数据为空")
    messages = data.get('messages', [])
    if not messages:
        raise HTTPException(status_code=400, detail="消息不能为空")
    if len(messages) > 50:
        raise HTTPException(status_code=400, detail="消息条数过多")
    for msg in messages:
        if len(msg.get('content', '')) > 2000:
            raise HTTPException(status_code=400, detail="单条消息过长")
    thread_id = data.get('thread_id')
    new_thread = False
    if not thread_id:
        thread_id = str(uuid.uuid4())
        new_thread = True
    model = data.get('model', 'gpt-4o')
    config = {"configurable": {"thread_id": thread_id, "model": model}}
    try:
        langchain_messages = convert_to_langchain(messages)
    except Exception:
        raise HTTPException(status_code=400, detail="消息格式错误")
    try:
        result = await app.state.graph.ainvoke({"messages": langchain_messages}, config=config)
        final_message = result["messages"][-1]
        reply = final_message.content if hasattr(final_message, "content") else str(final_message)
        reply = remove_urls(reply)
        resp = {"reply": reply, "thread_id": thread_id}
        if new_thread:
            resp["new_thread"] = True
        return JSONResponse(content=resp)
    except Exception:
        logger.exception("处理请求异常")
        raise HTTPException(status_code=500, detail="服务器内部错误")

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=8000)
