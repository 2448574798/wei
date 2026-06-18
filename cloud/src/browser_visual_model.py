from __future__ import annotations

import json
import re
from contextlib import suppress

import requests

from src.browser_visual_trace import format_browser_diagnostics
from src.runtime_config import (
    BROWSER_VISION_ACTION_MIN_CONFIDENCE,
    BROWSER_VISION_MODEL,
    BROWSER_VISION_VERIFY_MIN_CONFIDENCE,
    ONE_API_TOKEN,
    ONE_API_URL,
)


def _extract_json_object(text: str) -> dict:
    raw = (text or "").strip()
    if not raw:
        raise ValueError("Vision model returned empty content.")
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE).strip()
        raw = re.sub(r"\s*```$", "", raw).strip()
    with suppress(Exception):
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        parsed = json.loads(raw[start : end + 1])
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("Vision model did not return a JSON object.")


def _call_browser_vision_model(*, instruction: str, screenshot: dict, round_index: int, history: list[str]) -> dict:
    if not ONE_API_TOKEN:
        raise RuntimeError("ONE_API_TOKEN is not configured.")
    image_base64 = str(screenshot.get("image_base64") or "").strip()
    if not image_base64:
        raise RuntimeError("Browser screenshot is missing image data.")

    diagnostic_text = "\n".join(format_browser_diagnostics(screenshot, max_controls=30)) or "[Diagnostics disabled or unavailable]"
    page_title = str(screenshot.get("title") or "-").strip() or "-"
    page_url = str(screenshot.get("url") or "-").strip() or "-"
    history_text = "\n".join(history[-8:]) if history else "[No previous visual actions]"
    prompt = (
        "You are the cloud browser vision controller for a local browser.\n"
        "Decide the next safe browser action from the screenshot. Diagnostics are optional helper data and may be unavailable.\n"
        "Return JSON only. Do not use markdown.\n\n"
        "Coordinate system:\n"
        "- For click actions, return x and y as integers from 0 to 1000, normalized to the visible screenshot viewport.\n"
        "- x=0 is the left edge, x=1000 is the right edge, y=0 is the top, y=1000 is the bottom.\n\n"
        "Action candidates:\n"
        "- Diagnostics may include Visible action candidates with candidate_id, label, rect, and normalized center.\n"
        "- Prefer returning candidate_id for click actions when a candidate matches the visible target.\n"
        "- Use x/y only as a fallback when no candidate_id clearly matches.\n\n"
        "Allowed JSON schema:\n"
        "{\n"
        '  "status": "continue" | "done" | "need_user",\n'
        '  "summary": "short observation/result",\n'
        '  "actions": [\n'
        '    {"action": "click", "candidate_id": "c1_abcd1234ef56", "x": 500, "y": 500, "reason": "...", "target_description": "visible element to click", "expected_change": "what should change after clicking", "confidence": 0.0, "wait_ms": 1200},\n'
        '    {"action": "scroll", "delta_y": 700, "reason": "...", "target_description": "page/feed/list", "expected_change": "new content becomes visible", "confidence": 0.0, "wait_ms": 1000},\n'
        '    {"action": "press", "key": "Escape", "reason": "...", "target_description": "current browser/page focus", "expected_change": "modal closes or page state changes", "confidence": 0.0, "wait_ms": 500},\n'
        '    {"action": "type_text", "text": "...", "reason": "...", "target_description": "focused input", "expected_change": "text appears in input", "confidence": 0.0, "wait_ms": 500},\n'
        '    {"action": "wait", "reason": "...", "target_description": "page loading", "expected_change": "page settles or new content loads", "confidence": 1.0, "wait_ms": 1000}\n'
        "  ]\n"
        "}\n\n"
        "Rules:\n"
        "- Return at most one action unless typing immediately after focusing an input is clearly required.\n"
        f"- Every non-wait action must include target_description, expected_change, and confidence. Use confidence >= {BROWSER_VISION_ACTION_MIN_CONFIDENCE:.2f} only when the visible target is clear.\n"
        "- For click actions, include candidate_id when a Visible action candidate matches the target. Keep x/y if available, but candidate_id is preferred.\n"
        "- If the target is uncertain, do not click/type. Return wait, scroll, or need_user with a clear summary.\n"
        "- On video-feed or card-grid pages, click the center of the intended visible video card/thumbnail, not a nearby icon or blank gutter. "
        "Use target_description words such as video card or thumbnail when that is the intended target.\n"
        "- Prefer done when the requested result is already visible.\n"
        "- Use need_user for captcha, login approval, payment, purchase, irreversible posting, or ambiguous destructive actions.\n"
        "- Do not invent hidden page state. Use the screenshot first; use diagnostics only when present.\n\n"
        f"User instruction: {instruction}\n"
        f"Round: {round_index}\n"
        f"Page title: {page_title}\n"
        f"Page URL: {page_url}\n"
        f"Recent visual history:\n{history_text}\n\n"
        f"Diagnostics:\n{diagnostic_text}"
    )
    return _post_vision_chat_completion(prompt, screenshot)


