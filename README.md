# Wei Agent

Wei Agent 是一个基于 `FastAPI + LangGraph` 的分层调度型智能助手。当前仓库按运行边界拆成云端和本地两部分：

- `cloud/`：云端 Agent、API、调度、视觉识别、任务状态、前端静态资源和部署脚本。
- `local_launcher/`：本地 Thin Browser Worker、Playwright MCP 客户端、本地 Edge/Chrome 操作入口。

目标架构：

```text
Cloud Wei Agent
  -> Cloud Browser Orchestrator / Task Core
  -> WebSocket long connection
  -> Local Thin Browser Worker
  -> Playwright MCP
  -> Local Edge / Chrome
```

## 边界原则

- 云端负责模型调用、任务规划、视觉识别、动作安全策略、点击前预检、动作后校验、Redis job 状态和前端展示。
- 本地只负责连接云端 WebSocket、调用 Playwright MCP、截图、坐标命中检测和执行单步动作。
- 本地 Worker 不保存业务状态，不做模型判断，不对外暴露 HTTP Browser Bridge。
- 不再依赖旧的 `18100/18000/7000` 端口、SearXNG 本地配置、frp 或其他内网穿透。
- 云端配置放在 `cloud/.env`；本地配置放在 `local_launcher/.env`；两者不要混用。

## 目录结构

```text
.
|-- cloud/
|   |-- src/                     # 云端 Python 包，包名仍为 src
|   |-- static/                  # 云端 Web 前端静态资源
|   |-- config/                  # 云端 system prompt 等配置
|   |-- deploy/                  # 云端 systemd/gunicorn/清理脚本
|   |-- tests/                   # 云端单元测试
|   |-- tools/                   # 云端维护/诊断工具
|   |-- .env.example             # 云端环境变量模板
|   `-- requirements.txt         # 云端 Python 依赖
|-- local_launcher/
|   |-- browser_bridge.py        # Local Thin Browser Worker + Playwright MCP client
|   |-- local_launcher.py        # 本地进程守护启动器
|   |-- start_local_services.bat # 本地服务启动入口
|   |-- launcher_config.example.json
|   |-- .env.example
|   `-- README.md
|-- scripts/
|   |-- check_project.bat
|   |-- check_project.ps1
|   |-- run_cloud_dev.bat
|   |-- run_cloud_dev.ps1
|   `-- start_local_worker.bat
|-- README.md
`-- .gitignore
```

## 云端能力

- 分层模型调度：`simple / standard / advanced`。
- 联网研究：通过在线研究模型处理时效性问题。
- Open Interpreter WebSocket：后续本地代码执行走 WS，不再走旧 HTTP URL。
- 浏览器 Worker 编排：云端通过 `/ws/browser-worker` 与本地 Worker 长连接通信。
- 截图视觉识别：云端视觉模型读取本地截图并生成下一步动作。
- 点击前预检：所有 click 必须先调用本地 `browser.hit_test`，本地只返回坐标下 DOM 摘要，云端判断是否明显点偏。
- 动作后校验：所有 click 执行后必须再次截图并校验是否达到 `expected_change`。
- Visual Trace：长任务记录截图摘要、模型决策、动作结果、校验结果和失败分类，方便复盘识别不准。
- 人工确认：验证码、登录、支付、发帖、破坏性动作等场景暂停等待用户确认。
- Redis 长任务：`/api/jobs/{job_id}` 查询进度、结果和 artifacts。

## 本地 Worker 能力

本地 Worker 当前只暴露这些 WebSocket 命令：

- `browser.tabs`
- `browser.navigate`
- `browser.snapshot`
- `browser.screenshot`
- `browser.hit_test`
- `browser.visual_action`

本地 Worker 不维护 `BrowserJob`、`_jobs`、`start_job`、`watch_text` 或 `interaction_watch` 这类业务状态；它只响应云端发来的单步 WebSocket 命令。

## 浏览器状态机

云端 Orchestrator 的浏览器循环：

```text
created
  -> worker_check
  -> take_screenshot
  -> vision_decide
  -> policy_check
  -> preflight
  -> execute_action
  -> verify_action
  -> continue / retry / manual_confirm / failed / completed
```

