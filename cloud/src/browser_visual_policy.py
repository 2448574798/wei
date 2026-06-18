from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from src.runtime_config import (
    BROWSER_VISION_ACTION_MIN_CONFIDENCE,
    BROWSER_VISION_MODEL,
    BROWSER_VISION_VERIFY_MAX_RETRIES,
    BROWSER_VISION_VERIFY_MIN_CONFIDENCE,
)


VISUAL_ACTION_NAMES = {"click", "click_xy", "scroll", "press", "key", "type", "type_text", "wait"}
VISUAL_DECISION_STATUSES = {"continue", "done", "need_user"}
VISUAL_VERIFICATION_STATUSES = {"continue", "done", "need_user", "retry"}
VISUAL_RISK_LEVELS = {"none", "low", "medium", "high"}


def _compact_text(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())[:limit]


def _clamp_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except Exception:
        number = default
    return max(minimum, min(number, maximum))


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(float(value))
    except Exception:
        number = default
    return max(minimum, min(number, maximum))


class VisualActionSchema(BaseModel):
    """Cloud-side action contract; the local Worker only receives validated single-step actions."""

    model_config = ConfigDict(extra="allow")

    action: str
    reason: str = ""
    target_description: str = ""
    expected_change: str = ""
    confidence: float = 0.0
    wait_ms: int = 800
    x: int | None = None
    y: int | None = None
    candidate_id: str | None = None
    delta_y: int | None = None
    direction: str | None = None
    key: str | None = None
    text: str | None = None
    task_id: str = ""
    action_id: str = ""

    @field_validator("action", mode="before")
    @classmethod
    def _normalise_action_name(cls, value: Any) -> str:
        action_name = str(value or "").strip().lower()
        if action_name not in VISUAL_ACTION_NAMES:
            raise ValueError("unsupported visual action")
        return action_name

    @field_validator("reason", "target_description", "expected_change", mode="before")
    @classmethod
    def _normalise_short_text(cls, value: Any) -> str:
        return _compact_text(value, 240)

    @field_validator("task_id", "action_id", mode="before")
    @classmethod
    def _normalise_id(cls, value: Any) -> str:
        return _compact_text(value, 160)

    @field_validator("confidence", mode="before")
    @classmethod
    def _normalise_confidence(cls, value: Any) -> float:
        return _clamp_float(value, 0.0, 0.0, 1.0)

    @field_validator("wait_ms", mode="before")
    @classmethod
    def _normalise_wait_ms(cls, value: Any) -> int:
        return _clamp_int(value if value not in (None, "") else 800, 800, 0, 10000)

    @field_validator("x", "y", mode="before")
    @classmethod
    def _normalise_coordinate(cls, value: Any) -> int | None:
        if value is None or value == "":
            return None
        return _clamp_int(value, 0, 0, 1000)

    @field_validator("delta_y", mode="before")
    @classmethod
    def _normalise_delta_y(cls, value: Any) -> int | None:
        if value is None or value == "":
            return None
        return _clamp_int(value, 700, -3000, 3000)

    @field_validator("direction", mode="before")
    @classmethod
    def _normalise_direction(cls, value: Any) -> str | None:
        if value is None or value == "":
            return None
        direction = str(value).strip().lower()
        return "up" if direction in {"up", "backward"} else "down"

    @field_validator("key", mode="before")
    @classmethod
    def _normalise_key(cls, value: Any) -> str | None:
        if value is None:
            return None
        return str(value).strip()[:80] or None

    @field_validator("candidate_id", mode="before")
    @classmethod
    def _normalise_candidate_id(cls, value: Any) -> str | None:
        if value is None:
            return None
        return _compact_text(value, 120) or None

    @field_validator("text", mode="before")
    @classmethod
    def _normalise_type_text(cls, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value)
        return text[:2000] if text else None

    @model_validator(mode="after")
    def _validate_action_contract(self) -> "VisualActionSchema":
        if self.action in {"click", "click_xy"} and not self.candidate_id and (self.x is None or self.y is None):
            raise ValueError("click action requires x/y or candidate_id")
        if self.action == "scroll" and self.delta_y is None and not self.direction:
            self.direction = "down"
        if self.action in {"press", "key"} and not self.key:
            raise ValueError("key action requires key")
        if self.action in {"type", "type_text"} and not self.text:
            raise ValueError("type action requires text")
        return self

    def to_payload(self) -> dict:
        payload: dict = {
            "action": self.action,
            "reason": self.reason,
            "target_description": self.target_description,
            "expected_change": self.expected_change,
            "wait_ms": self.wait_ms,
            "confidence": self.confidence,
        }
        for key in ("x", "y", "candidate_id", "delta_y", "direction", "key", "text", "task_id", "action_id"):
            value = getattr(self, key)
            if value not in (None, ""):
                payload[key] = value
        return payload


