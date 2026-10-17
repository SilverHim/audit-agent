"""审计助理对话 (A10) 的契约: 发给模型的消息与工具, 模型的流式事件, 对话接口的请求与返回.

规则见 开发接口文档.md 第 6.1 节与第 10 节. 对话模型走 OpenAI 兼容 Chat Completions 的工具调用格式:
assistant 消息带 tool_calls, 其后每个调用一条 role=tool 的消息, 按 tool_call_id 配对.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator

from backend.contracts.models import EvidencePointer, Model

Lang = Literal["zh", "en"]  # 回答语言
CitationKind = Literal["judgment", "procedure", "evidence", "trail"]  # 引用指向的对象种类
QUESTION_MAX = 2000  # 问题最多字数


class ToolCall(Model):
    """模型要求调用的一个工具."""

    id: str  # 调用 ID, 工具结果按它配对
    name: str  # 工具名
    arguments: str  # 参数, 模型给出的 JSON 原文 (可能不合法, 由工具箱校验)


class ChatMessage(Model):
    """发给对话模型的一条消息."""

    role: Literal["system", "user", "assistant", "tool"]  # 发言方
    content: str = ""  # 正文; assistant 只调工具时可为空
    tool_calls: list[ToolCall] = []  # assistant 要求调用的工具
    tool_call_id: str | None = None  # role=tool 时: 回应的是哪个调用


class ToolSpec(Model):
    """提供给模型的一个工具: 名称, 用途说明与参数的 JSON schema."""

    name: str  # 工具名
    description: str  # 用途说明, 模型据此决定调不调
    parameters: dict[str, Any]  # 参数的 JSON schema


class TextDelta(Model):
    """流式输出中的一段回答文本."""

    text: str  # 新增的文本


class AssistantTurn(Model):
    """模型一次调用的完整结果: 流式输出结束时给出."""

    text: str  # 回答文本, 等于本次全部 TextDelta 的拼接
    tool_calls: list[ToolCall] = []  # 要求调用的工具, 没有为空
    finish_reason: str | None = None  # 结束原因: stop, tool_calls, length 等
    model: str  # 实际使用的模型名
    input_tokens: int = 0  # 输入 token 数, 服务商不给时为 0
    output_tokens: int = 0  # 输出 token 数, 服务商不给时为 0


ChatEvent = TextDelta | AssistantTurn  # 对话模型流式产出的事件: 若干 TextDelta, 最后一个 AssistantTurn


class ConciergeCitation(Model):
    """回答引用的来源: 回答文本里提到的编号, 且在本轮工具结果或对话上下文中出现过."""

    kind: CitationKind  # 引用种类
    id: str  # 被引用对象的编号: 判断 ID, 程序编号, 证据行或文件 ID, 留痕 ID
    label: str  # 显示名
    pointer: EvidencePointer | None = None  # kind 为 evidence 时的证据指针, 前端据此打开原件


class ConciergeQuestion(Model):
    """审计师的一次提问."""

    question: str = Field(max_length=QUESTION_MAX)  # 问题, 不超过 2000 字, 不能全是空白
    lang: Lang  # 回答语言

    @field_validator("question")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("问题不能为空")
        return value


class ConciergeAnswer(Model):
    """一次提问的完整回答."""

    answer: str  # 回答全文
    citations: list[ConciergeCitation]  # 引用, 按在回答中首次出现的顺序
    trail_id: str  # 这轮对话的 conversation 留痕 ID


class ConversationTurn(Model):
    """历史中的一轮问答, 取自 conversation 留痕."""

    question: str  # 问题
    answer: str  # 回答; 中途断开时为已生成的部分
    lang: Lang  # 回答语言
    citations: list[ConciergeCitation]  # 引用
    trail_id: str  # 留痕 ID
    created_at: datetime | None  # 留痕写入时间
    aborted: bool = False  # 审计师中途断开, 回答不完整
