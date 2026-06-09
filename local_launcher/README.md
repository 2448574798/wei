# Local Launcher

这个目录用于管理本地运行组件，当前主要包含：

- `frpc`
- `Open Interpreter`
- `Browser Bridge`
- `Playwright + Edge` 本地网页登录入口

## 目录说明

```text
local_launcher/
|-- local_launcher.py
|-- launcher_config.json
|-- launcher_config.example.json
|-- start_local_services.bat
|-- browser_bridge.py
|-- open_douyin_edge.bat
|-- playwright_edge_runner.py
|-- runtime_env.local
`-- launcher_logs/
```

## 启动本地服务

双击或运行：

```bat
local_launcher\start_local_services.bat
```

它会读取：

- `launcher_config.json`
- `runtime_env.local`

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

双击或运行：

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

## Playwright 配置

`open_douyin_edge.bat` 会优先从 `runtime_env.local` 读取这些可选变量：

```env
PLAYWRIGHT_PYTHON_EXE=D:\Download\oi-env-310\Scripts\python.exe
PLAYWRIGHT_EDGE_EXECUTABLE=C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe
PLAYWRIGHT_EDGE_USER_DATA_DIR=D:\Download\playwright-user-data\edge-douyin-bridge
PLAYWRIGHT_DEFAULT_URL=https://www.douyin.com/
PLAYWRIGHT_BROWSER_WIDTH=1440
PLAYWRIGHT_BROWSER_HEIGHT=900
```

Browser Bridge 也会使用：

```env
BROWSER_BRIDGE_HOST=127.0.0.1
BROWSER_BRIDGE_PORT=18100
BROWSER_BRIDGE_TOKEN=browser-bridge-local-token
```

如果没有设置：

- Python 会优先尝试 `D:\Download\oi-env-310\Scripts\python.exe`
- Edge 会自动从常见安装路径查找
- 用户数据目录会默认使用 `D:\Download\playwright-user-data\edge-douyin-bridge`

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
- 云端 `BROWSER_BRIDGE_URL` / `BROWSER_BRIDGE_TOKEN` 与之匹配

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