class VisualDecisionSchema(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    status: str = "continue"
    summary: str = ""
    model_name: str = Field(default=BROWSER_VISION_MODEL, alias="model")

    @field_validator("status", mode="before")
    @classmethod
    def _normalise_status(cls, value: Any) -> str:
        status = str(value or "continue").strip().lower()
        return status if status in VISUAL_DECISION_STATUSES else "continue"

    @field_validator("summary", mode="before")
    @classmethod
    def _normalise_summary(cls, value: Any) -> str:
        return _compact_text(value, 500)

    @field_validator("model_name", mode="before")
    @classmethod
    def _normalise_model(cls, value: Any) -> str:
        return str(value or BROWSER_VISION_MODEL).strip() or BROWSER_VISION_MODEL

    def to_payload(self, actions: list[dict]) -> dict:
        return {
            "status": self.status,
            "summary": self.summary,
            "actions": actions[:2],
            "model": self.model_name,
        }


class VisualVerificationSchema(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    status: str = "continue"
    changed: bool = False
    matched_expected_change: bool | None = None
    misclick: bool = False
    confidence: float = 0.0
    observed_change: str = ""
    risk: str = "none"
    summary: str = ""
    retry_hint: str = ""
    model_name: str = Field(default=BROWSER_VISION_MODEL, alias="model")

    @field_validator("status", mode="before")
    @classmethod
    def _normalise_status(cls, value: Any) -> str:
        status = str(value or "continue").strip().lower()
        return status if status in VISUAL_VERIFICATION_STATUSES else "continue"

    @field_validator("confidence", mode="before")
    @classmethod
    def _normalise_confidence(cls, value: Any) -> float:
        return _clamp_float(value, 0.0, 0.0, 1.0)

    @field_validator("risk", mode="before")
    @classmethod
    def _normalise_risk(cls, value: Any) -> str:
        risk = str(value or "none").strip().lower()
        return risk if risk in VISUAL_RISK_LEVELS else "none"

    @field_validator("observed_change", "summary", "retry_hint", mode="before")
    @classmethod
    def _normalise_text(cls, value: Any) -> str:
        return _compact_text(value, 500)

    @field_validator("model_name", mode="before")
    @classmethod
    def _normalise_model(cls, value: Any) -> str:
        return str(value or BROWSER_VISION_MODEL).strip() or BROWSER_VISION_MODEL

    def to_payload(self) -> dict:
        matched_expected_change = self.changed if self.matched_expected_change is None else self.matched_expected_change
        return {
            "status": self.status,
            "changed": self.changed,
            "matched_expected_change": bool(matched_expected_change),
            "misclick": self.misclick,
            "confidence": self.confidence,
            "observed_change": self.observed_change,
            "risk": self.risk,
            "summary": self.summary,
            "retry_hint": self.retry_hint,
            "model": self.model_name,
        }


def normalise_failure_type(value: str) -> str:
    text = re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower()).strip("_")
    return text or "unknown"


def _context_text(value: object, *, limit: int = 2200) -> str:
    chunks: list[str] = []

    def visit(item: object, depth: int = 0) -> None:
        if depth > 4 or len(" ".join(chunks)) > limit:
            return
        if isinstance(item, dict):
            for key, child in item.items():
                if key in {"image_base64", "text_raw", "raw"}:
                    continue
                if key in {"text", "ariaLabel", "title", "placeholder", "dataE2e", "className", "role", "tag", "type", "href", "selector", "summary", "observed_change", "reason"}:
                    text = str(child or "").strip()
                    if text:
                        chunks.append(text)
                else:
                    visit(child, depth + 1)
        elif isinstance(item, list):
            for child in item[:8]:
                visit(child, depth + 1)

    visit(value)
    return re.sub(r"\s+", " ", " ".join(chunks).lower()).strip()[:limit]


def _looks_like_login_wall(text: str) -> bool:
    return any(
        marker in text
        for marker in (
            "login",
            "log in",
            "sign in",
            "signin",
            "captcha",
            "登录",
            "登陆",
            "验证码",
            "验证",
            "账号",
            "密码",
            "请先登录",
        )
    )


def _looks_like_overlay(text: str) -> bool:
    return any(
        marker in text
        for marker in (
            "dialog",
            "modal",
            "overlay",
            "popup",
            "pop-up",
            "mask",
            "drawer",
            "toast",
            "弹窗",
            "浮层",
            "遮罩",
            "对话框",
            "权限",
            "allow",
            "consent",
        )
    )


def _context_viewport_mismatch(context: dict) -> bool:
    hit_test = context.get("hit_test") if isinstance(context.get("hit_test"), dict) else {}
    screenshot = context.get("screenshot") if isinstance(context.get("screenshot"), dict) else {}
    hit_viewport = hit_test.get("viewport") if isinstance(hit_test.get("viewport"), dict) else {}
    shot_viewport = screenshot.get("viewport") if isinstance(screenshot.get("viewport"), dict) else {}
    if not hit_viewport or not shot_viewport:
        return False
    try:
        width_diff = abs(int(hit_viewport.get("width") or 0) - int(shot_viewport.get("width") or 0))
        height_diff = abs(int(hit_viewport.get("height") or 0) - int(shot_viewport.get("height") or 0))
    except Exception:
        return False
    return width_diff > 24 or height_diff > 24


def _context_dpr_mismatch(context: dict) -> bool:
    before = context.get("before") if isinstance(context.get("before"), dict) else {}
    after = context.get("after") if isinstance(context.get("after"), dict) else {}
    screenshot = context.get("screenshot") if isinstance(context.get("screenshot"), dict) else {}
    values = []
    for item in (before, after, screenshot):
        dpr = item.get("device_pixel_ratio")
        if dpr is None and isinstance(item.get("viewport"), dict):
            dpr = item["viewport"].get("devicePixelRatio") or item["viewport"].get("dpr")
        if dpr is not None:
            try:
                values.append(float(dpr))
            except Exception:
                continue
    return bool(values) and max(values) - min(values) > 0.05


def _context_page_state_mismatch(context: dict) -> bool:
    before = context.get("page_state_before") if isinstance(context.get("page_state_before"), dict) else {}
    after = context.get("page_state_after") if isinstance(context.get("page_state_after"), dict) else {}
    if not before or not after:
        screenshot = context.get("screenshot") if isinstance(context.get("screenshot"), dict) else {}
        hit_test = context.get("hit_test") if isinstance(context.get("hit_test"), dict) else {}
        before = screenshot.get("page_state") if isinstance(screenshot.get("page_state"), dict) else before
        after = hit_test.get("page_state") if isinstance(hit_test.get("page_state"), dict) else after
    if not before or not after:
        return False

    before_url = str(before.get("url") or "").strip().split("#", 1)[0]
    after_url = str(after.get("url") or "").strip().split("#", 1)[0]
    if before_url and after_url and before_url != after_url:
        return True

    before_viewport = before.get("viewport") if isinstance(before.get("viewport"), dict) else {}
    after_viewport = after.get("viewport") if isinstance(after.get("viewport"), dict) else {}
    try:
        width_diff = abs(int(before_viewport.get("width") or 0) - int(after_viewport.get("width") or 0))
        height_diff = abs(int(before_viewport.get("height") or 0) - int(after_viewport.get("height") or 0))
    except Exception:
        width_diff = height_diff = 0
    if width_diff > 24 or height_diff > 24:
        return True

    try:
        before_dpr = float(before_viewport.get("devicePixelRatio") or before_viewport.get("dpr") or 1)
        after_dpr = float(after_viewport.get("devicePixelRatio") or after_viewport.get("dpr") or 1)
    except Exception:
        before_dpr = after_dpr = 1.0
    if abs(before_dpr - after_dpr) > 0.05:
        return True

    before_scroll = before.get("scroll") if isinstance(before.get("scroll"), dict) else {}
    after_scroll = after.get("scroll") if isinstance(after.get("scroll"), dict) else {}
    try:
        scroll_diff = abs(int(before_scroll.get("y") or 0) - int(after_scroll.get("y") or 0))
    except Exception:
        scroll_diff = 0
    return scroll_diff > 160


def _context_page_refreshed(context: dict, reason_text: str) -> bool:
    combined = f"{reason_text} {_context_text(context)}"
    if any(marker in combined for marker in ("reload", "refreshed", "refresh", "重新加载", "刷新")):
        return True
    action_result = context.get("action_result") if isinstance(context.get("action_result"), dict) else {}
    result = action_result.get("result") if isinstance(action_result.get("result"), dict) else action_result
    target = result.get("target") if isinstance(result.get("target"), dict) else {}
    before_url = str(target.get("beforeUrl") or "").strip()
    after_url = str(target.get("afterUrl") or "").strip()
    expected = str((context.get("action") or {}).get("expected_change") if isinstance(context.get("action"), dict) else "").lower()
    expects_navigation = bool(re.search(r"\b(navigate|navigation|detail page|new page|open page)\b", expected)) or any(
        word in expected for word in ("进入", "打开页面", "跳转", "详情页")
    )
    if before_url and after_url and before_url != after_url and not expects_navigation:
        return True
    return False


def _classify_visual_context(source_name: str, reason_text: str, context: dict) -> tuple[str, str] | None:
    combined_text = f"{reason_text.lower()} {_context_text(context)}"
    if _context_page_state_mismatch(context) or "page state mismatch" in combined_text:
        return "page_state_mismatch", "Screenshot, snapshot, or hit-test came from a different page state."
    if _looks_like_login_wall(combined_text):
        return "login_wall_blocking", "Page is blocked by login/captcha/verification UI."
    if source_name in {"click_preflight", "verification"} and _looks_like_overlay(combined_text):
        return "overlay_blocking_target", "An overlay or dialog appears to be blocking the intended target."
    if source_name == "click_preflight" and _context_viewport_mismatch(context):
        return "viewport_mismatch", "Screenshot viewport and hit-test viewport do not match."
    if source_name in {"click_preflight", "verification"} and _context_dpr_mismatch(context):
        return "dpr_coordinate_mismatch", "Device pixel ratio changed across screenshot/action context."
    if source_name == "verification" and _context_page_refreshed(context, reason_text):
        return "page_refreshed_unexpectedly", "Page navigated or refreshed after the action."
    return None


def normalise_visual_action(action: dict) -> dict | None:
    if not isinstance(action, dict):
        return None
    try:
        return VisualActionSchema.model_validate(action).to_payload()
    except ValidationError:
        return None
    except Exception:
        return None


def visual_action_safety_issue(action: dict) -> str:
    action_name = str(action.get("action") or "").strip().lower()
    if action_name == "wait":
        return ""
    confidence = float(action.get("confidence") or 0.0)
    if confidence < BROWSER_VISION_ACTION_MIN_CONFIDENCE:
        return f"low confidence {confidence:.2f} < {BROWSER_VISION_ACTION_MIN_CONFIDENCE:.2f}"
    if action_name in {"click", "click_xy", "type", "type_text"}:
        if not str(action.get("target_description") or "").strip():
            return "missing target_description"
        if not str(action.get("expected_change") or "").strip():
            return "missing expected_change"
    return ""


def classify_visual_failure(
    source: str,
    reason: str = "",
    *,
    status: str = "",
    retry_count: int = 0,
    context: dict | None = None,
) -> dict:
    source_name = str(source or "").strip().lower()
    reason_text = re.sub(r"\s+", " ", str(reason or "").strip())[:500]
    status_name = str(status or "").strip().lower()
    context_payload = context if isinstance(context, dict) else {}

    failure_type = "unknown"
    severity = "warning"
    recoverable = True
    diagnostic_hint = ""

    if source_name in {"", "none"} and not reason_text:
        failure_type = "none"
        severity = "info"
        recoverable = True
    elif source_name == "model_no_action":
        failure_type = "model_no_action"
        severity = "warning"
        recoverable = False
    elif source_name == "action_safety":
        if "low confidence" in reason_text:
            failure_type = "action_low_confidence"
        elif "target_description" in reason_text:
            failure_type = "action_missing_target"
        elif "expected_change" in reason_text:
            failure_type = "action_missing_expected_change"
        else:
            failure_type = "action_safety_blocked"
        severity = "warning"
        recoverable = True
    elif source_name == "click_preflight":
        context_failure = _classify_visual_context(source_name, reason_text, context_payload)
        if context_failure:
            failure_type, diagnostic_hint = context_failure
        elif "background" in reason_text:
            failure_type = "click_landed_on_background"
        elif "no actionable" in reason_text:
            failure_type = "click_no_actionable_target"
        elif "target mismatch" in reason_text:
            failure_type = "click_target_mismatch"
        elif "hit-test failed" in reason_text:
            failure_type = "click_hit_test_failed"
        else:
            failure_type = "click_preflight_rejected"
        severity = "warning"
        recoverable = True
    elif source_name == "click_preflight_unavailable":
        failure_type = "click_preflight_unavailable"
        severity = "error"
        recoverable = False
    elif source_name == "action_execution":
        failure_type = "action_execution_failed"
        severity = "error"
        recoverable = False
    elif source_name == "verification_unavailable":
        failure_type = "verification_unavailable"
        severity = "error"
        recoverable = False
    elif source_name == "verification":
        context_failure = _classify_visual_context(source_name, reason_text, context_payload)
        if context_failure:
            failure_type, diagnostic_hint = context_failure
            if failure_type in {"login_wall_blocking", "page_refreshed_unexpectedly"}:
                severity = "error"
                recoverable = False
        elif "misclick" in reason_text:
            failure_type = "verification_misclick"
            severity = "error"
            recoverable = False
        elif "high-risk" in reason_text or "high risk" in reason_text:
            failure_type = "verification_high_risk"
            severity = "error"
            recoverable = False
        elif "retry limit" in reason_text:
            failure_type = "verification_retry_limit"
            severity = "error"
            recoverable = False
        elif "expected change not matched" in reason_text:
            failure_type = "verification_expected_change_mismatch"
        elif "no meaningful visible change" in reason_text:
            failure_type = "verification_no_visible_change"
        elif "confidence" in reason_text:
            failure_type = "verification_low_confidence"
        elif status_name == "retry":
            failure_type = "verification_requested_retry"
        elif status_name == "need_user":
            failure_type = "verification_needs_user"
            severity = "error"
            recoverable = False
        else:
            failure_type = "verification_uncertain"
        if status_name == "need_user":
            severity = "error"
            recoverable = False
    else:
        failure_type = normalise_failure_type(source_name)

    failure_type = normalise_failure_type(failure_type)

    return {
        "failure_type": failure_type,
        "category": failure_type,
        "source": source_name or "unknown",
        "severity": severity,
        "recoverable": recoverable,
        "status": status_name,
        "retry_count": max(0, int(retry_count or 0)),
        "reason": reason_text,
        "diagnostic_hint": diagnostic_hint,
    }


def normalise_visual_decision(decision: dict) -> dict:
    payload = decision if isinstance(decision, dict) else {}
    try:
        schema = VisualDecisionSchema.model_validate(payload)
    except Exception:
        schema = VisualDecisionSchema()
    raw_actions = payload.get("actions") if isinstance(payload.get("actions"), list) else []
    actions = [item for item in (normalise_visual_action(action) for action in raw_actions) if item]
    return schema.to_payload(actions)


def normalise_visual_verification(payload: dict) -> dict:
    source = payload if isinstance(payload, dict) else {}
    try:
        return VisualVerificationSchema.model_validate(source).to_payload()
    except Exception:
        return VisualVerificationSchema().to_payload()


def classify_visual_verification(verification: dict, action: dict, *, retry_count: int) -> tuple[str, str]:
    status = str(verification.get("status") or "continue").strip().lower()
    confidence = float(verification.get("confidence") or 0.0)
    changed = bool(verification.get("changed"))
    expected_change = str(action.get("expected_change") or "").strip()
    matched_expected = bool(verification.get("matched_expected_change"))
    risk = str(verification.get("risk") or "none").strip().lower()

    if verification.get("misclick"):
        return "need_user", "Verification detected a possible misclick."
    if risk == "high":
        return "need_user", "Verification detected high-risk page state."
    if status in {"done", "need_user"}:
        return status, str(verification.get("summary") or "").strip()

    retry_reasons: list[str] = []
    if confidence < BROWSER_VISION_VERIFY_MIN_CONFIDENCE:
        retry_reasons.append(f"confidence {confidence:.2f} < {BROWSER_VISION_VERIFY_MIN_CONFIDENCE:.2f}")
    if not changed and str(action.get("action") or "").strip().lower() != "wait":
        retry_reasons.append("no meaningful visible change")
    if expected_change and not matched_expected:
        retry_reasons.append("expected change not matched")
    if status == "retry":
        retry_reasons.append("verifier requested retry")

    if retry_reasons:
        if retry_count >= BROWSER_VISION_VERIFY_MAX_RETRIES:
            return "need_user", "Verification retry limit reached: " + "; ".join(dict.fromkeys(retry_reasons))
        return "retry", "; ".join(dict.fromkeys(retry_reasons))
    return "continue", str(verification.get("summary") or "").strip()
