from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable


ContextDict = dict[str, Any]
AsyncEmitter = Callable[[str, dict[str, Any]], Any] | None
SyncEmitter = Callable[[str, dict[str, Any]], None] | None


_request_context: ContextVar[ContextDict] = ContextVar("request_context", default={})
_async_emitter: ContextVar[AsyncEmitter] = ContextVar("async_emitter", default=None)
_sync_emitter: ContextVar[SyncEmitter] = ContextVar("sync_emitter", default=None)


@contextmanager
def bind_execution_context(
    context: ContextDict,
    *,
    async_emitter: AsyncEmitter = None,
    sync_emitter: SyncEmitter = None,
):
    context_token = _request_context.set(context or {})
    async_token = _async_emitter.set(async_emitter)
    sync_token = _sync_emitter.set(sync_emitter)
    try:
        yield
    finally:
        _request_context.reset(context_token)
        _async_emitter.reset(async_token)
        _sync_emitter.reset(sync_token)


def get_execution_context() -> ContextDict:
    return dict(_request_context.get() or {})


async def emit_runtime_event(event_type: str, payload: dict[str, Any] | None = None) -> None:
    emitter = _async_emitter.get()
    if emitter is None:
        return
    result = emitter(event_type, payload or {})
    if hasattr(result, "__await__"):
        await result


def emit_runtime_event_sync(event_type: str, payload: dict[str, Any] | None = None) -> None:
    emitter = _sync_emitter.get()
    if emitter is None:
        return
    emitter(event_type, payload or {})
