# Wei Agent

Wei Agent 是一个基于 `FastAPI + LangGraph` 的分层调度型智能助手。当前仓库已经按运行边界拆成两块：

- `cloud/`：云端 Agent、API、调度、视觉识别、任务状态、前端静态资源和部署脚本。
- `local_launcher/`：本地薄浏览器 Worker、Playwright MCP 启动器、本地 Edge/Chrome 操作入口。

目标架构：

```text
Cloud Wei Agent
  -> Cloud Browser Orchestrator / Task Core
  -> WebSocket 长连接
  -> Local Thin Browser Worker
  -> Playwright MCP
  -> Local Edge / Chrome
```

## 边界原则

- 云端负责模型调用、任务规划、视觉识别、动作安全策略、点击前预检决策、动作后校验、Redis job 状态和前端展示。
- 本地只负责连接云端 WebSocket、调用 Playwright MCP、截图、坐标命中检测和执行动作。
- 本地 Worker 不保存业务状态，不做模型判断，不对外暴露 HTTP 浏览器桥。
- 不再依赖旧的 `18100/18000/7000` 端口、SearXNG 本地配置、frp/内网穿透。
- 云端配置放 `cloud/.env`；本地配置放 `local_launcher/.env`，两者不要混用。

## 目录结构

```text
.
|-- cloud/
|   |-- src/                     # 云端 Python 包，包名仍为 src
|   |-- static/                  # 云端 Web 前端静态资源
|   |-- config/                  # 云端 system prompt 等配置
|   |-- deploy/                  # 云端 systemd/gunicorn/清理脚本
|   |   `-- check_server.sh      # 云端部署后 smoke check
|   |-- tests/                   # 云端单元测试
|   |-- tools/                   # 云端维护/诊断工具
|   |-- .env.example             # 云端环境变量模板
|   `-- requirements.txt         # 云端 Python 依赖
|-- local_launcher/
|   |-- browser_bridge.py        # Local Thin Browser Worker + Playwright MCP client
|   |-- local_launcher.py
|   |-- start_local_services.bat
|   |-- launcher_config.example.json
|   |-- .env.example             # 本地 Worker 环境变量模板
|   `-- README.md
|-- scripts/
|   |-- check_project.bat        # Windows 一键检查入口
|   |-- run_cloud_dev.bat        # Windows 云端开发服务入口
|   |-- run_cloud_dev.ps1
|   |-- check_project.ps1        # Smoke Check + 测试/编译/语法检查
|   `-- start_local_worker.bat   # Windows 本地 Worker 入口
|-- README.md                    # 总说明
`-- .gitignore
```

## 云端能力

- 分层模型调度：`simple / standard / advanced`。
- 联网思考：通过在线研究模型处理时效性问题。
- Open Interpreter WebSocket：后续本地代码执行走 WS，不再走旧 HTTP URL。
- 浏览器 Worker 编排：云端通过 `/ws/browser-worker` 与本地 Worker 长连接通信。
- 截图视觉识别：云端视觉模型读取本地截图并生成动作。
- 点击前预检：本地 `browser.hit_test` 只返回坐标下 DOM 摘要，云端判断是否明显点偏。
- 动作后校验：动作后再次截图，判断是否达到 `expected_change`。
- Visual Trace：长任务记录截图摘要、模型决策、动作结果、校验结果，方便复盘识别不准。
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

它不再提供旧的 `browser.interact`，也不再启动旧的本地 HTTP Browser Bridge。

## 云端配置

从仓库根目录：

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

从仓库根目录：

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

如果你之前还在用根目录 `.env`，先迁移一次：

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

如果不想切目录，也可以临时设置：

```powershell
$env:PYTHONPATH = "cloud"
.venv\Scripts\python.exe -m src.app
```

Linux 部署：

```bash
cd /opt/wei/cloud
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
./deploy/start_server.sh
```

Windows 本地 Worker：

```bat
scripts\start_local_worker.bat
```

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

## 浏览器视觉任务流程

```text
1. 云端请求本地 Worker 截图
2. 云端视觉模型读取截图和页面诊断
3. 模型返回 action JSON
4. 云端策略检查 confidence / target_description / expected_change
5. click 动作先调用 browser.hit_test 做坐标预检
6. 本地 Worker 执行动作
7. 云端再次截图并做动作后校验
8. 根据校验结果继续、重试、完成或请求人工介入
```

常见 artifact：

- `screenshot`
- `vision_decision`
- `click_preflight`
- `action_result`
- `verification_screenshot`
- `verification`
- `verification_policy`
- `failure`
- `final`

`failure` 和 `final.last_failure` 会给出结构化失败分类，例如 `action_low_confidence`、`click_target_mismatch`、`verification_expected_change_mismatch`、`verification_retry_limit`。排查“识别不准”时优先看它们，再回看对应截图和模型决策。

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
- `POST /api/jobs/{job_id}/cancel`
- `GET /api/browser-workers`
- `WS /ws/browser-worker`
- `POST /api/auth/login`
- `POST /api/auth/logout`
- `GET /api/auth/me`

## systemd 部署

`cloud/deploy/wei-agent.service` 默认以 `/opt/wei/cloud` 为工作目录：

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
- 如果服务器只部署云端代码，可以只同步 `cloud/`。
- 如果服务器保留完整仓库，也不要提交或依赖 `local_launcher/.env`、`local_launcher/launcher_config.json`、`local_launcher/launcher_logs/`。

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

说明：

- `-Online` 会检查 Redis TCP/PING 和云端 `/health`，适合服务已经启动后使用。
- `-CheckOneApi` 会请求 `ONE_API_URL` + `/models`，需要真实 `ONE_API_TOKEN`。
- `-CheckOpenInterpreter` 会连接 `OPEN_INTERPRETER_WS_URL` 并验证 auth key。
- 如果只想直接跑 Python 诊断器，可以执行 `.venv\Scripts\python.exe cloud\tools\smoke_check.py`。

手动分项检查：

```powershell
$env:PYTHONPATH = "cloud"
.venv\Scripts\python.exe -m unittest discover -s cloud\tests
.venv\Scripts\python.exe -m compileall cloud local_launcher
node --check cloud\static\app.js
.venv\Scripts\python.exe -m pip check
git diff --check
```

也可以进入云端目录：

```powershell
Set-Location cloud
..\.venv\Scripts\python.exe -m unittest discover -s tests
..\.venv\Scripts\python.exe -m src.app
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

### 浏览器识别不准

优先看 Visual Trace：

- `screenshot`：模型看到的截图是否正确。
- `vision_decision`：模型目标描述是否明确。
- `click_preflight`：坐标下元素是否和目标匹配。
- `verification`：是否发现 misclick 或 expected change 不匹配。
- `failure` / `final.last_failure`：失败分类、严重级别、是否可恢复、原始原因。

可调参数：

- `BROWSER_VISION_ACTION_MIN_CONFIDENCE`
- `BROWSER_VISUAL_CLICK_PREFLIGHT_MIN_SCORE`
- `BROWSER_VISUAL_TRACE_SCREENSHOTS`
- 本地 `BROWSER_DIAGNOSTICS_ENABLED`

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

仓库应只跟踪：

- `local_launcher/.env.example`
- `local_launcher/launcher_config.example.json`

不应提交：

- `local_launcher/.env`
- `local_launcher/launcher_config.json`
- `local_launcher/launcher_logs/`
- `local_launcher/.playwright-mcp/`
