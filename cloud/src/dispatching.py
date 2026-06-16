import re
from datetime import datetime, timedelta
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from src.runtime_config import (
    DISPATCHER_MODEL,
    EXECUTION_MODEL_ADVANCED,
    EXECUTION_MODEL_SIMPLE,
    EXECUTION_MODEL_STANDARD,
    LOCAL_EXECUTION_MODEL,
)


TIME_SENSITIVE_PATTERN = re.compile(
    r"(今天|今日|昨天|昨日|明天|后天|现在|当前|目前|最新|最近|刚刚|实时|近况|行情|价格|汇率|股价|新闻|天气|"
    r"版本|更新|发布|文档|政策|法规|公告|比赛|赛程|票房|销量|today|yesterday|tomorrow|now|current|latest|"
    r"recent|price|weather|news|version|release)",
    re.IGNORECASE,
)
SEARCH_ACTION_PATTERN = re.compile(
    r"(搜索|查询|联网|网页|网站|查一下|搜一下|fetch|search|browse|look up)",
    re.IGNORECASE,
)
EMAIL_ACTION_PATTERN = re.compile(
    r"(发送到|发送给|发到|发给|发邮件|邮箱|邮件|email|mail)",
    re.IGNORECASE,
)
EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
LOCAL_EXECUTION_PATTERN = re.compile(
    r"(本地|本机|电脑|桌面|打开|启动|运行|执行代码|解释器|浏览器|记事本|计算器|文件|"
    r"open local|on my computer|on my machine|local computer|local machine|"
    r"open browser|open notepad|open calculator|run code|execute code)",
    re.IGNORECASE,
)
FILE_OUTPUT_PATTERN = re.compile(
    r"(写入|保存|生成.*文件|导出|另存为|保存到|文档|txt|md|markdown|excel|csv|pdf|桌面)",
    re.IGNORECASE,
)
MULTI_STEP_PATTERN = re.compile(
    r"(然后|之后|并且|同时|先.*再|整理.*写入|搜索.*写入|总结.*发送|并发送|再发送)",
    re.IGNORECASE,
)


class PlannerDecision(BaseModel):
    route: Literal["research", "agent"] = Field(default="agent")
    reason: str = Field(default="")
    complexity: Literal["simple", "standard", "advanced"] = Field(default="standard")
    search_query: str = Field(default="")
    answer_mode: Literal["grounded_summary", "tool_agent"] = Field(default="tool_agent")
    post_actions: list[Literal["send_email"]] = Field(default_factory=list)


class DispatchSignals(BaseModel):
    needs_latest_info: bool = False
    needs_research: bool = False
    needs_local_execution: bool = False
    needs_file_output: bool = False
    needs_email: bool = False
    email_recipient_present: bool = False
    has_time_reference: bool = False
    is_multi_step: bool = False
    complexity_hint: Literal["simple", "standard", "advanced"] = "standard"


def format_cn_date(value: datetime) -> str:
    return f"{value.year}年{value.month}月{value.day}日"


def expand_relative_dates(text: str) -> str:
    if not text:
        return text

    now = datetime.now()
    replacements = [
        (r"今天|今日", format_cn_date(now)),
        (r"明天", format_cn_date(now + timedelta(days=1))),
        (r"后天", format_cn_date(now + timedelta(days=2))),
        (r"昨天|昨日", format_cn_date(now - timedelta(days=1))),
        (r"\btoday\b", now.strftime("%Y-%m-%d")),
        (r"\btomorrow\b", (now + timedelta(days=1)).strftime("%Y-%m-%d")),
        (r"\byesterday\b", (now - timedelta(days=1)).strftime("%Y-%m-%d")),
    ]

    expanded = text
    for pattern, replacement in replacements:
        expanded = re.sub(pattern, replacement, expanded, flags=re.IGNORECASE)
    return expanded


def has_relative_date(text: str) -> bool:
    return bool(
        re.search(r"今天|今日|明天|后天|昨天|昨日|\btoday\b|\btomorrow\b|\byesterday\b", text or "", re.IGNORECASE)
    )


def has_explicit_date(text: str) -> bool:
    return bool(re.search(r"\d{4}[-/.年]\d{1,2}([-/\.月]\d{1,2})?", text or ""))


def extract_email_targets(user_text: str) -> list[str]:
    return EMAIL_PATTERN.findall(user_text or "")


def is_time_sensitive(user_text: str) -> bool:
    return bool(TIME_SENSITIVE_PATTERN.search(user_text or ""))


def likely_needs_research(user_text: str) -> bool:
    text = user_text or ""
    return is_time_sensitive(text) or bool(SEARCH_ACTION_PATTERN.search(text))


