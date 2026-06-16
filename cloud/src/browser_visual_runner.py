import json
import logging
import re
from contextlib import suppress
from typing import Callable

import requests

from src.browser_orchestrator import browser_orchestrator, get_browser_request_timeout
from src.browser_visual_policy import (
    classify_visual_verification,
    classify_visual_failure,
    normalise_visual_decision,
    normalise_visual_verification,
    visual_action_safety_issue,
)
from src.runtime_config import (
    BROWSER_VISION_ACTION_MIN_CONFIDENCE,
    BROWSER_VISION_MODEL,
    BROWSER_VISION_VERIFY_ENABLED,
    BROWSER_VISION_VERIFY_MIN_CONFIDENCE,
    BROWSER_VISUAL_CLICK_PREFLIGHT_ENABLED,
    BROWSER_VISUAL_CLICK_PREFLIGHT_MIN_SCORE,
    BROWSER_VISUAL_TRACE_MAX_IMAGE_CHARS,
    BROWSER_VISUAL_TRACE_SCREENSHOTS,
    ONE_API_TOKEN,
    ONE_API_URL,
)


logger = logging.getLogger("wei_agent")


ProgressCallback = Callable[[str], None]
CancelCheck = Callable[[], bool]
ArtifactCallback = Callable[[dict], None]


def format_browser_diagnostics(payload: dict, *, max_controls: int = 18) -> list[str]:
    diagnostics = payload.get("diagnostics") if isinstance(payload.get("diagnostics"), dict) else {}
    if not diagnostics or diagnostics.get("ok") is False:
        return []

    lines: list[str] = []
    viewport = diagnostics.get("viewport") if isinstance(diagnostics.get("viewport"), dict) else {}
    scroll = diagnostics.get("scroll") if isinstance(diagnostics.get("scroll"), dict) else {}
    if viewport or scroll:
        lines.append(
            "Page diagnostics: "
            f"viewport={viewport.get('width', '-')}x{viewport.get('height', '-')}, "
            f"scrollY={scroll.get('y', '-')}/{scroll.get('height', '-')}, "
            f"readyState={diagnostics.get('readyState', '-')}"
        )

    headings = diagnostics.get("headings") if isinstance(diagnostics.get("headings"), list) else []
    visible_headings = [str(item).strip() for item in headings if str(item).strip()]
    if visible_headings:
        lines.append("Visible headings: " + " | ".join(visible_headings[:8]))

    dialogs = diagnostics.get("dialogs") if isinstance(diagnostics.get("dialogs"), list) else []
    if dialogs:
        lines.append("Visible dialogs/overlays:")
        for item in dialogs[:5]:
            if not isinstance(item, dict):
                continue
            label = str(
                item.get("text")
                or item.get("ariaLabel")
                or item.get("title")
                or item.get("dataE2e")
                or item.get("selector")
                or ""
            ).strip()
            selector = str(item.get("selector") or "").strip()
            if label or selector:
                lines.append(f"- {label[:140] or '[no text]'} | selector: {selector or '-'}")

    controls = diagnostics.get("controls") if isinstance(diagnostics.get("controls"), list) else []
    if controls:
        lines.append("Visible controls candidates:")
        for item in controls[:max_controls]:
            if not isinstance(item, dict):
                continue
            label = str(
                item.get("text")
                or item.get("ariaLabel")
                or item.get("placeholder")
                or item.get("title")
                or item.get("dataE2e")
                or item.get("href")
                or ""
            ).strip()
            selector = str(item.get("selector") or "").strip()
            tag = str(item.get("tag") or "element").strip()
            role = str(item.get("role") or "").strip()
            data_e2e = str(item.get("dataE2e") or "").strip()
            meta = ", ".join(
                part for part in (tag, f"role={role}" if role else "", f"data-e2e={data_e2e}" if data_e2e else "") if part
            )
            if label or selector:
                lines.append(f"- {label[:160] or '[no text]'} | {meta} | selector: {selector or '-'}")
        control_count = diagnostics.get("controlCount")
        if isinstance(control_count, int) and control_count > max_controls:
            lines.append(f"- ... {control_count - max_controls} more visible controls omitted")

    visible_text = str(diagnostics.get("visibleText") or "").strip()
    if visible_text:
        lines.append("Visible page text preview:")
        lines.append(visible_text[:1000])
    return lines


