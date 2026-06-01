const messagesEl = document.getElementById("messages");
        const emptyStateEl = document.getElementById("emptyState");
        const promptEl = document.getElementById("prompt");
        const sendBtnEl = document.getElementById("sendBtn");
        const clearBtnEl = document.getElementById("clearBtn");
        const themeBtnEl = document.getElementById("themeBtn");
        const modelSelectEl = document.getElementById("modelSelect");
        const modelPickerEl = document.getElementById("modelPicker");
        const modelTriggerEl = document.getElementById("modelTrigger");
        const modelMenuEl = document.getElementById("modelMenu");
        const modelCurrentNameEl = document.getElementById("modelCurrentName");
        const modelCurrentDescEl = document.getElementById("modelCurrentDesc");
        const statusTextEl = document.getElementById("statusText");
        const statusLineEl = document.getElementById("statusLine");
        const charCountEl = document.getElementById("charCount");

        const STORAGE_KEY = "sunwin_messages_v4";
        const MODEL_KEY = "sunwin_model_v4";
        const THEME_KEY = "sunwin_theme_v4";
        const THREAD_KEY = "sunwin_thread_v4";

        let chatHistory = [];
        let isSending = false;

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

        function plannerLabel(route) {
            if (route === "research") return "调研";
            if (route === "agent") return "执行";
            return "未知";
        }

        function getModelMeta(value) {
            const map = {
                "gpt-4o-mini": {
                    name: "GPT-4o-mini",
                    desc: "更快更省，适合日常问答与轻量工作流",
                },
                "gpt-4o": {
                    name: "GPT-4o",
                    desc: "更均衡，适合复杂一些的对话与工具协作",
                },
                "gpt-5.4": {
                    name: "GPT-5.4",
                    desc: "更强的综合能力，适合高质量回答和较复杂流程",
                },
                "gpt-5.5": {
                    name: "GPT-5.5",
                    desc: "更偏高性能场景，适合最重的推理和执行任务",
                },
            };
            return map[value] || { name: value, desc: "当前选择的模型" };
        }

        function syncModelPicker() {
            const meta = getModelMeta(modelSelectEl.value);
            modelCurrentNameEl.textContent = meta.name;
            modelCurrentDescEl.textContent = meta.desc;

            document.querySelectorAll(".model-option").forEach((button) => {
                button.classList.toggle("active", button.dataset.value === modelSelectEl.value);
            });
        }

        function closeModelPicker() {
            modelPickerEl.classList.remove("open");
            modelTriggerEl.setAttribute("aria-expanded", "false");
        }

        function setupModelPicker() {
            modelTriggerEl.addEventListener("click", (event) => {
                event.stopPropagation();
                const isOpen = modelPickerEl.classList.toggle("open");
                modelTriggerEl.setAttribute("aria-expanded", String(isOpen));
            });

            document.querySelectorAll(".model-option").forEach((button) => {
                button.addEventListener("click", (event) => {
                    event.stopPropagation();
                    modelSelectEl.value = button.dataset.value;
                    syncModelPicker();
                    persistState();
                    closeModelPicker();
                });
            });

            document.addEventListener("click", () => {
                closeModelPicker();
            });

            modelMenuEl.addEventListener("click", (event) => {
                event.stopPropagation();
            });
        }

        function answerModeLabel(mode) {
            if (mode === "grounded_summary") return "基于证据总结";
            if (mode === "tool_agent") return "工具执行";
            return "-";
        }

        function toolLabel(name) {
            const map = {
                web_search: "联网搜索",
                fetch_webpage: "抓取网页",
                send_email: "发送邮件",
            };
            return map[name] || name || "工具";
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

                const actions = planner.post_actions?.length ? planner.post_actions.join(", ") : "无";
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
                            <strong>是否抓取网页</strong>
                            <div>${planner.needs_fetch ? "是" : "否"}</div>
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
                    <div class="kv">
                        <strong>搜索词</strong>
                        <pre class="trace-pre">${escapeHtml(planner.search_query || "-")}</pre>
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
                    block.innerHTML = `
                        <strong>步骤 ${index + 1} · ${escapeHtml(toolLabel(item.tool))}</strong>
                        <pre class="trace-pre">${escapeHtml(item.content || "-")}</pre>
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

        function addLoadingMessage() {
            const wrapper = document.createElement("div");
            wrapper.className = "message assistant";
            wrapper.dataset.loading = "true";

            const bubble = document.createElement("div");
            bubble.className = "bubble";
            bubble.innerHTML = `
                <p class="loading-bubble">
                    <span>思考中</span>
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

        function persistState() {
            localStorage.setItem(STORAGE_KEY, JSON.stringify(chatHistory));
            localStorage.setItem(MODEL_KEY, modelSelectEl.value);
        }

        function loadState() {
            const savedModel = localStorage.getItem(MODEL_KEY);
            if (savedModel) {
                modelSelectEl.value = savedModel;
            }

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
            setStatus("正在请求模型...");

            promptEl.value = "";
            resizeInput();
            addMessage("user", userMessage);

            const loadingEl = addLoadingMessage();

            try {
                const payload = {
                    model: modelSelectEl.value,
                    messages: [{ role: "user", content: userMessage }],
                    include_tool_trace: true,
                };

                const threadId = getThreadId();
                if (threadId) {
                    payload.thread_id = threadId;
                }

                const response = await fetch("/api/chat", {
                    method: "POST",
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

                loadingEl.remove();

                const meta = {
                    plannerDecision: data.planner_decision || null,
                    toolTrace: data.tool_trace || [],
                };

                addMessage("assistant", data.reply || "无回复", { meta });

                const route = data.planner_decision?.route;
                const traceCount = data.tool_trace?.length || 0;
                const statusSuffix = [route ? `路由：${plannerLabel(route)}` : "", traceCount ? `工具：${traceCount}` : ""]
                    .filter(Boolean)
                    .join(" · ");
                setStatus(statusSuffix ? `回答完成 · ${statusSuffix}` : "回答完成");
            } catch (error) {
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
            if (!window.confirm("确定要清空当前会话吗？这会开始一个新的会话线程。")) {
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
            setStatus("已清空会话，新的会话线程已就绪");
        }

        sendBtnEl.addEventListener("click", sendMessage);
        clearBtnEl.addEventListener("click", clearConversation);

        promptEl.addEventListener("input", resizeInput);
        promptEl.addEventListener("keydown", (event) => {
            if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                sendMessage();
            }
        });

        modelSelectEl.addEventListener("change", () => {
            persistState();
            syncModelPicker();
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

        loadState();
        setupModelPicker();
        syncModelPicker();
        resizeInput();
        promptEl.focus();
