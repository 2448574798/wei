# Local Launcher

这个目录用于把 `frp` 和 `Open Interpreter` 做成一个本地一键启动器。

## 目录建议

```text
local_launcher/
├─ bin/
│  ├─ frpc.exe
│  ├─ frpc.toml
│  └─ open-interpreter-server.exe
├─ launcher_config.json
├─ local_launcher.py
└─ launcher_logs/
```

## 快速使用

1. 把 `launcher_config.example.json` 复制成 `launcher_config.json`
2. 按你的实际路径修改里面的 `command`
3. 双击根目录的 [start_local_runtime.bat](/d:/VS/wei/start_local_runtime.bat:1)

如果某个组件暂时还没装好，可以在 `launcher_config.json` 里先设置：

```json
{
  "enabled": false
}
```

## 打包成 exe

运行 [build_launcher.ps1](/d:/VS/wei/local_launcher/build_launcher.ps1:1)。

打包后会生成：

```text
local_launcher/dist/LocalRuntimeLauncher.exe
```

建议把这些文件放在同一个目录分发：

- `LocalRuntimeLauncher.exe`
- `launcher_config.json`
- `bin/frpc.exe`
- `bin/frpc.toml`
- `bin/open-interpreter-server.exe`

## 说明

- 启动器本身可以打成单个 `exe`
- `frpc.exe` 和 `Open Interpreter` 可执行文件通常仍建议作为外部文件放在 `bin/` 目录
- 启动器退出时会尝试一并停止这两个子进程
- 日志会写入 `launcher_logs/`