这些阶段会以 `state_transition` artifact 写入云端 Redis job。已有的 `screenshot`、`vision_decision`、`click_preflight`、`action_result`、`verification_screenshot`、`verification`、`verification_policy`、`failure`、`final` artifact 继续保留，用于排查每一步的细节。

每个视觉任务携带 `task_id`，每个动作携带 `action_id`，并贯穿模型决策、预检、执行、验证和失败记录。云端 Redis job 创建的视觉任务会使用 `job_id` 作为 `task_id`。

相关云端模块：

- `cloud/src/job_store.py`：云端 Redis job 状态、进度和 artifact 存储。
- `cloud/src/browser_visual_runner.py`：视觉浏览器状态机主循环。
- `cloud/src/browser_visual_model.py`：视觉模型 JSON 调用和动作后 verifier 调用。
- `cloud/src/browser_visual_worker.py`：云端到 Local Thin Worker 的单步命令封装。
- `cloud/src/browser_visual_trace.py`：页面诊断和 trace 展示辅助。

## 动作安全契约

- 视觉模型输出必须先通过云端强 schema 归一化，再进入状态机；本地 Worker 不解析模型结果。
- 所有 click 必须有 `expected_change`，否则在 `policy_check` 阶段拒绝。
- 所有 click 必须先执行 `browser.hit_test`，预检失败或不可用时不会执行点击。
- 所有 click 执行后必须截图并调用 verifier，即使 `BROWSER_VISION_VERIFY_ENABLED=false`。
- click 后验证不可用会标准化失败为 `verification_unavailable`。
- 失败标准字段是 `failure_type`，`category` 仅保留兼容。

常见 `failure_type`：

- `action_low_confidence`
- `action_missing_target`
- `action_missing_expected_change`
- `click_target_mismatch`
- `click_preflight_unavailable`
- `action_execution_failed`
- `verification_unavailable`
- `verification_expected_change_mismatch`
- `verification_retry_limit`
- `viewport_mismatch`
- `dpr_coordinate_mismatch`
- `overlay_blocking_target`
- `login_wall_blocking`
- `page_refreshed_unexpectedly`

## Worker 连接协议

- 本地 Worker 每次 WebSocket 连接建立后必须先发送 `register`，断线重连后会重新注册。
- 云端 command 会带 `request_id`，本地响应必须回传同一个 `request_id`。
- 浏览器动作会携带并回传 `action_id`。
- 本地 Worker 会发送 WebSocket `heartbeat`，云端回 `heartbeat_ack`。
- 云端 worker 列表会记录 `heartbeat_age_sec` 和 `stale`。
- Playwright MCP 传输层不可用时，本地 Worker 会自动 restart MCP 并重试一次工具调用。
- `browser.screenshot` 返回顶层 `viewport` 和 `device_pixel_ratio`，即使未开启详细 diagnostics。

## 云端配置

从仓库根目录复制模板：

```powershell
Copy-Item cloud\.env.example cloud\.env
```

Linux/macOS：

```bash
cp cloud/.env.example cloud/.env
```

最少需要配置：

```env
ONE_API_URL=http://127.0.0.1:3000/v1
ONE_API_TOKEN=replace-with-real-token
REDIS_URL=redis://127.0.0.1:6380/0

DISPATCHER_MODEL=gpt-4o-mini
EXECUTION_MODEL_SIMPLE=gpt-4o-mini
EXECUTION_MODEL_STANDARD=gpt-4o
EXECUTION_MODEL_ADVANCED=gpt-5.4
LOCAL_EXECUTION_MODEL=gpt-5.4
ONLINE_RESEARCH_MODEL=gpt-4o-mini-search-preview
```

浏览器 Worker 云端配置：

```env
BROWSER_WORKER_ENABLED=true
BROWSER_WORKER_DEFAULT_ID=default
BROWSER_WORKER_TOKEN=replace-with-browser-worker-token
BROWSER_WORKER_REQUEST_TIMEOUT=60
```

视觉浏览器配置：

