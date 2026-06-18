const messagesEl = document.getElementById("messages");
const emptyStateEl = document.getElementById("emptyState");
const promptEl = document.getElementById("prompt");
const sendBtnEl = document.getElementById("sendBtn");
const clearBtnEl = document.getElementById("clearBtn");
const themeBtnEl = document.getElementById("themeBtn");
const localExecBtnEl = document.getElementById("localExecBtn");
const logoutBtnEl = document.getElementById("logoutBtn");
const statusTextEl = document.getElementById("statusText");
const statusLineEl = document.getElementById("statusLine");
const charCountEl = document.getElementById("charCount");

const STORAGE_KEY = "sunwin_messages_v5";
const LOCAL_EXEC_KEY = "sunwin_local_exec_v2";
const THEME_KEY = "sunwin_theme_v4";
const THREAD_KEY = "sunwin_thread_v4";

let chatHistory = [];
let isSending = false;
let localExecutionMode = false;
let currentUser = null;
let appHealth = null;

function setViewportHeight() {
    document.documentElement.style.setProperty("--vh", `${window.innerHeight * 0.01}px`);
}

function nowTime() {
    return new Date().toLocaleTimeString("zh-CN", {
        hour: "2-digit",
        minute: "2-digit",
    });
}

function escapeHtml(value) {
    return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}