def compact_visual_screenshot(payload: dict, *, include_image: bool = True) -> dict:
    image_base64 = str(payload.get("image_base64") or "")
    image_length = int(payload.get("image_base64_length") or len(image_base64) or 0)
    compact = {
        "title": str(payload.get("title") or "").strip(),
        "url": str(payload.get("url") or "").strip(),
        "mime_type": str(payload.get("mime_type") or "image/png").strip() or "image/png",
        "image_optimized": bool(payload.get("image_optimized")),
        "image_base64_length": image_length,
        "diagnostics": format_browser_diagnostics(payload, max_controls=8),
    }
    if include_image and BROWSER_VISUAL_TRACE_SCREENSHOTS and image_base64:
        if len(image_base64) <= BROWSER_VISUAL_TRACE_MAX_IMAGE_CHARS:
            compact["image_base64"] = image_base64
        else:
            compact["image_omitted_reason"] = (
                f"image_base64 length {len(image_base64)} exceeds "
                f"BROWSER_VISUAL_TRACE_MAX_IMAGE_CHARS={BROWSER_VISUAL_TRACE_MAX_IMAGE_CHARS}"
            )
    elif image_base64:
        compact["image_omitted_reason"] = "screenshot trace disabled"
    return compact


def _compact_action_result(result: dict) -> dict:
    if not isinstance(result, dict):
        return {"raw": str(result)[:1200]}
    compact = {
        "ok": bool(result.get("ok")),
        "result": result.get("result") if isinstance(result.get("result"), dict) else {},
    }
    diagnostics = result.get("diagnostics") if isinstance(result.get("diagnostics"), dict) else {}
    if diagnostics:
        compact["diagnostics"] = format_browser_diagnostics({"diagnostics": diagnostics}, max_controls=8)
    return compact


def _hit_test_text_chunks(hit_test: dict) -> list[str]:
    chunks: list[str] = []
    containers = []
    for key in ("target", "element"):
        value = hit_test.get(key)
        if isinstance(value, dict):
            containers.append(value)
    ancestors = hit_test.get("ancestors") if isinstance(hit_test.get("ancestors"), list) else []
    containers.extend(item for item in ancestors[:2] if isinstance(item, dict))

    for item in containers:
        for key in ("text", "ariaLabel", "title", "placeholder", "dataE2e", "href", "selector", "role", "tag", "type"):
            value = str(item.get(key) or "").strip()
            if value:
                chunks.append(value)
    return chunks


def _match_tokens(text: str) -> set[str]:
    lowered = str(text or "").lower()
    latin = re.findall(r"[a-z0-9]{2,}", lowered)
    cjk = re.findall(r"[\u4e00-\u9fff]", lowered)
    stopwords = {
        "button",
        "btn",
        "link",
        "input",
        "field",
        "text",
        "icon",
        "element",
        "control",
        "page",
        "click",
        "open",
        "the",
        "a",
        "an",
        "to",
        "of",
        "按钮",
        "链接",
        "输入",
        "页面",
        "点击",
    }
    return {token for token in (latin + cjk) if token not in stopwords}


def click_hit_test_match_score(target_description: str, hit_test: dict) -> float:
    expected = _match_tokens(target_description)
    if not expected:
        return 0.0
    observed = _match_tokens(" ".join(_hit_test_text_chunks(hit_test)))
    if not observed:
        return 0.0
    return len(expected & observed) / max(1, len(expected))


def format_click_hit_test_summary(hit_test: dict) -> str:
    if not isinstance(hit_test, dict):
        return "invalid hit-test payload"
    if not hit_test.get("ok"):
        return str(hit_test.get("reason") or "hit-test failed")
    target = hit_test.get("target") if isinstance(hit_test.get("target"), dict) else {}
    point = hit_test.get("point") if isinstance(hit_test.get("point"), dict) else {}
    label = (
        str(target.get("text") or target.get("ariaLabel") or target.get("placeholder") or target.get("title") or target.get("dataE2e") or "").strip()
    )
    tag = str(target.get("tag") or hit_test.get("targetTag") or "-").strip() or "-"
    selector = str(target.get("selector") or "").strip()
    return (
        f"point=({point.get('x', '-')},{point.get('y', '-')}); "
        f"actionable={bool(hit_test.get('actionable'))}; tag={tag}; "
        f"label={label[:120] or '-'}; selector={selector[:160] or '-'}"
    )


