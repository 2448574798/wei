# Wei Agent

`Wei Agent` 是一个基于 `FastAPI + LangGraph` 的分层调度型智能助手。  
当前版本的核心目标不是“单模型直接回答”，而是：

- 先由调度员模型判断任务类型与复杂度
- 再自动选择更合适的执行模型
- 执行模型在受控权限下调用联网思考、本地执行、邮件等工具

## 核心工作模式

整体流程如下：

```text
用户请求
  -> dispatcher(gpt-4o-mini)
     -> research
        -> online_research 节点
        -> agent（可选）
     -> agent
        -> tools
        -> agent
```

说明：

- `dispatcher`
  - 负责判断：
    - 是否需要联网
    - 任务复杂度
    - 是否需要后续动作
- `online_research`
  - 负责最新信息、时效性信息的联网思考
- `agent`
  - 根据复杂度选择执行模型
  - 在权限控制下调用工具
- `tools`
  - 当前主要包括：
    - `online_research`
    - `ask_open_interpreter`
    - `send_email`

## 分层调度

默认模型分层如下：

- `DISPATCHER_MODEL=gpt-4o-mini`
- `EXECUTION_MODEL_SIMPLE=gpt-4o-mini`
- `EXECUTION_MODEL_STANDARD=gpt-4o`
- `EXECUTION_MODEL_ADVANCED=gpt-5.4`
- `LOCAL_EXECUTION_MODEL=gpt-5.4`
- `ONLINE_RESEARCH_MODEL=gpt-4o-mini-search-preview`

### 调度原则

- 简单任务
  - 进入 `simple`
  - 适合：轻量问答、改写、翻译、简要总结
- 常规任务
  - 进入 `standard`
  - 适合：普通分析、常规工具协作、时效性查询后的整理
- 复杂任务
  - 进入 `advanced`
  - 适合：复杂推理、代码生成、复合任务、本地执行

### 本地执行模式

如果满足以下任一条件，会优先走本地执行主导路线：

- 前端开启“本地执行模式”
- 用户请求中明显包含本地操作意图，例如：
  - 打开浏览器
  - 打开记事本
  - 在本地写文件
  - 运行本地代码

## 工具权限控制

当前按复杂度限制工具权限：

- `simple`
  - 允许：`send_email`
- `standard`
  - 允许：`online_research`、`send_email`
- `advanced`
  - 允许：`online_research`、`send_email`、`ask_open_interpreter`
- `local_execution=true`
  - 视为高级执行模式
  - 允许全部当前工具

这样做的目的是：

- 简单任务不误触发本地执行
- 普通任务不轻易操作本地电脑
- 只有复杂任务或明确本地执行任务，才开放 `ask_open_interpreter`

## 当前能力

当前系统支持：

- 普通问答
- 联网思考与时效性问题回答
- 发送邮件
- 调用本地 `Open Interpreter` 执行代码或操作本地电脑
- 登录鉴权
- 工具轨迹展示
- 更清晰的后端日志记录

## 工具轨迹与日志

当前后端会记录结构化工具轨迹，前端会展示：

- 工具标题
- 工具摘要
- 执行状态
- 使用的模型（如适用）
- 原始输出内容

后端日志会记录：

- 调度结果
- 执行模型选择
- 计划调用哪些工具
- 工具开始/结束
- 最终回复摘要

这有助于排查以下问题：

- 调度员是否判断正确
- 执行模型是否选对工具
- 工具是否执行失败
- 是不是被权限控制拦截

## 项目结构

```text
.
|-- src/
|   |-- app.py
|   |-- auth_store.py
|   |-- chat_helpers.py
|   |-- dispatching.py
|   |-- research_client.py
|   |-- runtime_config.py
|   |-- tools.py
|   `-- web_helpers.py
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

### 各模块职责

- `src/app.py`
  - FastAPI 路由
  - LangGraph 图节点
  - 主流程编排
- `src/runtime_config.py`
  - 环境变量
  - 日志
  - LLM 初始化
- `src/dispatching.py`
  - 调度规则
  - 复杂度判断
  - 本地执行意图识别
- `src/research_client.py`
  - 联网思考模型调用
- `src/tools.py`
  - 邮件工具
  - 本地 `Open Interpreter` 工具
  - agent 可调用的 `online_research` 工具封装
- `src/chat_helpers.py`
  - 消息转换
  - 文本清洗
  - 工具轨迹辅助
- `src/web_helpers.py`
  - 请求解析
  - 鉴权辅助
  - 会话辅助
- `src/auth_store.py`
  - SQLite 用户与会话存储

## 环境变量

先复制模板：

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

认证接口：

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

```bash
curl http://127.0.0.1:8000/health
```

重点查看：

- token 是否配置
- 分层模型是否正确
- 本地解释器是否已配置
- SMTP 是否已配置

Redis 可用性：

```bash
redis-cli ping
```

服务日志：

```bash
journalctl -u wei-agent -f
tail -f /opt/wei/logs/wei_agent.log
```

## 前端说明

前端静态资源位于 `static/`：

- `index.html`
  - 主页面
- `login.html`
  - 登录页
- `app.js`
  - 前端交互逻辑
- `styles.css`
  - 样式

前端当前会展示：

- 当前轮次的规划决策
- 工具执行轨迹
- 回复内容
- 本地执行模式开关

## 登录与后续扩展

当前已具备基础登录能力：

- 访问 `/login.html` 可登录
- 未登录访问主页面会被重定向到登录页
- `/api/chat` 需要登录后访问

如果后续要扩展注册与用户级 `FRP / Open Interpreter` 绑定，可以沿现有用户表继续扩展：

- 用户注册流程
- 每个用户独立的 `frp_client_name`
- 每个用户独立的 `frp_remote_port`
- 每个用户独立的 `open_interpreter_url`
- 与 `frps API` 的联动解析
