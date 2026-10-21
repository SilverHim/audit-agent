"""审计助理的对话历史: 从审计留痕重建, 超预算时压缩 (参考 pi 与 MyAgent 的 compaction).

- 一个项目一条连续对话. 每轮问答一条 conversation 留痕, 压缩摘要一条 conversation_summary 留痕; 两者只追加.
  重建 = 最新一份摘要 + 它覆盖到的那一轮之后的各轮 (规格书: A10 的对话记忆重建自 audit_trail).
- 回放给模型的只有 "问 + 答", 不回放工具结果: 工具结果只在本轮内使用, 下一轮要用时重新查 (项目状态在变,
  旧结果会过期). 所以压缩总是按整轮切, 不会把工具调用与其结果拆开.
- 预算按 token 估算 (estimate_tokens): 汉字各算 1 个, 其余字符每 3 个算 1 个, 对中文偏保守.
- 压缩: 保留最近 keep 轮原文, 较早的轮连同上一份摘要交模型写成新摘要; 失败时只截掉较早的轮, 不写摘要 (下一轮再试).
"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from backend.agents.concierge_chat.citations import citation_pool
from backend.contracts.conversation import (
    AssistantTurn,
    ChatMessage,
    ConciergeCitation,
    ConversationTurn,
    Lang,
    ToolSpec,
)
from backend.contracts.models import TrailEntry, TrailKind
from backend.contracts.ports import AuditTrail, ChatModel
from backend.services.trail_pages import all_of_kind

CJK = re.compile(r"[　-〿㐀-䶿一-鿿＀-￯]")  # 汉字与中文标点
INTERRUPTED: dict[Lang, str] = {"zh": "（回答中断）", "en": "(answer interrupted)"}


@dataclass(frozen=True)
class Turn:
    """历史中的一轮问答."""

    trail_id: str  # conversation 留痕 ID
    question: str
    answer: str
    answer_lang: Lang  # 回答语言 (需求 R342)
    citations: list[ConciergeCitation]
    created_at: datetime | None
    aborted: bool

    def view(self) -> ConversationTurn:
        return ConversationTurn(
            question=self.question,
            answer=self.answer,
            answer_lang=self.answer_lang,
            citations=self.citations,
            trail_id=self.trail_id,
            created_at=self.created_at,
            aborted=self.aborted,
        )


@dataclass(frozen=True)
class Summary:
    """一份压缩摘要."""

    trail_id: str  # conversation_summary 留痕 ID
    text: str  # 摘要正文
    covers_through: str  # 覆盖到哪一轮 (该轮的 conversation 留痕 ID, 含)
    cite_pool: list[ConciergeCitation]  # 被压缩各轮的引用, 之后的回答提到这些编号仍算引用


@dataclass(frozen=True)
class History:
    """发给模型的历史: 摘要 (可能没有) 与其后的各轮."""

    summary: Summary | None
    turns: list[Turn]

    def pool(self) -> dict[str, ConciergeCitation]:
        """历史中可引用的编号."""
        groups: list[list[ConciergeCitation]] = [self.summary.cite_pool] if self.summary else []
        return citation_pool(*groups, *(t.citations for t in self.turns))


def citations_of(raw: Any) -> list[ConciergeCitation]:
    """留痕 payload 里的引用; 不合格式的项跳过 (留痕只追加, 旧格式不改)."""
    found = []
    for item in raw if isinstance(raw, list) else []:
        try:
            found.append(ConciergeCitation.model_validate(item))
        except ValidationError:
            continue
    return found


def recorded_language(payload: dict[str, Any]) -> Lang:
    """一轮问答的回答语言 (需求 R342): 取 answer_lang; 需求 R342 之前的留痕没有, 取当时提问带的 lang (那时回答跟它走);
    值不是 zh, en 的同没有, 往下一项取. 两项都没有为 en (同 Engagement.working_language 的缺省): 写入时总有其一,
    只有手写或损坏的留痕会走到这一步."""
    return next((value for value in (payload.get("answer_lang"), payload.get("lang")) if value in ("zh", "en")), "en")


def turn_of(entry: TrailEntry) -> Turn:
    """一条 conversation 留痕 → 一轮问答. 回答语言依次取 answer_lang, lang, en (recorded_language)."""
    payload = entry.payload
    return Turn(
        trail_id=entry.id or "",
        question=str(payload.get("question", entry.summary)),
        answer=str(payload.get("answer", "")),
        answer_lang=recorded_language(payload),
        citations=citations_of(payload.get("citations")),
        created_at=entry.created_at,
        aborted=payload.get("aborted") is True,
    )


async def all_turns(trail: AuditTrail, eid: str) -> list[Turn]:
    """项目的全部问答, 按写入顺序."""
    return [turn_of(e) for e in await all_of_kind(trail, eid, TrailKind.conversation)]


async def load_history(trail: AuditTrail, eid: str) -> History:
    """重建发给模型的历史: 最新一份摘要 (其覆盖到的那一轮须存在) 与其后的各轮; 没有摘要时为全部各轮."""
    turns = await all_turns(trail, eid)
    ids = [t.trail_id for t in turns]
    for entry in reversed(await all_of_kind(trail, eid, TrailKind.conversation_summary)):
        through = entry.payload.get("covers_through")
        if through in ids and isinstance(entry.payload.get("summary"), str):
            summary = Summary(
                trail_id=entry.id or "",
                text=entry.payload["summary"],
                covers_through=str(through),
                cite_pool=citations_of(entry.payload.get("cite_pool")),
            )
            return History(summary, turns[ids.index(str(through)) + 1 :])
    return History(None, turns)


def estimate_tokens(text: str) -> int:
    """粗估 token 数: 汉字与中文标点各 1 个, 其余字符每 3 个 1 个."""
    cjk = len(CJK.findall(text))
    return cjk + (len(text) - cjk + 2) // 3


def history_messages(turns: Sequence[Turn]) -> list[ChatMessage]:
    """历史各轮回放为 user, assistant 消息对; 中断且没有文字的回答写明中断."""
    messages = []
    for turn in turns:
        messages.append(ChatMessage(role="user", content=turn.question))
        messages.append(ChatMessage(role="assistant", content=turn.answer or INTERRUPTED[turn.answer_lang]))
    return messages


def context_tokens(system: str, turns: Sequence[Turn], question: str, tools: Sequence[ToolSpec]) -> int:
    """一轮开始时发给模型的内容的估算 token 数: 系统提示, 历史, 本轮问题, 工具说明."""
    text = system + question + "".join(t.question + t.answer for t in turns)
    return estimate_tokens(text) + estimate_tokens(json.dumps([t.model_dump() for t in tools], ensure_ascii=False))


def summary_material(turns: Sequence[Turn]) -> list[dict[str, Any]]:
    """压缩提示词的 turns 变量."""
    return [
        {
            "question": t.question,
            "answer": t.answer or INTERRUPTED[t.answer_lang],
            "citations": [c.id for c in t.citations],
        }
        for t in turns
    ]


async def collect(model: ChatModel, messages: Sequence[ChatMessage], max_tokens: int) -> AssistantTurn:
    """不带工具调一次模型, 取完整结果 (压缩摘要用, 不流式外发)."""
    result: AssistantTurn | None = None
    async for event in model.stream_chat(messages, [], max_tokens=max_tokens):
        if isinstance(event, AssistantTurn):
            result = event
    if result is None or not result.text.strip():
        raise ValueError("模型没有给出摘要")
    return result
