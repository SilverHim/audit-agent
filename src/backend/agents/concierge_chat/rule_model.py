"""mock 模式的对话模型: 不调真实模型, 按问题关键词调工具, 再用工具结果拼出确定的回答.

完整的 Agent 循环 (调模型 → 调工具 → 再调模型) 在 mock 模式下照样走一遍, 离线演示与测试都能跑.
覆盖的问题: 进度, 现在在做什么 (当前阶段 n/m 与正在运行的程序, 需求 R325), 待处理事项 (含稍后处理), 重要性;
要求代为确认, 批准的给出引导; 其余回固定话术.
不带工具调用时 (压缩摘要) 列出被压缩各轮的问题, 作为确定的摘要, 语言按压缩提示词的要求.
"""

import json
import re
from collections.abc import AsyncIterator, Sequence
from typing import Any, Literal

from backend.contracts.conversation import AssistantTurn, ChatEvent, ChatMessage, Lang, TextDelta, ToolCall, ToolSpec
from backend.contracts.ports import ChatModel

MODEL_NAME = "mock"
CHUNK = 12  # 流式输出每段的字数
LANGUAGE = re.compile(r"^Language: (zh|en)$", re.MULTILINE)  # 系统提示词里的回答语言行 (项目的工作语言, 需求 R342)
AUDITOR = "[Auditor] "  # 压缩提示词里审计师问题行的前缀
SUMMARY_LANGUAGE = re.compile(r"^Write the new summary in Simplified Chinese\b", re.MULTILINE)  # 压缩提示词要中文摘要

Intent = Literal["todo", "approve", "materiality", "activity", "progress", "other"]
# 按顺序匹配, 先中先得: "有什么需要我确认" 是问待办, 不是要求代为确认, 所以 todo 在 approve 之前
INTENTS: tuple[tuple[Intent, re.Pattern[str]], ...] = (
    ("todo", re.compile(r"待办|待处理|需要我|要我做|稍后处理|待复核|to ?do|pending|open items|deferred|need me", re.I)),
    ("approve", re.compile(r"确认|批准|同意|签发|签字|通过吧|approve|confirm|sign[ -]?off", re.I)),
    ("materiality", re.compile(r"重要性|materiality", re.I)),
    ("activity", re.compile(r"在做什么|在干什么|在跑什么|正在执行|doing|working on|running", re.I)),
    ("progress", re.compile(r"哪一步|进度|阶段|到哪|完成了|progress|stage|status|where", re.I)),
)
TOOL_FOR: dict[Intent, str] = {
    "todo": "list_open_items",
    "materiality": "get_overview",
    "activity": "get_overview",
    "progress": "get_overview",
}

PHASES: dict[str, dict[Lang, str]] = {
    "P0_Acceptance": {"zh": "承接", "en": "acceptance"},
    "P1_Planning": {"zh": "计划", "en": "planning"},
    "P2_Executing": {"zh": "执行", "en": "fieldwork"},
    "P3_Completion": {"zh": "完成", "en": "completion"},
    "P4_Reporting": {"zh": "报告", "en": "reporting"},
    "Archived": {"zh": "已签字归档", "en": "signed and archived"},
    "Declined": {"zh": "已拒绝承接", "en": "declined"},
}
GATES: dict[str, dict[Lang, str]] = {
    "ACC-01": {"zh": "承接决定", "en": "the acceptance decision"},
    "PLN-01/03": {"zh": "风险与重要性决定", "en": "the risk and materiality decision"},
    "PLN-15": {"zh": "总体审计计划审批", "en": "the approval of the overall audit plan"},
    "FIN-01": {"zh": "意见类型决定", "en": "the opinion decision"},
    "RPT-01": {"zh": "报告签发决定", "en": "the report sign-off decision"},
}


def classify(question: str) -> Intent:
    return next((intent for intent, pattern in INTENTS if pattern.search(question)), "other")