function renderMarkdownLite(text) {
    let html = escapeHtml(text || "");
    html = html.replace(/```([\s\S]*?)```/g, (_, code) => `<pre><code>${code.trim()}</code></pre>`);
    html = html.replace(/`([^`]+)`/g, "<code>$1</code>");

    return html
        .split(/\n{2,}/)
        .map((block) => {
            const trimmed = block.trim();
            if (!trimmed) return "";
            if (trimmed.startsWith("<pre>")) return trimmed;
            return `<p>${trimmed.replace(/\n/g, "<br>")}</p>`;
        })
        .join("");
}

function getThreadId() {
    return localStorage.getItem(THREAD_KEY) || "";
}

function setThreadId(threadId) {
    if (threadId) {
        localStorage.setItem(THREAD_KEY, threadId);
    }
}

function clearThreadId() {
    localStorage.removeItem(THREAD_KEY);
}

function updateEmptyState() {
    emptyStateEl.style.display = chatHistory.length ? "none" : "block";
}

function resizeInput() {
    promptEl.style.height = "auto";
    promptEl.style.height = `${Math.min(promptEl.scrollHeight, 180)}px`;
    charCountEl.textContent = String(promptEl.value.length);
}

function setStatus(text, type = "ok") {
    statusTextEl.textContent = text;
    const dot = statusLineEl.querySelector(".status-dot");
    dot.style.background = type === "error" ? "var(--danger)" : type === "warn" ? "var(--warning)" : "var(--success)";
}

async function ensureAuthenticated() {
    const response = await fetch("/api/auth/me", { credentials: "same-origin" });
    if (!response.ok) {
        const next = encodeURIComponent(window.location.pathname + window.location.search);
        window.location.replace(`/login.html?next=${next}`);
        throw new Error("Not authenticated");
    }
    const data = await response.json();
    currentUser = data.user || null;
    if (currentUser?.display_name) {
        setStatus(`已登录：${currentUser.display_name}`);
    }
}

async function refreshHealth() {
    const response = await fetch("/health", { credentials: "same-origin" });
    if (!response.ok) {
        throw new Error(`健康检查失败：HTTP ${response.status}`);
    }
    appHealth = await response.json();
    return appHealth;
}

function getDefaultBrowserWorker(health) {
    const defaultId = String(health?.browser_worker_default_id || "default").trim() || "default";
    const workers = Array.isArray(health?.browser_workers) ? health.browser_workers : [];
    return workers.find((item) => String(item?.worker_id || "").trim() === defaultId) || null;
}

async function waitForBrowserWorkerReady(maxAttempts = 4) {
    let health = await refreshHealth();
    for (let attempt = 1; attempt < maxAttempts; attempt += 1) {
        if (!health?.browser_worker_enabled || health?.browser_worker_connected) {
            return health;
        }
        setStatus(`本地 Worker 未连接，正在重试 ${attempt}/${maxAttempts - 1}`, "warn");
        await new Promise((resolve) => setTimeout(resolve, 1000));
        health = await refreshHealth();
    }
    return health;
}

function updateLocalExecButton() {
    localExecBtnEl.classList.toggle("active", localExecutionMode);
    localExecBtnEl.setAttribute("aria-pressed", String(localExecutionMode));
    localExecBtnEl.title = localExecutionMode ? "本地执行模式已开启" : "本地执行模式已关闭";
}

function validateLocalExecutionHealth(health) {
    if (!localExecutionMode) {
        return;
    }
    if (health?.browser_worker_enabled && !health?.browser_worker_connected) {
        throw new Error("本地浏览器 Worker 未连接。请先启动 local_launcher\\start_local_services.bat，并确认本机已连上云端 /ws/browser-worker。");
    }
    if (!health?.browser_worker_enabled) {
        throw new Error("Local browser execution is disabled. Set BROWSER_WORKER_ENABLED=true on the cloud server.");
    }
}

function runtimeStatusSuffix(health) {
    if (!health) return "";
    const worker = getDefaultBrowserWorker(health);
    const heartbeat = typeof worker?.heartbeat_age_sec === "number" ? `，心跳 ${Math.round(worker.heartbeat_age_sec)}s` : "";
    if (localExecutionMode) {
        if (health.browser_worker_enabled) {
            return health.browser_worker_connected ? `本地 Worker 已连接${heartbeat}` : "本地 Worker 未连接";
        }
        return "Browser Worker disabled";
    }
    if (health.browser_worker_enabled) {
        return health.browser_worker_connected ? `Worker 在线${heartbeat}` : "Worker 离线";
    }
    return "";
}

function plannerLabel(route) {
    if (route === "research") return "思考";
    if (route === "agent") return "执行";
    return "未知";
}

function answerModeLabel(mode) {
    if (mode === "grounded_summary") return "基于证据总结";
    if (mode === "tool_agent") return "工具执行";
    return "-";
}

function complexityLabel(value) {
    const map = {
        simple: "简单",
        standard: "标准",
        advanced: "高级",
    };
    return map[value] || "标准";
}

function toolLabel(name) {
    const map = {
        send_email: "发送邮件",
        online_research: "联网思考",
        ask_open_interpreter: "本地解释器",
        tool_guard: "工具权限控制",
        request_human_confirmation: "人工确认",
        start_open_interpreter_job: "本地长任务",
        start_cloud_browser_visual_job: "云端视觉长任务",
    };
    return map[name] || name || "工具";
}

function toolStatusLabel(status) {
    const map = {
        ok: "已完成",
        error: "失败",
        pending: "待执行",
    };
    return map[status] || "已完成";
}

function actionLabel(name) {
    const map = {
        send_email: "发送邮件",
    };
    return map[name] || name || "无";
}

function formatEmailTrace(text) {
    return text
        .replace(/^Email is not configured\./gm, "邮件功能尚未配置。")
        .replace(/^Set SMTP_USER and SMTP_PASSWORD\./gm, "请先配置 SMTP_USER 和 SMTP_PASSWORD。")
        .replace(/^Email sent to\s*/gm, "已发送到：")
        .replace(/^Email send failed:\s*/gm, "邮件发送失败：");
}

function formatToolTraceContent(content, toolName = "") {
    const text = String(content || "").trim();
    if (!text) return "-";
    if (toolName === "send_email") return formatEmailTrace(text);
    return text;
}

function copyText(text, buttonEl) {
    navigator.clipboard.writeText(text).then(() => {
        const prev = buttonEl.textContent;
        buttonEl.textContent = "已复制";
        setTimeout(() => {
            buttonEl.textContent = prev;
        }, 1200);
    });
}

function createTraceBlock(meta) {
    if (!meta || (!meta.plannerDecision && !meta.toolTrace?.length)) {
        return null;
    }

    const stack = document.createElement("div");
    stack.className = "trace-stack";

    if (meta.plannerDecision) {
        const planner = meta.plannerDecision;
        const card = document.createElement("details");
        card.className = "trace-card planner-card";

        const summary = document.createElement("summary");
        summary.innerHTML = `
            <div class="trace-labels">
                <span class="trace-tag route-${escapeHtml(planner.route || "unknown")}">${escapeHtml(plannerLabel(planner.route))}</span>
                <span>规划决策</span>
            </div>
        `;

        const content = document.createElement("div");
        content.className = "trace-content";

        const actions = planner.post_actions?.length
            ? planner.post_actions.map((item) => actionLabel(item)).join("、")
            : "无";
        content.innerHTML = `
            <div class="kv-grid">
                <div class="kv">
                    <strong>路由</strong>
                    <div>${escapeHtml(plannerLabel(planner.route))}</div>
                </div>
                <div class="kv">
                    <strong>模式</strong>
                    <div>${escapeHtml(answerModeLabel(planner.answer_mode))}</div>
                </div>
                <div class="kv">
                    <strong>复杂度</strong>
                    <div>${escapeHtml(complexityLabel(planner.complexity))}</div>
                </div>
                <div class="kv">
                    <strong>后续动作</strong>
                    <div>${escapeHtml(actions)}</div>
                </div>
            </div>
            <div class="kv">
                <strong>原因</strong>
                <div>${escapeHtml(planner.reason || "-")}</div>
            </div>
        `;

        card.append(summary, content);
        stack.appendChild(card);
    }

    if (meta.toolTrace?.length) {
        const card = document.createElement("details");
        card.className = "trace-card tool-trace-card";

        const summary = document.createElement("summary");
        summary.innerHTML = `
            <div class="trace-labels">
                <span class="trace-tag trace">${meta.toolTrace.length}</span>
                <span>工具轨迹</span>
            </div>
        `;

        const content = document.createElement("div");
        content.className = "trace-content";

        meta.toolTrace.forEach((item, index) => {
            const block = document.createElement("div");
            block.className = "kv";
            const title = item.title || toolLabel(item.tool);
            const summary = item.summary ? `<div class="trace-summary">${escapeHtml(item.summary)}</div>` : "";
            const status = item.status ? `<div class="trace-summary">${escapeHtml(toolStatusLabel(item.status))}</div>` : "";
            const model = item.model ? `<div class="trace-summary">模型：${escapeHtml(item.model)}</div>` : "";
            block.innerHTML = `
                <strong>步骤 ${index + 1} · ${escapeHtml(title)}</strong>
                ${summary}
                ${status}
                ${model}
                <pre class="trace-pre">${escapeHtml(formatToolTraceContent(item.content || "-", item.tool || ""))}</pre>
            `;
            content.appendChild(block);
        });

        card.append(summary, content);
        stack.appendChild(card);
    }

    return stack;
}

function addMessage(role, content, options = {}) {
    const { save = true, time = nowTime(), meta = null } = options;
    const wrapper = document.createElement("div");
    wrapper.className = `message ${role}`;

    const main = document.createElement("div");
    main.className = "message-main";

    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.innerHTML = renderMarkdownLite(content);

    const metaRow = document.createElement("div");
    metaRow.className = "message-meta";

    const timeEl = document.createElement("span");
    timeEl.textContent = time;

    const copyBtn = document.createElement("button");
    copyBtn.className = "copy-btn";
    copyBtn.type = "button";
    copyBtn.textContent = "复制";
    copyBtn.addEventListener("click", () => copyText(content, copyBtn));

    metaRow.append(timeEl, copyBtn);
    main.append(bubble, metaRow);
    wrapper.appendChild(main);

    const traceBlock = createTraceBlock(meta);
    if (traceBlock) {
        wrapper.classList.add("has-trace");
        wrapper.appendChild(traceBlock);
    }

    if (meta?.awaitingConfirmation || meta?.pendingJob) {
        appendInteractivePanel(wrapper, meta);
    }

    messagesEl.appendChild(wrapper);
    messagesEl.scrollTop = messagesEl.scrollHeight;

    if (save) {
        chatHistory.push({ role, content, time, meta });
        persistState();
    }

    updateEmptyState();
    return wrapper;
}

function addLoadingMessage(label = "正在思考") {
    const wrapper = document.createElement("div");
    wrapper.className = "message assistant";
    wrapper.dataset.loading = "true";

    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.innerHTML = `
        <div class="loading-bubble">
            <p class="loading-line">
                <span class="loading-label">${escapeHtml(label)}</span>
                <span class="dots">
                    <span class="dot"></span>
                    <span class="dot"></span>
                    <span class="dot"></span>
                </span>
            </p>
            <div class="loading-progress"></div>
        </div>
    `;

    wrapper.appendChild(bubble);
    messagesEl.appendChild(wrapper);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return wrapper;
}

function updateLoadingMessage(wrapper, label) {
    if (!wrapper) return;
    const labelEl = wrapper.querySelector(".loading-label");
    if (labelEl) {
        labelEl.textContent = label;
    }
}

function summarizeLoadingLabel(text, limit = 32) {
    const compact = String(text || "").replace(/\s+/g, " ").trim();
    if (!compact) return "";
    if (compact.length <= limit) return compact;
    return `${compact.slice(0, limit).trimEnd()}...`;
}

function appendLoadingProgress(wrapper, text) {
    if (!wrapper || !text) return;
    const progressEl = wrapper.querySelector(".loading-progress");
    if (!progressEl) return;
    const item = document.createElement("div");
    item.className = "loading-progress-item";
    item.textContent = text;
    progressEl.appendChild(item);
    progressEl.scrollTop = progressEl.scrollHeight;

    const summary = summarizeLoadingLabel(text);
    if (summary) {
        updateLoadingMessage(wrapper, summary);
    }
}

function setLoadingStage(wrapper, label, status = `${label}...`, type = "ok") {
    updateLoadingMessage(wrapper, label);
    setStatus(status, type);
}

function syncLoadingStage(wrapper, event, data = {}) {
    if (!wrapper) return;

    if (event === "planner_started") {
        setLoadingStage(wrapper, "正在规划", "正在规划...", "ok");
    } else if (event === "planner_finished") {
        const route = plannerLabel(data.planner_decision?.route);
        setLoadingStage(wrapper, `规划完成: ${route}`, "规划完成", "ok");
    } else if (event === "research_started") {
        const query = (data.query || "").trim();
        setLoadingStage(wrapper, query ? `正在联网: ${query}` : "正在联网", "正在联网搜索...", "ok");
    } else if (event === "research_finished") {
        setLoadingStage(wrapper, "联网完成", "联网完成", "ok");
    } else if (event === "agent_started") {
        const complexity = complexityLabel(data.complexity || "standard");
        setLoadingStage(wrapper, `正在执行: ${complexity}`, "正在执行任务...", "ok");
    } else if (event === "agent_finished") {
        setLoadingStage(wrapper, "正在整理回答", "正在整理回答...", "ok");
    } else if (event === "agent_tool_plan") {
        const names = (data.tools || []).map(toolLabel).filter(Boolean);
        setLoadingStage(wrapper, names.length ? `准备调用: ${names.join("、")}` : "准备调用工具", "准备调用工具...", "ok");
    } else if (event === "tool_started") {
        const title = data.title || toolLabel(data.tool);
        setLoadingStage(wrapper, `正在执行: ${title}`, `正在执行 ${title}...`, "ok");
    } else if (event === "tool_progress") {
        const title = toolLabel(data.tool);
        const chunk = (data.chunk || "").trim().replace(/\s+/g, " ");
        const preview = chunk ? chunk.slice(0, 24) : "";
        setLoadingStage(wrapper, preview ? `${title}: ${preview}` : `正在执行: ${title}`, `正在执行 ${title}...`, "ok");
    } else if (event === "tool_finished") {
        const title = data.title || toolLabel(data.tool);
        setLoadingStage(wrapper, `${title} 已完成`, `${title} 已完成`, data.status === "pending" ? "warn" : "ok");
    } else if (event === "tool_error") {
        const title = data.title || toolLabel(data.tool);
        setLoadingStage(wrapper, `${title} 失败`, `${title} 执行失败`, "error");
    } else if (event === "job_created") {
        const title = data.job?.title || "本地长任务";
        setLoadingStage(wrapper, `后台执行: ${title}`, "任务已转入后台执行", "ok");
    } else if (event === "awaiting_confirmation") {
        setLoadingStage(wrapper, "等待你的确认", "等待你的确认", "warn");
    } else if (event === "run_failed") {
        setLoadingStage(wrapper, "处理失败", data.detail || "处理失败", "error");
    }
}

function startLoadingStageRotation(wrapper) {
    setLoadingStage(wrapper, "正在规划", "正在规划...", "ok");
    return () => {};
}

function parseSseBlock(block) {
    const lines = block.split("\n");
    let event = "message";
    const dataLines = [];
    lines.forEach((line) => {
        if (line.startsWith("event:")) {
            event = line.slice(6).trim();
        } else if (line.startsWith("data:")) {
            dataLines.push(line.slice(5).trim());
        }
    });
    const raw = dataLines.join("\n");
    return {
        event,
        data: raw ? JSON.parse(raw) : {},
    };
}

async function consumeSseResponse(response, onEvent) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";

    while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        let boundary = buffer.indexOf("\n\n");
        while (boundary >= 0) {
            const block = buffer.slice(0, boundary).trim();
            buffer = buffer.slice(boundary + 2);
            if (block) {
                const parsed = parseSseBlock(block);
                await onEvent(parsed.event, parsed.data);
            }
            boundary = buffer.indexOf("\n\n");
        }
    }
}

function buildMessageMetaFromResponse(data) {
    return {
        plannerDecision: data.planner_decision || null,
        toolTrace: data.tool_trace || [],
        awaitingConfirmation: data.awaiting_confirmation || null,
        pendingJob: data.pending_job || null,
    };
}

function pollCloudJob(jobId, onUpdate, shouldContinue = () => true) {
    let active = true;

    (async () => {
        while (active && shouldContinue()) {
            const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`, {
                credentials: "same-origin",
            });
            if (!response.ok) {
                break;
            }
            const job = await response.json();
            onUpdate(job);
            if (job.status === "completed" || job.status === "failed") {
                break;
            }
            await new Promise((resolve) => window.setTimeout(resolve, 2000));
        }
    })().catch((error) => {
        console.error("pollCloudJob failed", error);
    });

    return () => {
        active = false;
    };
}