def _post_vision_chat_completion(prompt: str, screenshot: dict) -> dict:
    if not ONE_API_TOKEN:
        raise RuntimeError("ONE_API_TOKEN is not configured.")
    image_base64 = str(screenshot.get("image_base64") or "").strip()
    mime_type = str(screenshot.get("mime_type") or "image/png").strip() or "image/png"
    if not image_base64:
        raise RuntimeError("Browser screenshot is missing image data.")

    headers = {
        "Authorization": f"Bearer {ONE_API_TOKEN}",
        "Content-Type": "application/json",
    }
    body = {
        "model": BROWSER_VISION_MODEL,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_base64}"}},
                ],
            }
        ],
    }
    response = requests.post(f"{ONE_API_URL}/chat/completions", headers=headers, json=body, timeout=120)
    if response.status_code in {400, 422}:
        body.pop("response_format", None)
        response = requests.post(f"{ONE_API_URL}/chat/completions", headers=headers, json=body, timeout=120)
    response.raise_for_status()
    data = response.json()
    choices = data.get("choices") if isinstance(data, dict) else []
    if not choices:
        raise RuntimeError("Vision model returned no choices.")
    message = choices[0].get("message") if isinstance(choices[0], dict) else {}
    content = message.get("content") if isinstance(message, dict) else ""
    if isinstance(content, list):
        content = "\n".join(str(item.get("text") or "") for item in content if isinstance(item, dict))
    parsed = _extract_json_object(str(content or ""))
    parsed["model"] = BROWSER_VISION_MODEL
    return parsed


def _call_browser_visual_verifier(
    *,
    instruction: str,
    before: dict,
    after: dict,
    action: dict,
    action_result: dict,
    round_index: int,
    history: list[str],
) -> dict:
    before_title = str(before.get("title") or "-").strip() or "-"
    before_url = str(before.get("url") or "-").strip() or "-"
    after_title = str(after.get("title") or "-").strip() or "-"
    after_url = str(after.get("url") or "-").strip() or "-"
    history_text = "\n".join(history[-10:]) if history else "[No previous visual actions]"
    action_summary = json.dumps(
        {
            "action": action,
            "result": action_result.get("result") if isinstance(action_result, dict) else action_result,
        },
        ensure_ascii=False,
    )[:1600]
    prompt = (
        "You are verifying whether a local browser visual action worked.\n"
        "Compare the current screenshot against the action intent and recent history.\n"
        "Return JSON only. Do not use markdown.\n\n"
        "Allowed JSON schema:\n"
        "{\n"
        '  "status": "continue" | "done" | "need_user" | "retry",\n'
        '  "changed": true,\n'
        '  "matched_expected_change": true,\n'
        '  "misclick": false,\n'
        '  "confidence": 0.0,\n'
        '  "observed_change": "what visibly changed after the action",\n'
        '  "risk": "none" | "low" | "medium" | "high",\n'
        '  "summary": "short verification result",\n'
        '  "retry_hint": "optional safer next target or strategy"\n'
        "}\n\n"
        "Rules:\n"
        "- status=done when the user instruction appears satisfied in the current screenshot.\n"
        "- status=continue when the action worked or page advanced but more steps are needed.\n"
        "- status=retry when the action likely had no effect or missed the intended target.\n"
        f"- If confidence is below {BROWSER_VISION_VERIFY_MIN_CONFIDENCE:.2f}, use status=retry unless the page clearly needs user help.\n"
        "- Compare the action.expected_change against the current screenshot when it is provided.\n"
        "- matched_expected_change must be false when the visible result does not match action.expected_change.\n"
        "- risk=high for wrong navigation, wrong modal, destructive side effects, captcha, login approval, payment, purchase, or posting.\n"
        "- status=need_user for captcha, login approval, payment, irreversible posting, or ambiguous destructive actions.\n"
        "- Set misclick=true if the page moved to the wrong place, opened the wrong modal, or selected the wrong item.\n"
        "- Do not invent hidden page state. Use the current screenshot first.\n\n"
        f"User instruction: {instruction}\n"
        f"Round: {round_index}\n"
        f"Before title/url: {before_title} | {before_url}\n"
        f"After title/url: {after_title} | {after_url}\n"
        f"Action and execution result: {action_summary}\n"
        f"Recent visual history:\n{history_text}"
    )
    return _post_vision_chat_completion(prompt, after)