def is_local_execution_intent(user_text: str) -> bool:
    return bool(LOCAL_EXECUTION_PATTERN.search(user_text or ""))


def should_prefer_local_execution(user_text: str, config=None) -> bool:
    configurable = (config or {}).get("configurable", {})
    return bool(configurable.get("local_execution")) or is_local_execution_intent(user_text)


def infer_post_actions(user_text: str) -> list[str]:
    emails = extract_email_targets(user_text)
    text = user_text or ""
    lowered = text.lower()
    terms = ["发送到", "发送给", "发到", "发给", "发邮件", "邮箱", "邮件", "email", "mail"]
    if emails and any(term in text or term in lowered for term in terms):
        return ["send_email"]
    return []


def classify_complexity_heuristic(
    user_text: str,
    local_execution: bool = False,
) -> Literal["simple", "standard", "advanced"]:
    text = user_text or ""
    lowered = text.lower()

    if local_execution or is_local_execution_intent(text):
        return "advanced"
    if likely_needs_research(text):
        return "standard"

    advanced_markers = [
        "分析",
        "设计",
        "重构",
        "架构",
        "复杂",
        "多步骤",
        "代码",
        "脚本",
        "自动化",
        "workflow",
        "refactor",
        "architecture",
        "multi-step",
    ]
    simple_markers = ["解释一下", "简要", "一句话", "翻译", "润色", "总结"]

    if any(marker.lower() in lowered for marker in advanced_markers) or len(text) > 180:
        return "advanced"
    if any(marker.lower() in lowered for marker in simple_markers) or len(text) < 40:
        return "simple"
    return "standard"


