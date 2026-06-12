# Local Launcher

`local_launcher/.env` is the only local runtime env entry now.

## What runs locally

- `frpc`
- `Open Interpreter`
- `Browser Bridge`
- `Playwright MCP Server` managed by `browser_bridge.py`
- Local Chrome/Edge profile reused by Playwright MCP

## Directory

```text
local_launcher/
|-- .env
|-- .env.example
|-- README.md
|-- browser_bridge.py
|-- build_launcher.ps1
|-- launcher_config.example.json
|-- launcher_config.json
|-- local_launcher.py
|-- open_douyin_edge.bat
|-- start_local_services.bat
|-- start_redis_tunnel.bat
`-- launcher_logs/
```

## Start local services

Run:

```bat
local_launcher\start_local_services.bat
```

It reads:

- `local_launcher\launcher_config.json`
- `local_launcher\.env`

It starts:

- `frpc`
- `Open Interpreter`
- `Browser Bridge`

## Open Douyin

Run:

```bat
local_launcher\open_douyin_edge.bat
```

This script no longer launches a separate Playwright runner. It reuses the running Browser Bridge and calls `POST /mcp/navigate` to open the configured URL.

## Local env example

Create the file first:

```powershell
Copy-Item local_launcher\.env.example local_launcher\.env
```

Common variables:

```env
OPEN_INTERPRETER_URL=http://127.0.0.1:18000
OPEN_INTERPRETER_AUTH_KEY=dummy-api-key
OPEN_INTERPRETER_TIMEOUT=90

PLAYWRIGHT_PYTHON_EXE=D:\Download\oi-env-310\Scripts\python.exe
PLAYWRIGHT_DEFAULT_URL=https://www.douyin.com/
PLAYWRIGHT_USER_DATA_DIR=D:\Download\playwright-user-data\chrome-douyin-mcp

BROWSER_BRIDGE_HOST=127.0.0.1
BROWSER_BRIDGE_PORT=18100
BROWSER_BRIDGE_TOKEN=replace-with-local-browser-bridge-token

WEI_REDIS_TUNNEL_ENABLED=true
WEI_REDIS_SSH_USER=ubuntu
WEI_REDIS_SSH_HOST=43.134.7.123
WEI_REDIS_LOCAL_PORT=6380
WEI_REDIS_REMOTE_PORT=6379

PLAYWRIGHT_MCP_ENABLED=true
PLAYWRIGHT_MCP_COMMAND=C:\Program Files\nodejs\npx.cmd
PLAYWRIGHT_MCP_ARGS_JSON=["@playwright/mcp@latest","--browser=chrome","--user-data-dir=D:\\Download\\playwright-user-data\\chrome-douyin-mcp"]
PLAYWRIGHT_MCP_PROTOCOL_VERSION=2025-11-25
PLAYWRIGHT_MCP_STARTUP_TIMEOUT=30
PLAYWRIGHT_MCP_REQUEST_TIMEOUT=120
```

## Browser Bridge API

Available endpoints:

- `GET /health`
- `GET /mcp/status`
- `GET /mcp/tools`
- `GET /mcp/tabs`
- `POST /mcp/restart`
- `POST /mcp/navigate`
- `POST /mcp/snapshot`
- `POST /mcp/interact`
- `POST /jobs/start`
- `GET /jobs/{id}`
- `POST /jobs/{id}/cancel`

`/mcp/interact` is now MCP-only and supports readonly steps only:

- `wait`
- `goto`
- `click`
- `click_any`
- `press`
- `extract_text`
- `extract_any_text`
- `extract_list_text`
- `snapshot`

## Build launcher

Run:

```powershell
powershell -ExecutionPolicy Bypass -File .\local_launcher\build_launcher.ps1
```

Output:

```text
local_launcher/dist/LocalRuntimeLauncher.exe
```

## Logs

Local launcher logs:

```text
local_launcher/launcher_logs/
```
