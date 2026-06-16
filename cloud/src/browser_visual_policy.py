from __future__ import annotations

import re

from src.runtime_config import (
    BROWSER_VISION_ACTION_MIN_CONFIDENCE,
    BROWSER_VISION_MODEL,
    BROWSER_VISION_VERIFY_MAX_RETRIES,
    BROWSER_VISION_VERIFY_MIN_CONFIDENCE,
)


def normalise_visual_action(action: dict) -> dict | None:
    if not isinstance(action, dict):
        return None
    action_name = str(action.get("action") or "").strip().lower()
    if action_name not in {"click", "click_xy", "scroll", "press", "key", "type", "type_text", "wait"}:
        return None

    try:
        wait_ms = max(0, min(int(float(action.get("wait_ms") or 800)), 10000))
    except Exception:
        wait_ms = 800
    cleaned: dict = {
        "action": action_name,
        "reason": re.sub(r"\s+", " ", str(action.get("reason") or "").strip())[:240],
        "target_description": re.sub(r"\s+", " ", str(action.get("target_description") or "").strip())[:240],
        "expected_change": re.sub(r"\s+", " ", str(action.get("expected_change") or "").strip())[:240],
        "wait_ms": wait_ms,
    }
    try:
        cleaned["confidence"] = max(0.0, min(float(action.get("confidence") or 0.0), 1.0))
    except Exception:
        cleaned["confidence"] = 0.0
    if action_name in {"click", "click_xy"}:
        try:
            cleaned["x"] = max(0, min(int(float(action.get("x"))), 1000))
            cleaned["y"] = max(0, min(int(float(action.get("y"))), 1000))
        except Exception:
            return None
    elif action_name == "scroll":
        delta_y = action.get("delta_y")
        if delta_y is not None:
            try:
                cleaned["delta_y"] = max(-3000, min(int(float(delta_y)), 3000))
            except Exception:
                cleaned["delta_y"] = 700
        else:
            direction = str(action.get("direction") or "down").strip().lower()
            cleaned["direction"] = "up" if direction in {"up", "backward"} else "down"
    elif action_name in {"press", "key"}:
        key = str(action.get("key") or "").strip()
        if not key:
            return None
        cleaned["key"] = key[:80]
    elif action_name in {"type", "type_text"}:
        text = str(action.get("text") or "")
        if not text:
            return None
        cleaned["text"] = text[:2000]
    return cleaned


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
) -> dict:
    source_name = str(source or "").strip().lower()
    reason_text = re.sub(r"\s+", " ", str(reason or "").strip())[:500]
    status_name = str(status or "").strip().lower()

    category = "unknown"
    severity = "warning"
    recoverable = True

    if source_name in {"", "none"} and not reason_text:
        category = "none"
        severity = "info"
        recoverable = True
    elif source_name == "model_no_action":
        category = "model_no_action"
        severity = "warning"
        recoverable = False
    elif source_name == "action_safety":
        if "low confidence" in reason_text:
            category = "action_low_confidence"
        elif "target_description" in reason_text:
            category = "action_missing_target"
        elif "expected_change" in reason_text:
            category = "action_missing_expected_change"
        else:
            category = "action_safety_blocked"
        severity = "warning"
        recoverable = True
    elif source_name == "click_preflight":
        if "background" in reason_text:
            category = "click_landed_on_background"
        elif "no actionable" in reason_text:
            category = "click_no_actionable_target"
        elif "target mismatch" in reason_text:
            category = "click_target_mismatch"
        elif "hit-test failed" in reason_text:
            category = "click_hit_test_failed"
        else:
            category = "click_preflight_rejected"
        severity = "warning"
        recoverable = True
    elif source_name == "click_preflight_unavailable":
        category = "click_preflight_unavailable"
        severity = "warning"
        recoverable = True
    elif source_name == "action_execution":
        category = "action_execution_failed"
        severity = "error"
        recoverable = False
    elif source_name == "verification":
        if "misclick" in reason_text:
            category = "verification_misclick"
            severity = "error"
            recoverable = False
        elif "high-risk" in reason_text or "high risk" in reason_text:
            category = "verification_high_risk"
            severity = "error"
            recoverable = False
        elif "retry limit" in reason_text:
            category = "verification_retry_limit"
            severity = "error"
            recoverable = False
        elif "expected change not matched" in reason_text:
            category = "verification_expected_change_mismatch"
        elif "no meaningful visible change" in reason_text:
            category = "verification_no_visible_change"
        elif "confidence" in reason_text:
            category = "verification_low_confidence"
        elif status_name == "retry":
            category = "verification_requested_retry"
        elif status_name == "need_user":
            category = "verification_needs_user"
            severity = "error"
            recoverable = False
        else:
            category = "verification_uncertain"
        if status_name == "need_user":
            severity = "error"
            recoverable = False
    else:
        category = source_name or "unknown"

    return {
        "category": category,
        "source": source_name or "unknown",
        "severity": severity,
        "recoverable": recoverable,
        "status": status_name,
        "retry_count": max(0, int(retry_count or 0)),
        "reason": reason_text,
    }


def normalise_visual_decision(decision: dict) -> dict:
    status = str(decision.get("status") or "continue").strip().lower()
    if status not in {"continue", "done", "need_user"}:
        status = "continue"
    summary = re.sub(r"\s+", " ", str(decision.get("summary") or "").strip())[:500]
    raw_actions = decision.get("actions") if isinstance(decision.get("actions"), list) else []
    actions = [item for item in (normalise_visual_action(action) for action in raw_actions) if item]
    return {
        "status": status,
        "summary": summary,
        "actions": actions[:2],
        "model": str(decision.get("model") or BROWSER_VISION_MODEL).strip() or BROWSER_VISION_MODEL,
    }


def normalise_visual_verification(payload: dict) -> dict:
    status = str(payload.get("status") or "continue").strip().lower()
    if status not in {"continue", "done", "need_user", "retry"}:
        status = "continue"
    try:
        confidence = max(0.0, min(float(payload.get("confidence") or 0.0), 1.0))
    except Exception:
        confidence = 0.0
    risk = str(payload.get("risk") or "none").strip().lower()
    if risk not in {"none", "low", "medium", "high"}:
        risk = "none"
    return {
        "status": status,
        "changed": bool(payload.get("changed")),
        "matched_expected_change": bool(payload.get("matched_expected_change", payload.get("changed"))),
        "misclick": bool(payload.get("misclick")),
        "confidence": confidence,
        "observed_change": re.sub(r"\s+", " ", str(payload.get("observed_change") or "").strip())[:500],
        "risk": risk,
        "summary": re.sub(r"\s+", " ", str(payload.get("summary") or "").strip())[:500],
        "retry_hint": re.sub(r"\s+", " ", str(payload.get("retry_hint") or "").strip())[:500],
        "model": str(payload.get("model") or BROWSER_VISION_MODEL).strip() or BROWSER_VISION_MODEL,
    }


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
