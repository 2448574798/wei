from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterable


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def load_config(config_path: Path) -> dict:
    if not config_path.exists():
        raise FileNotFoundError(
            f"Missing config file: {config_path}\n"
            "Copy launcher_config.example.json to launcher_config.json and edit it first."
        )
    return json.loads(config_path.read_text(encoding="utf-8-sig"))


def load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[str(key).strip()] = value.strip().strip('"').strip("'")


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def write_log(log_path: Path, message: str) -> None:
    line = f"[{now_text()}] {message}\n"
    print(message)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(line)


def wait_for_url(url: str, timeout_sec: int, log_path: Path, name: str, headers: dict[str, str] | None = None) -> bool:
    if not url:
        return True
    deadline = time.time() + max(timeout_sec, 1)
    while time.time() < deadline:
        try:
            request = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(request, timeout=3) as response:
                if 200 <= response.status < 500:
                    write_log(log_path, f"{name} ready: {url}")
                    return True
        except urllib.error.URLError:
            pass
        except Exception:
            pass
        time.sleep(1)
    write_log(log_path, f"{name} not ready before timeout: {url}")
    return False


def expand_value(value: str, base_dir: Path) -> str:
    text = os.path.expandvars(str(value))
    text = text.replace("{APP_DIR}", str(base_dir))
    return text


def build_env(extra_env: dict[str, str], base_dir: Path) -> dict[str, str]:
    env = dict(os.environ)
    for key, value in extra_env.items():
        env[str(key)] = expand_value(str(value), base_dir)
    return env


def expand_mapping(mapping: dict[str, str], base_dir: Path) -> dict[str, str]:
    expanded: dict[str, str] = {}
    for key, value in mapping.items():
        expanded[str(key)] = expand_value(str(value), base_dir)
    return expanded


def iter_listening_pids(port: int) -> list[int]:
    if os.name != "nt":
        return []

    command = [
        "powershell",
        "-NoProfile",
        "-Command",
        (
            "Get-NetTCPConnection -LocalPort "
            f"{port} -State Listen -ErrorAction SilentlyContinue | "
            "Select-Object -ExpandProperty OwningProcess -Unique"
        ),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return []

    pids: list[int] = []
    for line in result.stdout.splitlines():
        text = line.strip()
        if text.isdigit():
            pids.append(int(text))
    return pids


def kill_ports(ports: Iterable[int], log_path: Path) -> None:
    seen: set[int] = set()
    for port in ports:
        if port <= 0:
            continue
        for pid in iter_listening_pids(port):
            if pid in seen or pid == os.getpid():
                continue
            seen.add(pid)
            try:
                write_log(log_path, f"Killing pid={pid} on port {port} before restart")
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            except Exception as exc:
                write_log(log_path, f"Failed to kill pid={pid} on port {port}: {exc}")


def spawn_process(entry: dict, base_dir: Path, logs_dir: Path, log_path: Path) -> subprocess.Popen:
    name = entry["name"]
    command = [expand_value(part, base_dir) for part in entry["command"]]
    cwd = Path(expand_value(entry.get("cwd", str(base_dir)), base_dir))
    env = build_env(entry.get("env", {}), base_dir)
    stdout_path = logs_dir / f"{name}.out.log"
    stderr_path = logs_dir / f"{name}.err.log"

    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

    stdout_handle = stdout_path.open("a", encoding="utf-8")
    stderr_handle = stderr_path.open("a", encoding="utf-8")
    try:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            stdout=stdout_handle,
            stderr=stderr_handle,
            creationflags=creationflags,
        )
    except Exception:
        stdout_handle.close()
        stderr_handle.close()
        raise

    process._wei_stdout_handle = stdout_handle  # type: ignore[attr-defined]
    process._wei_stderr_handle = stderr_handle  # type: ignore[attr-defined]
    write_log(log_path, f"Started {name} pid={process.pid} cmd={command}")
    return process


def close_process_handles(process: subprocess.Popen) -> None:
    for attr in ("_wei_stdout_handle", "_wei_stderr_handle"):
        handle = getattr(process, attr, None)
        if handle:
            handle.close()


def stop_process(entry: dict, process: subprocess.Popen, log_path: Path) -> None:
    name = entry["name"]
    if process.poll() is not None:
        write_log(log_path, f"{name} already stopped with code {process.returncode}")
        close_process_handles(process)
        return

    write_log(log_path, f"Stopping {name} pid={process.pid}")
    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.terminate()
        process.wait(timeout=8)
    except Exception:
        process.kill()
        process.wait(timeout=5)
    finally:
        write_log(log_path, f"Stopped {name} code={process.returncode}")
        close_process_handles(process)


def validate_config(config: dict) -> list[dict]:
    processes = config.get("processes")
    if not isinstance(processes, list) or not processes:
        raise ValueError("Config must contain a non-empty 'processes' list.")
    for item in processes:
        if item.get("enabled", True) is False:
            continue
        if not item.get("name"):
            raise ValueError("Each process must have a name.")
        command = item.get("command")
        if not isinstance(command, list) or not command:
            raise ValueError(f"Process {item.get('name', '<unknown>')} must have a non-empty command list.")
        ports = item.get("kill_ports", [])
        if ports and not isinstance(ports, list):
            raise ValueError(f"Process {item.get('name', '<unknown>')} kill_ports must be a list.")
        headers = item.get("ready_headers", {})
        if headers and not isinstance(headers, dict):
            raise ValueError(f"Process {item.get('name', '<unknown>')} ready_headers must be an object.")
    return processes


def main() -> int:
    base_dir = app_dir()
    config_name = "launcher_config.json"
    config_path = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else base_dir / config_name
    env_path = base_dir / "runtime_env.local"
    logs_dir = ensure_dir(base_dir / "launcher_logs")
    log_path = logs_dir / "launcher.log"

    load_env_file(env_path)
    write_log(log_path, f"Launcher started from {base_dir}")
    write_log(log_path, f"Using config {config_path}")
    if env_path.exists():
        write_log(log_path, f"Loaded local runtime env from {env_path}")

    config = load_config(config_path)
    processes = validate_config(config)
    started: list[tuple[dict, subprocess.Popen]] = []

    try:
        for entry in processes:
            if entry.get("enabled", True) is False:
                write_log(log_path, f"Skip disabled process: {entry.get('name', '<unknown>')}")
                continue
            kill_ports([int(port) for port in entry.get("kill_ports", [])], log_path)
            process = spawn_process(entry, base_dir, logs_dir, log_path)
            started.append((entry, process))

            ready_url = str(entry.get("ready_url", "") or "").strip()
            ready_timeout = int(entry.get("ready_timeout_sec", 0) or 0)
            if ready_url and ready_timeout > 0:
                ready_headers = expand_mapping(entry.get("ready_headers", {}), base_dir)
                wait_for_url(ready_url, ready_timeout, log_path, entry["name"], headers=ready_headers)

        write_log(log_path, "All processes started. Press Ctrl+C to stop them.")

        while True:
            dead = [(entry, process) for entry, process in started if process.poll() is not None]
            if dead:
                for entry, process in dead:
                    write_log(log_path, f"{entry['name']} exited unexpectedly with code {process.returncode}")
                return 1
            time.sleep(1)
    except KeyboardInterrupt:
        write_log(log_path, "Received Ctrl+C, shutting down.")
        return 0
    finally:
        for entry, process in reversed(started):
            stop_process(entry, process, log_path)


if __name__ == "__main__":
    raise SystemExit(main())
