from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from fastapi import WebSocket


@dataclass
class BrowserWorkerConnection:
    worker_id: str
    websocket: WebSocket
    loop: asyncio.AbstractEventLoop
    loop_thread_id: int
    meta: dict[str, Any] = field(default_factory=dict)
    pending: dict[str, concurrent.futures.Future] = field(default_factory=dict)


class BrowserWorkerHub:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._workers: dict[str, BrowserWorkerConnection] = {}

    async def register(self, worker_id: str, websocket: WebSocket, meta: dict[str, Any] | None = None) -> None:
        loop = asyncio.get_running_loop()
        with self._lock:
            existing = self._workers.get(worker_id)
            if existing:
                self._fail_pending(existing, RuntimeError("Browser worker connection was replaced by a newer session."))
            self._workers[worker_id] = BrowserWorkerConnection(
                worker_id=worker_id,
                websocket=websocket,
                loop=loop,
                loop_thread_id=threading.get_ident(),
                meta=dict(meta or {}),
            )

    async def unregister(self, worker_id: str, websocket: WebSocket | None = None) -> None:
        with self._lock:
            existing = self._workers.get(worker_id)
            if not existing:
                return
            if websocket is not None and existing.websocket is not websocket:
                return
            self._workers.pop(worker_id, None)
            self._fail_pending(existing, RuntimeError("Browser worker disconnected."))

    async def list_workers(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "worker_id": worker_id,
                    "meta": dict(connection.meta),
                    "pending_count": len(connection.pending),
                }
                for worker_id, connection in sorted(self._workers.items())
            ]

    async def handle_message(self, worker_id: str, message: dict[str, Any]) -> None:
        message_type = str(message.get("type") or "").strip().lower()
        if message_type == "response":
            request_id = str(message.get("request_id") or "").strip()
            if not request_id:
                return
            with self._lock:
                connection = self._workers.get(worker_id)
                future = connection.pending.get(request_id) if connection else None
            if not future or future.done():
                return
            if bool(message.get("ok")):
                payload = message.get("payload")
                future.set_result(payload if isinstance(payload, dict) else {})
            else:
                error_text = str(message.get("error") or "Browser worker request failed.").strip()
                future.set_exception(RuntimeError(error_text))
            return

        if message_type == "heartbeat":
            with self._lock:
                connection = self._workers.get(worker_id)
                if connection:
                    connection.meta["last_heartbeat"] = message.get("at")

    async def request(self, worker_id: str, command: str, payload: dict[str, Any] | None = None, *, timeout_sec: int = 30) -> dict[str, Any]:
        return await asyncio.to_thread(self.request_sync, worker_id, command, payload, timeout_sec=timeout_sec)

    def request_sync(self, worker_id: str, command: str, payload: dict[str, Any] | None = None, *, timeout_sec: int = 30) -> dict[str, Any]:
        worker_name = str(worker_id or "").strip()
        command_name = str(command or "").strip()
        if not worker_name:
            raise RuntimeError("Browser worker id is not configured.")
        if not command_name:
            raise ValueError("Browser worker command cannot be empty.")

        request_id = str(uuid4())
        response_future: concurrent.futures.Future = concurrent.futures.Future()

        with self._lock:
            connection = self._workers.get(worker_name)
            if not connection:
                raise RuntimeError(f"Browser worker is not connected: {worker_name}")
            if threading.get_ident() == connection.loop_thread_id:
                raise RuntimeError("Browser worker sync request cannot run on the websocket event loop thread.")
            connection.pending[request_id] = response_future

        send_future = asyncio.run_coroutine_threadsafe(
            connection.websocket.send_json(
                {
                    "type": "command",
                    "request_id": request_id,
                    "command": command_name,
                    "payload": payload or {},
                }
            ),
            connection.loop,
        )

        try:
            send_future.result(timeout=timeout_sec)
            result = response_future.result(timeout=timeout_sec)
            return result if isinstance(result, dict) else {}
        except concurrent.futures.TimeoutError as exc:
            send_future.cancel()
            raise RuntimeError(f"Browser worker request timed out: {command_name}") from exc
        except Exception:
            send_future.cancel()
            raise
        finally:
            with self._lock:
                current = self._workers.get(worker_name)
                if current:
                    current.pending.pop(request_id, None)

    def _fail_pending(self, connection: BrowserWorkerConnection, exc: Exception) -> None:
        for future in list(connection.pending.values()):
            if not future.done():
                future.set_exception(exc)
        connection.pending.clear()


browser_worker_hub = BrowserWorkerHub()
