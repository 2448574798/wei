import os
import json
import logging
from logging.handlers import RotatingFileHandler
from flask import Flask, request, jsonify
from flask_cors import CORS
from langgraph.graph import StateGraph, MessagesState
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import ToolNode
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage
from tools import web_search, fetch_webpage
import uuid

app = Flask(__name__)
CORS(app)

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

# ---------- LangGraph 节点 ----------
def call_model(state: MessagesState, config=None):
    # 获取模型名称
    model_name = "gpt-5.4"
    if config and "configurable" in config:
        model_name = config["configurable"].get("model", model_name)
    llm = ChatOpenAI(
        model=model_name,
        temperature=0.2,
        openai_api_key=ONE_API_TOKEN,
        openai_api_base=ONE_API_URL,
    )
    llm_with_tools = llm.bind_tools([web_search, fetch_webpage])
    system_msg = SystemMessage(
        content="你是一个智能助手，可以使用搜索和抓取工具获取实时信息。回答要准确。禁止输出任何URL链接，不要显示http或https地址。"
    )
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

# ---------- 构建工作流 ----------
workflow = StateGraph(MessagesState)
workflow.add_node("agent", call_model)
workflow.add_node("tools", ToolNode([web_search, fetch_webpage]))
workflow.set_entry_point("agent")
workflow.add_conditional_edges("agent", should_continue, {"tools": "tools", "__end__": "__end__"})
workflow.add_edge("tools", "agent")

checkpointer = MemorySaver()
graph = workflow.compile(checkpointer=checkpointer)

# ---------- Flask 路由 ----------
@app.route('/api/chat', methods=['POST'])
def chat():
    # 1. 安全获取 JSON 数据，并判空
    data = request.get_json(silent=True)
    if not data:
        app.logger.warning("收到空请求或无效 JSON")
        return jsonify({"error": "无效的请求数据"}), 400

    messages = data.get('messages', [])
    if not messages:
        return jsonify({"error": "消息不能为空"}), 400

    # 2. thread_id 不要使用固定默认值，建议前端传递或生成 UUID
    thread_id = data.get('thread_id')
    if not thread_id:
        thread_id = str(uuid.uuid4())   # 自动生成唯一会话 ID

    model = data.get('model', 'gpt-5.4')
    config = {"configurable": {"thread_id": thread_id, "model": model}}

    try:
        result = graph.invoke({"messages": messages}, config=config)
        final_message = result["messages"][-1]
        reply = final_message.content if hasattr(final_message, "content") else str(final_message)
        return jsonify({"reply": reply})
    except Exception as e:
        # 3. 使用 logger.exception 记录完整堆栈
        app.logger.exception(f"处理请求时发生异常: {e}")
        # 4. 前端不要直接看到内部异常，返回通用错误信息
        return jsonify({"error": "服务器内部错误，请稍后再试"}), 500

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000)