function streamCloudJob(jobId, handlers = {}, shouldContinue = () => true) {
    if (!window.EventSource) return null;
    const source = new EventSource(`/api/jobs/${encodeURIComponent(jobId)}/stream`, { withCredentials: true });
    let closed = false;

    const close = () => {
        if (closed) return;
        closed = true;
        source.close();
    };

    const handleJson = (event, callback) => {
        if (!shouldContinue()) {
            close();
            return;
        }
        try {
            callback(JSON.parse(event.data || "{}"));
        } catch (error) {
            console.error("job stream parse failed", error);
        }
    };

    source.addEventListener("job_snapshot", (event) => handleJson(event, (data) => handlers.onSnapshot?.(data.job)));
    source.addEventListener("job_progress", (event) => handleJson(event, (data) => handlers.onProgress?.(data)));
    source.addEventListener("job_artifact", (event) => handleJson(event, (data) => handlers.onArtifact?.(data)));
    source.addEventListener("job_finished", (event) => handleJson(event, (data) => {
        handlers.onSnapshot?.(data.job);
        handlers.onFinished?.(data.job);
        close();
    }));
    source.addEventListener("job_missing", () => close());
    source.onerror = () => {
        close();
        handlers.onError?.();
    };
    return close;
}

function getJobResultPayload(job) {
    if (job?.result_payload && typeof job.result_payload === "object") {
        return job.result_payload;
    }
    const raw = String(job?.result || "").trim();
    if (!raw || !raw.startsWith("{")) return null;
    try {
        const parsed = JSON.parse(raw);
        return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : null;
    } catch {
        return null;
    }
}

