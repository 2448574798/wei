from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


PLACEHOLDER_MARKERS = (
    "replace-with",
    "your-",
    "your_",
    "example.com",
    "YourName",
    "C:\\Path\\To",
)


@dataclass
class CheckResult:
    status: str
    name: str
    detail: str


class SmokeReport:
    def __init__(self) -> None:
        self.results: list[CheckResult] = []

    def ok(self, name: str, detail: str) -> None:
        self.results.append(CheckResult("OK", name, detail))

    def warn(self, name: str, detail: str) -> None:
        self.results.append(CheckResult("WARN", name, detail))

    def fail(self, name: str, detail: str) -> None:
        self.results.append(CheckResult("FAIL", name, detail))

    def print(self) -> None:
        for result in self.results:
            print(f"[{result.status}] {result.name}: {result.detail}")

    def exit_code(self) -> int:
        return 1 if any(item.status == "FAIL" for item in self.results) else 0


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def has_placeholder(value: str) -> bool:
    if not value:
        return False
    return any(marker.lower() in value.lower() for marker in PLACEHOLDER_MARKERS)


def bool_env(values: dict[str, str], key: str, default: bool = False) -> bool:
    raw = values.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def choose_cloud_env(root: Path) -> tuple[Path | None, dict[str, str], list[Path]]:
    candidates = [root / "cloud" / ".env"]
    existing = [path for path in candidates if path.exists()]
    if not existing:
        return None, {}, []
    selected = existing[0]
    return selected, read_env_file(selected), existing


def check_layout(report: SmokeReport, root: Path, *, cloud_only: bool = False) -> None:
    required = [
        root / "cloud" / "src" / "app.py",
        root / "cloud" / "requirements.txt",
        root / "cloud" / "static" / "app.js",
        root / "cloud" / "tests",
    ]
    if not cloud_only:
        required.extend(
            [
                root / "local_launcher" / "browser_bridge.py",
                root / "local_launcher" / "start_local_services.bat",
            ]
        )
    missing = [str(path.relative_to(root)) for path in required if not path.exists()]
    if missing:
        report.fail("repo layout", "missing: " + ", ".join(missing))
    else:
        if cloud_only:
            report.ok("repo layout", "cloud/ runtime layout is present")
        else:
            report.ok("repo layout", "cloud/ and local_launcher/ boundaries are present")

    legacy_dirs = [name for name in ("src", "static", "config", "deploy", "tests") if (root / name).exists()]
    if legacy_dirs:
        report.warn("repo layout", "legacy cloud directories still exist at root: " + ", ".join(legacy_dirs))


def check_cloud_env(report: SmokeReport, root: Path, cloud_env: dict[str, str], cloud_env_path: Path | None, existing: list[Path]) -> None:
    if not cloud_env_path:
        report.warn("cloud env", "cloud/.env is missing; using runtime defaults only")
        return

    rel = cloud_env_path.relative_to(root)
    report.ok("cloud env", f"loaded {rel}")
    if len(existing) > 1:
        rels = ", ".join(str(path.relative_to(root)) for path in existing)
        report.warn("cloud env", f"multiple cloud env candidates exist ({rels}); cloud/.env wins")

    for key in ("ONE_API_URL", "REDIS_URL"):
        value = cloud_env.get(key, "")
        if not value:
            report.fail("cloud env", f"{key} is missing")
        elif has_placeholder(value):
            report.warn("cloud env", f"{key} still looks like an example value")

    one_api_token = cloud_env.get("ONE_API_TOKEN", "")
    if not one_api_token:
        report.fail("cloud env", "ONE_API_TOKEN is missing")
    elif has_placeholder(one_api_token):
        report.warn("cloud env", "ONE_API_TOKEN still looks like an example value")

    browser_enabled = bool_env(cloud_env, "BROWSER_WORKER_ENABLED")
    if browser_enabled:
        token = cloud_env.get("BROWSER_WORKER_TOKEN", "")
        if not token:
            report.fail("browser worker cloud env", "BROWSER_WORKER_TOKEN is required when BROWSER_WORKER_ENABLED=true")
        elif has_placeholder(token):
            report.warn("browser worker cloud env", "BROWSER_WORKER_TOKEN still looks like an example value")
        report.ok("browser worker cloud env", f"default id={cloud_env.get('BROWSER_WORKER_DEFAULT_ID', 'default')}")
    else:
        report.warn("browser worker cloud env", "BROWSER_WORKER_ENABLED is not true; local browser execution will be disabled")

    oi_url = cloud_env.get("OPEN_INTERPRETER_WS_URL", "")
    oi_key = cloud_env.get("OPEN_INTERPRETER_AUTH_KEY", "")
    if bool(oi_url) ^ bool(oi_key):
        report.fail("open interpreter env", "OPEN_INTERPRETER_WS_URL and OPEN_INTERPRETER_AUTH_KEY must be configured together")
    elif oi_url and oi_key:
        if not oi_url.startswith(("ws://", "wss://")):
            report.fail("open interpreter env", "OPEN_INTERPRETER_WS_URL must start with ws:// or wss://")
        elif has_placeholder(oi_key):
            report.warn("open interpreter env", "OPEN_INTERPRETER_AUTH_KEY still looks like an example value")
        else:
            report.ok("open interpreter env", "websocket endpoint is configured")
    else:
        report.warn("open interpreter env", "not configured; local code execution via Open Interpreter is disabled")

    model = cloud_env.get("BROWSER_VISION_MODEL", "gpt-4o")
    if model:
        report.ok("browser vision env", f"model={model}, verify={cloud_env.get('BROWSER_VISION_VERIFY_ENABLED', 'true')}")