def click_hit_test_safety_issue(action: dict, hit_test: dict) -> tuple[str, float]:
    if not BROWSER_VISUAL_CLICK_PREFLIGHT_ENABLED:
        return "", 0.0
    if not isinstance(hit_test, dict):
        return "invalid hit-test payload", 0.0
    if not hit_test.get("ok"):
        return f"hit-test failed: {hit_test.get('reason') or 'unknown'}", 0.0

    target = hit_test.get("target") if isinstance(hit_test.get("target"), dict) else {}
    target_tag = str(target.get("tag") or hit_test.get("targetTag") or "").strip().lower()
    chunks = _hit_test_text_chunks(hit_test)
    readable = " ".join(chunks).strip()
    if target_tag in {"html", "body"} and not readable:
        return "hit-test landed on page background", 0.0
    if not hit_test.get("actionable") and not readable:
        return "hit-test found no actionable or readable target", 0.0

    target_description = str(action.get("target_description") or "")
    expected_tokens = _match_tokens(target_description)
    score = click_hit_test_match_score(target_description, hit_test)
    confidence = float(action.get("confidence") or 0.0)
    if expected_tokens and readable and score < BROWSER_VISUAL_CLICK_PREFLIGHT_MIN_SCORE and confidence < 0.85:
        return (
            f"hit-test target mismatch score {score:.2f} < {BROWSER_VISUAL_CLICK_PREFLIGHT_MIN_SCORE:.2f}",
            score,
        )
    return "", score


def _emit_artifact(callback: ArtifactCallback | None, *, event: str, round_index: int, **payload) -> None:
    if not callback:
        return
    artifact = {
        "event": event,
        "round": round_index,
        **payload,
    }
    try:
        callback(artifact)
    except Exception as exc:
        logger.warning("visual artifact callback failed: %s", exc)


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
    mime_type = str(screenshot.get("mime_type") or "image/png").strip() or "image/png"
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
        "Allowed JSON schema:\n"
        "{\n"
        '  "status": "continue" | "done" | "need_user",\n'
        '  "summary": "short observation/result",\n'
        '  "actions": [\n'
        '    {"action": "click", "x": 500, "y": 500, "reason": "...", "target_description": "visible element to click", "expected_change": "what should change after clicking", "confidence": 0.0, "wait_ms": 1200},\n'
        '    {"action": "scroll", "delta_y": 700, "reason": "...", "target_description": "page/feed/list", "expected_change": "new content becomes visible", "confidence": 0.0, "wait_ms": 1000},\n'
        '    {"action": "press", "key": "Escape", "reason": "...", "target_description": "current browser/page focus", "expected_change": "modal closes or page state changes", "confidence": 0.0, "wait_ms": 500},\n'
        '    {"action": "type_text", "text": "...", "reason": "...", "target_description": "focused input", "expected_change": "text appears in input", "confidence": 0.0, "wait_ms": 500},\n'
        '    {"action": "wait", "reason": "...", "target_description": "page loading", "expected_change": "page settles or new content loads", "confidence": 1.0, "wait_ms": 1000}\n'
        "  ]\n"
        "}\n\n"
        "Rules:\n"
        "- Return at most one action unless typing immediately after focusing an input is clearly required.\n"
        f"- Every non-wait action must include target_description, expected_change, and confidence. Use confidence >= {BROWSER_VISION_ACTION_MIN_CONFIDENCE:.2f} only when the visible target is clear.\n"
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
            "x": action.get("x"),
            "y": action.get("y"),
            "target_description": str(action.get("target_description") or "").strip(),
        },
        timeout=max(15, min(get_browser_request_timeout(), 30)),
    )


