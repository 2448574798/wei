from __future__ import annotations

from src.browser_orchestrator import browser_orchestrator, get_browser_request_timeout


def _browser_worker_request(command: str, payload: dict, timeout: int | None = None) -> dict:
    return browser_orchestrator.request(command, payload, timeout=timeout)


def _run_visual_action(action: dict) -> dict:
    if not isinstance(action, dict):
        raise ValueError("Visual action must be a JSON object.")
    action_name = str(action.get("action") or "").strip().lower()
    if action_name not in {"click", "click_xy", "scroll", "press", "key", "type", "type_text", "wait"}:
        raise ValueError(f"Unsupported visual action: {action_name or '<empty>'}")
    payload = dict(action)
    payload["action"] = action_name
    return _browser_worker_request(
        "browser.visual_action",
        payload,
        timeout=max(30, get_browser_request_timeout()),
    )


def _run_click_hit_test(action: dict) -> dict:
    return _browser_worker_request(
        "browser.hit_test",
        {
            "task_id": str(action.get("task_id") or "").strip(),
            "action_id": str(action.get("action_id") or "").strip(),
            "x": action.get("x"),
            "y": action.get("y"),
            "target_description": str(action.get("target_description") or "").strip(),
        },
        timeout=max(15, min(get_browser_request_timeout(), 30)),
    )
