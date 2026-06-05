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

function updateLocalExecButton() {
    localExecBtnEl.classList.toggle("active", localExecutionMode);
    localExecBtnEl.setAttribute("aria-pressed", String(localExecutionMode));
    localExecBtnEl.title = localExecutionMode ? "本地执行模式已开启" : "本地执行模式已关闭";
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
        <p class="loading-bubble">
            <span class="loading-label">${escapeHtml(label)}</span>
            <span class="dots">
                <span class="dot"></span>
                <span class="dot"></span>
                <span class="dot"></span>
            </span>
        </p>
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

function startLoadingStageRotation(wrapper) {
    const stages = [
        { label: "正在规划", status: "正在规划..." },
        { label: "正在思考", status: "正在思考..." },
        { label: "正在整理回答", status: "正在整理回答..." },
    ];

    let index = 0;
    updateLoadingMessage(wrapper, stages[0].label);
    setStatus(stages[0].status, "ok");

    const timer = window.setInterval(() => {
        index = Math.min(index + 1, stages.length - 1);
        updateLoadingMessage(wrapper, stages[index].label);
        setStatus(stages[index].status, "ok");
        if (index >= stages.length - 1) {
            window.clearInterval(timer);
        }
    }, 900);

    return () => window.clearInterval(timer);
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
        const payload = {
            messages: [{ role: "user", content: userMessage }],
            include_tool_trace: true,
            local_execution: localExecutionMode,
        };

        const threadId = getThreadId();
        if (threadId) {
            payload.thread_id = threadId;
        }

        const response = await fetch("/api/chat", {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });

        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            throw new Error(data.detail || data.error || `HTTP ${response.status}`);
        }

        if (data.thread_id) {
            setThreadId(data.thread_id);
        }

        const meta = {
            plannerDecision: data.planner_decision || null,
            toolTrace: data.tool_trace || [],
        };

        const route = data.planner_decision?.route;
        if (route === "research") {
            updateLoadingMessage(loadingEl, "正在思考");
            setStatus("正在思考...", "ok");
        } else {
            updateLoadingMessage(loadingEl, "正在整理回答");
            setStatus("正在整理回答...", "ok");
        }

        stopLoadingStages();
        loadingEl.remove();
        addMessage("assistant", data.reply || "无回复", { meta });

        const traceCount = data.tool_trace?.length || 0;
        const complexity = data.planner_decision?.complexity ? `复杂度：${complexityLabel(data.planner_decision.complexity)}` : "";
        const statusSuffix = [
            route ? `路由：${plannerLabel(route)}` : "",
            complexity,
            traceCount ? `工具：${traceCount}` : "",
            localExecutionMode ? "本地执行模式已生效" : "",
        ]
            .filter(Boolean)
            .join(" · ");
        setStatus(statusSuffix ? `回答完成 · ${statusSuffix}` : "回答完成");
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
    setStatus(localExecutionMode ? "本地执行模式已开启" : "本地执行模式已关闭");
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
        loadState();
        resizeInput();
        promptEl.focus();
    } catch (error) {
        console.error(error);
    }
})();
