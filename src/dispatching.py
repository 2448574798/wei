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


class PlannerDecision(BaseModel):
    route: Literal["research", "agent"] = Field(default="agent")
    reason: str = Field(default="")
    complexity: Literal["simple", "standard", "advanced"] = Field(default="standard")
    search_query: str = Field(default="")
    answer_mode: Literal["grounded_summary", "tool_agent"] = Field(default="tool_agent")
    post_actions: list[Literal["send_email"]] = Field(default_factory=list)


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
    return bool(re.search(r"今天|今日|明天|后天|昨天|昨日|\btoday\b|\btomorrow\b|\byesterday\b", text, re.IGNORECASE))


def has_explicit_date(text: str) -> bool:
    return bool(re.search(r"\d{4}[-/.年]\d{1,2}([-/\.月]\d{1,2})?", text))


def extract_email_targets(user_text: str) -> list[str]:
    return EMAIL_PATTERN.findall(user_text or "")


def is_time_sensitive(user_text: str) -> bool:
    return bool(TIME_SENSITIVE_PATTERN.search(user_text or ""))


def likely_needs_research(user_text: str) -> bool:
    text = user_text or ""
    return is_time_sensitive(text) or bool(SEARCH_ACTION_PATTERN.search(text))


def is_local_execution_intent(user_text: str) -> bool:
    text = (user_text or "").lower()
    keywords = [
        "本地",
        "本机",
        "电脑",
        "桌面",
        "打开",
        "启动",
        "运行",
        "执行代码",
        "解释器",
        "浏览器",
        "记事本",
        "计算器",
        "文件",
        "open local",
        "on my computer",
        "on my machine",
        "local computer",
        "local machine",
        "open browser",
        "open notepad",
        "open calculator",
        "run code",
        "execute code",
    ]
    return any(keyword in text for keyword in keywords)


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


def classify_complexity_heuristic(user_text: str, local_execution: bool = False) -> Literal["simple", "standard", "advanced"]:
    text = user_text or ""
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

    if any(marker.lower() in text.lower() for marker in advanced_markers) or len(text) > 180:
        return "advanced"
    if any(marker.lower() in text.lower() for marker in simple_markers) or len(text) < 40:
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


def localize_planner_reason(reason: str, route: str, complexity: str, post_actions: list[str] | None = None) -> str:
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

    data["reason"] = localize_planner_reason(data.get("reason", ""), data.get("route", "agent"), data["complexity"], post_actions)
    return data


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


def build_dispatcher_prompt(user_text: str, today: str, local_execution: bool = False) -> list:
    return [
        SystemMessage(
            content=(
                "你是任务调度员，只返回结构化决策。\n"
                f"默认使用 {DISPATCHER_MODEL} 的调度能力，判断请求是否需要联网，以及任务复杂度。\n"
                "规则如下：\n"
                "1. 如果请求依赖当前、最新、会变化的信息，route 选 research。\n"
                "2. 如果请求涉及本地电脑、本地程序、本地文件、浏览器操作或执行代码，route 选 agent，complexity 选 advanced。\n"
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
                f"本地执行意图：{'是' if is_local_execution_intent(user_text) else '否'}\n"
                f"时效性提示：{'是' if is_time_sensitive(user_text) else '否'}"
            )
        ),
    ]