```env
BROWSER_VISION_MODEL=gpt-4o
BROWSER_VISION_VERIFY_ENABLED=true
BROWSER_VISION_ACTION_MIN_CONFIDENCE=0.55
BROWSER_VISION_VERIFY_MIN_CONFIDENCE=0.45
BROWSER_VISION_VERIFY_MAX_RETRIES=2
BROWSER_VISUAL_CLICK_PREFLIGHT_ENABLED=true
BROWSER_VISUAL_CLICK_PREFLIGHT_MIN_SCORE=0.16
BROWSER_VISUAL_TRACE_SCREENSHOTS=false
BROWSER_VISUAL_TRACE_MAX_IMAGE_CHARS=1500000
LOCAL_JOB_ARTIFACT_LIMIT=80
```

说明：

- `BROWSER_VISUAL_TRACE_SCREENSHOTS=false` 是生产更安全的默认值，只保存结构化 trace，不保存截图 base64。
- 调试“浏览器识别不准”时可临时设为 `true`，任务结束后建议改回 `false`。
- click 的 `browser.hit_test` 和动作后验证是强制安全门；`BROWSER_VISUAL_CLICK_PREFLIGHT_ENABLED` 不再用于关闭 click 预检。
- `BROWSER_DIAGNOSTICS_ENABLED` 是本地 Worker 配置，不属于云端 `.env`。

Open Interpreter WebSocket：

```env
OPEN_INTERPRETER_WS_URL=ws://127.0.0.1:8765/
OPEN_INTERPRETER_AUTH_KEY=replace-with-open-interpreter-key
OPEN_INTERPRETER_MODEL=open-interpreter
OPEN_INTERPRETER_TIMEOUT=90
```

`OPEN_INTERPRETER_WS_URL` 和 `OPEN_INTERPRETER_AUTH_KEY` 必须同时配置，`/health` 才会显示 `open_interpreter_configured=true`。

## 本地 Worker 配置

从仓库根目录复制模板：

```powershell
Copy-Item local_launcher\.env.example local_launcher\.env
```

常用配置：

```env
PLAYWRIGHT_PYTHON_EXE=C:\Path\To\Python\python.exe
PLAYWRIGHT_DEFAULT_URL=https://www.douyin.com/
PLAYWRIGHT_USER_DATA_DIR=C:\Users\YourName\AppData\Local\WeiAgent\playwright-user-data\edge-worker

BROWSER_WORKER_ENABLED=true
BROWSER_WORKER_ID=default
BROWSER_WORKER_TOKEN=replace-with-browser-worker-token
BROWSER_WORKER_WS_URL=wss://your-domain.example.com/ws/browser-worker
BROWSER_WORKER_RECONNECT_SEC=5

BROWSER_SCREENSHOT_OPTIMIZE=true
BROWSER_SCREENSHOT_MAX_WIDTH=1280
BROWSER_SCREENSHOT_JPEG_QUALITY=72
BROWSER_DIAGNOSTICS_ENABLED=false

PLAYWRIGHT_MCP_ENABLED=true
PLAYWRIGHT_MCP_COMMAND=C:\Program Files\nodejs\npx.cmd
PLAYWRIGHT_MCP_ARGS_JSON=["@playwright/mcp@latest","--browser=msedge","--user-data-dir=C:\\Users\\YourName\\AppData\\Local\\WeiAgent\\playwright-user-data\\edge-worker"]
PLAYWRIGHT_MCP_PROTOCOL_VERSION=2025-11-25
PLAYWRIGHT_MCP_STARTUP_TIMEOUT=30
PLAYWRIGHT_MCP_REQUEST_TIMEOUT=120
```

Redis SSH 隧道配置：

```env
WEI_REDIS_TUNNEL_ENABLED=true
WEI_REDIS_SSH_USER=ubuntu
WEI_REDIS_SSH_HOST=your-cloud-host.example.com
WEI_REDIS_LOCAL_PORT=6380
WEI_REDIS_REMOTE_PORT=6379
```

如果本机不需要 Redis SSH 隧道：

```env
WEI_REDIS_TUNNEL_ENABLED=false
```

`start_local_services.bat` 不再内置云主机地址；启用隧道时必须显式配置 `WEI_REDIS_SSH_HOST`。

## 启动

推荐从根目录使用统一入口启动云端开发服务：

