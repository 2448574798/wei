import logging
import re
from contextvars import ContextVar
from typing import Callable
from uuid import uuid4

from src.browser_orchestrator import get_browser_request_timeout
from src.browser_visual_model import _call_browser_vision_model, _call_browser_visual_verifier, _extract_json_object
from src.browser_visual_policy import (
    classify_visual_verification,
    classify_visual_failure,
    normalise_visual_decision,
    normalise_visual_verification,
    visual_action_safety_issue,
)
from src.browser_visual_trace import format_browser_diagnostics
from src.browser_visual_worker import _browser_worker_request, _run_click_hit_test, _run_visual_action
from src.runtime_config import (
    BROWSER_VISION_MODEL,
    BROWSER_VISION_VERIFY_ENABLED,
    BROWSER_VISUAL_CLICK_PREFLIGHT_ENABLED,
    BROWSER_VISUAL_CLICK_PREFLIGHT_MIN_SCORE,
    BROWSER_VISUAL_TRACE_MAX_IMAGE_CHARS,
    BROWSER_VISUAL_TRACE_SCREENSHOTS,
)


logger = logging.getLogger("wei_agent")


ProgressCallback = Callable[[str], None]
CancelCheck = Callable[[], bool]
ArtifactCallback = Callable[[dict], None]
_CURRENT_VISUAL_TASK_ID: ContextVar[str] = ContextVar("wei_visual_task_id", default="")