def build_default_search_query(user_text: str) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    if not user_text:
        return today

    normalized = EMAIL_PATTERN.sub("", user_text)
    normalized = EMAIL_ACTION_PATTERN.sub(" ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    normalized = expand_relative_dates(normalized or user_text)
    if has_explicit_date(normalized):
        return normalized
    return f"{normalized} {today}".strip()


def normalize_search_query(query: str, user_text: str) -> str:
    if has_relative_date(user_text):
        return build_default_search_query(user_text)
    cleaned = (query or "").strip()
    if not cleaned:
        return build_default_search_query(user_text)
    return expand_relative_dates(cleaned)


def collect_dispatch_signals(user_text: str, local_execution: bool = False) -> dict:
    text = user_text or ""
    recipients = extract_email_targets(text)
    needs_latest_info = is_time_sensitive(text)
    needs_local_execution = local_execution or is_local_execution_intent(text)
    needs_file_output = bool(FILE_OUTPUT_PATTERN.search(text))
    needs_email = bool(EMAIL_ACTION_PATTERN.search(text))
    has_time_reference = needs_latest_info or has_relative_date(text) or has_explicit_date(text)
    is_multi_step = bool(MULTI_STEP_PATTERN.search(text)) or sum(
        [
            1 if needs_latest_info else 0,
            1 if needs_local_execution else 0,
            1 if needs_file_output else 0,
            1 if needs_email else 0,
        ]
    ) >= 2
    needs_research = likely_needs_research(text) or (needs_latest_info and not needs_local_execution)

    complexity_hint = classify_complexity_heuristic(text, local_execution)
    if needs_local_execution and (needs_latest_info or needs_file_output or is_multi_step):
        complexity_hint = "advanced"
    elif needs_research and complexity_hint == "simple":
        complexity_hint = "standard"

    signals = DispatchSignals(
        needs_latest_info=needs_latest_info,
        needs_research=needs_research,
        needs_local_execution=needs_local_execution,
        needs_file_output=needs_file_output,
        needs_email=needs_email,
        email_recipient_present=bool(recipients),
        has_time_reference=has_time_reference,
        is_multi_step=is_multi_step,
        complexity_hint=complexity_hint,
    )
    return signals.model_dump()


def localize_planner_reason(
    reason: str,
    route: str,
    complexity: str,
    post_actions: list[str] | None = None,
) -> str:
    text = (reason or "").strip()
    post_actions = post_actions or []
    if re.search(r"[\u4e00-\u9fff]", text):
        return text

    if not text:
        if route == "research":
            return "请求涉及最新或会变化的信息，适合先思考再回答。"
        if post_actions:
            return "需要先整理结果，再执行后续动作。"
        if complexity == "advanced":
            return "请求复杂度较高，适合交给更强的执行模型处理。"
        if complexity == "simple":
            return "请求较简单，可直接进入轻量执行流程。"
        return "请求适合直接进入常规执行流程。"

    lowered = text.lower()
    if "time-sensitive" in lowered or "current" in lowered or "latest" in lowered or "recent" in lowered:
        return "请求涉及最新或会变化的信息，适合先思考再回答。"
    if "email" in lowered and post_actions:
        return "需要先整理结果，再执行后续动作。"
    if "simple" in lowered:
        return "请求较简单，可直接进入轻量执行流程。"
    if "advanced" in lowered or "complex" in lowered:
        return "请求复杂度较高，适合交给更强的执行模型处理。"
    if "local" in lowered or "computer" in lowered:
        return "请求涉及本地执行或电脑操作，优先进入执行流程。"
    if route == "research":
        return "已判断这次请求更适合先思考，再基于结果作答。"
    return "已判断这次请求可直接进入执行流程。"


def normalize_planner_decision(
    decision: PlannerDecision | dict,
    user_text: str,
    local_execution: bool = False,
) -> dict:
    data = decision.model_dump() if isinstance(decision, PlannerDecision) else dict(decision)
    prefer_local_execution = local_execution or is_local_execution_intent(user_text)

    if prefer_local_execution:
        data["route"] = "agent"
        data["answer_mode"] = "tool_agent"
        data["complexity"] = "advanced"
        data["search_query"] = ""
        data["reason"] = data.get("reason") or "请求涉及本地执行或电脑操作，优先进入执行流程。"

    if is_time_sensitive(user_text) and not prefer_local_execution:
        data["route"] = "research"
        data["answer_mode"] = "grounded_summary"
        data["reason"] = data.get("reason") or "请求涉及最新或会变化的信息，适合先思考再回答。"

    complexity = data.get("complexity") or classify_complexity_heuristic(user_text, local_execution)
    if complexity not in {"simple", "standard", "advanced"}:
        complexity = "standard"
    data["complexity"] = complexity

    if data.get("route") == "research":
        data["search_query"] = normalize_search_query(data.get("search_query", ""), user_text)

    post_actions = infer_post_actions(user_text)
    data["post_actions"] = post_actions
    if post_actions and data.get("route") != "research" and likely_needs_research(user_text) and not prefer_local_execution:
        data["route"] = "research"
        data["answer_mode"] = "grounded_summary"
        data["search_query"] = normalize_search_query(data.get("search_query", ""), user_text)
        if not data.get("reason"):
            data["reason"] = "需要先完成思考，再继续执行后续动作。"

    data["reason"] = localize_planner_reason(
        data.get("reason", ""),
        data.get("route", "agent"),
        data["complexity"],
        post_actions,
    )
    return data


def validate_dispatch_decision(
    decision: PlannerDecision | dict,
    signals: dict,
    user_text: str,
    local_execution: bool = False,
) -> dict:
    validated = normalize_planner_decision(decision, user_text, local_execution=local_execution)
    complexity_hint = signals.get("complexity_hint", "standard")

    if complexity_hint == "advanced" and validated.get("complexity") != "advanced":
        validated["complexity"] = "advanced"
    elif (
        complexity_hint == "simple"
        and validated.get("complexity") == "standard"
        and not signals.get("needs_research")
        and not signals.get("needs_local_execution")
        and not signals.get("needs_file_output")
        and not signals.get("is_multi_step")
        and not signals.get("needs_email")
    ):
        validated["complexity"] = "simple"

    if signals.get("needs_local_execution"):
        validated["route"] = "agent"
        validated["answer_mode"] = "tool_agent"
        validated["complexity"] = "advanced"
        validated["search_query"] = ""

    if signals.get("needs_research") and not signals.get("needs_local_execution"):
        validated["route"] = "research"
        validated["answer_mode"] = "grounded_summary"
        validated["search_query"] = normalize_search_query(validated.get("search_query", ""), user_text)

    if signals.get("needs_email") and signals.get("email_recipient_present"):
        if "send_email" not in validated.get("post_actions", []):
            validated["post_actions"] = ["send_email"]

    if signals.get("needs_file_output") and validated.get("complexity") == "simple":
        validated["complexity"] = "advanced" if signals.get("needs_local_execution") else "standard"

    if signals.get("is_multi_step") and validated.get("complexity") == "simple":
        validated["complexity"] = "standard"

    if signals.get("needs_latest_info") and signals.get("needs_local_execution"):
        validated["route"] = "agent"
        validated["answer_mode"] = "tool_agent"
        validated["complexity"] = "advanced"
        validated["search_query"] = ""
        validated["reason"] = "请求同时涉及最新信息和本地操作，适合由高级执行模型先决定是否联网，再继续本地执行。"

    validated["reason"] = localize_planner_reason(
        validated.get("reason", ""),
        validated.get("route", "agent"),
        validated.get("complexity", "standard"),
        validated.get("post_actions", []),
    )
    return validated


def build_heuristic_planner_decision(user_text: str, local_execution: bool = False) -> dict:
    route = "research" if likely_needs_research(user_text) and not local_execution else "agent"
    complexity = classify_complexity_heuristic(user_text, local_execution)
    return normalize_planner_decision(
        {
            "route": route,
            "reason": "已使用本地规则完成调度。",
            "complexity": complexity,
            "search_query": build_default_search_query(user_text) if route == "research" else "",
            "answer_mode": "grounded_summary" if route == "research" else "tool_agent",
            "post_actions": infer_post_actions(user_text),
        },
        user_text,
        local_execution=local_execution,
    )


def choose_execution_model(decision: dict | None, local_execution: bool = False) -> str:
    if local_execution:
        return LOCAL_EXECUTION_MODEL

    complexity = (decision or {}).get("complexity", "standard")
    if complexity == "simple":
        return EXECUTION_MODEL_SIMPLE
    if complexity == "advanced":
        return EXECUTION_MODEL_ADVANCED
    return EXECUTION_MODEL_STANDARD


def build_dispatcher_prompt(
    user_text: str,
    today: str,
    signals: dict,
    local_execution: bool = False,
) -> list:
    return [
        SystemMessage(
            content=(
                "你是任务调度器，只返回结构化决策。\n"
                f"默认使用 {DISPATCHER_MODEL} 的调度能力，根据用户请求和结构化信号选择一个主执行者。\n"
                "一次只能选择一个主执行者：route 只能是 research 或 agent，不能同时调度两个执行者，也不要设计成先 research 再 agent 的双执行者链路。\n"
                "请优先依据结构化信号做分类，而不是自由发挥。\n"
                "规则如下：\n"
                "1. complexity 只能是 simple、standard、advanced。\n"
                "2. simple 适合简短问答、改写、翻译、轻量整理；通常直接进入 agent，除非请求明确依赖最新或会变化的信息。\n"
                "3. standard 适合普通分析、信息整合、常规工具协助；如果核心诉求是获取最新信息、查行情、查版本、查新闻，则优先选择 research。\n"
                "4. advanced 适合复杂推理、多步任务、代码生成、本地执行、长链路任务；这类任务优先选择 agent。\n"
                "5. 只要请求涉及本地电脑、本地程序、本地文件、浏览器操作或执行代码，route 优先选择 agent，complexity 至少为 advanced。\n"
                "6. 如果你选择了 research，就表示本次主执行者是联网研究；只有在用户明确要求发送邮件且消息里已有收件邮箱时，post_actions 才能包含 send_email。\n"
                "7. 如果你选择了 agent，就不要再把任务拆成“先 research 再 agent”的方案；search_query 必须留空。\n"
                "8. 只有 route=research 时才填写 search_query，而且要简洁可用。\n"
                "9. reason 用中文，保持简短，说明为什么选择这个主执行者和复杂度。"
            )
        ),
        HumanMessage(
            content=(
                f"今天日期：{today}\n"
                f"用户请求：{user_text}\n"
                f"本地执行模式：{'开启' if local_execution else '关闭'}\n"
                f"结构化信号：{signals}"
            )
        ),
    ]


def _legacy_build_dispatcher_prompt(
    user_text: str,
    today: str,
    signals: dict,
    local_execution: bool = False,
) -> list:
    return [
        SystemMessage(
            content=(
                "你是任务调度员，只返回结构化决策。\n"
                f"默认使用 {DISPATCHER_MODEL} 的调度能力，判断请求是否需要联网，以及任务复杂度。\n"
                "你会同时看到用户原始请求和已提取的结构化信号。\n"
                "请优先依据结构化信号做分类，而不是自由发挥。\n"
                "规则如下：\n"
                "1. 如果请求依赖当前、最新、会变化的信息，route 优先考虑 research。\n"
                "2. 如果请求涉及本地电脑、本地程序、本地文件、浏览器操作或执行代码，route 优先考虑 agent，complexity 至少为 advanced。\n"
                "3. complexity 只允许 simple、standard、advanced。\n"
                "4. simple 适合简短问答、改写、翻译、轻量整理。\n"
                "5. standard 适合普通分析、常规工具协作、多信息整合。\n"
                "6. advanced 适合复杂推理、代码生成、本地执行、长链路任务。\n"
                "7. 只有当用户明确要求发送邮件且消息中已有收件邮箱时，post_actions 才能包含 send_email，否则必须为空数组。\n"
                "8. 如果 route 选 research，请给出简洁可用的 search_query。\n"
                "9. reason 用中文，保持简短。"
            )
        ),
        HumanMessage(
            content=(
                f"今天日期：{today}\n"
                f"用户请求：{user_text}\n"
                f"本地执行模式：{'开启' if local_execution else '关闭'}\n"
                f"结构化信号：{signals}"
            )
        ),
    ]