```bat
scripts\run_cloud_dev.bat
```

如果之前还在用根目录 `.env`，先迁移一次：

```bat
scripts\run_cloud_dev.bat -MigrateLegacyEnv
```

说明：

- 云端运行时只读取 `cloud/.env`，不再读取根目录 `.env`。
- `run_cloud_dev` 会自动设置 `PYTHONPATH=cloud`，并先跑一次快速 Smoke Check。
- 如果没有 `cloud/.env`，脚本会从 `cloud/.env.example` 创建模板并停止，等你填真实配置后再启动。

手动启动云端开发服务：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r cloud\requirements.txt
Copy-Item cloud\.env.example cloud\.env
$env:PYTHONPATH = "cloud"
.venv\Scripts\python.exe -m src.app
```

Windows 本地 Worker：

```bat
scripts\start_local_worker.bat
```

也可以直接启动本地服务集合：

```bat
local_launcher\start_local_services.bat
```

## Linux 部署

云端目录建议部署到 `/opt/wei/cloud`：

```bash
cd /opt/wei/cloud
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
./deploy/start_server.sh
```

systemd：

```bash
sudo cp /opt/wei/cloud/deploy/wei-agent.service /etc/systemd/system/wei-agent.service
sudo systemctl daemon-reload
sudo systemctl enable wei-agent
sudo systemctl restart wei-agent
sudo systemctl status wei-agent
```

部署后云端检查：

```bash
cd /opt/wei/cloud
bash deploy/check_server.sh
```

服务已启动后可加在线检查：

```bash
bash deploy/check_server.sh --online --cloud-url http://127.0.0.1:8000
```

注意：

- 云端 `.env` 在 `/opt/wei/cloud/.env`。
- 启动脚本是 `/opt/wei/cloud/deploy/start_server.sh`。
- 当前 Browser Worker WebSocket hub 是进程内状态，`GUNICORN_WORKERS` 必须保持 `1`。
- 如果服务器只部署云端代码，可以只同步 `cloud/`。
- 如果服务器保留完整仓库，也不要提交或依赖 `local_launcher/.env`、`local_launcher/launcher_config.json`、`local_launcher/launcher_logs/`。

## 健康检查

健康检查：

```bash
curl http://127.0.0.1:8000/health
```

重点看：

- `browser_worker_enabled`
- `browser_worker_connected`
- `browser_execution_ready`
- `browser_workers`
- `browser_visual_click_preflight_enabled`
- `browser_visual_trace_screenshots`
- `open_interpreter_configured`

Worker 列表：

```text
GET /api/browser-workers
```

重点看：

- `default_worker_connected`
- `pending_count`
- `heartbeat_age_sec`
- `stale`
- `capabilities`

## 浏览器视觉任务流程

```text
1. 云端请求本地 Worker 截图
2. 云端视觉模型读取截图和页面诊断
3. 模型返回 action JSON
4. 云端策略检查 confidence / target_description / expected_change
5. 所有 click 必须有 expected_change
6. click 强制 browser.hit_test 坐标预检
7. 预检失败则不执行点击
8. 本地 Worker 执行单步 action
9. click 后云端强制再次截图并做动作后校验
10. 根据校验结果继续、重试、完成或请求人工介入
```

常见 artifact：

- `state_transition`
- `screenshot`
- `vision_decision`
- `click_preflight`
- `action_result`
- `verification_screenshot`
- `verification`
- `verification_policy`
- `failure`
- `final`

排查“识别不准”时优先看：

- `screenshot`：模型看到的截图是否正确。
- `vision_decision`：模型目标描述是否明确。
- `click_preflight`：坐标下元素是否和目标匹配。
- `verification`：是否发现 misclick 或 expected change 不匹配。
- `failure` / `final.last_failure`：标准 `failure_type`、严重级别、是否可恢复、原始原因。

可调参数：

- `BROWSER_VISION_ACTION_MIN_CONFIDENCE`
- `BROWSER_VISUAL_CLICK_PREFLIGHT_MIN_SCORE`
- `BROWSER_VISUAL_TRACE_SCREENSHOTS`
- 本地 `BROWSER_DIAGNOSTICS_ENABLED`

## 端口和网络边界

推荐云端端口边界：

- `80/443`：Nginx 对外入口。
- `8000`：FastAPI，仅监听 `127.0.0.1`，由 Nginx 反代。
- `3000`：One API，如只给本机或内网使用，建议不要公网暴露。
- `6379`：Redis，只监听 `127.0.0.1`，本地通过 SSH 隧道访问。

不再依赖：

- `18100`：旧本地 Browser Bridge HTTP 端口。
- `18000`：旧 Open Interpreter HTTP 端口。
- `7000`：旧内网穿透端口。
- SearXNG 本地配置。
- frp/内网穿透。

## API 概览

- `POST /api/chat`
- `POST /api/chat/stream`
- `POST /api/chat/confirm`
- `POST /api/chat/confirm/stream`
- `GET /api/jobs/{job_id}`
- `GET /api/jobs/{job_id}/stream`
- `POST /api/jobs/{job_id}/cancel`
- `GET /api/browser-workers`
- `WS /ws/browser-worker`
- `POST /api/auth/login`
- `POST /api/auth/logout`
- `GET /api/auth/me`

## 测试和检查

推荐先跑一键 Smoke Check。它会检查仓库边界、云端/本地 env、Browser Worker 配置、Open Interpreter 配置，并串联单测、编译、前端语法、依赖和 Git 空白检查。

Windows，从仓库根目录运行：

```bat
scripts\check_project.bat
```

快速配置检查，不跑单测和编译：

```bat
scripts\check_project.bat -SkipTests -SkipCompile
```

联调时开启在线检查：

```bat
scripts\check_project.bat -Online -CloudUrl http://127.0.0.1:8000
```

可选深度检查：

```bat
scripts\check_project.bat -Online -CheckOneApi -CheckOpenInterpreter
```

手动分项检查：

```powershell
$env:PYTHONPATH = "cloud"
.venv\Scripts\python.exe -m unittest discover -s cloud\tests
.venv\Scripts\python.exe -m compileall cloud local_launcher
node --check cloud\static\app.js
.venv\Scripts\python.exe -m pip check
git diff --check
```

旧端口和旧配置残留扫描：

```powershell
rg -n "18100|18000|7000|BROWSER_BRIDGE|SEARXNG|browser\.interact|OPEN_INTERPRETER_URL" cloud local_launcher README.md -S -g "!local_launcher/launcher_logs/**"
```

## 常见问题

### Worker 离线

检查：

- 云端 `cloud/.env` 是否设置 `BROWSER_WORKER_ENABLED=true`。
- 本地 `local_launcher/.env` 的 `BROWSER_WORKER_WS_URL` 是否指向云端 `/ws/browser-worker`。
- 云端和本地 `BROWSER_WORKER_TOKEN` 是否一致。
- 本地是否能访问云端域名和 WebSocket。
- `GET /api/browser-workers` 中 `heartbeat_age_sec` 是否持续增长，`stale` 是否为 `true`。

### 浏览器识别不准

优先看 Visual Trace：

- `state_transition`：卡在哪个阶段。
- `screenshot`：截图是否符合当前页面。
- `vision_decision`：模型决策是否明确。
- `click_preflight`：点击坐标是否命中目标 DOM。
- `verification`：动作后页面变化是否符合 `expected_change`。
- `failure`：`failure_type` 是否指向预检、执行、校验、模型决策、遮罩/登录阻塞、viewport/DPR 坐标漂移或页面意外刷新。

### Redis 连接问题

云端 Redis 推荐只监听 `127.0.0.1`。本地需要访问云端 Redis 时，用 SSH 隧道：

```bash
ssh -N -L 6380:127.0.0.1:6379 ubuntu@your-cloud-host.example.com
```

对应开发环境：

```env
REDIS_URL=redis://127.0.0.1:6380/0
```

### 本地配置被误提交

仓库应该只跟踪：

- `local_launcher/.env.example`
- `local_launcher/launcher_config.example.json`

不应提交：

- `local_launcher/.env`
- `local_launcher/launcher_config.json`
- `local_launcher/launcher_logs/`
- `local_launcher/.playwright-mcp/`
