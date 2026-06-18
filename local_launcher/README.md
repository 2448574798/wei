# Local Thin Browser Worker

`local_launcher/.env` is the local runtime env entry.

## Runtime Shape

```text
Cloud Wei Agent
  -> Cloud Browser Orchestrator / Task Core
  -> WebSocket long connection
  -> Local Thin Browser Worker
  -> Playwright MCP
  -> Local Edge / Chrome
```

The local launcher no longer starts a local Open Interpreter HTTP service or a local HTTP browser bridge. Browser automation is driven through the worker websocket connection to the cloud server.
Vision planning, verification, and retry policy are cloud-side responsibilities. The local worker only captures screenshots, executes browser actions, and reports results over the websocket.
It does not keep `BrowserJob`, `_jobs`, `start_job`, `watch_text`, or `interaction_watch` state locally; Redis job state belongs to the cloud server.
On every websocket reconnect it sends a fresh `register` message. Commands carry `request_id`, browser actions carry `action_id`, and responses echo those IDs for cloud-side trace alignment. Screenshots include viewport width, height, and device pixel ratio. If Playwright MCP becomes unavailable at the transport layer, the worker restarts MCP and retries the tool call once.

## What Runs Locally

- `local_thin_browser_worker` from `browser_bridge.py`
- `Playwright MCP Server` managed by the local worker
- Local Edge/Chrome profile reused by Playwright MCP

## Start

```bat
local_launcher\start_local_services.bat
```

It reads:

- `local_launcher\launcher_config.json`
- `local_launcher\.env`

If `WEI_REDIS_TUNNEL_ENABLED` is not `false`, it also starts the Redis SSH tunnel used by local long-task code. This logic is built into `start_local_services.bat`; it is not an inbound browser tunnel and does not expose local services to the cloud.

## Local Env Example

```powershell
Copy-Item local_launcher\.env.example local_launcher\.env
```

Common variables:

```env
PLAYWRIGHT_PYTHON_EXE=C:\Path\To\Python\python.exe
PLAYWRIGHT_DEFAULT_URL=https://www.douyin.com/
PLAYWRIGHT_USER_DATA_DIR=C:\Users\YourName\AppData\Local\WeiAgent\playwright-user-data\edge-worker

BROWSER_WORKER_ENABLED=true
BROWSER_WORKER_ID=default
BROWSER_WORKER_TOKEN=replace-with-browser-worker-token
BROWSER_WORKER_WS_URL=wss://sunw.chat/ws/browser-worker
BROWSER_SCREENSHOT_OPTIMIZE=true
BROWSER_SCREENSHOT_MAX_WIDTH=1280
BROWSER_SCREENSHOT_JPEG_QUALITY=72
BROWSER_DIAGNOSTICS_ENABLED=false

WEI_REDIS_TUNNEL_ENABLED=true
WEI_REDIS_SSH_USER=ubuntu
WEI_REDIS_SSH_HOST=your-cloud-host.example.com
WEI_REDIS_LOCAL_PORT=6380
WEI_REDIS_REMOTE_PORT=6379

PLAYWRIGHT_MCP_ENABLED=true
PLAYWRIGHT_MCP_COMMAND=C:\Program Files\nodejs\npx.cmd
PLAYWRIGHT_MCP_ARGS_JSON=["@playwright/mcp@latest","--browser=msedge","--user-data-dir=C:\\Users\\YourName\\AppData\\Local\\WeiAgent\\playwright-user-data\\edge-worker"]
PLAYWRIGHT_MCP_PROTOCOL_VERSION=2025-11-25
PLAYWRIGHT_MCP_STARTUP_TIMEOUT=30
PLAYWRIGHT_MCP_REQUEST_TIMEOUT=120
```

## Cloud-Side Checks

- `GET /health` includes `browser_worker_connected`, `browser_execution_ready`, and `browser_workers`.
- `GET /api/browser-workers` returns connected workers for authenticated troubleshooting.

`browser_execution_ready=true` means the cloud server has a live worker websocket connection.

Cloud-only browser vision variables such as `BROWSER_VISION_MODEL` and
`BROWSER_VISION_VERIFY_ENABLED` belong in the cloud server environment, not in
`local_launcher/.env`.

## Build Launcher

```powershell
powershell -ExecutionPolicy Bypass -File .\local_launcher\build_launcher.ps1
```

Output:

```text
local_launcher/dist/LocalRuntimeLauncher.exe
```

## Logs

```text
local_launcher/launcher_logs/
```