function buildJobBackendLabel(payload) {
    if (!payload) return "";
    const backend = String(payload.backend || "").trim();
    return backend || "";
}

function visualTraceTone(item) {
    const event = String(item?.event || "").toLowerCase();
    const status = String(item?.status || item?.failure?.severity || item?.last_failure?.severity || "").toLowerCase();
    if (event.includes("failed") || event === "failure" || status === "failed" || status === "error") return "error";
    if (status === "rejected" || status === "warning" || status === "retry" || status === "pending") return "warn";
    if (status === "running") return "running";
    if (status === "ok" || status === "done" || status === "completed") return "ok";
    return "neutral";
}

function visualTraceTitle(item) {
    const event = String(item?.event || "artifact").trim();
    if (event === "state_transition") return item.state ? `State · ${item.state}` : "State";
    if (event === "vision_decision") return "Vision Decision";
    if (event === "click_preflight") return "Click Preflight";
    if (event === "click_preflight_failed") return "Click Preflight Failed";
    if (event === "action_result") return "Action Result";
    if (event === "verification_screenshot") return "Verification Screenshot";
    if (event === "verification") return "Action Verification";
    if (event === "verification_policy") return "Verification Policy";
    if (event === "failure") return "Failure";
    if (event === "final") return "Final";
    if (event === "screenshot") return "Screenshot";
    return event;
}

function visualTraceBadges(item) {
    const failure = item?.failure || item?.last_failure || {};
    const action = item?.action || {};
    return [
        item?.round !== undefined ? `round ${item.round}` : "",
        item?.status ? String(item.status) : "",
        item?.action_id || action.action_id ? `action ${item.action_id || action.action_id}` : "",
        action.action ? String(action.action) : "",
        failure.failure_type || failure.category ? String(failure.failure_type || failure.category) : "",
        failure.severity ? String(failure.severity) : "",
    ].filter(Boolean);
}

function visualTraceOverview(artifacts) {
    const states = artifacts.filter((item) => item?.event === "state_transition").length;
    const failures = artifacts.filter((item) => item?.event === "failure" || item?.failure || item?.last_failure).length;
    const screenshots = artifacts.filter((item) => item?.screenshot).length;
    const lastFailure = [...artifacts].reverse().map((item) => item?.last_failure || item?.failure).find(Boolean) || {};
    return [
        `${artifacts.length} events`,
        states ? `${states} states` : "",
        screenshots ? `${screenshots} screenshots` : "",
        failures ? `${failures} failures` : "",
        lastFailure.failure_type || lastFailure.category ? `last=${lastFailure.failure_type || lastFailure.category}` : "",
    ].filter(Boolean);
}

