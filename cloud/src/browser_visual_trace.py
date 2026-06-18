from __future__ import annotations


def format_browser_diagnostics(payload: dict, *, max_controls: int = 18) -> list[str]:
    diagnostics = payload.get("diagnostics") if isinstance(payload.get("diagnostics"), dict) else {}
    page_state = payload.get("page_state") if isinstance(payload.get("page_state"), dict) else {}
    action_candidates = payload.get("action_candidates") if isinstance(payload.get("action_candidates"), list) else []
    lines: list[str] = []
    if page_state and page_state.get("ok") is not False:
        state_viewport = page_state.get("viewport") if isinstance(page_state.get("viewport"), dict) else {}
        state_scroll = page_state.get("scroll") if isinstance(page_state.get("scroll"), dict) else {}
        lines.append(
            "Page state: "
            f"sig={page_state.get('signature', '-')}, "
            f"url={str(page_state.get('url') or '-')[:180]}, "
            f"viewport={state_viewport.get('width', '-')}x{state_viewport.get('height', '-')}, "
            f"dpr={state_viewport.get('devicePixelRatio', state_viewport.get('dpr', '-'))}, "
            f"scrollY={state_scroll.get('y', '-')}, "
            f"textHash={page_state.get('visibleTextHash', '-')}"
        )
    if action_candidates:
        lines.append("Visible action candidates:")
        for item in action_candidates[:max_controls]:
            if not isinstance(item, dict):
                continue
            candidate_id = str(item.get("candidate_id") or "").strip()
            label = str(item.get("label") or item.get("text") or item.get("ariaLabel") or item.get("href") or "").strip()
            tag = str(item.get("tag") or "element").strip()
            rect = item.get("rect") if isinstance(item.get("rect"), dict) else {}
            center = item.get("center") if isinstance(item.get("center"), dict) else {}
            selector = str(item.get("selector") or "").strip()
            lines.append(
                f"- {candidate_id or '-'} | {label[:160] or '[no text]'} | {tag} | "
                f"rect={rect.get('x', '-')},{rect.get('y', '-')},{rect.get('width', '-')}x{rect.get('height', '-')} | "
                f"center={center.get('normalizedX', '-')},{center.get('normalizedY', '-')} | selector: {selector[:140] or '-'}"
            )
    if not diagnostics or diagnostics.get("ok") is False:
        return lines

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
