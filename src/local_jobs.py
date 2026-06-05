from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Thread
from uuid import uuid4

from redis import Redis

from src.runtime_config import REDIS_URL


JOB_TTL_SECONDS = 7 * 24 * 60 * 60


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_text(value: object | None) -> str:
    return "" if value is None else str(value)


def _to_bool(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class LocalJob:
    id: str
    title: str
    thread_id: str
    user_id: int | None
    username: str
    created_at: str = field(default_factory=utcnow_iso)
    updated_at: str = field(default_factory=utcnow_iso)
    status: str = "running"
    progress: list[str] = field(default_factory=list)
    result: str = ""
    error: str = ""
    cancel_requested: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "thread_id": self.thread_id,
            "user_id": self.user_id,
            "username": self.username,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "status": self.status,
            "progress": list(self.progress),
            "result": self.result,
            "error": self.error,
            "cancel_requested": self.cancel_requested,
        }


class LocalJobStore:
    def __init__(self) -> None:
        self._redis = Redis.from_url(REDIS_URL, decode_responses=True)

    def _meta_key(self, job_id: str) -> str:
        return f"wei:job:{job_id}:meta"

    def _progress_key(self, job_id: str) -> str:
        return f"wei:job:{job_id}:progress"

    def _touch_ttl(self, job_id: str) -> None:
        with self._redis.pipeline() as pipe:
            pipe.expire(self._meta_key(job_id), JOB_TTL_SECONDS)
            pipe.expire(self._progress_key(job_id), JOB_TTL_SECONDS)
            pipe.execute()

    def create(self, *, title: str, thread_id: str, user_id: int | None, username: str) -> dict:
        job = LocalJob(
            id=str(uuid4()),
            title=title.strip() or "本地长任务",
            thread_id=thread_id,
            user_id=user_id,
            username=username,
        )
        meta_key = self._meta_key(job.id)
        progress_key = self._progress_key(job.id)
        with self._redis.pipeline() as pipe:
            pipe.hset(
                meta_key,
                mapping={
                    "id": job.id,
                    "title": job.title,
                    "thread_id": job.thread_id,
                    "user_id": _to_text(job.user_id),
                    "username": job.username,
                    "created_at": job.created_at,
                    "updated_at": job.updated_at,
                    "status": job.status,
                    "result": job.result,
                    "error": job.error,
                    "cancel_requested": "0",
                },
            )
            pipe.delete(progress_key)
            pipe.expire(meta_key, JOB_TTL_SECONDS)
            pipe.expire(progress_key, JOB_TTL_SECONDS)
            pipe.execute()
        return job.to_dict()

    def get(self, job_id: str) -> dict | None:
        meta = self._redis.hgetall(self._meta_key(job_id))
        if not meta:
            return None
        user_id_raw = meta.get("user_id", "")
        return {
            "id": meta.get("id", ""),
            "title": meta.get("title", ""),
            "thread_id": meta.get("thread_id", ""),
            "user_id": int(user_id_raw) if user_id_raw else None,
            "username": meta.get("username", ""),
            "created_at": meta.get("created_at", ""),
            "updated_at": meta.get("updated_at", ""),
            "status": meta.get("status", "running"),
            "progress": self._redis.lrange(self._progress_key(job_id), 0, -1),
            "result": meta.get("result", ""),
            "error": meta.get("error", ""),
            "cancel_requested": _to_bool(meta.get("cancel_requested")),
        }

    def append_progress(self, job_id: str, message: str) -> None:
        text = (message or "").strip()
        if not text:
            return
        updated_at = utcnow_iso()
        with self._redis.pipeline() as pipe:
            pipe.rpush(self._progress_key(job_id), text)
            pipe.hset(self._meta_key(job_id), mapping={"updated_at": updated_at})
            pipe.expire(self._meta_key(job_id), JOB_TTL_SECONDS)
            pipe.expire(self._progress_key(job_id), JOB_TTL_SECONDS)
            pipe.execute()

    def finish(self, job_id: str, result: str) -> None:
        with self._redis.pipeline() as pipe:
            pipe.hset(
                self._meta_key(job_id),
                mapping={
                    "status": "completed",
                    "result": (result or "").strip(),
                    "updated_at": utcnow_iso(),
                },
            )
            pipe.expire(self._meta_key(job_id), JOB_TTL_SECONDS)
            pipe.expire(self._progress_key(job_id), JOB_TTL_SECONDS)
            pipe.execute()

    def fail(self, job_id: str, error: str) -> None:
        with self._redis.pipeline() as pipe:
            pipe.hset(
                self._meta_key(job_id),
                mapping={
                    "status": "failed",
                    "error": (error or "").strip(),
                    "updated_at": utcnow_iso(),
                },
            )
            pipe.expire(self._meta_key(job_id), JOB_TTL_SECONDS)
            pipe.expire(self._progress_key(job_id), JOB_TTL_SECONDS)
            pipe.execute()

    def cancel(self, job_id: str) -> dict | None:
        meta_key = self._meta_key(job_id)
        if not self._redis.exists(meta_key):
            return None
        updates = {
            "cancel_requested": "1",
            "updated_at": utcnow_iso(),
        }
        current_status = self._redis.hget(meta_key, "status")
        if current_status == "running":
            updates["status"] = "cancelling"
        with self._redis.pipeline() as pipe:
            pipe.hset(meta_key, mapping=updates)
            pipe.expire(meta_key, JOB_TTL_SECONDS)
            pipe.expire(self._progress_key(job_id), JOB_TTL_SECONDS)
            pipe.execute()
        return self.get(job_id)

    def is_cancel_requested(self, job_id: str) -> bool:
        return _to_bool(self._redis.hget(self._meta_key(job_id), "cancel_requested"))

    def run_in_background(self, job_id: str, target, *args, **kwargs) -> None:
        def runner():
            try:
                result = target(*args, **kwargs)
                if self.is_cancel_requested(job_id):
                    self.fail(job_id, "任务已被请求取消，但底层执行器暂不支持强制中断。")
                    return
                self.finish(job_id, result)
            except Exception as exc:
                self.fail(job_id, str(exc))

        thread = Thread(target=runner, daemon=True)
        thread.start()


local_job_store = LocalJobStore()
