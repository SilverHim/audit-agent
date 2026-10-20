"""审计助理对话测试的替身: 脚本对话模型, 固定的状态查询, 带 ConciergeChat 的接口客户端."""

import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from backend.agents.base import AgentDeps
from backend.agents.concierge_chat.service import ChatLimits, ConciergeChat
from backend.contracts.conversation import AssistantTurn, ChatEvent, ChatMessage, Lang, TextDelta, ToolCall, ToolSpec
from backend.contracts.models import Engagement, Phase
from backend.contracts.ports import ChatModel
from backend.contracts.views import EngagementOverview, GateView, ProcedureSummary
from backend.core.security import create_access_token
from backend.main import create_app
from tests.fakes import ENGAGEMENT, NUMBAT_AUDITOR, FakeRegistry, FakeTrail, make_deps

MODEL = "scripted-model"


@dataclass(frozen=True)
class ToolStep:
    """脚本里的一步: 先说一段话 (可为空), 再要求调用这些工具 (名称, 参数)."""

    calls: list[tuple[str, dict[str, Any] | str]]
    text: str = ""


Step = str | ToolStep | Exception  # 回答文本, 工具调用, 或在开始输出前抛出的异常


@dataclass
class Request:
    """模型收到的一次请求."""

    messages: list[ChatMessage]
    tools: list[ToolSpec]
    tool_choice: str


@dataclass
class ScriptedChatModel:
    """按脚本逐次作答的对话模型. 每次调用取下一步; 脚本用完时回 "（脚本已用完）". 文本按 chunk 字切段流式输出."""

    steps: list[Step]
    chunk: int = 5
    requests: list[Request] = field(default_factory=list)

    async def stream_chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        *,
        tool_choice: Literal["auto", "none"] = "auto",
        max_tokens: int,
    ) -> AsyncIterator[ChatEvent]:
        self.requests.append(Request(list(messages), list(tools), tool_choice))
        step: Step = self.steps.pop(0) if self.steps else "（脚本已用完）"
        if isinstance(step, Exception):
            raise step
        text, calls = (step, []) if isinstance(step, str) else (step.text, step.calls)
        for start in range(0, len(text), self.chunk):
            yield TextDelta(text=text[start : start + self.chunk])
        tool_calls = [
            ToolCall(id=f"call_{i}", name=name, arguments=args if isinstance(args, str) else json.dumps(args))
            for i, (name, args) in enumerate(calls)
        ]
        yield AssistantTurn(
            text=text,
            tool_calls=tool_calls,
            finish_reason="tool_calls" if tool_calls else "stop",
            model=MODEL,
            input_tokens=100,
            output_tokens=10,
        )


@dataclass
class StaticSource:
    """总是给同一个模型的模型来源."""

    model: ChatModel

    async def current(self) -> ChatModel:
        return self.model


def procedure_summary(pid: str, status: str = "completed", pending: int = 0) -> ProcedureSummary:
    return ProcedureSummary.model_validate(
        {
            "id": pid,
            "name": f"{pid} 程序",
            "phase": "P2_Executing",
            "owner": "A6",
            "engine": "rule",
            "autonomy_level": "L3",
            "status": status,
            "pending_tasks": pending,
        }
    )


def make_overview(engagement: Engagement = ENGAGEMENT, **changes: Any) -> EngagementOverview:
    """项目总览: 缺省为执行阶段, 3 条程序完成 2 条, 1 项待复核, 重要性已定, 不在阶段决定处."""
    base = EngagementOverview(
        engagement=engagement.model_copy(update={"phase": Phase.P2_Executing}),
        client_name="Numbat Electronics Pty Ltd",
        running=False,
        run_error=None,
        current_gate=None,
        awaiting_review=True,
        awaiting_data=False,
        missing_tables=[],
        procedures=[
            procedure_summary("REV-08"),
            procedure_summary("CSH-05"),
            procedure_summary("REV-02", status="awaiting_human", pending=1),
        ],
        pending_tasks=1,
        materiality_cents=12_000_000,
        performance_materiality_cents=9_000_000,
        clearly_trivial_cents=600_000,
        opinion_route=None,
    )
    return base.model_copy(update=changes)


@dataclass
class FixedState:
    """固定的项目位置与闸门材料 (代替 GraphStateQuery)."""

    view: EngagementOverview = field(default_factory=make_overview)
    gate_view: GateView | None = None

    async def overview(self, eid: str) -> EngagementOverview:
        if eid != self.view.engagement.id:
            raise LookupError(f"Engagement {eid} not found")
        return self.view

    async def gate(self, eid: str) -> GateView | None:
        return self.gate_view


def project_state(language: Lang, **changes: Any) -> FixedState:
    """项目的工作语言为 language 的固定总览 (审计助理的回答语言随它, 需求 R342); changes 同 make_overview."""
    return FixedState(view=make_overview(ENGAGEMENT.model_copy(update={"working_language": language}), **changes))


def chat_deps(trail: FakeTrail | None = None, **kwargs: Any) -> AgentDeps:
    """对话测试的依赖: make_deps 的假存储; 留痕缺省为新的空留痕."""
    return make_deps(FakeRegistry({}, {}), trail if trail is not None else FakeTrail(), **kwargs)


def make_chat(
    model: ChatModel,
    deps: AgentDeps | None = None,
    state: FixedState | None = None,
    limits: ChatLimits | None = None,
) -> ConciergeChat:
    return ConciergeChat(
        deps if deps is not None else chat_deps(),
        state if state is not None else FixedState(),
        StaticSource(model),
        limits,
    )


@asynccontextmanager
async def chat_client(concierge: ConciergeChat, user_id: str = NUMBAT_AUDITOR.id) -> AsyncIterator[httpx.AsyncClient]:
    """在测试的事件循环里直接调 ASGI 应用 (不触发 lifespan), 只挂对话接口用到的 deps 与 concierge."""
    app = create_app()
    app.state.deps = concierge.deps
    app.state.concierge = concierge
    headers = {"Authorization": f"Bearer {create_access_token(user_id)}"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=headers) as client:
        yield client


def parse_sse(body: str) -> list[tuple[str, Any]]:
    """把 SSE 正文解析成 (事件名, 数据) 列表; 跳过注释行 (保活)."""
    events = []
    for block in body.split("\n\n"):
        name, data = "message", []
        for line in block.split("\n"):
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
        if data:
            events.append((name, json.loads("\n".join(data))))
    return events