function summarizeVisualArtifact(item) {
    const event = String(item?.event || "artifact").trim();
    if (event === "state_transition") {
        return [
            item.state ? `state=${item.state}` : "",
            item.status ? `status=${item.status}` : "",
            item.action_id ? `action_id=${item.action_id}` : "",
            item.summary ? `summary=${item.summary}` : "",
        ].filter(Boolean).join("; ");
    }
    if (event === "vision_decision") {
        const actions = Array.isArray(item.actions) ? item.actions : [];
        const action = actions[0] || {};
        return [
            item.status ? `status=${item.status}` : "",
            item.summary ? `summary=${item.summary}` : "",
            action.action ? `action=${action.action}` : "",
            action.target_description ? `target=${action.target_description}` : "",
            action.confidence !== undefined ? `confidence=${action.confidence}` : "",
        ].filter(Boolean).join("; ");
    }
    if (event === "action_result") {
        const action = item.action || {};
        return [
            item.action_id || action.action_id ? `action_id=${item.action_id || action.action_id}` : "",
            action.action ? `action=${action.action}` : "",
            action.target_description ? `target=${action.target_description}` : "",
            item.result?.ok !== undefined ? `ok=${Boolean(item.result.ok)}` : "",
            item.trace || "",
        ].filter(Boolean).join("; ");
    }
    if (event === "click_preflight") {
        return [
            item.action_id ? `action_id=${item.action_id}` : "",
            item.score !== undefined ? `score=${Number(item.score).toFixed(2)}` : "",
            item.issue ? `issue=${item.issue}` : "",
            item.summary || "",
        ].filter(Boolean).join("; ");
    }
    if (event === "click_preflight_failed") {
        return item.error ? `error=${item.error}` : "click preflight unavailable";
    }
    if (event === "verification") {
        const verification = item.verification || {};
        return [
            item.action_id ? `action_id=${item.action_id}` : "",
            verification.status ? `status=${verification.status}` : "",
            verification.matched_expected_change !== undefined ? `matched=${Boolean(verification.matched_expected_change)}` : "",
            verification.misclick !== undefined ? `misclick=${Boolean(verification.misclick)}` : "",
            verification.risk ? `risk=${verification.risk}` : "",
            verification.summary ? `summary=${verification.summary}` : "",
        ].filter(Boolean).join("; ");
    }
    if (event === "verification_policy") {
        return [item.action_id ? `action_id=${item.action_id}` : "", item.status ? `status=${item.status}` : "", item.reason || ""].filter(Boolean).join("; ");
    }
    if (event === "failure") {
        const failure = item.failure || {};
        return [
            item.action_id ? `action_id=${item.action_id}` : "",
            failure.failure_type || failure.category ? `failure_type=${failure.failure_type || failure.category}` : "",
            failure.severity ? `severity=${failure.severity}` : "",
            failure.reason ? `reason=${failure.reason}` : "",
        ].filter(Boolean).join("; ");
    }
    if (event === "final") {
        return [item.status ? `status=${item.status}` : "", item.summary || ""].filter(Boolean).join("; ");
    }
    if (item.screenshot) {
        return [
            item.screenshot.title ? `title=${item.screenshot.title}` : "",
            item.screenshot.url ? `url=${item.screenshot.url}` : "",
            item.screenshot.image_base64_length ? `image=${item.screenshot.image_base64_length} chars` : "",
            item.screenshot.image_omitted_reason || "",
        ].filter(Boolean).join("; ");
    }
    return item.trace || "";
}

function buildVisualArtifactsHtml(job) {
    const artifacts = Array.isArray(job?.artifacts) ? job.artifacts : [];
    const visualArtifacts = artifacts.filter((item) => item && typeof item === "object");
    if (!visualArtifacts.length) return "";

    const overview = visualTraceOverview(visualArtifacts);
    const rows = visualArtifacts.slice(-24).map((item) => {
        const screenshot = item.screenshot || {};
        const failure = item.failure || item.last_failure || {};
        const tone = visualTraceTone(item);
        const badges = visualTraceBadges(item).map((badge) => {
            const badgeTone = badge === failure.failure_type || badge === failure.category || badge === failure.severity ? ` ${tone}` : "";
            return `<span class="job-trace-badge${badgeTone}">${escapeHtml(badge)}</span>`;
        }).join("");
        const image = screenshot.image_base64
            ? `<img class="job-artifact-image" alt="browser screenshot" src="data:${escapeHtml(screenshot.mime_type || "image/png")};base64,${screenshot.image_base64}">`
            : "";
        const diagnostics = Array.isArray(screenshot.diagnostics) && screenshot.diagnostics.length
            ? `<div class="job-artifact-diagnostics">${escapeHtml(screenshot.diagnostics.slice(0, 4).join("\n"))}</div>`
            : "";
        const hint = failure.diagnostic_hint ? `<div class="job-trace-hint">${escapeHtml(failure.diagnostic_hint)}</div>` : "";
        return `
            <div class="job-trace-item ${escapeHtml(tone)}">
                <div class="job-trace-marker"></div>
                <div class="job-trace-body">
                    <div class="job-artifact-head">
                        <span>${escapeHtml(visualTraceTitle(item))}</span>
                        <span>${escapeHtml(item.event || "artifact")}</span>
                    </div>
                    <div class="job-trace-badges">${badges}</div>
                    <div class="job-artifact-summary">${escapeHtml(summarizeVisualArtifact(item) || "-")}</div>
                    ${hint}
                    ${image}
                    ${diagnostics}
                </div>
            </div>
        `;
    }).join("");

    return `
        <div class="job-detail-block">
            <div class="visual-trace-header">
                <div class="job-detail-label">Visual Trace</div>
                <div class="visual-trace-overview">${overview.map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</div>
            </div>
            <div class="job-trace-timeline">${rows}</div>
        </div>
    `;
}

function buildJobResultHtml(job) {
    const payload = getJobResultPayload(job);
    const blocks = [];
    if (payload) {
        const backendLabel = buildJobBackendLabel(payload);
        if (backendLabel) {
            blocks.push(`<div class="job-detail-row"><span class="job-detail-label">Backend</span><span>${escapeHtml(backendLabel)}</span></div>`);
        }
        if (payload.matched_keyword) {
            blocks.push(`<div class="job-detail-row"><span class="job-detail-label">Keyword</span><span>${escapeHtml(payload.matched_keyword)}</span></div>`);
        }
        const extracts = Array.isArray(payload.extracts) ? payload.extracts : [];
        extracts.slice(0, 4).forEach((item) => {
            const name = String(item?.name || "extract").trim() || "extract";
            const text = String(item?.text || "").trim();
            if (!text) return;
            blocks.push(`<div class="job-detail-block"><div class="job-detail-label">${escapeHtml(name)}</div><div class="job-detail-text">${escapeHtml(text)}</div></div>`);
        });
        if (payload.text) {
            blocks.push(`<div class="job-detail-block"><div class="job-detail-label">Snapshot</div><div class="job-detail-text">${escapeHtml(String(payload.text).slice(0, 800))}</div></div>`);
        }
    } else if (job?.result) {
        blocks.push(`<div class="job-detail-block"><div class="job-detail-label">Result</div><div class="job-detail-text">${escapeHtml(job.result)}</div></div>`);
    }
    if (job?.error) {
        blocks.push(`<div class="job-detail-block job-detail-error"><div class="job-detail-label">Error</div><div class="job-detail-text">${escapeHtml(job.error)}</div></div>`);
    }
    const visualArtifacts = buildVisualArtifactsHtml(job);
    if (visualArtifacts) {
        blocks.push(visualArtifacts);
    }
    return blocks.join("");
}

