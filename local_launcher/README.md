# Local Launcher

这个目录用于管理本地运行组件，目前主要包括：

- `frpc`
- `Open Interpreter`
- `Browser Bridge`
- `Playwright + Edge/Chrome` 本地浏览器入口

## 目录说明

```text
local_launcher/
|-- .env
|-- .env.example
|-- local_launcher.py
|-- launcher_config.json
|-- launcher_config.example.json
|-- start_local_services.bat
|-- browser_bridge.py
|-- open_douyin_edge.bat
|-- playwright_edge_runner.py
`-- launcher_logs/
```

`local_launcher/.env` 是唯一的本地配置入口。

## 启动本地服务

运行：

```bat
local_launcher\start_local_services.bat
```

它会读取：

- `launcher_config.json`
- `local_launcher/.env`

当前会一起拉起：

- `frpc`
- `Open Interpreter`
- `Browser Bridge`

如果某个组件暂时不需要，可以在 `launcher_config.json` 里设置：

```json
{
  "enabled": false
}
```

## 打开抖音网页

运行：

```bat
local_launcher\open_douyin_edge.bat
```

它会：

- 使用 Playwright 启动本机 Edge
- 打开 `https://www.douyin.com/`
- 复用持久化用户数据目录
- 保持窗口打开，直到你手动关闭

默认用户数据目录：

```text
D:\Download\playwright-user-data\edge-douyin-bridge
```

第一次运行后，请在打开的 Edge 窗口里手动登录，之后登录态会保存在这个目录里。

## 本地配置文件

建议先复制：

```powershell
Copy-Item local_launcher\.env.example local_launcher\.env
```

常用变量：

```env
OPEN_INTERPRETER_URL=http://127.0.0.1:18000
OPEN_INTERPRETER_AUTH_KEY=dummy-api-key
OPEN_INTERPRETER_TIMEOUT=90

PLAYWRIGHT_PYTHON_EXE=D:\Download\oi-env-310\Scripts\python.exe
PLAYWRIGHT_EDGE_EXECUTABLE=C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe
PLAYWRIGHT_EDGE_USER_DATA_DIR=D:\Download\playwright-user-data\edge-douyin-bridge
PLAYWRIGHT_DEFAULT_URL=https://www.douyin.com/
PLAYWRIGHT_BROWSER_WIDTH=1440
PLAYWRIGHT_BROWSER_HEIGHT=900

BROWSER_BRIDGE_HOST=127.0.0.1
BROWSER_BRIDGE_PORT=18100
BROWSER_BRIDGE_TOKEN=replace-with-local-browser-bridge-token
```

如果没有额外配置：

- Python 会优先尝试 `D:\Download\oi-env-310\Scripts\python.exe`
- Edge 会自动从常见安装路径查找
- 用户数据目录默认使用 `D:\Download\playwright-user-data\edge-douyin-bridge`

## Browser Bridge

`browser_bridge.py` 是本地常驻网页自动化服务：

- 默认监听 `127.0.0.1:18100`
- 使用 `Playwright + Edge`
- 复用持久化登录态
- 提供最小接口：
  - `GET /health`
  - `POST /page/open`
  - `POST /page/snapshot`
  - `POST /jobs/start`
  - `GET /jobs/{id}`
  - `POST /jobs/{id}/cancel`

如果你通过 FRP 暴露它给云端，请保证：

- 本地 `BROWSER_BRIDGE_TOKEN` 已设置
- 云端 `.env` 中的 `BROWSER_BRIDGE_URL` / `BROWSER_BRIDGE_TOKEN` 与之匹配

## Playwright MCP Bridge Mode

Browser Bridge 现在包含一条可选的首阶段 MCP 集成路径。它不会马上替换现有自动化流，但已经支持 Local Bridge 启动和监控一个基于 stdio 的 Playwright MCP 子进程，并通过 HTTP 暴露 MCP 状态与基础操作。

`.env` 示例：

```env
PLAYWRIGHT_MCP_ENABLED=true
PLAYWRIGHT_MCP_COMMAND=C:\Program Files\nodejs\npx.cmd
PLAYWRIGHT_MCP_ARGS_JSON=["@playwright/mcp@latest","--browser=chrome","--headless","--user-data-dir=D:\\Download\\playwright-user-data\\chrome-douyin-mcp"]
PLAYWRIGHT_MCP_PROTOCOL_VERSION=2025-11-25
PLAYWRIGHT_MCP_STARTUP_TIMEOUT=30
PLAYWRIGHT_MCP_REQUEST_TIMEOUT=120
```

当前可用的 MCP HTTP 入口：

- `GET /mcp/status`
- `GET /mcp/tools`
- `GET /mcp/tabs`
- `POST /mcp/restart`
- `POST /mcp/navigate`
- `POST /mcp/snapshot`
- `POST /mcp/interact`

`GET /health` 也会带上 `playwright_mcp` 状态块。

建议给 MCP 单独使用一个 browser profile。不要和 legacy Browser Bridge/CDP 流共用同一个 `user-data-dir`，否则容易出现 `Browser is already in use` 启动错误。

## 打包启动器

运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\local_launcher\build_launcher.ps1
```

生成：

```text
local_launcher/dist/LocalRuntimeLauncher.exe
```

## 日志

本地启动器日志目录：

```text
local_launcher/launcher_logs/
```