def compact_visual_screenshot(payload: dict, *, include_image: bool = True) -> dict:
    image_base64 = str(payload.get("image_base64") or "")
    image_length = int(payload.get("image_base64_length") or len(image_base64) or 0)
    compact = {
        "request_id": str(payload.get("request_id") or "").strip(),
        "task_id": str(payload.get("task_id") or "").strip(),
        "action_id": str(payload.get("action_id") or "").strip(),
        "title": str(payload.get("title") or "").strip(),
        "url": str(payload.get("url") or "").strip(),
        "mime_type": str(payload.get("mime_type") or "image/png").strip() or "image/png",
        "image_optimized": bool(payload.get("image_optimized")),
        "image_base64_length": image_length,
        "viewport": payload.get("viewport") if isinstance(payload.get("viewport"), dict) else {},
        "device_pixel_ratio": payload.get("device_pixel_ratio"),
        "page_state": compact_page_state(payload.get("page_state") if isinstance(payload.get("page_state"), dict) else {}),
        "action_candidates": compact_action_candidates(payload.get("action_candidates") if isinstance(payload.get("action_candidates"), list) else []),
        "navigation": payload.get("navigation") if isinstance(payload.get("navigation"), dict) else {},
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


def compact_action_candidates(candidates: list, *, limit: int = 24) -> list[dict]:
    compact: list[dict] = []
    if not isinstance(candidates, list):
        return compact
    for item in candidates[: max(0, min(limit, 80))]:
        if not isinstance(item, dict):
            continue
        compact.append(
            {
                "candidate_id": str(item.get("candidate_id") or "").strip(),
                "label": str(item.get("label") or item.get("text") or item.get("ariaLabel") or "").strip()[:180],
                "tag": str(item.get("tag") or "").strip(),
                "role": str(item.get("role") or "").strip(),
                "href": str(item.get("href") or "").strip()[:180],
                "selector": str(item.get("selector") or "").strip()[:220],
                "rect": item.get("rect") if isinstance(item.get("rect"), dict) else {},
                "center": item.get("center") if isinstance(item.get("center"), dict) else {},
            }
        )
    return compact


def compact_page_state(page_state: dict) -> dict:
    if not isinstance(page_state, dict) or page_state.get("ok") is False:
        return {}
    viewport = page_state.get("viewport") if isinstance(page_state.get("viewport"), dict) else {}
    scroll = page_state.get("scroll") if isinstance(page_state.get("scroll"), dict) else {}
    return {
        "signature": str(page_state.get("signature") or "").strip(),
        "url": str(page_state.get("url") or "").strip(),
        "title": str(page_state.get("title") or "").strip(),
        "readyState": str(page_state.get("readyState") or "").strip(),
        "capturedAt": page_state.get("capturedAt"),
        "viewport": viewport,
        "scroll": scroll,
        "visibleTextHash": str(page_state.get("visibleTextHash") or "").strip(),
        "visibleTextLength": int(page_state.get("visibleTextLength") or 0),
        "visibleTextPreview": str(page_state.get("visibleTextPreview") or "").strip()[:280],
    }


def page_state_drift_issue(reference: dict, current: dict) -> str:
    reference_state = reference.get("page_state") if isinstance(reference.get("page_state"), dict) else {}
    current_state = current.get("page_state") if isinstance(current.get("page_state"), dict) else {}
    if not reference_state or not current_state:
        return ""
    ref_url = str(reference_state.get("url") or "").strip().split("#", 1)[0]
    cur_url = str(current_state.get("url") or "").strip().split("#", 1)[0]
    if ref_url and cur_url and ref_url != cur_url:
        return f"page state mismatch: url changed from {ref_url[:160]} to {cur_url[:160]}"

    ref_viewport = reference_state.get("viewport") if isinstance(reference_state.get("viewport"), dict) else {}
    cur_viewport = current_state.get("viewport") if isinstance(current_state.get("viewport"), dict) else {}
    try:
        width_diff = abs(int(ref_viewport.get("width") or 0) - int(cur_viewport.get("width") or 0))
        height_diff = abs(int(ref_viewport.get("height") or 0) - int(cur_viewport.get("height") or 0))
    except Exception:
        width_diff = height_diff = 0
    if width_diff > 24 or height_diff > 24:
        return f"page state mismatch: viewport changed by {width_diff}x{height_diff}"

    try:
        ref_dpr = float(ref_viewport.get("devicePixelRatio") or ref_viewport.get("dpr") or 1)
        cur_dpr = float(cur_viewport.get("devicePixelRatio") or cur_viewport.get("dpr") or 1)
    except Exception:
        ref_dpr = cur_dpr = 1.0
    if abs(ref_dpr - cur_dpr) > 0.05:
        return f"page state mismatch: device pixel ratio changed from {ref_dpr:.2f} to {cur_dpr:.2f}"

    ref_scroll = reference_state.get("scroll") if isinstance(reference_state.get("scroll"), dict) else {}
    cur_scroll = current_state.get("scroll") if isinstance(current_state.get("scroll"), dict) else {}
    try:
        scroll_diff = abs(int(ref_scroll.get("y") or 0) - int(cur_scroll.get("y") or 0))
    except Exception:
        scroll_diff = 0
    if scroll_diff > 160:
        return f"page state mismatch: scrollY changed by {scroll_diff}px"
    return ""


def _compact_action_result(result: dict) -> dict:
    if not isinstance(result, dict):
        return {"raw": str(result)[:1200]}
    compact = {
        "ok": bool(result.get("ok")),
        "task_id": str(result.get("task_id") or "").strip(),
        "action_id": str(result.get("action_id") or "").strip(),
        "result": result.get("result") if isinstance(result.get("result"), dict) else {},
        "page_state": compact_page_state(result.get("page_state") if isinstance(result.get("page_state"), dict) else {}),
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
        for key in ("text", "ariaLabel", "title", "placeholder", "dataE2e", "href", "selector", "className", "role", "tag", "type"):
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


def _is_generic_video_card_target(target_description: str) -> bool:
    text = str(target_description or "").lower()
    return any(keyword in text for keyword in ("video", "card", "thumbnail", "cover", "feed", "视频", "卡片", "封面", "播放"))


def _hit_test_looks_like_video_card(hit_test: dict) -> bool:
    chunks = " ".join(_hit_test_text_chunks(hit_test)).lower()
    if any(keyword in chunks for keyword in ("video", "card", "thumbnail", "cover", "feed", "waterfall", "douyin")):
        return True
    # Many video cards expose only duration/view-count text at the exact hit point.
    if re.search(r"\b\d{1,2}:\d{2}\b", chunks):
        return True
    if re.search(r"\d+(?:\.\d+)?\s*(?:万|w|k)\b", chunks):
        return True
    return False


def _candidate_text_chunks(candidate: dict) -> list[str]:
    if not isinstance(candidate, dict):
        return []
    chunks: list[str] = []
    for key in ("label", "text", "ariaLabel", "title", "dataE2e", "href", "selector", "className", "role", "tag"):
        value = str(candidate.get(key) or "").strip()
        if value:
            chunks.append(value)
    return chunks


def candidate_match_score(target_description: str, candidate: dict) -> float:
    expected = _match_tokens(target_description)
    if not expected:
        return 0.0
    observed = _match_tokens(" ".join(_candidate_text_chunks(candidate)))
    if not observed:
        return 0.0
    return len(expected & observed) / max(1, len(expected))


def build_click_retry_action(action: dict, screenshot: dict, hit_issue: str) -> tuple[dict | None, str]:
    if not isinstance(action, dict) or not isinstance(screenshot, dict) or action.get("_preflight_retry"):
        return None, ""
    candidates = screenshot.get("action_candidates") if isinstance(screenshot.get("action_candidates"), list) else []
    if not candidates:
        return None, ""

    target_description = str(action.get("target_description") or "").strip()
    scored: list[tuple[float, dict]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict) or not str(candidate.get("candidate_id") or "").strip():
            continue
        score = candidate_match_score(target_description, candidate)
        if _is_generic_video_card_target(target_description):
            text = " ".join(_candidate_text_chunks(candidate)).lower()
            rect = candidate.get("rect") if isinstance(candidate.get("rect"), dict) else {}
            try:
                area = int(rect.get("width") or 0) * int(rect.get("height") or 0)
            except Exception:
                area = 0
            if any(word in text for word in ("video", "card", "feed", "waterfall", "item", "thumbnail", "视频", "卡片", "封面")):
                score = max(score, 0.72)
            elif area > 18000:
                score = max(score, 0.55)
        if score > 0:
            scored.append((score, candidate))

    if not scored:
        return None, ""
    scored.sort(key=lambda item: item[0], reverse=True)
    best_score, best_candidate = scored[0]
    min_score = 0.55 if _is_generic_video_card_target(target_description) else max(0.34, BROWSER_VISUAL_CLICK_PREFLIGHT_MIN_SCORE)
    if best_score < min_score:
        return None, ""

    retry_action = dict(action)
    retry_action["candidate_id"] = str(best_candidate.get("candidate_id") or "").strip()
    center = best_candidate.get("center") if isinstance(best_candidate.get("center"), dict) else {}
    if center.get("normalizedX") is not None and center.get("normalizedY") is not None:
        retry_action["x"] = int(center.get("normalizedX") or 0)
        retry_action["y"] = int(center.get("normalizedY") or 0)
    retry_action["_preflight_retry"] = True
    retry_action["_retry_strategy"] = "candidate_match"
    retry_action["_retry_reason"] = str(hit_issue or "").strip()
    retry_action["_retry_candidate_score"] = round(best_score, 3)
    if not retry_action.get("target_description"):
        retry_action["target_description"] = str(best_candidate.get("label") or "").strip()
    detail = (
        f"candidate_match score={best_score:.2f}; candidate={retry_action['candidate_id']}; "
        f"label={str(best_candidate.get('label') or '-')[:120]}"
    )
    return retry_action, detail


def format_click_hit_test_summary(hit_test: dict) -> str:
    if not isinstance(hit_test, dict):
        return "invalid hit-test payload"
    if not hit_test.get("ok"):
        return str(hit_test.get("reason") or "hit-test failed")
    target = hit_test.get("target") if isinstance(hit_test.get("target"), dict) else {}
    point = hit_test.get("point") if isinstance(hit_test.get("point"), dict) else {}
    candidate = hit_test.get("candidate") if isinstance(hit_test.get("candidate"), dict) else {}
    label = (
        str(target.get("text") or target.get("ariaLabel") or target.get("placeholder") or target.get("title") or target.get("dataE2e") or "").strip()
    )
    candidate_label = str(candidate.get("label") or "").strip()
    tag = str(target.get("tag") or hit_test.get("targetTag") or "-").strip() or "-"
    selector = str(target.get("selector") or "").strip()
    return (
        f"point=({point.get('x', '-')},{point.get('y', '-')}); "
        f"candidate={hit_test.get('candidate_id') or '-'}:{candidate_label[:80] or '-'}; "
        f"actionable={bool(hit_test.get('actionable'))}; tag={tag}; "
        f"label={label[:120] or '-'}; selector={selector[:160] or '-'}"
    )


def click_hit_test_safety_issue(action: dict, hit_test: dict) -> tuple[str, float]:
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
    if _is_generic_video_card_target(target_description) and _hit_test_looks_like_video_card(hit_test):
        return "", max(score, BROWSER_VISUAL_CLICK_PREFLIGHT_MIN_SCORE)
    if expected_tokens and readable and score < BROWSER_VISUAL_CLICK_PREFLIGHT_MIN_SCORE and confidence < 0.85:
        return (
            f"hit-test target mismatch score {score:.2f} < {BROWSER_VISUAL_CLICK_PREFLIGHT_MIN_SCORE:.2f}",
            score,
        )
    return "", score


def _emit_artifact(callback: ArtifactCallback | None, *, event: str, round_index: int, **payload) -> None:
    if not callback:
        return
    task_id = str(payload.pop("task_id", "") or _CURRENT_VISUAL_TASK_ID.get() or "").strip()
    artifact = {
        "event": event,
        "round": round_index,
        **payload,
    }
    if task_id:
        artifact["task_id"] = task_id
    try:
        callback(artifact)
    except Exception as exc:
        logger.warning("visual artifact callback failed: %s", exc)


def _emit_state_transition(
    artifact_callback: ArtifactCallback | None,
    progress_callback: ProgressCallback | None,
    *,
    state: str,
    round_index: int,
    status: str = "running",
    summary: str = "",
    **payload,
) -> None:
    state_name = str(state or "").strip()
    if not state_name:
        return
    status_text = str(status or "running").strip() or "running"
    summary_text = str(summary or "").strip()
    _emit_artifact(
        artifact_callback,
        event="state_transition",
        round_index=round_index,
        state=state_name,
        status=status_text,
        summary=summary_text,
        **payload,
    )
    if progress_callback:
        suffix = f": {summary_text}" if summary_text else ""
        progress_callback(f"State {state_name}: {status_text}{suffix}")


def _terminal_visual_state(status: str) -> str:
    status_name = str(status or "").strip().lower()
    if status_name == "done":
        return "completed"
    if status_name == "need_user":
        return "manual_confirm"
    if status_name == "failed":
        return "failed"
    if status_name == "retry":
        return "retry"
    return "continue"


def run_local_browser_visual_operation(
    *,
    url: str = "",
    instruction: str = "",
    rounds: int = 3,
    max_round_cap: int = 8,
    task_id: str = "",
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
    visual_task_id = str(task_id or "").strip() or f"visual-task-{uuid4().hex}"
    _CURRENT_VISUAL_TASK_ID.set(visual_task_id)

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
    terminal_state_emitted = False

    _emit_state_transition(
        artifact_callback,
        progress_callback,
        state="created",
        round_index=0,
        status="ok",
        summary=f"Cloud visual browser task created; rounds={max_rounds}; model={BROWSER_VISION_MODEL}",
        max_rounds=max_rounds,
        model=BROWSER_VISION_MODEL,
        task_id=visual_task_id,
    )

    for round_index in range(1, max_rounds + 1):
        if cancel_check and cancel_check():
            return "Visual browser operation cancelled."
        _emit_state_transition(
            artifact_callback,
            progress_callback,
            state="worker_check",
            round_index=round_index,
            status="running",
            summary="Checking browser worker by requesting a screenshot.",
        )
        _emit_state_transition(
            artifact_callback,
            progress_callback,
            state="take_screenshot",
            round_index=round_index,
            status="running",
            summary="Requesting current browser screenshot.",
        )
        try:
            screenshot = _browser_worker_request(
                "browser.screenshot",
                {
                    "url": current_url,
                    "instruction": task,
                    "wait_ms": 1200 if round_index == 1 else 700,
                    "task_id": visual_task_id,
                },
                timeout=max(60, get_browser_request_timeout()),
            )
        except Exception as exc:
            logger.warning("operate_local_browser_visual screenshot failed: %s", exc)
            _emit_state_transition(
                artifact_callback,
                progress_callback,
                state="worker_check",
                round_index=round_index,
                status="failed",
                summary=str(exc),
            )
            _emit_state_transition(
                artifact_callback,
                progress_callback,
                state="failed",
                round_index=round_index,
                status="failed",
                summary=f"Visual browser operation failed while taking screenshot: {exc}",
            )
            return f"Visual browser operation failed while taking screenshot: {exc}"

        current_url = ""
        current_title = str(screenshot.get("title") or current_title or "").strip()
        page_url = str(screenshot.get("url") or "").strip()
        last_diagnostics = format_browser_diagnostics(screenshot, max_controls=12)
        _emit_state_transition(
            artifact_callback,
            progress_callback,
            state="worker_check",
            round_index=round_index,
            status="ok",
            summary="Browser worker responded.",
        )
        _emit_state_transition(
            artifact_callback,
            progress_callback,
            state="take_screenshot",
            round_index=round_index,
            status="ok",
            summary=f"Screenshot captured; title={current_title or '-'}",
            title=current_title,
            url=page_url,
        )
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
                f"title={current_title or '-'}; navigation={screenshot.get('navigation', {}).get('mode', '-') if isinstance(screenshot.get('navigation'), dict) else '-'}"
            )

        _emit_state_transition(
            artifact_callback,
            progress_callback,
            state="vision_decide",
            round_index=round_index,
            status="running",
            summary=f"Calling {BROWSER_VISION_MODEL} for the next browser decision.",
            model=BROWSER_VISION_MODEL,
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
            _emit_state_transition(
                artifact_callback,
                progress_callback,
                state="vision_decide",
                round_index=round_index,
                status="failed",
                summary=str(exc),
                model=BROWSER_VISION_MODEL,
            )
            _emit_state_transition(
                artifact_callback,
                progress_callback,
                state="failed",
                round_index=round_index,
                status="failed",
                summary=f"Visual browser operation failed while calling {BROWSER_VISION_MODEL}: {exc}",
            )
            return f"Visual browser operation failed while calling {BROWSER_VISION_MODEL}: {exc}"

        final_status = str(decision.get("status") or "continue").strip().lower()
        final_summary = str(decision.get("summary") or "").strip()
        _emit_state_transition(
            artifact_callback,
            progress_callback,
            state="vision_decide",
            round_index=round_index,
            status=final_status,
            summary=final_summary,
            model=decision.get("model") or BROWSER_VISION_MODEL,
        )
        actions = decision.get("actions") if isinstance(decision.get("actions"), list) else []
        for action_index, action in enumerate(actions[:2], start=1):
            if not isinstance(action, dict):
                continue
            action["task_id"] = visual_task_id
            action["action_id"] = str(action.get("action_id") or "").strip() or f"{visual_task_id}:r{round_index}:a{action_index}:{uuid4().hex[:8]}"

        _emit_artifact(
            artifact_callback,
            event="vision_decision",
            round_index=round_index,
            status=final_status,
            summary=final_summary,
            actions=actions[:2],
            model=decision.get("model") or BROWSER_VISION_MODEL,
        )
        history.append(f"Round {round_index}: status={final_status}; summary={final_summary or '-'}; url={page_url or '-'}")
        if progress_callback:
            progress_callback(f"Round {round_index}: vision status={final_status}; summary={final_summary or '-'}")

        if final_status in {"done", "need_user"}:
            terminal_state = _terminal_visual_state(final_status)
            _emit_state_transition(
                artifact_callback,
                progress_callback,
                state=terminal_state,
                round_index=round_index,
                status="ok" if terminal_state == "completed" else "pending",
                summary=final_summary,
            )
            terminal_state_emitted = True
            break

        if not actions:
            final_summary = final_summary or "Vision model returned no action."
            last_failure = classify_visual_failure("model_no_action", final_summary, status=final_status)
            failure_events.append(last_failure)
            _emit_state_transition(
                artifact_callback,
                progress_callback,
                state="policy_check",
                round_index=round_index,
                status="failed",
                summary=final_summary,
                failure=last_failure,
            )
            _emit_artifact(
                artifact_callback,
                event="failure",
                round_index=round_index,
                failure=last_failure,
            )
            if progress_callback:
                progress_callback("Vision model returned no action; stopping.")
            _emit_state_transition(
                artifact_callback,
                progress_callback,
                state="failed",
                round_index=round_index,
                status="failed",
                summary=final_summary,
                failure=last_failure,
            )
            terminal_state_emitted = True
            break

        for action in actions[:2]:
            if cancel_check and cancel_check():
                return "Visual browser operation cancelled."
            if not isinstance(action, dict):
                continue
            action_name = str(action.get("action") or "").strip().lower()
            action_id = str(action.get("action_id") or "").strip()
            reason = str(action.get("reason") or "").strip()
            target_description = str(action.get("target_description") or "").strip()
            expected_change = str(action.get("expected_change") or "").strip()
            confidence = float(action.get("confidence") or 0.0)
            _emit_state_transition(
                artifact_callback,
                progress_callback,
                state="policy_check",
                round_index=round_index,
                status="running",
                summary=f"Checking action safety for {action_name or '<empty>'}.",
                action=action,
            )
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
                next_state = "manual_confirm" if unsafe_replans >= 2 else "continue"
                _emit_state_transition(
                    artifact_callback,
                    progress_callback,
                    state="policy_check",
                    round_index=round_index,
                    status="rejected",
                    summary=safety_issue,
                    failure=last_failure,
                    action=action,
                )
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
                _emit_state_transition(
                    artifact_callback,
                    progress_callback,
                    state=next_state,
                    round_index=round_index,
                    status="pending" if next_state == "manual_confirm" else "ok",
                    summary=final_summary,
                    failure=last_failure,
                )
                if next_state == "manual_confirm":
                    terminal_state_emitted = True
                break
            unsafe_replans = 0
            _emit_state_transition(
                artifact_callback,
                progress_callback,
                state="policy_check",
                round_index=round_index,
                status="ok",
                summary=f"Action allowed: {action_name or '<empty>'}.",
                action=action,
            )
            if action_name in {"click", "click_xy"}:
                _emit_state_transition(
                    artifact_callback,
                    progress_callback,
                    state="preflight",
                    round_index=round_index,
                    status="running",
                    summary=f"Hit-testing click target: {target_description or action.get('candidate_id') or '-'}",
                    action=action,
                    action_id=action_id,
                )
                try:
                    hit_test = _run_click_hit_test(action)
                    state_drift_issue = page_state_drift_issue(screenshot, hit_test)
                    hit_issue, hit_score = click_hit_test_safety_issue(action, hit_test)
                    if state_drift_issue:
                        hit_issue = state_drift_issue
                        hit_score = 0.0
                    hit_summary = format_click_hit_test_summary(hit_test)
                    _emit_artifact(
                        artifact_callback,
                        event="click_preflight",
                        round_index=round_index,
                        action=action,
                        action_id=action_id,
                        hit_test=hit_test,
                        page_state_before=compact_page_state(
                            screenshot.get("page_state") if isinstance(screenshot.get("page_state"), dict) else {}
                        ),
                        page_state_after=compact_page_state(
                            hit_test.get("page_state") if isinstance(hit_test.get("page_state"), dict) else {}
                        ),
                        score=hit_score,
                        issue=hit_issue,
                        summary=hit_summary,
                    )
                    if progress_callback:
                        progress_callback(
                            f"Click preflight: score={hit_score:.2f}; issue={hit_issue or '-'}; {hit_summary}"
                    )
                    if hit_issue:
                        retry_action, retry_detail = build_click_retry_action(action, screenshot, hit_issue)
                        if retry_action:
                            _emit_artifact(
                                artifact_callback,
                                event="click_preflight_retry",
                                round_index=round_index,
                                action=retry_action,
                                action_id=action_id,
                                strategy=retry_action.get("_retry_strategy"),
                                reason=retry_action.get("_retry_reason"),
                                detail=retry_detail,
                            )
                            if progress_callback:
                                progress_callback(f"Click preflight retry: {retry_detail}")
                            try:
                                retry_hit_test = _run_click_hit_test(retry_action)
                                retry_state_drift_issue = page_state_drift_issue(screenshot, retry_hit_test)
                                retry_issue, retry_score = click_hit_test_safety_issue(retry_action, retry_hit_test)
                                if retry_state_drift_issue:
                                    retry_issue = retry_state_drift_issue
                                    retry_score = 0.0
                                retry_summary = format_click_hit_test_summary(retry_hit_test)
                                _emit_artifact(
                                    artifact_callback,
                                    event="click_preflight",
                                    round_index=round_index,
                                    action=retry_action,
                                    action_id=action_id,
                                    hit_test=retry_hit_test,
                                    page_state_before=compact_page_state(
                                        screenshot.get("page_state") if isinstance(screenshot.get("page_state"), dict) else {}
                                    ),
                                    page_state_after=compact_page_state(
                                        retry_hit_test.get("page_state") if isinstance(retry_hit_test.get("page_state"), dict) else {}
                                    ),
                                    score=retry_score,
                                    issue=retry_issue,
                                    summary=retry_summary,
                                    retry=True,
                                )
                                if progress_callback:
                                    progress_callback(
                                        f"Click preflight retry result: score={retry_score:.2f}; issue={retry_issue or '-'}; {retry_summary}"
                                    )
                                if not retry_issue:
                                    action = retry_action
                                    target_description = str(action.get("target_description") or "").strip()
                                    expected_change = str(action.get("expected_change") or "").strip()
                                    hit_issue = ""
                                    hit_score = retry_score
                                    hit_test = retry_hit_test
                                    hit_summary = retry_summary
                            except Exception as retry_exc:
                                _emit_artifact(
                                    artifact_callback,
                                    event="click_preflight_retry_failed",
                                    round_index=round_index,
                                    action=retry_action,
                                    action_id=action_id,
                                    error=str(retry_exc),
                                    strategy=retry_action.get("_retry_strategy"),
                                )
                                if progress_callback:
                                    progress_callback(f"Click preflight retry failed: {retry_exc}")
                        if not hit_issue:
                            click_preflight_replans = 0
                            _emit_state_transition(
                                artifact_callback,
                                progress_callback,
                                state="preflight",
                                round_index=round_index,
                                status="ok",
                                summary=f"Retry accepted: {hit_summary}",
                                score=hit_score,
                                action=action,
                                action_id=action_id,
                            )
                        else:
                            click_preflight_replans += 1
                            last_failure = classify_visual_failure(
                                "click_preflight",
                                hit_issue,
                                status="need_user" if click_preflight_replans >= 2 else "continue",
                                retry_count=click_preflight_replans,
                                context={
                                    "action": action,
                                    "hit_test": hit_test,
                                    "screenshot": screenshot,
                                    "score": hit_score,
                                    "page_state_before": screenshot.get("page_state") if isinstance(screenshot.get("page_state"), dict) else {},
                                    "page_state_after": hit_test.get("page_state") if isinstance(hit_test.get("page_state"), dict) else {},
                                },
                            )
                            failure_events.append(last_failure)
                            trace = (
                                f"Skipped visual click after preflight: issue={hit_issue}; score={hit_score:.2f}; "
                                f"target={target_description or '-'}; hit={hit_summary}"
                            )
                            history.append(trace)
                            next_state = "manual_confirm" if click_preflight_replans >= 2 else "continue"
                            _emit_state_transition(
                                artifact_callback,
                                progress_callback,
                                state="preflight",
                                round_index=round_index,
                                status="rejected",
                                summary=hit_issue,
                                score=hit_score,
                                failure=last_failure,
                                action=action,
                                action_id=action_id,
                            )
                            if progress_callback:
                                progress_callback(trace)
                            _emit_artifact(
                                artifact_callback,
                                event="failure",
                                round_index=round_index,
                                failure=last_failure,
                                action=action,
                                action_id=action_id,
                                trace=trace,
                            )
                            if click_preflight_replans >= 2:
                                final_status = "need_user"
                                final_summary = "Click preflight could not confirm a safe target."
                            else:
                                final_status = "continue"
                                final_summary = "Click preflight rejected the target; replanning from a fresh screenshot."
                            _emit_state_transition(
                                artifact_callback,
                                progress_callback,
                                state=next_state,
                                round_index=round_index,
                                status="pending" if next_state == "manual_confirm" else "ok",
                                summary=final_summary,
                                failure=last_failure,
                                action_id=action_id,
                            )
                            if next_state == "manual_confirm":
                                terminal_state_emitted = True
                            break
                    click_preflight_replans = 0
                    _emit_state_transition(
                        artifact_callback,
                        progress_callback,
                        state="preflight",
                        round_index=round_index,
                        status="ok",
                        summary=hit_summary,
                        score=hit_score,
                        action=action,
                        action_id=action_id,
                    )
                except Exception as exc:
                    logger.warning("visual click preflight failed: %s", exc)
                    preflight_failure = classify_visual_failure(
                        "click_preflight_unavailable",
                        str(exc),
                        status="failed",
                    )
                    last_failure = preflight_failure
                    failure_events.append(last_failure)
                    final_status = "failed"
                    final_summary = f"Click preflight failed; click was not executed: {exc}"
                    _emit_state_transition(
                        artifact_callback,
                        progress_callback,
                        state="preflight",
                        round_index=round_index,
                        status="failed",
                        summary=final_summary,
                        failure=preflight_failure,
                        action=action,
                        action_id=action_id,
                    )
                    _emit_artifact(
                        artifact_callback,
                        event="click_preflight_failed",
                        round_index=round_index,
                        action=action,
                        action_id=action_id,
                        error=str(exc),
                        failure=preflight_failure,
                    )
                    _emit_artifact(
                        artifact_callback,
                        event="failure",
                        round_index=round_index,
                        failure=last_failure,
                        action=action,
                        action_id=action_id,
                    )
                    _emit_state_transition(
                        artifact_callback,
                        progress_callback,
                        state="failed",
                        round_index=round_index,
                        status="failed",
                        summary=final_summary,
                        failure=last_failure,
                        action_id=action_id,
                    )
                    terminal_state_emitted = True
                    if progress_callback:
                        progress_callback(final_summary)
                    break
            else:
                _emit_state_transition(
                    artifact_callback,
                    progress_callback,
                    state="preflight",
                    round_index=round_index,
                    status="skipped",
                    summary="Preflight is only required for visual click actions.",
                    action=action,
                    action_id=action_id,
                )
            if progress_callback:
                progress_callback(
                    f"Executing visual action: {action_name or '<empty>'}; confidence={confidence:.2f}; "
                    f"target={target_description or '-'}; expected={expected_change or '-'}; reason={reason or '-'}"
                )
            _emit_state_transition(
                artifact_callback,
                progress_callback,
                state="execute_action",
                round_index=round_index,
                status="running",
                summary=f"Executing {action_name or '<empty>'}.",
                action=action,
                action_id=action_id,
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
                _emit_state_transition(
                    artifact_callback,
                    progress_callback,
                    state="execute_action",
                    round_index=round_index,
                    status="failed",
                    summary=final_summary,
                    failure=last_failure,
                    action=action,
                    action_id=action_id,
                )
                _emit_state_transition(
                    artifact_callback,
                    progress_callback,
                    state="failed",
                    round_index=round_index,
                    status="failed",
                    summary=final_summary,
                    failure=last_failure,
                    action_id=action_id,
                )
                terminal_state_emitted = True
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
            if action.get("_preflight_retry"):
                trace += f"; retry_strategy={action.get('_retry_strategy') or '-'}"
            if backend:
                trace += f"; backend={backend}"
            history.append(trace)
            _emit_artifact(
                artifact_callback,
                event="action_result",
                round_index=round_index,
                action=action,
                action_id=action_id,
                result=_compact_action_result(result),
                trace=trace,
            )
            _emit_state_transition(
                artifact_callback,
                progress_callback,
                state="execute_action",
                round_index=round_index,
                status="ok",
                summary=trace,
                action=action,
                action_id=action_id,
                result=_compact_action_result(result),
            )
            if progress_callback:
                progress_callback(trace)

            if not BROWSER_VISION_VERIFY_ENABLED and action_name not in {"click", "click_xy"}:
                _emit_state_transition(
                    artifact_callback,
                    progress_callback,
                    state="verify_action",
                    round_index=round_index,
                    status="skipped",
                    summary="Action verification is disabled.",
                    action=action,
                    action_id=action_id,
                )
                continue
            if cancel_check and cancel_check():
                return "Visual browser operation cancelled."
            _emit_state_transition(
                artifact_callback,
                progress_callback,
                state="verify_action",
                round_index=round_index,
                status="running",
                summary="Taking post-action screenshot and verifying expected change.",
                action=action,
                action_id=action_id,
            )
            try:
                verification_screenshot = _browser_worker_request(
                    "browser.screenshot",
                    {
                        "url": "",
                        "instruction": f"Verify action result for: {task}",
                        "wait_ms": 400,
                        "task_id": visual_task_id,
                        "action_id": action_id,
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
                    action_id=action_id,
                    screenshot=compact_visual_screenshot(verification_screenshot),
                )
            except Exception as exc:
                logger.warning("operate_local_browser_visual verification failed: %s", exc)
                history.append(f"Verification failed: {exc}")
                if action_name in {"click", "click_xy"}:
                    last_failure = classify_visual_failure(
                        "verification_unavailable",
                        str(exc),
                        status="failed",
                    )
                    failure_events.append(last_failure)
                    final_status = "failed"
                    final_summary = f"Click verification failed after action: {exc}"
                    _emit_artifact(
                        artifact_callback,
                        event="failure",
                        round_index=round_index,
                        failure=last_failure,
                        action=action,
                        action_id=action_id,
                    )
                    _emit_state_transition(
                        artifact_callback,
                        progress_callback,
                        state="verify_action",
                        round_index=round_index,
                        status="failed",
                        summary=final_summary,
                        failure=last_failure,
                        action=action,
                        action_id=action_id,
                    )
                    _emit_state_transition(
                        artifact_callback,
                        progress_callback,
                        state="failed",
                        round_index=round_index,
                        status="failed",
                        summary=final_summary,
                        failure=last_failure,
                        action_id=action_id,
                    )
                    terminal_state_emitted = True
                    if progress_callback:
                        progress_callback(final_summary)
                    break
                _emit_state_transition(
                    artifact_callback,
                    progress_callback,
                    state="verify_action",
                    round_index=round_index,
                    status="warning",
                    summary=f"Verification failed: {exc}",
                    action=action,
                    action_id=action_id,
                )
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
                action_id=action_id,
                trace=verify_trace,
            )
            _emit_state_transition(
                artifact_callback,
                progress_callback,
                state="verify_action",
                round_index=round_index,
                status=verify_status,
                summary=verify_summary,
                action=action,
                action_id=action_id,
                verification=verification,
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
                context={
                    "action": action,
                    "action_result": result,
                    "verification": verification,
                    "before": screenshot,
                    "after": verification_screenshot,
                },
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
                action_id=action_id,
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
                _emit_state_transition(
                    artifact_callback,
                    progress_callback,
                    state="retry",
                    round_index=round_index,
                    status="ok",
                    summary=final_summary,
                    failure=last_failure,
                    retry_hint=retry_hint,
                    action_id=action_id,
                )
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
                terminal_state = _terminal_visual_state(policy_status)
                _emit_state_transition(
                    artifact_callback,
                    progress_callback,
                    state=terminal_state,
                    round_index=round_index,
                    status="ok" if terminal_state == "completed" else "pending",
                    summary=final_summary,
                    failure=verification_failure if terminal_state == "manual_confirm" else {},
                    action_id=action_id,
                )
                terminal_state_emitted = True
                break
            _emit_state_transition(
                artifact_callback,
                progress_callback,
                state="continue",
                round_index=round_index,
                status="ok",
                summary=policy_reason or verify_summary or "Action verified; continuing if more steps are needed.",
                action_id=action_id,
            )

        if final_status == "failed":
            break
        if final_status in {"done", "need_user"}:
            break

    lines = [
        f"Visual browser model: {BROWSER_VISION_MODEL}",
        f"Task ID: {visual_task_id}",
        f"Final status: {final_status or 'continue'}",
        f"Page title: {current_title or '-'}",
    ]
    if final_summary:
        lines.append(f"Summary: {final_summary}")
    if last_failure.get("category") != "none":
        lines.append(
            "Last failure: "
            f"failure_type={last_failure.get('failure_type') or last_failure.get('category')}; "
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
    if not terminal_state_emitted:
        _emit_state_transition(
            artifact_callback,
            progress_callback,
            state=_terminal_visual_state(final_status),
            round_index=max_rounds,
            status="ok" if final_status == "done" else ("failed" if final_status == "failed" else "pending" if final_status == "need_user" else "ok"),
            summary=final_summary,
            last_failure=last_failure,
        )
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