function appendInteractivePanel(wrapper, meta) {
    if (!wrapper || !meta) return;

    if (meta.awaitingConfirmation) {
        const panel = document.createElement("div");
        panel.className = "interactive-panel";
        panel.dataset.confirmationPanel = "true";
        panel.innerHTML = `
            <div class="interactive-title">等待你的确认</div>
            <div class="interactive-copy">${escapeHtml(meta.awaitingConfirmation.question || "")}</div>
            ${meta.awaitingConfirmation.context ? `<div class="interactive-sub">${escapeHtml(meta.awaitingConfirmation.context)}</div>` : ""}
            <textarea class="interactive-input" rows="2" placeholder="可选：补充说明"></textarea>
            <div class="interactive-actions">
                <button class="toolbar-btn" type="button" data-action="approve">继续执行</button>
                <button class="toolbar-btn subtle" type="button" data-action="reject">取消执行</button>
            </div>
        `;
        wrapper.appendChild(panel);
        const inputEl = panel.querySelector(".interactive-input");
        panel.querySelector('[data-action="approve"]').addEventListener("click", async () => {
            await submitConfirmation(meta.awaitingConfirmation, true, inputEl.value.trim(), wrapper);
        });
        panel.querySelector('[data-action="reject"]').addEventListener("click", async () => {
            await submitConfirmation(meta.awaitingConfirmation, false, inputEl.value.trim(), wrapper);
        });
    }

    if (meta.pendingJob) {
        const panel = document.createElement("div");
        panel.className = "interactive-panel job-panel";
        panel.innerHTML = `
            <div class="interactive-title">云端长任务</div>
            <div class="interactive-copy">${escapeHtml(meta.pendingJob.title || "")}</div>
            <div class="interactive-sub">任务编号：${escapeHtml(meta.pendingJob.id || "")}</div>
            <div class="interactive-sub job-status">状态：${escapeHtml(meta.pendingJob.status || "running")}</div>
            <div class="job-result"></div>
            <div class="interactive-log"></div>
            <div class="interactive-actions">
                <button class="toolbar-btn subtle" type="button" data-action="cancel-job">请求取消</button>
            </div>
        `;
        wrapper.appendChild(panel);
        const statusEl = panel.querySelector(".job-status");
        const resultEl = panel.querySelector(".job-result");
        const logEl = panel.querySelector(".interactive-log");
        let currentJob = {
            ...meta.pendingJob,
            progress: Array.isArray(meta.pendingJob.progress) ? [...meta.pendingJob.progress] : [],
            artifacts: Array.isArray(meta.pendingJob.artifacts) ? [...meta.pendingJob.artifacts] : [],
        };
        const renderJob = (job) => {
            if (!job) return;
            currentJob = {
                ...currentJob,
                ...job,
                progress: Array.isArray(job.progress) ? job.progress : currentJob.progress,
                artifacts: Array.isArray(job.artifacts) ? job.artifacts : currentJob.artifacts,
            };
            statusEl.textContent = `状态：${currentJob.status}`;
            logEl.innerHTML = (currentJob.progress || [])
                .slice(-24)
                .map((line) => `<div class="loading-progress-item">${escapeHtml(line)}</div>`)
                .join("");
            logEl.scrollTop = logEl.scrollHeight;
            resultEl.innerHTML = buildJobResultHtml(currentJob);
        };
        const appendJobProgress = (message) => {
            const text = String(message || "").trim();
            if (!text) return;
            currentJob.progress = [...(currentJob.progress || []), text];
            renderJob(currentJob);
        };
        const appendJobArtifact = (artifact) => {
            if (!artifact || typeof artifact !== "object") return;
            currentJob.artifacts = [...(currentJob.artifacts || []), artifact];
            renderJob(currentJob);
        };
        renderJob(currentJob);
        panel.querySelector('[data-action="cancel-job"]').addEventListener("click", async () => {
            await fetch(`/api/jobs/${encodeURIComponent(meta.pendingJob.id)}/cancel`, {
                method: "POST",
                credentials: "same-origin",
            });
            statusEl.textContent = "状态：已请求取消";
        });
        streamCloudJob(meta.pendingJob.id, {
            onSnapshot: renderJob,
            onProgress: (data) => appendJobProgress(data.message),
            onArtifact: (data) => appendJobArtifact(data.artifact),
            onFinished: renderJob,
        }, () => panel.isConnected);
        pollCloudJob(meta.pendingJob.id, (job) => {
            statusEl.textContent = `状态：${job.status}`;
            logEl.innerHTML = (job.progress || [])
                .slice(-24)
                .map((line) => `<div class="loading-progress-item">${escapeHtml(line)}</div>`)
                .join("");
            resultEl.innerHTML = buildJobResultHtml(job);
        }, () => panel.isConnected);
    }
}

function persistState() {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(chatHistory));
    localStorage.setItem(LOCAL_EXEC_KEY, localExecutionMode ? "1" : "0");
}

function loadState() {
    localExecutionMode = localStorage.getItem(LOCAL_EXEC_KEY) === "1";

    const savedTheme = localStorage.getItem(THEME_KEY);
    if (savedTheme === "dark") {
        document.documentElement.setAttribute("data-theme", "dark");
    }

    try {
        const raw = localStorage.getItem(STORAGE_KEY);
        chatHistory = raw ? JSON.parse(raw) : [];
    } catch {
        chatHistory = [];
    }

    chatHistory.forEach((item) => addMessage(item.role, item.content, { save: false, time: item.time, meta: item.meta || null }));
    updateEmptyState();
    updateLocalExecButton();
}

