from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18100
DEFAULT_DEBUG_PORT = 19222
DEFAULT_USER_DATA_DIR = Path(r"D:\Download\playwright-user-data\edge-douyin-bridge")
DEFAULT_EDGE_PATHS = [
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
]


@dataclass
class BrowserJob:
    id: str
    title: str
    job_type: str
    status: str = "running"
    progress: list[str] = field(default_factory=list)
    result: str = ""
    error: str = ""
    cancel_requested: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "job_type": self.job_type,
            "status": self.status,
            "progress": list(self.progress),
            "result": self.result,
            "error": self.error,
            "cancel_requested": self.cancel_requested,
        }


class BrowserBridge:
    def __init__(self) -> None:
        self.host = os.getenv("BROWSER_BRIDGE_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST
        self.port = int(os.getenv("BROWSER_BRIDGE_PORT", str(DEFAULT_PORT)))
        self.token = os.getenv("BROWSER_BRIDGE_TOKEN", "").strip()
        self.edge_executable = self._resolve_edge_executable()
        self.user_data_dir = self._resolve_user_data_dir()
        self.python_executable = self._resolve_python_executable()
        self.debug_port = int(os.getenv("PLAYWRIGHT_EDGE_DEBUG_PORT", str(DEFAULT_DEBUG_PORT)))
        self.runner_script = Path(__file__).with_name("playwright_edge_runner.py")

        self._browser_lock = threading.Lock()
        self._jobs_lock = threading.Lock()
        self._jobs: dict[str, BrowserJob] = {}

    def _resolve_edge_executable(self) -> Path:
        configured = os.getenv("PLAYWRIGHT_EDGE_EXECUTABLE", "").strip()
        if configured:
            path = Path(configured)
            if path.exists():
                return path
            raise FileNotFoundError(f"PLAYWRIGHT_EDGE_EXECUTABLE not found: {path}")

        for candidate in DEFAULT_EDGE_PATHS:
            if candidate.exists():
                return candidate
        raise FileNotFoundError("Microsoft Edge executable not found.")

    def _resolve_user_data_dir(self) -> Path:
        configured = os.getenv("PLAYWRIGHT_EDGE_USER_DATA_DIR", "").strip()
        return Path(configured) if configured else DEFAULT_USER_DATA_DIR

    def _resolve_python_executable(self) -> Path:
        configured = os.getenv("PLAYWRIGHT_PYTHON_EXE", "").strip()
        if configured:
            path = Path(configured)
            if path.exists():
                return path
        return Path(sys.executable).resolve()

    def _append_job_progress(self, job: BrowserJob, message: str) -> None:
        text = re.sub(r"\s+", " ", (message or "").strip())
        if text:
            job.progress.append(text)

    def _cdp_base_url(self) -> str:
        return f"http://127.0.0.1:{self.debug_port}"

    def _debug_json_url(self, path: str) -> str:
        return self._cdp_base_url().rstrip("/") + path

    def _debug_endpoint_ready(self) -> bool:
        try:
            with urllib.request.urlopen(self._debug_json_url("/json/version"), timeout=2) as response:
                return 200 <= response.status < 300
        except Exception:
            return False

    def _ensure_browser_started(self, initial_url: str = "about:blank") -> bool:
        if self._debug_endpoint_ready():
            return False

        with self._browser_lock:
            if self._debug_endpoint_ready():
                return False

            self.user_data_dir.mkdir(parents=True, exist_ok=True)
            creationflags = 0
            if os.name == "nt":
                creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

            subprocess.Popen(
                [
                    str(self.edge_executable),
                    f"--remote-debugging-port={self.debug_port}",
                    f"--user-data-dir={self.user_data_dir}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    initial_url or "about:blank",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )

        deadline = time.time() + 20
        while time.time() < deadline:
            if self._debug_endpoint_ready():
                return True
            time.sleep(1)
        raise RuntimeError("Edge remote debugging endpoint did not become ready in time.")

    def _run_runner_command(self, payload: dict[str, Any], timeout_sec: int) -> dict[str, Any]:
        env = dict(os.environ)
        process = subprocess.run(
            [str(self.python_executable), str(self.runner_script), "--bridge-stdio"],
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
            env=env,
        )
        if process.returncode != 0:
            detail = (process.stderr or process.stdout or "").strip()
            raise RuntimeError(detail or f"Playwright runner failed with exit code {process.returncode}.")

        output = (process.stdout or "").strip()
        if not output:
            raise RuntimeError("Playwright runner returned no output.")
        return json.loads(output)

    def browser_ready(self) -> bool:
        return self._debug_endpoint_ready()

    def open_page(self, url: str, wait_ms: int = 3000) -> dict[str, Any]:
        started_fresh = self._ensure_browser_started(url or "about:blank")
        return self._run_runner_command(
            {
                "action": "open",
                "cdp_url": self._cdp_base_url(),
                "url": url,
                "wait_ms": wait_ms,
                "reuse_existing_page": started_fresh,
                "close_page": False,
            },
            timeout_sec=60,
        )

    def snapshot(self, url: str, instruction: str = "", wait_ms: int = 3000, selector: str = "body") -> dict[str, Any]:
        self._ensure_browser_started("about:blank")
        return self._run_runner_command(
            {
                "action": "snapshot",
                "cdp_url": self._cdp_base_url(),
                "url": url,
                "instruction": instruction,
                "wait_ms": wait_ms,
                "selector": selector,
                "reuse_existing_page": False,
                "close_page": True,
            },
            timeout_sec=max(60, int(wait_ms / 1000) + 45),
        )

    def interact(
        self,
        url: str = "",
        *,
        instruction: str = "",
        steps: list[dict[str, Any]] | None = None,
        wait_ms: int = 1000,
    ) -> dict[str, Any]:
        self._ensure_browser_started(url or "about:blank")
        interaction_steps = steps or []
        timeout_sec = max(60, int(wait_ms / 1000) + max(1, len(interaction_steps)) * 12)
        return self._run_runner_command(
            {
                "action": "interact",
                "cdp_url": self._cdp_base_url(),
                "url": url,
                "instruction": instruction,
                "steps": interaction_steps,
                "wait_ms": wait_ms,
                "reuse_existing_page": True,
                "close_page": False,
            },
            timeout_sec=timeout_sec,
        )

    def start_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        job_type = str(payload.get("job_type") or "").strip()
        if job_type != "watch_text":
            raise ValueError(f"Unsupported job_type: {job_type or '<empty>'}")

        job = BrowserJob(
            id=str(uuid4()),
            title=str(payload.get("title") or "Local webpage watch").strip() or "Local webpage watch",
            job_type=job_type,
        )
        with self._jobs_lock:
            self._jobs[job.id] = job

        thread = threading.Thread(
            target=self._run_watch_text_job,
            args=(job.id, payload),
            daemon=True,
        )
        thread.start()
        return {"ok": True, "job_id": job.id, "status": job.status}

    def _run_watch_text_job(self, job_id: str, payload: dict[str, Any]) -> None:
        job = self._jobs[job_id]
        url = str(payload.get("url") or "").strip()
        keyword = str(payload.get("keyword") or "").strip()
        rounds = max(1, min(int(payload.get("rounds") or 20), 200))
        interval_sec = max(2, min(int(payload.get("interval_sec") or 8), 300))
        wait_ms = max(1000, min(int(payload.get("wait_ms") or 3000), 15000))

        try:
            if not url:
                raise ValueError("url is required")
            if not keyword:
                raise ValueError("keyword is required")

            lowered_keyword = keyword.lower()
            for index in range(rounds):
                if job.cancel_requested:
                    job.status = "cancelled"
                    self._append_job_progress(job, "Job cancelled.")
                    job.result = "Job cancelled."
                    return

                snapshot = self.snapshot(url, instruction=keyword, wait_ms=wait_ms)
                text = str(snapshot.get("text") or "")
                excerpt = text[:240] or "[No body text extracted]"
                self._append_job_progress(job, f"Round {index + 1}: {excerpt}")

                if lowered_keyword in text.lower():
                    job.status = "completed"
                    job.result = f"Matched keyword: {keyword}\n{text[:2000]}"
                    self._append_job_progress(job, f"Matched keyword: {keyword}")
                    return

                if index < rounds - 1:
                    time.sleep(interval_sec)

            job.status = "completed"
            job.result = f"Keyword not found: {keyword}"
            self._append_job_progress(job, f"Keyword not found: {keyword}")
        except Exception as exc:
            job.status = "failed"
            job.error = str(exc)
            self._append_job_progress(job, f"Execution failed: {exc}")

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            return job.to_dict() if job else None

    def cancel_job(self, job_id: str) -> dict[str, Any] | None:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            job.cancel_requested = True
            if job.status == "running":
                job.status = "cancelling"
            self._append_job_progress(job, "Cancellation requested.")
            return job.to_dict()


bridge = BrowserBridge()


class BrowserBridgeHandler(BaseHTTPRequestHandler):
    server_version = "WeiBrowserBridge/0.3"

    def do_GET(self) -> None:
        try:
            self._authorize()
            if self.path == "/health":
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "status": "ok",
                        "browser_ready": bridge.browser_ready(),
                        "host": bridge.host,
                        "port": bridge.port,
                    },
                )
                return

            job_id = self._extract_job_id()
            if job_id:
                job = bridge.get_job(job_id)
                if not job:
                    self._send_json(HTTPStatus.NOT_FOUND, {"detail": "Job not found."})
                    return
                self._send_json(HTTPStatus.OK, job)
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"detail": "Not found."})
        except Exception as exc:
            self._send_error_json(exc)

    def do_POST(self) -> None:
        try:
            self._authorize()
            payload = self._read_json()

            if self.path == "/page/open":
                result = bridge.open_page(str(payload.get("url") or "").strip(), int(payload.get("wait_ms") or 3000))
                self._send_json(HTTPStatus.OK, result)
                return

            if self.path == "/page/snapshot":
                result = bridge.snapshot(
                    str(payload.get("url") or "").strip(),
                    instruction=str(payload.get("instruction") or "").strip(),
                    wait_ms=int(payload.get("wait_ms") or 3000),
                    selector=str(payload.get("selector") or "body").strip() or "body",
                )
                self._send_json(HTTPStatus.OK, result)
                return

            if self.path == "/page/interact":
                result = bridge.interact(
                    str(payload.get("url") or "").strip(),
                    instruction=str(payload.get("instruction") or "").strip(),
                    steps=payload.get("steps") if isinstance(payload.get("steps"), list) else [],
                    wait_ms=int(payload.get("wait_ms") or 1000),
                )
                self._send_json(HTTPStatus.OK, result)
                return

            if self.path == "/jobs/start":
                result = bridge.start_job(payload)
                self._send_json(HTTPStatus.OK, result)
                return

            job_id = self._extract_job_id(suffix="/cancel")
            if job_id:
                job = bridge.cancel_job(job_id)
                if not job:
                    self._send_json(HTTPStatus.NOT_FOUND, {"detail": "Job not found."})
                    return
                self._send_json(HTTPStatus.OK, job)
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"detail": "Not found."})
        except Exception as exc:
            self._send_error_json(exc)

    def _extract_job_id(self, suffix: str = "") -> str | None:
        prefix = "/jobs/"
        if not self.path.startswith(prefix):
            return None
        tail = self.path[len(prefix) :]
        if suffix:
            if not tail.endswith(suffix):
                return None
            tail = tail[: -len(suffix)]
        if not tail or "/" in tail.strip("/"):
            return None
        return tail.strip("/")

    def _authorize(self) -> None:
        if not bridge.token:
            return
        auth = self.headers.get("Authorization", "").strip()
        expected = f"Bearer {bridge.token}"
        if auth != expected:
            raise PermissionError("Unauthorized")

    def _read_json(self) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0") or "0")
        if content_length <= 0:
            return {}
        raw = self.rfile.read(content_length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_error_json(self, exc: Exception) -> None:
        if isinstance(exc, PermissionError):
            self._send_json(HTTPStatus.UNAUTHORIZED, {"detail": str(exc)})
            return
        if isinstance(exc, FileNotFoundError):
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"detail": str(exc)})
            return
        if isinstance(exc, ValueError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"detail": str(exc)})
            return
        if isinstance(exc, subprocess.TimeoutExpired):
            self._send_json(HTTPStatus.GATEWAY_TIMEOUT, {"detail": str(exc)})
            return
        self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"detail": str(exc)})

    def log_message(self, format: str, *args: Any) -> None:
        return


def main() -> int:
    server = ThreadingHTTPServer((bridge.host, bridge.port), BrowserBridgeHandler)
    print(f"Browser Bridge listening on http://{bridge.host}:{bridge.port}")
    print(f"Edge executable: {bridge.edge_executable}")
    print(f"User data dir: {bridge.user_data_dir}")
    print(f"CDP endpoint: {bridge._cdp_base_url()}")
    if bridge.token:
        print("Auth token: configured")
    else:
        print("Auth token: not configured")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