def answer_language(messages: Sequence[ChatMessage]) -> Lang:
    """回答语言: 系统提示词里的 "Language:" 一行 (取项目的工作语言, 不看提问带的界面语言)."""
    system = next((m.content for m in messages if m.role == "system"), "")
    match = LANGUAGE.search(system)
    return "en" if match and match.group(1) == "en" else "zh"


def tool_results(messages: Sequence[ChatMessage]) -> list[dict[str, Any]]:
    """最后一条 user 消息之后的工具结果 (JSON); 解析不了的跳过."""
    last_user = max((i for i, m in enumerate(messages) if m.role == "user"), default=-1)
    results = []
    for message in messages[last_user + 1 :]:
        if message.role == "tool":
            try:
                data = json.loads(message.content)
            except ValueError:
                continue
            if isinstance(data, dict):
                results.append(data)
    return results


def progress(data: dict[str, Any], lang: Lang) -> str:
    phase = PHASES.get(data.get("phase", ""), {"zh": data.get("phase", ""), "en": data.get("phase", "")})[lang]
    done, total, pending = data["procedures_completed"], data["procedures_total"], data["pending_review_tasks"]
    gate = data.get("current_gate")
    if lang == "zh":
        text = f"项目目前处于{phase}阶段，{done}/{total} 个审计程序已完成，{pending} 项复核任务待处理。"
        if gate:
            text += f"流程停在{GATES.get(gate, {'zh': gate})['zh']}，等待审计人员作出决定。"
        if data.get("running"):
            text += "后台正在执行审计程序。"
        return text
    text = (
        f"The engagement is in the {phase} phase: {done} of {total} procedures are complete"
        f" and {pending} review tasks are open."
    )
    if gate:
        text += f" It is waiting for {GATES.get(gate, {'en': gate})['en']}."
    if data.get("running"):
        text += " Procedures are running in the background."
    return text


def activity(data: dict[str, Any], lang: Lang) -> str:
    """现在在做什么: 当前阶段, 本阶段已结清的程序数/总数 (已完成与未执行 (无数据) 都算结清, 不说 "已完成"),
    正在运行的程序 (编号与名称)."""
    progress = data["progress"]
    phase = PHASES.get(progress["phase"], {"zh": progress["phase"], "en": progress["phase"]})[lang]
    done, total = progress["done"], progress["total"]
    names = [f"{p['id']} {p['name']}" for p in progress["running"]]
    if lang == "zh":
        text = f"项目目前处于{phase}阶段，本阶段 {done}/{total} 个审计程序已结清。"
        return text + (f"正在执行：{'、'.join(names)}。" if names else "现在没有正在执行的程序。")
    text = f"The engagement is in the {phase} phase: {done} of {total} procedures in this phase are settled."
    return text + (f" Running now: {', '.join(names)}." if names else " No procedure is running right now.")


def materiality(data: dict[str, Any], lang: Lang) -> str:
    overall = data.get("materiality")
    if overall is None:
        if lang == "zh":
            return "重要性水平还没有确定，要在风险与重要性决定通过后才有。"
        return "Materiality has not been set yet; it is set when the risk and materiality decision is approved."
    performance, trivial = data.get("performance_materiality"), data.get("clearly_trivial_threshold")
    if lang == "zh":
        return f"整体重要性为 {overall}，执行重要性为 {performance}，明显微小错报临界值为 {trivial}。"
    return (
        f"Overall materiality is {overall}, performance materiality is {performance},"
        f" and the clearly trivial threshold is {trivial}."
    )