function setConfirmationPanelState(panel, state, message = "") {
    if (!panel) return;
    panel.dataset.state = state;
    const buttons = Array.from(panel.querySelectorAll("button"));
    const input = panel.querySelector(".interactive-input");
    let stateEl = panel.querySelector(".confirmation-state");
    if (!stateEl) {
        stateEl = document.createElement("div");
        stateEl.className = "confirmation-state";
        const actions = panel.querySelector(".interactive-actions");
        if (actions) {
            actions.before(stateEl);
        } else {
            panel.appendChild(stateEl);
        }
    }

    if (state === "submitting") {
        buttons.forEach((button) => {
            button.disabled = true;
            button.dataset.originalText = button.dataset.originalText || button.textContent;
            if (button.dataset.action === panel.dataset.decision) {
                button.textContent = panel.dataset.decision === "approve" ? "继续中..." : "拒绝中...";
            }
        });
        if (input) input.disabled = true;
        stateEl.textContent = message || "正在提交确认...";
        return;
    }

    if (state === "approved" || state === "rejected") {
        buttons.forEach((button) => {
            button.disabled = true;
            button.classList.add("is-confirmed");
            if (button.dataset.action === "approve") {
                button.textContent = state === "approved" ? "已确认" : (button.dataset.originalText || button.textContent);
            }
            if (button.dataset.action === "reject") {
                button.textContent = state === "rejected" ? "已拒绝" : (button.dataset.originalText || button.textContent);
            }
        });
        if (input) input.disabled = true;
        stateEl.textContent = message || (state === "approved" ? "已确认，正在继续执行..." : "已拒绝，正在停止...");
        return;
    }

    if (state === "error") {
        buttons.forEach((button) => {
            button.disabled = false;
            if (button.dataset.originalText) button.textContent = button.dataset.originalText;
        });
        if (input) input.disabled = false;
        stateEl.textContent = message || "确认提交失败，请重试。";
    }
}