def check_local_env(report: SmokeReport, root: Path, cloud_env: dict[str, str]) -> None:
    local_env_path = root / "local_launcher" / ".env"
    if not local_env_path.exists():
        report.warn("local env", "local_launcher/.env is missing; local worker cannot start without it")
        return

    local_env = read_env_file(local_env_path)
    report.ok("local env", "loaded local_launcher/.env")

    if not bool_env(local_env, "BROWSER_WORKER_ENABLED"):
        report.warn("local worker env", "BROWSER_WORKER_ENABLED is not true")

    ws_url = local_env.get("BROWSER_WORKER_WS_URL", "")
    if not ws_url:
        report.fail("local worker env", "BROWSER_WORKER_WS_URL is missing")
    elif not ws_url.startswith(("ws://", "wss://", "http://", "https://")):
        report.fail("local worker env", "BROWSER_WORKER_WS_URL must be ws(s):// or http(s)://")
    elif has_placeholder(ws_url):
        report.warn("local worker env", "BROWSER_WORKER_WS_URL still looks like an example value")
    else:
        report.ok("local worker env", f"ws={ws_url}")

    local_token = local_env.get("BROWSER_WORKER_TOKEN", "")
    cloud_token = cloud_env.get("BROWSER_WORKER_TOKEN", "")
    if not local_token:
        report.fail("local worker env", "BROWSER_WORKER_TOKEN is missing")
    elif has_placeholder(local_token):
        report.warn("local worker env", "BROWSER_WORKER_TOKEN still looks like an example value")
    elif cloud_token and not has_placeholder(cloud_token) and local_token != cloud_token:
        report.fail("local worker env", "BROWSER_WORKER_TOKEN does not match cloud token")
    else:
        report.ok("local worker env", "worker token is present")

    py_exe = local_env.get("PLAYWRIGHT_PYTHON_EXE", "")
    if py_exe and not has_placeholder(py_exe):
        if Path(py_exe).exists():
            report.ok("local playwright env", f"python exists: {py_exe}")
        else:
            report.warn("local playwright env", f"PLAYWRIGHT_PYTHON_EXE does not exist: {py_exe}")

    mcp_command = local_env.get("PLAYWRIGHT_MCP_COMMAND", "")
    if not mcp_command:
        report.warn("local playwright env", "PLAYWRIGHT_MCP_COMMAND is missing; browser_bridge.py will use defaults")
    elif has_placeholder(mcp_command):
        report.warn("local playwright env", "PLAYWRIGHT_MCP_COMMAND still looks like an example value")
    elif Path(mcp_command).exists():
        report.ok("local playwright env", f"MCP command exists: {mcp_command}")
    else:
        report.warn("local playwright env", f"MCP command path does not exist: {mcp_command}")

    tunnel_enabled = bool_env(local_env, "WEI_REDIS_TUNNEL_ENABLED", default=True)
    if tunnel_enabled:
        host = local_env.get("WEI_REDIS_SSH_HOST", "")
        if not host or has_placeholder(host):
            report.warn("redis tunnel env", "SSH tunnel is enabled but WEI_REDIS_SSH_HOST is missing or placeholder")
        else:
            report.ok("redis tunnel env", f"SSH tunnel target={host}:{local_env.get('WEI_REDIS_REMOTE_PORT', '6379')}")
    else:
        report.ok("redis tunnel env", "SSH tunnel disabled")


def socket_check(host: str, port: int, timeout: float = 2.5) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, "connected"
    except Exception as exc:
        return False, str(exc)