def open_items(data: dict[str, Any], lang: Lang) -> str:
    items = data.get("items", [])
    if not items:
        return "目前没有待处理事项。" if lang == "zh" else "There are no pending items."
    lines = [f"待处理事项共 {len(items)} 项：" if lang == "zh" else f"{len(items)} items are pending:"]
    for n, item in enumerate(items, start=1):
        later = ("（已标记稍后处理）" if lang == "zh" else " (marked for later)") if item.get("deferred") else ""
        if item["type"] == "gate_decision":
            name = GATES.get(item["gate"], {"zh": item["gate"], "en": item["gate"]})[lang]
            lines.append(f"{n}. {name}{later}")
        elif item["type"] == "data_request":
            used = "、".join(item.get("used_for") or []) if lang == "zh" else ", ".join(item.get("used_for") or [])
            if lang == "zh":
                lines.append(f"{n}. 补交{item['title']}" + (f"（{used} 要用）" if used else "") + later)
            else:
                what = item.get("table_kind") or item.get("document_type") or item["title"]
                lines.append(f"{n}. Provide {what}" + (f" (needed by {used})" if used else "") + later)
        else:
            subject = item.get("subject_id") or item["target_id"]
            what = "复核" if lang == "zh" else "Review"
            procedure = f"{item['procedure_id']} {item['procedure_name']}"
            lines.append(f"{n}. {what} {procedure} · {subject}，{item['target_id']}{later}")
    return "\n".join(lines)


APPROVE: dict[Lang, str] = {
    "zh": "复核事项可在工作台生成固定范围的操作方案, 核对结论后授权助理批量执行。"
    "阶段审批与报告签发通过对应决定卡片办理。",
    "en": "Review items can be prepared as a fixed-scope action proposal and executed after authorisation. "
    "Stage approvals and report sign-off use their decision cards.",
}

OTHER: dict[Lang, str] = {
    "zh": "这个问题我暂时回答不了，请换一种问法，例如问项目进度、待处理事项或重要性水平。",
    "en": "I can't answer that one yet. Try asking about progress, pending items, or materiality.",
}


def compose(intent: Intent, results: list[dict[str, Any]], lang: Lang) -> str:
    data = results[-1] if results else None
    if intent == "approve":
        return APPROVE[lang]
    if data is None or "error" in data:
        return OTHER[lang]
    if intent == "progress":
        return progress(data, lang)
    if intent == "activity":
        return activity(data, lang)
    if intent == "materiality":
        return materiality(data, lang)
    if intent == "todo":
        return open_items(data, lang)
    return OTHER[lang]


def summarize(messages: Sequence[ChatMessage]) -> str:
    """压缩摘要: 列出材料里审计师的各个问题, 用压缩提示词要求的语言 (回答语言, 需求 R342)."""
    material = next((m.content for m in reversed(messages) if m.role == "user"), "")
    questions = [line[len(AUDITOR) :].strip() for line in material.splitlines() if line.startswith(AUDITOR)]
    if SUMMARY_LANGUAGE.search(material):
        return "审计师先后问过：" + "；".join(questions) + "。"
    return "The auditor asked: " + "; ".join(questions) + "."


class RuleChatModel:
    """mock 对话模型 (见模块说明)."""

    async def stream_chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        *,
        tool_choice: Literal["auto", "none"] = "auto",
        max_tokens: int,
    ) -> AsyncIterator[ChatEvent]:
        if not tools:
            text = summarize(messages)
        else:
            answer_lang = answer_language(messages)
            question = next((m.content for m in reversed(messages) if m.role == "user"), "")
            intent = classify(question)
            results = tool_results(messages)
            tool = TOOL_FOR.get(intent)
            if tool is not None and not results and tool_choice == "auto":
                call = ToolCall(id="call_0", name=tool, arguments="{}")
                yield AssistantTurn(text="", tool_calls=[call], finish_reason="tool_calls", model=MODEL_NAME)
                return
            text = compose(intent, results, answer_lang)
        for start in range(0, len(text), CHUNK):
            yield TextDelta(text=text[start : start + CHUNK])
        yield AssistantTurn(text=text, finish_reason="stop", model=MODEL_NAME)


class RuleChatSource:
    """mock 模式的模型来源: 总是规则模型."""

    async def current(self) -> ChatModel:
        return RuleChatModel()