def run_local_browser_visual_operation(
    *,
    url: str = "",
    instruction: str = "",
    rounds: int = 3,
    max_round_cap: int = 8,
    progress_callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    artifact_callback: ArtifactCallback | None = None,
) -> str:
    target_url = (url or "").strip()
    task = (instruction or "").strip()
    if target_url and not re.match(r"^https?://", target_url, re.IGNORECASE):
        target_url = "https://" + target_url
    if not task:
        task = "Inspect the current page visually and report what is visible."
    round_cap = max(1, min(int(max_round_cap or 8), 50))
    max_rounds = max(1, min(int(rounds or 3), round_cap))

    history: list[str] = []
    final_status = "continue"
    final_summary = ""
    current_url = target_url
    current_title = ""
    last_diagnostics: list[str] = []
    unsafe_replans = 0
    click_preflight_replans = 0
    verification_retries = 0
    failure_events: list[dict] = []
    last_failure: dict = classify_visual_failure("none")

    for round_index in range(1, max_rounds + 1):
        if cancel_check and cancel_check():
            return "Visual browser operation cancelled."
        try:
            screenshot = _browser_worker_request(
                "browser.screenshot",
                {
                    "url": current_url,
                    "instruction": task,
                    "wait_ms": 1200 if round_index == 1 else 700,
                },
                timeout=max(60, get_browser_request_timeout()),
            )
        except Exception as exc:
            logger.warning("operate_local_browser_visual screenshot failed: %s", exc)
            return f"Visual browser operation failed while taking screenshot: {exc}"

        current_url = ""
        current_title = str(screenshot.get("title") or current_title or "").strip()
        page_url = str(screenshot.get("url") or "").strip()
        last_diagnostics = format_browser_diagnostics(screenshot, max_controls=12)
        _emit_artifact(
            artifact_callback,
            event="screenshot",
            round_index=round_index,
            screenshot=compact_visual_screenshot(screenshot),
        )
        if progress_callback:
            screenshot_size = int(screenshot.get("image_base64_length") or 0)
            optimized = "optimized" if screenshot.get("image_optimized") else "raw"
            progress_callback(
                f"Round {round_index}: screenshot captured [{optimized}, {screenshot_size} base64 chars], "
                f"title={current_title or '-'}"
            )

        try:
            decision = normalise_visual_decision(
                _call_browser_vision_model(
                    instruction=task,
                    screenshot=screenshot,
                    round_index=round_index,
                    history=history,
                )
            )
        except Exception as exc:
            logger.warning("operate_local_browser_visual model failed: %s", exc)
            return f"Visual browser operation failed while calling {BROWSER_VISION_MODEL}: {exc}"

        final_status = str(decision.get("status") or "continue").strip().lower()
        final_summary = str(decision.get("summary") or "").strip()
        _emit_artifact(
            artifact_callback,
            event="vision_decision",
            round_index=round_index,
            status=final_status,
            summary=final_summary,
            actions=decision.get("actions") if isinstance(decision.get("actions"), list) else [],
            model=decision.get("model") or BROWSER_VISION_MODEL,
        )
        history.append(f"Round {round_index}: status={final_status}; summary={final_summary or '-'}; url={page_url or '-'}")
        if progress_callback:
            progress_callback(f"Round {round_index}: vision status={final_status}; summary={final_summary or '-'}")

        if final_status in {"done", "need_user"}:
            break

        actions = decision.get("actions") if isinstance(decision.get("actions"), list) else []
        if not actions:
            final_summary = final_summary or "Vision model returned no action."
            last_failure = classify_visual_failure("model_no_action", final_summary, status=final_status)
            failure_events.append(last_failure)
            _emit_artifact(
                artifact_callback,
                event="failure",
                round_index=round_index,
                failure=last_failure,
            )
            if progress_callback:
                progress_callback("Vision model returned no action; stopping.")
            break

        for action in actions[:2]:
            if cancel_check and cancel_check():
                return "Visual browser operation cancelled."
            if not isinstance(action, dict):
                continue
            action_name = str(action.get("action") or "").strip().lower()
            reason = str(action.get("reason") or "").strip()
            target_description = str(action.get("target_description") or "").strip()
            expected_change = str(action.get("expected_change") or "").strip()
            confidence = float(action.get("confidence") or 0.0)
            safety_issue = visual_action_safety_issue(action)
            if safety_issue:
                unsafe_replans += 1
                last_failure = classify_visual_failure(
                    "action_safety",
                    safety_issue,
                    status="need_user" if unsafe_replans >= 2 else "continue",
                    retry_count=unsafe_replans,
                )
                failure_events.append(last_failure)
                trace = (
                    f"Skipped unsafe visual action: {action_name or '<empty>'}; issue={safety_issue}; "
                    f"target={target_description or '-'}; expected={expected_change or '-'}; reason={reason or '-'}"
                )
                history.append(trace)
                _emit_artifact(
                    artifact_callback,
                    event="action_blocked",
                    round_index=round_index,
                    issue=safety_issue,
                    failure=last_failure,
                    action=action,
                    trace=trace,
                )
                if progress_callback:
                    progress_callback(trace)
                if unsafe_replans >= 2:
                    final_status = "need_user"
                    final_summary = "Visual model could not identify a safe target with enough confidence."
                else:
                    final_status = "continue"
                    final_summary = "Skipped a low-confidence action; replanning from a fresh screenshot."
                break
            unsafe_replans = 0
            if action_name in {"click", "click_xy"} and BROWSER_VISUAL_CLICK_PREFLIGHT_ENABLED:
                try:
                    hit_test = _run_click_hit_test(action)
                    hit_issue, hit_score = click_hit_test_safety_issue(action, hit_test)
                    hit_summary = format_click_hit_test_summary(hit_test)
                    _emit_artifact(
                        artifact_callback,
                        event="click_preflight",
                        round_index=round_index,
                        action=action,
                        hit_test=hit_test,
                        score=hit_score,
                        issue=hit_issue,
                        summary=hit_summary,
                    )
                    if progress_callback:
                        progress_callback(
                            f"Click preflight: score={hit_score:.2f}; issue={hit_issue or '-'}; {hit_summary}"
                    )
                    if hit_issue:
                        click_preflight_replans += 1
                        last_failure = classify_visual_failure(
                            "click_preflight",
                            hit_issue,
                            status="need_user" if click_preflight_replans >= 2 else "continue",
                            retry_count=click_preflight_replans,
                        )
                        failure_events.append(last_failure)
                        trace = (
                            f"Skipped visual click after preflight: issue={hit_issue}; score={hit_score:.2f}; "
                            f"target={target_description or '-'}; hit={hit_summary}"
                        )
                        history.append(trace)
                        if progress_callback:
                            progress_callback(trace)
                        _emit_artifact(
                            artifact_callback,
                            event="failure",
                            round_index=round_index,
                            failure=last_failure,
                            action=action,
                            trace=trace,
                        )
                        if click_preflight_replans >= 2:
                            final_status = "need_user"
                            final_summary = "Click preflight could not confirm a safe target."
                        else:
                            final_status = "continue"
                            final_summary = "Click preflight rejected the target; replanning from a fresh screenshot."
                        break
                    click_preflight_replans = 0
                except Exception as exc:
                    logger.warning("visual click preflight failed: %s", exc)
                    preflight_failure = classify_visual_failure(
                        "click_preflight_unavailable",
                        str(exc),
                        status="continue",
                    )
                    _emit_artifact(
                        artifact_callback,
                        event="click_preflight_failed",
                        round_index=round_index,
                        action=action,
                        error=str(exc),
                        failure=preflight_failure,
                    )
                    if progress_callback:
                        progress_callback(f"Click preflight unavailable; continuing with visual action: {exc}")
            if progress_callback:
                progress_callback(
                    f"Executing visual action: {action_name or '<empty>'}; confidence={confidence:.2f}; "
                    f"target={target_description or '-'}; expected={expected_change or '-'}; reason={reason or '-'}"
                )
            try:
                result = _run_visual_action(action)
            except Exception as exc:
                logger.warning("operate_local_browser_visual action failed: %s", exc)
                last_failure = classify_visual_failure("action_execution", str(exc), status="failed")
                failure_events.append(last_failure)
                history.append(f"Action failed: {action_name or '<empty>'}; reason={reason or '-'}; error={exc}")
                _emit_artifact(
                    artifact_callback,
                    event="failure",
                    round_index=round_index,
                    failure=last_failure,
                    action=action,
                )
                final_status = "failed"
                final_summary = f"Visual action failed: {exc}"
                break
            action_result = result.get("result") if isinstance(result.get("result"), dict) else {}
            backend = str(action_result.get("backend") or "").strip()
            diagnostics = result.get("diagnostics") if isinstance(result.get("diagnostics"), dict) else {}
            if diagnostics:
                last_diagnostics = format_browser_diagnostics({"diagnostics": diagnostics}, max_controls=12)
                current_title = str(diagnostics.get("title") or current_title or "").strip()
            trace = (
                f"Action: {action_name}; confidence={confidence:.2f}; target={target_description or '-'}; "
                f"expected={expected_change or '-'}; reason={reason or '-'}; ok={bool(result.get('ok'))}"
            )
            if backend:
                trace += f"; backend={backend}"
            history.append(trace)
            _emit_artifact(
                artifact_callback,
                event="action_result",
                round_index=round_index,
                action=action,
                result=_compact_action_result(result),
                trace=trace,
            )
            if progress_callback:
                progress_callback(trace)

            if not BROWSER_VISION_VERIFY_ENABLED:
                continue
            if cancel_check and cancel_check():
                return "Visual browser operation cancelled."
            try:
                verification_screenshot = _browser_worker_request(
                    "browser.screenshot",
                    {
                        "url": "",
                        "instruction": f"Verify action result for: {task}",
                        "wait_ms": 400,
                    },
                    timeout=max(60, get_browser_request_timeout()),
                )
                verification = normalise_visual_verification(
                    _call_browser_visual_verifier(
                        instruction=task,
                        before=screenshot,
                        after=verification_screenshot,
                        action=action,
                        action_result=result,
                        round_index=round_index,
                        history=history,
                    )
                )
                _emit_artifact(
                    artifact_callback,
                    event="verification_screenshot",
                    round_index=round_index,
                    screenshot=compact_visual_screenshot(verification_screenshot),
                )
            except Exception as exc:
                logger.warning("operate_local_browser_visual verification failed: %s", exc)
                history.append(f"Verification failed: {exc}")
                if progress_callback:
                    progress_callback(f"Verification failed: {exc}")
                continue

            verify_summary = str(verification.get("summary") or "-").strip() or "-"
            observed_change = str(verification.get("observed_change") or "").strip()
            verify_status = str(verification.get("status") or "continue").strip()
            verify_trace = (
                f"Verification: status={verify_status}; changed={bool(verification.get('changed'))}; "
                f"matched_expected={bool(verification.get('matched_expected_change'))}; "
                f"misclick={bool(verification.get('misclick'))}; confidence={verification.get('confidence')}; "
                f"risk={verification.get('risk')}; observed={observed_change or '-'}; summary={verify_summary}"
            )
            history.append(verify_trace)
            _emit_artifact(
                artifact_callback,
                event="verification",
                round_index=round_index,
                verification=verification,
                trace=verify_trace,
            )
            if progress_callback:
                progress_callback(verify_trace)

            current_title = str(verification_screenshot.get("title") or current_title or "").strip()
            last_diagnostics = format_browser_diagnostics(verification_screenshot, max_controls=12)
            policy_status, policy_reason = classify_visual_verification(
                verification,
                action,
                retry_count=verification_retries + 1,
            )
            verification_failure = classify_visual_failure(
                "verification",
                policy_reason,
                status=policy_status,
                retry_count=verification_retries + 1,
            )
            policy_trace = f"Verification policy: status={policy_status}; reason={policy_reason or '-'}"
            history.append(policy_trace)
            _emit_artifact(
                artifact_callback,
                event="verification_policy",
                round_index=round_index,
                status=policy_status,
                reason=policy_reason,
                failure=verification_failure,
            )
            if progress_callback:
                progress_callback(policy_trace)

            if policy_status == "retry":
                verification_retries += 1
                last_failure = verification_failure
                failure_events.append(last_failure)
                retry_hint = str(verification.get("retry_hint") or "").strip()
                final_summary = policy_reason or verify_summary or "Visual verification suggested retry."
                history.append("Verification requested retry; replanning from the next screenshot.")
                if progress_callback:
                    progress_callback("Verification requested retry; replanning from the next screenshot.")
                if retry_hint:
                    history.append(f"Retry hint: {retry_hint}")
                    if progress_callback:
                        progress_callback(f"Retry hint: {retry_hint}")
                break
            verification_retries = 0
            if policy_status in {"done", "need_user"}:
                final_status = policy_status
                final_summary = policy_reason or verify_summary or final_summary
                if policy_status == "need_user":
                    last_failure = verification_failure
                    failure_events.append(last_failure)
                break

        if final_status == "failed":
            break
        if final_status in {"done", "need_user"}:
            break

    lines = [
        f"Visual browser model: {BROWSER_VISION_MODEL}",
        f"Final status: {final_status or 'continue'}",
        f"Page title: {current_title or '-'}",
    ]
    if final_summary:
        lines.append(f"Summary: {final_summary}")
    if last_failure.get("category") != "none":
        lines.append(
            "Last failure: "
            f"category={last_failure.get('category')}; "
            f"severity={last_failure.get('severity')}; "
            f"recoverable={last_failure.get('recoverable')}; "
            f"reason={last_failure.get('reason') or '-'}"
        )
    if history:
        lines.append("Visual operation trace:")
        lines.extend(f"- {item}" for item in history[-12:])
    if last_diagnostics:
        lines.append("Last page diagnostics:")
        lines.extend(last_diagnostics[:18])
    _emit_artifact(
        artifact_callback,
        event="final",
        round_index=max_rounds,
        status=final_status,
        summary=final_summary,
        title=current_title,
        last_failure=last_failure,
        failures=failure_events[-8:],
        history=history[-12:],
    )
    return "\n".join(lines)