def check_redis_online(report: SmokeReport, redis_url: str) -> None:
    parsed = urlparse(redis_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 6379
    ok, detail = socket_check(host, port)
    if not ok:
        report.fail("redis tcp", f"{host}:{port} is not reachable: {detail}")
        return

    try:
        import redis  # type: ignore

        client = redis.Redis.from_url(redis_url, decode_responses=True, socket_connect_timeout=5, socket_timeout=5)
        if client.ping():
            report.ok("redis ping", f"PING ok via {redis_url}")
        else:
            report.fail("redis ping", f"PING returned false via {redis_url}")
    except Exception as exc:
        report.fail("redis ping", f"failed via {redis_url}: {exc}")


def request_json(url: str, *, token: str = "", timeout: float = 6.0) -> tuple[int, dict[str, Any] | list[Any] | None, str]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            data = json.loads(body) if body else None
            return int(response.status), data, ""
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return int(exc.code), None, body[:300]
    except Exception as exc:
        return 0, None, str(exc)


def check_cloud_health(report: SmokeReport, cloud_url: str) -> None:
    url = cloud_url.rstrip("/") + "/health"
    status, data, error = request_json(url)
    if status != 200 or not isinstance(data, dict):
        report.fail("cloud health", f"{url} failed: status={status}, error={error}")
        return

    report.ok("cloud health", f"status={data.get('status')}, checkpointer={data.get('checkpointer_backend')}")
    if data.get("browser_worker_enabled") and data.get("browser_worker_connected"):
        report.ok("browser worker online", f"default worker connected: {data.get('browser_worker_default_id')}")
    elif data.get("browser_worker_enabled"):
        report.fail("browser worker online", "cloud enabled but default worker is not connected")
    else:
        report.warn("browser worker online", "cloud browser worker mode is disabled")

    if data.get("open_interpreter_configured"):
        report.ok("open interpreter health", "cloud reports configured")
    else:
        report.warn("open interpreter health", "cloud reports not configured")


def check_one_api(report: SmokeReport, one_api_url: str, token: str) -> None:
    if not one_api_url or has_placeholder(one_api_url):
        report.warn("one api", "skipped because ONE_API_URL is missing or placeholder")
        return
    if not token or has_placeholder(token):
        report.warn("one api", "skipped because ONE_API_TOKEN is missing or placeholder")
        return
    url = one_api_url.rstrip("/") + "/models"
    status, data, error = request_json(url, token=token)
    if status == 200:
        count = len(data.get("data", [])) if isinstance(data, dict) and isinstance(data.get("data"), list) else "unknown"
        report.ok("one api", f"/models reachable, models={count}")
    else:
        report.fail("one api", f"{url} failed: status={status}, error={error}")


def check_open_interpreter_ws(report: SmokeReport, ws_url: str, auth_key: str) -> None:
    if not ws_url or not auth_key or has_placeholder(auth_key):
        report.warn("open interpreter ws", "skipped because websocket URL/auth key is missing or placeholder")
        return
    try:
        from websockets.sync.client import connect

        with connect(ws_url.rstrip("/"), open_timeout=5, close_timeout=3) as ws:
            ws.send(json.dumps({"auth": auth_key}))
            deadline = time.time() + 6
            while time.time() < deadline:
                raw = ws.recv(timeout=2)
                data = json.loads(raw)
                if data.get("auth") is True:
                    report.ok("open interpreter ws", "authenticated")
                    return
                if data.get("type") == "error":
                    report.fail("open interpreter ws", str(data.get("content") or "authentication error"))
                    return
            report.fail("open interpreter ws", "authentication response timed out")
    except Exception as exc:
        report.fail("open interpreter ws", f"connection failed: {exc}")


def infer_cloud_url(cloud_env: dict[str, str], explicit: str) -> str:
    if explicit:
        return explicit
    host = cloud_env.get("APP_HOST", "127.0.0.1")
    port = cloud_env.get("APP_PORT", "8000")
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    return f"http://{host}:{port}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Wei project smoke checker")
    parser.add_argument("--online", action="store_true", help="check live Redis/cloud health endpoints")
    parser.add_argument("--cloud-url", default="", help="cloud server base URL for online checks")
    parser.add_argument("--cloud-only", action="store_true", help="skip local_launcher checks for server-only deploys")
    parser.add_argument("--check-one-api", action="store_true", help="call ONE_API_URL /models")
    parser.add_argument("--check-open-interpreter", action="store_true", help="authenticate against Open Interpreter websocket")
    args = parser.parse_args()

    root = repo_root()
    report = SmokeReport()
    check_layout(report, root, cloud_only=args.cloud_only)

    cloud_env_path, cloud_env, existing_cloud_envs = choose_cloud_env(root)
    check_cloud_env(report, root, cloud_env, cloud_env_path, existing_cloud_envs)
    if not cloud_env_path and (root / ".env").exists():
        report.warn("cloud env", "legacy root .env exists but runtime only loads cloud/.env")
    if args.cloud_only:
        report.warn("local env", "skipped because --cloud-only was set")
    else:
        check_local_env(report, root, cloud_env)

    if args.online:
        redis_url = cloud_env.get("REDIS_URL", "")
        if redis_url:
            check_redis_online(report, redis_url)
        else:
            report.fail("redis tcp", "REDIS_URL is missing")
        cloud_url = infer_cloud_url(cloud_env, args.cloud_url)
        check_cloud_health(report, cloud_url)
    else:
        report.warn("online checks", "skipped; pass --online to check Redis and cloud /health")

    if args.check_one_api:
        check_one_api(report, cloud_env.get("ONE_API_URL", ""), cloud_env.get("ONE_API_TOKEN", ""))
    if args.check_open_interpreter:
        check_open_interpreter_ws(
            report,
            cloud_env.get("OPEN_INTERPRETER_WS_URL", ""),
            cloud_env.get("OPEN_INTERPRETER_AUTH_KEY", ""),
        )

    report.print()
    return report.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
