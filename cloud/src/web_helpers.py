import uuid

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from src.auth_store import get_session_user
from src.runtime_config import AUTH_COOKIE_NAME


class LoginPayload(BaseModel):
    username: str = Field(default="")
    password: str = Field(default="")


class ChatRequestPayload(BaseModel):
    messages: list[dict] = Field(default_factory=list)
    thread_id: str | None = None
    include_tool_trace: bool = False
    local_execution: bool = False


class ConfirmationPayload(BaseModel):
    thread_id: str
    confirmation_id: str
    approved: bool = True
    response_text: str = Field(default="")
    include_tool_trace: bool = True
    local_execution: bool = False


def serialize_user(user: dict, include_bindings: bool = True) -> dict:
    return {
        "id": user["id"],
        "username": user["username"],
        "display_name": user["display_name"],
        "role": user["role"],
    }


async def parse_json_body(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON request body.") from exc

    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
    return data


def parse_payload_model(data: dict, model_cls, detail: str):
    try:
        return model_cls.model_validate(data)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=detail) from exc


def ensure_thread_id(thread_id: str | None) -> tuple[str, bool]:
    value = (thread_id or "").strip()
    if value:
        return value, False
    return str(uuid.uuid4()), True


def build_graph_config(thread_id: str, current_user: dict, local_execution: bool) -> dict:
    return {
        "configurable": {
            "thread_id": thread_id,
            "local_execution": local_execution,
            "user_id": current_user["id"],
            "username": current_user["username"],
        }
    }


def get_current_user(request: Request) -> dict | None:
    session_id = request.cookies.get(AUTH_COOKIE_NAME, "").strip()
    if not session_id:
        return None
    return get_session_user(session_id)


def require_authenticated_user(request: Request) -> dict:
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return user
