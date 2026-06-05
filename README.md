# Wei Agent

`Wei Agent` 是一个基于 `FastAPI + LangGraph` 的智能助手服务，当前采用“调度员先判断，再由合适模型执行”的工作流。

## 核心流程

整体流程如下：

```text
用户请求
  -> dispatcher(gpt-4o-mini)
     -> agent
     -> online_research -> agent（可选）
```

说明：

- `dispatcher`：负责判断是否需要联网、任务复杂度、是否有后续动作。
- `agent`：根据复杂度自动选择合适的执行模型，并负责工具调用。
- `online_research`：调用支持联网搜索的模型获取最新信息。
- 本地执行类任务会优先进入 `agent + ask_open_interpreter` 路线。

## 分层调度

默认使用以下模型分层：

- `DISPATCHER_MODEL=gpt-4o-mini`
- `EXECUTION_MODEL_SIMPLE=gpt-4o-mini`
- `EXECUTION_MODEL_STANDARD=gpt-4o`
- `EXECUTION_MODEL_ADVANCED=gpt-5.4`
- `LOCAL_EXECUTION_MODEL=gpt-5.4`
- `ONLINE_RESEARCH_MODEL=gpt-4o-mini-search-preview`

前后端都不再提供手动模型选择功能，统一由调度员自动分配。

## 当前能力

- 普通问答
- 联网思考与时效性问题回答
- 发送邮件
- 调用本地 `Open Interpreter` 执行代码或本地电脑操作
- 登录鉴权

## 项目结构

```text
.
|-- src/
|   |-- app.py
|   |-- auth_store.py
|   `-- tools.py
|-- deploy/
|   |-- start_server.sh
|   |-- gunicorn.conf.py
|   `-- wei-agent.service
|-- config/
|   `-- system_prompt.txt
|-- static/
|   |-- index.html
|   |-- login.html
|   |-- app.js
|   |-- styles.css
|   `-- favicon.svg
|-- .env.example
|-- requirements.txt
`-- README.md
```

## 环境变量

先复制一份模板：

```bash
cp .env.example .env
```

至少需要配置：

- `ONE_API_URL`
- `ONE_API_TOKEN`
- `REDIS_URL`
- `DISPATCHER_MODEL`
- `EXECUTION_MODEL_SIMPLE`
- `EXECUTION_MODEL_STANDARD`
- `EXECUTION_MODEL_ADVANCED`
- `ONLINE_RESEARCH_MODEL`

如果需要本地执行：

- `OPEN_INTERPRETER_URL`
- `OPEN_INTERPRETER_AUTH_KEY`
- `OPEN_INTERPRETER_TIMEOUT`

如果需要邮件：

- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USER`
- `SMTP_PASSWORD`
- `SMTP_FROM`

如果需要登录：

- `AUTH_DB_PATH`
- `AUTH_ADMIN_USERNAME`
- `AUTH_ADMIN_PASSWORD`
- `AUTH_ADMIN_DISPLAY_NAME`
- `AUTH_COOKIE_NAME`
- `AUTH_COOKIE_SECURE`
- `AUTH_SESSION_TTL_DAYS`

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

当前主要请求字段：

- `messages`
- `thread_id`
- `include_tool_trace`
- `local_execution`

登录相关接口：

- `POST /api/auth/login`
- `POST /api/auth/logout`
- `GET /api/auth/me`

## 部署

统一使用：

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
  检查 token、模型分层、本地解释器和 SMTP 是否已配置。
- `redis-cli ping`
  检查 Redis 是否可用。
- `journalctl -u wei-agent -f`
  实时查看服务日志。

## 前端说明

前端静态资源位于 `static/`：

- `index.html`：主页面
- `login.html`：登录页
- `app.js`：交互逻辑
- `styles.css`：样式

前端会展示：

- 当前轮次的规划决策
- 工具执行轨迹
- 回复内容
- 本地执行模式开关

## 登录与后续扩展

当前版本已具备基础登录能力：

- 访问 `/login.html` 可登录
- 未登录访问主页面会被重定向到登录页
- `/api/chat` 需要登录后访问

后续如果要扩展注册与用户级 FRP / 本地解释器绑定，可以直接沿现有用户表增加：

- 用户注册流程
- 每个用户独立的 `frp_client_name`
- 每个用户独立的 `frp_remote_port`
- 每个用户独立的 `open_interpreter_url`
- 与 `frps API` 的联动解析
