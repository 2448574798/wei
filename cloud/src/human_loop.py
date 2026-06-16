from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

from redis import Redis

from src.runtime_config import REDIS_URL


CONFIRMATION_TTL_SECONDS = 24 * 60 * 60


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_text(value: object | None) -> str:
    return "" if value is None else str(value)


def _to_bool(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class ConfirmationRequest:
    id: str
    thread_id: str
    user_id: int | None
    username: str
    question: str
    context: str
    local_execution: bool
    created_at: str = field(default_factory=utcnow_iso)
    status: str = "pending"
    answer: str = ""
    approved: bool | None = None
    resolved_at: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "thread_id": self.thread_id,
            "user_id": self.user_id,
            "username": self.username,
            "question": self.question,
            "context": self.context,
            "local_execution": self.local_execution,
            "created_at": self.created_at,
            "status": self.status,
            "answer": self.answer,
            "approved": self.approved,
            "resolved_at": self.resolved_at,
        }


class ConfirmationStore:
    def __init__(self) -> None:
        self._redis = Redis.from_url(REDIS_URL, decode_responses=True)

    def _key(self, request_id: str) -> str:
        return f"wei:confirmation:{request_id}"

    def _serialize(self, item: ConfirmationRequest) -> dict[str, str]:
        return {
            "id": item.id,
            "thread_id": item.thread_id,
            "user_id": _to_text(item.user_id),
            "username": item.username,
            "question": item.question,
            "context": item.context,
            "local_execution": "1" if item.local_execution else "0",
            "created_at": item.created_at,
            "status": item.status,
            "answer": item.answer,
            "approved": "" if item.approved is None else ("1" if item.approved else "0"),
            "resolved_at": item.resolved_at,
        }

    def _deserialize(self, payload: dict[str, str]) -> dict | None:
        if not payload:
            return None
        approved_raw = payload.get("approved", "")
        approved = None if approved_raw == "" else _to_bool(approved_raw)
        user_id_raw = payload.get("user_id", "")
        user_id = int(user_id_raw) if user_id_raw else None
        return {
            "id": payload.get("id", ""),
            "thread_id": payload.get("thread_id", ""),
            "user_id": user_id,
            "username": payload.get("username", ""),
            "question": payload.get("question", ""),
            "context": payload.get("context", ""),
            "local_execution": _to_bool(payload.get("local_execution")),
            "created_at": payload.get("created_at", ""),
            "status": payload.get("status", "pending"),
            "answer": payload.get("answer", ""),
            "approved": approved,
            "resolved_at": payload.get("resolved_at", ""),
        }

    def create(
        self,
        *,
        thread_id: str,
        user_id: int | None,
        username: str,
        question: str,
        context: str,
        local_execution: bool,
    ) -> dict:
        request = ConfirmationRequest(
            id=str(uuid4()),
            thread_id=thread_id,
            user_id=user_id,
            username=username,
            question=question.strip(),
            context=context.strip(),
            local_execution=local_execution,
        )
        key = self._key(request.id)
        payload = self._serialize(request)
        with self._redis.pipeline() as pipe:
            pipe.hset(key, mapping=payload)
            pipe.expire(key, CONFIRMATION_TTL_SECONDS)
            pipe.execute()
        return request.to_dict()

    def get(self, request_id: str) -> dict | None:
        return self._deserialize(self._redis.hgetall(self._key(request_id)))

    def resolve(self, request_id: str, *, approved: bool, answer: str) -> dict | None:
        key = self._key(request_id)
        existing = self._redis.hgetall(key)
        if not existing:
            return None
        resolved_at = utcnow_iso()
        with self._redis.pipeline() as pipe:
            pipe.hset(
                key,
                mapping={
                    "status": "resolved",
                    "approved": "1" if approved else "0",
                    "answer": answer.strip(),
                    "resolved_at": resolved_at,
                },
            )
            pipe.expire(key, CONFIRMATION_TTL_SECONDS)
            pipe.execute()
        return self.get(request_id)


confirmation_store = ConfirmationStore()