async function submitConfirmation(confirmation, approved, responseText, wrapper) {
    if (!confirmation?.id || !confirmation?.thread_id) return;
    const confirmationPanel = wrapper?.querySelector(".interactive-panel[data-confirmation-panel='true']");
    if (confirmationPanel) confirmationPanel.dataset.decision = approved ? "approve" : "reject";
    setConfirmationPanelState(confirmationPanel, "submitting", approved ? "已确认，正在继续任务..." : "已拒绝，正在更新任务...");
    const loadingEl = addLoadingMessage(approved ? "正在继续执行" : "正在处理中");
    appendLoadingProgress(loadingEl, approved ? "已确认继续，正在恢复任务。" : "已拒绝继续，正在整理结果。");
    setStatus(approved ? "正在继续执行..." : "正在处理中...", "ok");

    try {
        const payload = {
            thread_id: confirmation.thread_id,
            confirmation_id: confirmation.id,
            approved,
            response_text: responseText || "",
            include_tool_trace: true,
            local_execution: localExecutionMode,
        };
        const response = await fetch("/api/chat/confirm/stream", {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        if (!response.ok) {
            const text = await response.text();
            throw new Error(text || `HTTP ${response.status}`);
        }

        let finalPayload = null;
        await consumeSseResponse(response, async (event, data) => {
            if (event === "planner_started") {
                updateLoadingMessage(loadingEl, "正在规划");
                appendLoadingProgress(loadingEl, "已重新进入调度。");
            } else if (event === "planner_finished") {
                appendLoadingProgress(loadingEl, `规划完成：${plannerLabel(data.planner_decision?.route)}`);
            } else if (event === "research_started") {
                updateLoadingMessage(loadingEl, "正在思考");
                appendLoadingProgress(loadingEl, `联网思考：${data.query || ""}`);
            } else if (event === "tool_started") {
                appendLoadingProgress(loadingEl, `开始执行：${data.title || toolLabel(data.tool)}`);
            } else if (event === "tool_progress") {
                appendLoadingProgress(loadingEl, `${toolLabel(data.tool)}：${data.chunk}`);
            } else if (event === "tool_finished") {
                appendLoadingProgress(loadingEl, `${data.title || toolLabel(data.tool)}已完成`);
            } else if (event === "tool_error") {
                appendLoadingProgress(loadingEl, `${data.title || toolLabel(data.tool)}失败：${data.summary || ""}`);
            } else if (event === "job_created") {
                appendLoadingProgress(loadingEl, `已创建本地长任务：${data.job?.title || ""}`);
            } else if (event === "awaiting_confirmation") {
                appendLoadingProgress(loadingEl, "又出现新的确认点，等待用户继续。");
            } else if (event === "final_answer") {
                finalPayload = data;
            } else if (event === "run_failed") {
                throw new Error(data.detail || "处理失败");
            }
        });

        loadingEl.remove();
        if (wrapper) {
            setConfirmationPanelState(confirmationPanel, approved ? "approved" : "rejected");
        }
        if (finalPayload) {
            addMessage("assistant", finalPayload.reply || "已处理完成。", {
                meta: buildMessageMetaFromResponse(finalPayload),
            });
            setStatus("已完成确认后的继续执行");
        }
    } catch (error) {
        loadingEl.remove();
        setConfirmationPanelState(confirmationPanel, "error", `失败：${error.message}`);
        addMessage("assistant", `确认后执行失败：${error.message}`);
        setStatus(`确认后执行失败：${error.message}`, "error");
    }
}

async function sendMessage() {
    const userMessage = promptEl.value.trim();
    if (!userMessage || isSending) {
        return;
    }

    if (userMessage.length > 2000) {
        setStatus("消息过长，最多 2000 字符", "error");
        return;
    }

    isSending = true;
    sendBtnEl.disabled = true;
    promptEl.value = "";
    resizeInput();
    addMessage("user", userMessage);

    const loadingEl = addLoadingMessage("正在规划");
    const stopLoadingStages = startLoadingStageRotation(loadingEl);

    try {
        const health = localExecutionMode ? await waitForBrowserWorkerReady() : await refreshHealth();
        if (!health.one_api_token_configured) {
            throw new Error("服务端未配置 ONE_API_TOKEN，请先更新 .env 并重启服务。");
        }
        validateLocalExecutionHealth(health);

        const payload = {
            messages: [{ role: "user", content: userMessage }],
            include_tool_trace: true,
            local_execution: localExecutionMode,
        };

        const threadId = getThreadId();
        if (threadId) {
            payload.thread_id = threadId;
        }

        const response = await fetch("/api/chat/stream", {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });

        if (!response.ok) {
            const text = await response.text();
            throw new Error(text || `HTTP ${response.status}`);
        }

        let finalPayload = null;
        await consumeSseResponse(response, async (event, data) => {
            if (event === "planner_started") {
                updateLoadingMessage(loadingEl, "正在规划");
                appendLoadingProgress(loadingEl, "开始分析任务和结构化信号。");
                setStatus("正在规划...", "ok");
            } else if (event === "planner_finished") {
                appendLoadingProgress(
                    loadingEl,
                    `规划完成：${plannerLabel(data.planner_decision?.route)} / ${complexityLabel(data.planner_decision?.complexity || "standard")}`,
                );
            } else if (event === "research_started") {
                updateLoadingMessage(loadingEl, "正在思考");
                appendLoadingProgress(loadingEl, `开始联网思考：${data.query || ""}`);
                setStatus("正在思考...", "ok");
            } else if (event === "research_finished") {
                appendLoadingProgress(loadingEl, `联网思考完成：${data.summary || ""}`);
            } else if (event === "agent_started") {
                updateLoadingMessage(loadingEl, "正在整理回答");
                appendLoadingProgress(
                    loadingEl,
                    `执行模型：${data.model || "-"} / 复杂度：${complexityLabel(data.complexity || "standard")}`,
                );
                setStatus("正在整理回答...", "ok");
            } else if (event === "agent_tool_plan") {
                appendLoadingProgress(loadingEl, `计划调用工具：${(data.tools || []).map(toolLabel).join("、") || "无"}`);
            } else if (event === "tool_started") {
                appendLoadingProgress(loadingEl, `开始执行：${data.title || toolLabel(data.tool)}`);
            } else if (event === "tool_progress") {
                appendLoadingProgress(loadingEl, `${toolLabel(data.tool)}：${data.chunk || ""}`);
            } else if (event === "tool_finished") {
                appendLoadingProgress(loadingEl, `${data.title || toolLabel(data.tool)}已完成`);
            } else if (event === "tool_error") {
                appendLoadingProgress(loadingEl, `${data.title || toolLabel(data.tool)}失败：${data.summary || ""}`);
            } else if (event === "job_created") {
                appendLoadingProgress(loadingEl, `已创建本地长任务：${data.job?.title || ""}`);
            } else if (event === "awaiting_confirmation") {
                updateLoadingMessage(loadingEl, "等待你的确认");
                appendLoadingProgress(loadingEl, data.confirmation?.question || "出现新的确认点");
                setStatus("等待你的确认", "warn");
            } else if (event === "final_answer") {
                finalPayload = data;
            } else if (event === "run_failed") {
                throw new Error(data.detail || "处理失败");
            }
        });

        stopLoadingStages();
        loadingEl.remove();
        if (!finalPayload) {
            throw new Error("未收到最终结果。");
        }
        if (finalPayload.thread_id) {
            setThreadId(finalPayload.thread_id);
        }
        addMessage("assistant", finalPayload.reply || "无回复", {
            meta: buildMessageMetaFromResponse(finalPayload),
        });

        const traceCount = finalPayload.tool_trace?.length || 0;
        const complexity = finalPayload.planner_decision?.complexity
            ? `复杂度：${complexityLabel(finalPayload.planner_decision.complexity)}`
            : "";
        const statusSuffix = [
            finalPayload.planner_decision?.route ? `路由：${plannerLabel(finalPayload.planner_decision.route)}` : "",
            complexity,
            traceCount ? `工具：${traceCount}` : "",
            runtimeStatusSuffix(appHealth),
        ]
            .filter(Boolean)
            .join(" / ");
        setStatus(statusSuffix ? `回答完成 / ${statusSuffix}` : "回答完成");
    } catch (error) {
        stopLoadingStages();
        loadingEl.remove();
        const message = `请求失败：${error.message}`;
        addMessage("assistant", message);
        setStatus(message, "error");
    } finally {
        isSending = false;
        sendBtnEl.disabled = false;
        promptEl.focus();
    }
}

function clearConversation() {
    if (!window.confirm("这会开始一个新的会话，同时清空当前会话")) {
        return;
    }

    chatHistory = [];
    localStorage.removeItem(STORAGE_KEY);
    clearThreadId();

    [...messagesEl.children].forEach((node) => {
        if (node !== emptyStateEl) {
            node.remove();
        }
    });

    updateEmptyState();
    setStatus("新的会话已就绪");
}

sendBtnEl.addEventListener("click", sendMessage);
clearBtnEl.addEventListener("click", clearConversation);
localExecBtnEl.addEventListener("click", () => {
    localExecutionMode = !localExecutionMode;
    updateLocalExecButton();
    persistState();
    const suffix = runtimeStatusSuffix(appHealth);
    const base = localExecutionMode ? "本地执行模式已开启" : "本地执行模式已关闭";
    setStatus(suffix ? `${base} / ${suffix}` : base, localExecutionMode && suffix.includes("未连接") ? "warn" : "ok");
});
logoutBtnEl.addEventListener("click", async () => {
    try {
        await fetch("/api/auth/logout", {
            method: "POST",
            credentials: "same-origin",
        });
    } finally {
        window.location.replace("/login.html");
    }
});

promptEl.addEventListener("input", resizeInput);
promptEl.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        sendMessage();
    }
});

themeBtnEl.addEventListener("click", () => {
    const dark = document.documentElement.getAttribute("data-theme") === "dark";
    if (dark) {
        document.documentElement.removeAttribute("data-theme");
        localStorage.setItem(THEME_KEY, "light");
    } else {
        document.documentElement.setAttribute("data-theme", "dark");
        localStorage.setItem(THEME_KEY, "dark");
    }
});

setViewportHeight();
window.addEventListener("resize", setViewportHeight);
window.addEventListener("orientationchange", setViewportHeight);

(async () => {
    try {
        await ensureAuthenticated();
        await refreshHealth();
        loadState();
        resizeInput();
        const suffix = runtimeStatusSuffix(appHealth);
        if (suffix) {
            setStatus(suffix, suffix.includes("未连接") || suffix.includes("离线") ? "warn" : "ok");
        }
        promptEl.focus();
    } catch (error) {
        console.error(error);
    }
})();
