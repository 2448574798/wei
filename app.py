
import os
import json
import re
import uuid
import logging
from logging.handlers import RotatingFileHandler
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from langgraph.graph import StateGraph, MessagesState
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import ToolNode
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage
from tools import web_search, fetch_webpage, send_email

app = Flask(__name__)
CORS(app)

# ---------- 限流配置 ----------
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://",
)

# ---------- 日志配置 ----------
if not app.debug:
    handler = RotatingFileHandler('flask_langgraph.log', maxBytes=10*1024*1024, backupCount=5)
    handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    handler.setLevel(logging.INFO)
    app.logger.addHandler(handler)
    app.logger.setLevel(logging.INFO)

# ---------- 环境变量 ----------
ONE_API_URL = os.getenv("ONE_API_URL", "http://127.0.0.1:3000/v1")
ONE_API_TOKEN = os.getenv("ONE_API_TOKEN")
if not ONE_API_TOKEN:
    app.logger.error("ONE_API_TOKEN 环境变量未设置")
    raise RuntimeError("环境变量 ONE_API_TOKEN 未设置")

# ---------- 全局缓存 ----------
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

# ---------- 系统提示词（启动时加载）----------
SYSTEM_PROMPT_TEXT = "你是一个智能助手。"
try:
    with open('/opt/wei/config/system_prompt.txt', 'r', encoding='utf-8') as f:
        SYSTEM_PROMPT_TEXT = f.read()
except FileNotFoundError:
    pass

# ---------- 消息格式转换 ----------
def convert_to_langchain(messages):
    langchain_msgs = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content", "")
        if role == "system":
            langchain_msgs.append(SystemMessage(content=content))
        elif role == "user":
            langchain_msgs.append(HumanMessage(content=content))
        elif role == "assistant":
            langchain_msgs.append(AIMessage(content=content))
        elif role == "tool":
            langchain_msgs.append(ToolMessage(content=content, tool_call_id=msg.get("tool_call_id", "")))
    return langchain_msgs

def remove_urls(text):
    return re.sub(r'https?://\S+', '', text)

# ---------- LangGraph 节点 ----------
def call_model(state: MessagesState, config=None):
    model_name = "gpt-5.4"
    if config and "configurable" in config:
        model_name = config["configurable"].get("model", model_name)
    llm = get_llm(model_name)
    llm_with_tools = llm.bind_tools([web_search, fetch_webpage, send_email])
    system_msg = SystemMessage(content=SYSTEM_PROMPT_TEXT)
    messages = state["messages"]
    if not messages or not isinstance(messages[0], SystemMessage):
        messages = [system_msg] + messages
    response = llm_with_tools.invoke(messages)
    return {"messages": [response]}

def should_continue(state: MessagesState):
    last_message = state["messages"][-1]
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "tools"
    return "__end__"

# ---------- 工作流 ----------
workflow = StateGraph(MessagesState)
workflow.add_node("agent", call_model)
workflow.add_node("tools", ToolNode([web_search, fetch_webpage, send_email]))
workflow.set_entry_point("agent")
workflow.add_conditional_edges("agent", should_continue, {"tools": "tools", "__end__": "__end__"})
workflow.add_edge("tools", "agent")
checkpointer = MemorySaver()
graph = workflow.compile(checkpointer=checkpointer)

# ---------- 路由 ----------
@app.route('/api/chat', methods=['POST'])
@limiter.limit("10 per minute")
def chat():
    data = request.get_json(silent=True)
    if not data:
        app.logger.warning("收到空请求或无效 JSON")
        return jsonify({"error": "无效的请求数据"}), 400
    messages = data.get('messages', [])
    if not messages:
        return jsonify({"error": "消息不能为空"}), 400
    if len(messages) > 50:
        return jsonify({"error": "消息条数过多，最多支持 50 条"}), 400
    for msg in messages:
        if len(msg.get('content', '')) > 2000:
            return jsonify({"error": "单条消息内容过长，最多 2000 字符"}), 400
    thread_id = data.get('thread_id')
    if not thread_id:
        thread_id = str(uuid.uuid4())
        new_thread = True
    else:
        new_thread = False
    model = data.get('model', 'gpt-5.4')
    config = {"configurable": {"thread_id": thread_id, "model": model}}
    try:
        langchain_messages = convert_to_langchain(messages)
    except Exception as e:
        app.logger.exception("消息格式转换失败")
        return jsonify({"error": "消息格式错误"}), 400
    try:
        result = graph.invoke({"messages": langchain_messages}, config=config)
        final_message = result["messages"][-1]
        reply = final_message.content if hasattr(final_message, "content") else str(final_message)
        reply = remove_urls(reply)
        response_data = {"reply": reply, "thread_id": thread_id}
        if new_thread:
            response_data["new_thread"] = True
        return jsonify(response_data)
    except Exception as e:
        app.logger.exception(f"处理请求时发生异常: {e}")
        return jsonify({"error": "服务器内部错误，请稍后再试"}), 500

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=8000)
