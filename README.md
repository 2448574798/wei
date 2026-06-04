# Wei Agent

`Wei Agent` 是一个基于 `FastAPI + LangGraph` 的智能助手服务，当前采用“先规划、再执行”的工作流。

## 功能概览

- `planner` 默认使用低成本模型（如 `gpt-4o-mini`）先判断请求类型
- 遇到时效性、最新信息、联网核实类问题时，会切到“思考”路径
- “思考”路径使用支持联网搜索的模型完成信息获取与整理
- 在需要时，可以继续执行后续动作，例如发送邮件

## 当前执行流程

整体流程如下：

```text
用户请求
  -> planner
     -> agent
     -> online_research -> agent（可选）
```

说明：

- `planner`：判断是直接执行，还是先联网思考
- `online_research`：调用支持联网的模型获取最新信息
- `agent`：负责常规回答，以及调用如 `send_email` 这类工具
- 如已配置 `OPEN_INTERPRETER_URL`，`agent` 还可以调用本地 Open Interpreter 服务执行代码

## 项目结构

```text
.
|-- src/
|   |-- app.py
|   `-- tools.py
|-- deploy/
|   |-- start_server.sh
|   |-- gunicorn.conf.py
|   `-- wei-agent.service
|-- config/
|   `-- system_prompt.txt
|-- static/
|   |-- index.html
|   |-- app.js
|   |-- styles.css
|   `-- favicon.svg
|-- .env.example
|-- requirements.txt
`-- README.md
```

## 环境变量

先复制一份环境变量模板：

```bash
cp .env.example .env
```

至少需要配置这些项目：

- `ONE_API_URL`
- `ONE_API_TOKEN`
- `REDIS_URL`
- `PLANNER_MODEL`
- `AGENT_MODEL`
- `ONLINE_RESEARCH_MODEL`
- `OPEN_INTERPRETER_URL`

如果需要发送邮件，还需要配置：

- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USER`
- `SMTP_PASSWORD`
- `SMTP_FROM`

可选的 Open Interpreter 相关配置：

- `OPEN_INTERPRETER_AUTH_KEY`
- `OPEN_INTERPRETER_TIMEOUT`

## 本地启动

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
./deploy/start_server.sh
```

健康检查：

```bash
curl http://127.0.0.1:8000/health
```

## 接口说明

核心接口：

- `POST /api/chat`

可选请求字段：

- `model`：主执行模型
- `planner_model`：覆盖规划模型
- `online_research_model`：覆盖联网思考模型
- `include_tool_trace`：返回工具执行摘要

## 部署

`deploy/start_server.sh` 是统一启动入口，手动启动和 `systemd` 都走这一份脚本。

```bash
chmod +x /opt/wei/deploy/start_server.sh
sudo cp /opt/wei/deploy/wei-agent.service /etc/systemd/system/wei-agent.service
sudo systemctl daemon-reload
sudo systemctl enable wei-agent
sudo systemctl restart wei-agent
sudo systemctl status wei-agent
```

## 排查建议

- `curl http://127.0.0.1:8000/health`
  检查 `one_api_token_configured`、`smtp_configured` 以及当前模型配置
- `redis-cli ping`
  检查 Redis 是否可用
- `journalctl -u wei-agent -f`
  实时查看服务日志

## 前端说明

前端静态资源位于 `static/`：

- `index.html`：页面结构
- `app.js`：交互逻辑
- `styles.css`：样式文件

前端会展示：

- 本轮规划结果
- 工具执行轨迹
- 回复内容

其中用户可见文案已统一使用“思考”表达，不直接暴露内部 `research` 路由名。
