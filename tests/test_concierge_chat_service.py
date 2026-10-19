"""审计助理对话服务 (service.py): 工具循环, 历史与压缩, 流式事件, 断开, 失败处理, mock 规则模型."""

import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Any, Literal

import httpx
import pytest

from backend.adapters.chat_llm import ContextOverflow, SecretEcho
from backend.adapters.llm import LLMTransportError
from backend.agents.concierge_chat.memory import load_history
from backend.agents.concierge_chat.rule_model import RuleChatModel
from backend.agents.concierge_chat.service import ChatLimits, ConciergeChat, ConversationBusy, ConversationFailed
from backend.contracts.conversation import (
    AssistantTurn,
    ChatEvent,
    ChatMessage,
    ConciergeQuestion,
    TextDelta,
    ToolSpec,
)
from backend.contracts.models import AgentId, GateId, TrailEntry, TrailKind
from tests.chat_fakes import (
    FixedState,
    ScriptedChatModel,
    StaticSource,
    ToolStep,
    chat_deps,
    make_chat,
    make_overview,
    parse_sse,
)
from tests.fakes import ENGAGEMENT, FakeOutputStore, FakeTrail
from tests.test_concierge_chat_llm import KEY, chunk, sse
from tests.test_concierge_chat_llm import make as live_model
from tests.test_concierge_chat_tools import EXCEPTION, JUDGMENT, TASK, data_with_requests

EID = ENGAGEMENT.id
USER = "U-1"


def q(text: str, lang: Literal["zh", "en"] = "zh") -> ConciergeQuestion:
    return ConciergeQuestion(question=text, lang=lang)


def entries(trail: FakeTrail, kind: TrailKind) -> list[TrailEntry]:
    return [e for e in trail.entries if e.kind is kind]


def deps_with_judgment(trail: FakeTrail) -> Any:
    return chat_deps(trail, output_store=FakeOutputStore([JUDGMENT, TASK, EXCEPTION]))


async def test_tool_loop_answers_with_citations_and_trail() -> None:
    trail = FakeTrail()
    model = ScriptedChatModel(
        [
            ToolStep([("get_judgment", {"judgment_id": JUDGMENT.id})], text="我先查这条判断。"),
            f"判断 {JUDGMENT.id} 认为收入记早了：发运单 DN-2026-0612 显示 7 月 3 日才签收。"
            "反方论证是合同可能约定出厂即转移。",
        ]
    )
    chat = make_chat(model, deps_with_judgment(trail))
    answer = await chat.ask(EID, USER, q("SI-2026-0612 为什么判错报？"))

    assert answer.answer.startswith("我先查这条判断。\n\n判断 J-REV-02-1-SI-2026-0612")
    assert [c.id for c in answer.citations] == [JUDGMENT.id, "DN-2026-0612"]
    assert "SI-2026-0612" not in [c.id for c in answer.citations[:1]]  # 问题里的编号不算, 回答里的才算
    # 第二次调用收到工具结果, 结果里有推理链与反方论证
    tool_message = model.requests[1].messages[-1]
    assert tool_message.role == "tool" and "counter_argument" in tool_message.content

    [turn] = entries(trail, TrailKind.conversation)
    assert turn.actor == USER and turn.id == answer.trail_id
    assert turn.payload["answer"] == answer.answer
    assert turn.payload["tools"][0]["name"] == "get_judgment" and turn.payload["tools"][0]["ok"] is True
    assert "content" not in turn.payload["tools"][0]  # 只记轨迹, 不记结果原文
    calls = entries(trail, TrailKind.llm_call)
    assert [c.actor for c in calls] == [AgentId.A10, AgentId.A10]
    assert [m["role"] for m in calls[0].payload["messages"]] == ["system", "user"]
    assert [m["role"] for m in calls[1].payload["messages"]] == ["tool"]  # 每次只记新增的消息
    assert all(c.prompt_hash for c in calls)


async def test_step_cap_forces_a_final_answer_without_tools() -> None:
    step = ToolStep([("get_overview", {})])
    model = ScriptedChatModel([step, step, ToolStep([("get_overview", {})], text="根据已查到的信息作答。")])
    chat = make_chat(model, limits=ChatLimits(max_steps=2))
    answer = await chat.ask(EID, USER, q("现在到哪一步了？"))
    assert [r.tool_choice for r in model.requests] == ["auto", "auto", "none"]
    assert answer.answer == "根据已查到的信息作答。"


async def test_history_replays_question_answer_pairs_across_instances() -> None:
    trail = FakeTrail()
    first = ScriptedChatModel([ToolStep([("get_overview", {})]), "项目在执行阶段。"])
    await make_chat(first, chat_deps(trail)).ask(EID, USER, q("现在到哪一步了？"))

    second = ScriptedChatModel(["还有 1 项待复核。"])
    await make_chat(second, chat_deps(trail)).ask(EID, USER, q("那还剩什么？"))
    roles = [(m.role, m.content) for m in second.requests[0].messages[1:]]
    assert roles == [
        ("user", "现在到哪一步了？"),
        ("assistant", "项目在执行阶段。"),
        ("user", "那还剩什么？"),
    ]


async def seed_turns(trail: FakeTrail, n: int) -> None:
    for i in range(n):
        await trail.record(
            TrailEntry(
                engagement_id=EID,
                kind=TrailKind.conversation,
                actor=USER,
                summary=f"问题 {i}",
                payload={"question": f"问题 {i}", "answer": f"回答 {i}", "lang": "zh", "citations": []},
            )
        )


async def test_compaction_writes_summary_and_keeps_recent_turns() -> None:
    """另: 这一轮写的三种留痕 (摘要, 两次 llm_call, conversation) 都记程序 GEN-10 (R152 验收 7); 重建历史不看它."""
    trail = FakeTrail()
    await seed_turns(trail, 3)
    model = ScriptedChatModel(["审计师问过问题 0 与问题 1。", "好的。"])
    chat = make_chat(model, chat_deps(trail), limits=ChatLimits(context_budget=10, keep_turns=1))
    await chat.ask(EID, USER, q("继续"))

    summary_call, answer_call = model.requests
    assert summary_call.tools == []  # 摘要调用不带工具
    assert "[Auditor] 问题 0" in summary_call.messages[-1].content
    assert "问题 2" not in summary_call.messages[-1].content
    system = answer_call.messages[0].content
    assert "审计师问过问题 0 与问题 1。" in system
    assert [m.content for m in answer_call.messages[1:]] == ["问题 2", "回答 2", "继续"]

    [summary] = entries(trail, TrailKind.conversation_summary)
    assert summary.payload["covers_through"] == "TR-2"
    written = trail.entries[3:]  # 前 3 条是预置的问答
    assert [e.kind for e in written] == [
        TrailKind.llm_call,
        TrailKind.conversation_summary,
        TrailKind.llm_call,
        TrailKind.conversation,
    ]
    assert {e.procedure_id for e in written} == {"GEN-10"}
    rebuilt = await load_history(trail, EID)  # 新实例从留痕重建出同样的上下文
    assert rebuilt.summary is not None and rebuilt.summary.text == "审计师问过问题 0 与问题 1。"
    assert [t.question for t in rebuilt.turns] == ["问题 2", "继续"]
    assert [t.question for t in await chat.history(EID)] == ["问题 0", "问题 1", "问题 2", "继续"]


async def test_failed_summary_only_truncates_old_turns() -> None:
    trail = FakeTrail()
    await seed_turns(trail, 3)
    model = ScriptedChatModel([LLMTransportError("summary down"), "好的。"])
    chat = make_chat(model, chat_deps(trail), limits=ChatLimits(context_budget=10, keep_turns=1))
    answer = await chat.ask(EID, USER, q("继续"))
    assert answer.answer == "好的。"
    assert entries(trail, TrailKind.conversation_summary) == []
    assert [m.content for m in model.requests[1].messages[1:]] == ["问题 2", "回答 2", "继续"]


async def test_context_overflow_compacts_and_retries_once() -> None:
    trail = FakeTrail()
    await seed_turns(trail, 2)
    model = ScriptedChatModel([ContextOverflow("too long"), "早先问过问题 0。", "答案。"])
    answer = await make_chat(model, chat_deps(trail)).ask(EID, USER, q("继续"))
    assert answer.answer == "答案。"
    assert len(entries(trail, TrailKind.conversation_summary)) == 1
    assert [m.content for m in model.requests[2].messages[1:]] == ["问题 1", "回答 1", "继续"]


async def test_overflow_again_fails_with_code() -> None:
    model = ScriptedChatModel([ContextOverflow("too long"), ContextOverflow("still too long")])
    with pytest.raises(ConversationFailed) as error:
        await make_chat(model).ask(EID, USER, q("继续"))
    assert error.value.code == "LLM_CONTEXT_OVERFLOW"


async def collect_sse(chat: ConciergeChat, question: ConciergeQuestion) -> list[tuple[str, Any]]:
    turn = chat.begin(EID, USER, question)
    body = "".join([frame async for frame in turn.sse()])
    return parse_sse(body)


async def test_stream_events_in_order_and_answer_equals_deltas() -> None:
    trail = FakeTrail()
    model = ScriptedChatModel(
        [ToolStep([("get_judgment", {"judgment_id": JUDGMENT.id})], text="稍等。"), f"{JUDGMENT.id} 待复核。"]
    )
    events = await collect_sse(make_chat(model, deps_with_judgment(trail)), q("这条判断怎么样？"))
    names = [name for name, _ in events]
    assert names[-2:] == ["citations", "done"]
    assert "tool" in names and names.index("tool") < names.index("citations")
    assert {"label": "查看判断"} in [data for name, data in events if name == "tool"]
    deltas = "".join(data["text"] for name, data in events if name == "delta")
    [turn] = entries(trail, TrailKind.conversation)
    assert deltas == turn.payload["answer"] == f"稍等。\n\n{JUDGMENT.id} 待复核。"
    assert events[-1][1] == {"trail_id": turn.id}
    assert events[-2][1]["citations"][0]["id"] == JUDGMENT.id


async def test_secret_echo_sends_error_and_records_no_conversation() -> None:
    trail = FakeTrail()
    events = await collect_sse(make_chat(ScriptedChatModel([SecretEcho("echo")]), chat_deps(trail)), q("你好"))
    assert events == [("error", {"code": "LLM_SECRET_ECHO", "message": "模型输出中出现了密钥, 已中止, 本轮不记录"})]
    assert entries(trail, TrailKind.conversation) == []


async def test_unavailable_model_reports_error_code() -> None:
    model = ScriptedChatModel([LLMTransportError("HTTP 401 Unauthorized")])
    with pytest.raises(ConversationFailed) as error:
        await make_chat(model).ask(EID, USER, q("你好"))
    assert error.value.code == "LLM_UNAVAILABLE"


class StallingModel:
    """先给一段, 然后一直等 (模拟回答很慢); closed 记上游流是否已关闭."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.closed = False

    async def stream_chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        *,
        tool_choice: Literal["auto", "none"] = "auto",
        max_tokens: int,
    ) -> AsyncIterator[ChatEvent]:
        try:
            yield TextDelta(text="项目目前")
            await self.release.wait()
            yield TextDelta(text="处于执行阶段。")
            yield AssistantTurn(text="项目目前处于执行阶段。", model="slow")
        finally:
            self.closed = True


async def test_disconnect_records_partial_answer_and_frees_engagement() -> None:
    trail = FakeTrail()
    model = StallingModel()
    chat = make_chat(model, chat_deps(trail))
    turn = chat.begin(EID, USER, q("现在到哪一步了？"))
    stream = turn.sse()
    first = await anext(stream)
    assert '"text": "项目目前"' in first
    await stream.aclose()  # 审计师断开: 生成器被关闭
    with pytest.raises(asyncio.CancelledError):
        await turn.task  # type: ignore[misc]
    await chat.settle()  # aborted 留痕由独立任务写
    [entry] = entries(trail, TrailKind.conversation)
    assert entry.payload["aborted"] is True and entry.payload["answer"] == "项目目前"
    assert model.closed
    assert EID not in chat.busy
    assert [t.aborted for t in await chat.history(EID)] == [True]


async def test_one_question_at_a_time_per_engagement() -> None:
    model = StallingModel()
    chat = make_chat(model)
    turn = chat.begin(EID, USER, q("第一问"))
    with pytest.raises(ConversationBusy):
        chat.begin(EID, USER, q("第二问"))
    model.release.set()
    await turn.result()
    assert EID not in chat.busy


async def test_shutdown_cancels_running_turns() -> None:
    trail = FakeTrail()
    chat = make_chat(StallingModel(), chat_deps(trail))
    chat.begin(EID, USER, q("第一问"))
    await asyncio.sleep(0)
    await chat.shutdown()
    assert [e.payload.get("aborted") for e in entries(trail, TrailKind.conversation)] == [True]
    assert not chat.turns


# ── mock 模式的规则模型 ──


async def rule_ask(text: str, lang: Literal["zh", "en"] = "zh", state: FixedState | None = None) -> str:
    trail = FakeTrail()
    chat = make_chat(RuleChatModel(), deps_with_judgment(trail), state=state)
    answer = await chat.ask(EID, USER, q(text, lang))
    assert {e.model for e in entries(trail, TrailKind.llm_call)} == {"mock"}
    return answer.answer


async def test_rule_model_progress_matches_overview() -> None:
    answer = await rule_ask("现在到哪一步了？")
    assert answer == "项目目前处于执行阶段，2/3 个审计程序已完成，1 项复核任务待处理。"
    gate = FixedState(view=make_overview(current_gate=GateId.FIN_01))
    assert "流程停在意见类型决定" in await rule_ask("进度如何", state=gate)


async def test_rule_model_materiality_and_open_items() -> None:
    assert await rule_ask("重要性是多少？") == (
        "整体重要性为 AUD 120,000.00，执行重要性为 AUD 90,000.00，明显微小错报临界值为 AUD 6,000.00。"
    )
    items = await rule_ask("有什么需要我处理的？")
    assert items.startswith("需要你处理的事项共 1 项：")
    assert JUDGMENT.id in items


async def test_rule_model_lists_data_requests_before_reviews() -> None:
    trail = FakeTrail()
    deps = chat_deps(trail, output_store=FakeOutputStore([JUDGMENT, TASK, EXCEPTION]), data=data_with_requests())
    answer = (await make_chat(RuleChatModel(), deps).ask(EID, USER, q("有什么需要我处理的？"))).answer
    lines = answer.splitlines()
    assert lines[0] == "需要你处理的事项共 3 项："
    assert lines[1:3] == ["1. 补交总账分录（REV-08 要用）", "2. 补交审批日志（REV-08、CSH-05 要用）"]
    assert lines[3].startswith("3. 复核 REV-02")


async def test_rule_model_refuses_to_approve_and_speaks_english() -> None:
    assert "决定卡片" in await rule_ask("帮我批准吧")
    assert (await rule_ask("What stage are we at?", "en")).startswith("The engagement is in the fieldwork phase")
    assert "换一种问法" in await rule_ask("今天天气怎么样")


# ── 取消与收尾 (第 1 轮审核 X02-002, 004, 005, 006 的探针) ──


async def test_cancel_before_task_starts_releases_engagement_and_records() -> None:
    trail = FakeTrail()
    chat = make_chat(StallingModel(), chat_deps(trail))
    turn = chat.begin(EID, USER, q("第一问"))
    turn.client_gone()  # 后台任务还没开始执行
    await asyncio.gather(turn.task, return_exceptions=True)  # type: ignore[arg-type]
    await chat.settle()
    assert EID not in chat.busy
    [entry] = entries(trail, TrailKind.conversation)
    assert entry.payload["aborted"] is True and entry.payload["answer"] == ""


async def test_cancelled_ask_caller_releases_engagement() -> None:
    trail = FakeTrail()
    chat = make_chat(StallingModel(), chat_deps(trail))
    caller = asyncio.create_task(chat.ask(EID, USER, q("第一问")))
    await asyncio.sleep(0)
    caller.cancel()
    await asyncio.gather(caller, return_exceptions=True)
    await chat.settle()
    assert EID not in chat.busy
    assert [e.payload["aborted"] for e in entries(trail, TrailKind.conversation)] == [True]


async def test_old_turn_does_not_release_a_newer_turn() -> None:
    first, second = StallingModel(), StallingModel()
    chat = make_chat(first)
    old = chat.begin(EID, USER, q("第一问"))
    first.release.set()
    await old.result()  # 旧的一轮已释放项目, 完成回调还可能没跑
    chat.source = StaticSource(second)
    new = chat.begin(EID, USER, q("第二问"))
    await asyncio.sleep(0)  # 让旧一轮的完成回调跑完
    assert chat.busy.get(EID) is new
    second.release.set()
    await new.result()


class SlowTrail(FakeTrail):
    """写 conversation 留痕时停住, 直到放行: 用来在写入途中再取消一次."""

    def __init__(self) -> None:
        super().__init__()
        self.recording = asyncio.Event()
        self.release = asyncio.Event()

    async def record(self, entry: TrailEntry) -> str:
        if entry.kind is TrailKind.conversation:
            self.recording.set()
            await self.release.wait()
        return await super().record(entry)


async def test_shutdown_during_aborted_write_keeps_the_record() -> None:
    trail = SlowTrail()
    chat = make_chat(StallingModel(), chat_deps(trail))
    turn = chat.begin(EID, USER, q("第一问"))
    stream = turn.sse()
    await anext(stream)
    await stream.aclose()  # 断开: 第一次取消
    await asyncio.wait_for(trail.recording.wait(), 1)
    stop = asyncio.create_task(chat.shutdown())  # 写 aborted 留痕途中 shutdown
    await asyncio.sleep(0)
    trail.release.set()
    await asyncio.wait_for(stop, 1)
    assert [e.payload["aborted"] for e in entries(trail, TrailKind.conversation)] == [True]


class ToolThenStall:
    """第一次调用要求查判断, 之后交给 stall 模型 (先说一段再一直等)."""

    def __init__(self, stall: Any) -> None:
        self.first = ScriptedChatModel([ToolStep([("get_judgment", {"judgment_id": JUDGMENT.id})])])
        self.stall = stall

    async def stream_chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        *,
        tool_choice: Literal["auto", "none"] = "auto",
        max_tokens: int,
    ) -> AsyncIterator[ChatEvent]:
        model: Any = self.first if not self.first.requests else self.stall
        async for event in model.stream_chat(messages, tools, tool_choice=tool_choice, max_tokens=max_tokens):
            yield event


class CitingStall(StallingModel):
    """先说一段提到判断编号的话, 再一直等."""

    async def stream_chat(self, *args: Any, **kwargs: Any) -> AsyncIterator[ChatEvent]:
        yield TextDelta(text=f"判断 {JUDGMENT.id} 认为")
        await self.release.wait()
        yield AssistantTurn(text="", model="slow")


async def disconnect_after_first_delta(stall: Any) -> TrailEntry:
    trail = FakeTrail()
    chat = make_chat(ToolThenStall(stall), deps_with_judgment(trail))
    turn = chat.begin(EID, USER, q("解释判断"))
    stream = turn.sse()
    while "event: delta" not in await anext(stream):
        pass
    await stream.aclose()
    await asyncio.gather(turn.task, return_exceptions=True)  # type: ignore[arg-type]
    await chat.settle()
    [entry] = entries(trail, TrailKind.conversation)
    assert (entry.payload["aborted"], entry.procedure_id) == (True, "GEN-10")
    return entry


async def test_aborted_turn_keeps_executed_tools() -> None:
    entry = await disconnect_after_first_delta(StallingModel())
    assert [t["name"] for t in entry.payload["tools"]] == ["get_judgment"]


async def test_aborted_partial_answer_cites_what_it_mentions() -> None:
    entry = await disconnect_after_first_delta(CitingStall())
    assert [c["id"] for c in entry.payload["citations"]] == [JUDGMENT.id]


# ── 留痕写入的时序 (第 2 轮审核 X02-008, 009, 010 的探针) ──


async def test_settle_right_after_early_cancel_waits_for_the_write() -> None:
    trail = FakeTrail()
    chat = make_chat(StallingModel(), chat_deps(trail))
    turn = chat.begin(EID, USER, q("第一问"))
    turn.client_gone()
    await chat.settle()  # 不先等任务: settle 自己对已结束的轮次收尾
    assert EID not in chat.busy and not chat.turns and not chat.writes
    assert len(entries(trail, TrailKind.conversation)) == 1


class SlowAbortedTrail(FakeTrail):
    """写 aborted 留痕时停住, 直到放行."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def record(self, entry: TrailEntry) -> str:
        if entry.kind is TrailKind.conversation and entry.payload.get("aborted"):
            self.entered.set()
            await self.release.wait()
        return await super().record(entry)


async def test_follow_up_waits_for_the_previous_aborted_turn() -> None:
    trail = SlowAbortedTrail()
    chat = make_chat(StallingModel(), chat_deps(trail))
    old = chat.begin(EID, USER, q("第一问: 请解释这项余额"))
    stream = old.sse()
    await anext(stream)
    await stream.aclose()
    await asyncio.wait_for(trail.entered.wait(), 1)
    model = ScriptedChatModel(["后续回答"])
    chat.source = StaticSource(model)
    follow_up = chat.begin(EID, USER, q("继续刚才的解释"))  # aborted 留痕还没写完就追问
    for _ in range(20):
        await asyncio.sleep(0)
    assert model.requests == []  # 还在等上一轮的留痕
    trail.release.set()
    await follow_up.result()
    content = [m.content for m in model.requests[0].messages]
    assert "第一问: 请解释这项余额" in content and "项目目前" in content


class CommittedThenSlowTrail(FakeTrail):
    """正常 conversation 留痕已提交, 返回调用方之前还要等一会 (如连接清理)."""

    def __init__(self) -> None:
        super().__init__()
        self.committed = asyncio.Event()
        self.release = asyncio.Event()

    async def record(self, entry: TrailEntry) -> str:
        trail_id = await super().record(entry)
        if entry.kind is TrailKind.conversation and not entry.payload.get("aborted"):
            self.committed.set()
            await self.release.wait()
        return trail_id


async def test_cancel_after_the_record_is_committed_does_not_duplicate_the_turn() -> None:
    trail = CommittedThenSlowTrail()
    chat = make_chat(ScriptedChatModel(["完整回答"]), chat_deps(trail))
    turn = chat.begin(EID, USER, q("第一问"))
    await asyncio.wait_for(trail.committed.wait(), 1)
    turn.client_gone()
    await asyncio.gather(turn.task, return_exceptions=True)  # type: ignore[arg-type]
    trail.release.set()
    await chat.settle()
    [entry] = entries(trail, TrailKind.conversation)
    assert entry.payload["answer"] == "完整回答" and "aborted" not in entry.payload
    assert EID not in chat.busy


# ── 同一项目的留痕写入串成链 (第 3 轮审核 X02-012 的探针) ──

FIRST = "第一轮问题"


class FirstWriteGateTrail(FakeTrail):
    """写第一轮的 conversation 留痕时停住, 直到放行."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def record(self, entry: TrailEntry) -> str:
        if entry.kind is TrailKind.conversation and entry.payload["question"] == FIRST:
            self.entered.set()
            await self.release.wait()
        return await super().record(entry)


@pytest.mark.parametrize("cancel_before_start", [True, False])
async def test_cancelled_second_turn_keeps_the_wait_for_the_first(cancel_before_start: bool) -> None:
    trail = FirstWriteGateTrail()
    chat = make_chat(StallingModel(), chat_deps(trail))
    first = chat.begin(EID, USER, q(FIRST))
    stream = first.sse()
    await anext(stream)
    await stream.aclose()
    await asyncio.wait_for(trail.entered.wait(), 1)
    model = ScriptedChatModel(["第三轮回答"])
    chat.source = StaticSource(model)
    second = chat.begin(EID, USER, q("第二轮问题"))
    if not cancel_before_start:
        await asyncio.sleep(0)  # 第二轮正在等第一轮的留痕
    second.client_gone()
    await asyncio.gather(second.task, return_exceptions=True)  # type: ignore[arg-type]
    for _ in range(10):
        await asyncio.sleep(0)
    third = chat.begin(EID, USER, q("继续第一轮的话题"))
    for _ in range(30):
        await asyncio.sleep(0)
    assert model.requests == []  # 第一轮还没写进去, 第三轮不能越过它
    trail.release.set()
    await third.result()
    content = [m.content for m in model.requests[0].messages]
    assert content.index(FIRST) < content.index("第二轮问题")  # 两轮都在, 按先后
    await chat.shutdown()


class OneFailedWriteTrail(FakeTrail):
    """第一次写 conversation 留痕时失败."""

    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    async def record(self, entry: TrailEntry) -> str:
        if entry.kind is TrailKind.conversation and not self.failed:
            self.failed = True
            raise RuntimeError("模拟留痕写入失败")
        return await super().record(entry)


@pytest.mark.parametrize("abort_first", [True, False])
async def test_failed_write_does_not_block_the_next_turn(abort_first: bool) -> None:
    trail = OneFailedWriteTrail()
    chat = make_chat(StallingModel() if abort_first else ScriptedChatModel(["第一轮回答"]), chat_deps(trail))
    if abort_first:
        chat.begin(EID, USER, q("第一轮问题")).client_gone()
        await chat.settle()
    else:
        with pytest.raises(ConversationFailed):
            await chat.ask(EID, USER, q("第一轮问题"))
    chat.source = StaticSource(ScriptedChatModel(["第二轮回答"]))
    result = await asyncio.wait_for(chat.ask(EID, USER, q("第二轮问题")), 1)
    await chat.shutdown()
    await asyncio.sleep(0)
    assert result.answer == "第二轮回答"
    assert not chat.busy and not chat.turns and not chat.writes and not chat.persisting


async def test_normal_record_survives_repeated_cancel_and_shutdown() -> None:
    trail = FirstWriteGateTrail()
    chat = make_chat(ScriptedChatModel(["完整回答"]), chat_deps(trail))
    first = chat.begin(EID, USER, q(FIRST))
    await asyncio.wait_for(trail.entered.wait(), 1)
    first.client_gone()
    first.client_gone()
    stop = asyncio.create_task(chat.shutdown())
    await asyncio.sleep(0)
    assert first.record is not None and not first.record.cancelled()
    trail.release.set()
    await asyncio.wait_for(stop, 1)
    [entry] = entries(trail, TrailKind.conversation)
    assert entry.payload["answer"] == "完整回答" and not entry.payload.get("aborted")


async def test_other_engagement_does_not_wait_for_this_ones_write() -> None:
    trail = FirstWriteGateTrail()
    chat = make_chat(StallingModel(), chat_deps(trail))
    first = chat.begin(EID, USER, q(FIRST))
    stream = first.sse()
    await anext(stream)
    await stream.aclose()
    await asyncio.wait_for(trail.entered.wait(), 1)
    other = "ENG-2026-002"
    chat.state = FixedState(view=make_overview(ENGAGEMENT.model_copy(update={"id": other})))
    chat.source = StaticSource(ScriptedChatModel(["其他项目回答"]))
    result = await asyncio.wait_for(chat.ask(other, USER, q("其他项目问题")), 1)
    assert result.answer == "其他项目回答"
    trail.release.set()
    await chat.shutdown()


async def test_secret_in_tool_name_reaches_neither_stream_nor_trail() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        tool = {"index": 0, "id": "c", "function": {"name": KEY, "arguments": "{}"}}
        return httpx.Response(200, content=sse(chunk(tool_calls=[tool]), chunk(finish="tool_calls")))

    trail = FakeTrail()
    events = await collect_sse(make_chat(live_model(handler), chat_deps(trail)), q("你好"))
    assert KEY not in repr(events)
    assert events == [("error", {"code": "LLM_SECRET_ECHO", "message": "模型输出中出现了密钥, 已中止, 本轮不记录"})]
    assert entries(trail, TrailKind.conversation) == []
    assert all(KEY not in repr(e.payload) for e in trail.entries)


async def test_secret_echo_while_summarizing_fails_the_turn() -> None:
    trail = FakeTrail()
    await seed_turns(trail, 3)
    model = ScriptedChatModel([SecretEcho("echo"), "正常回答"])
    chat = make_chat(model, chat_deps(trail), limits=ChatLimits(context_budget=10, keep_turns=1))
    with pytest.raises(ConversationFailed) as error:
        await chat.ask(EID, USER, q("继续"))
    assert error.value.code == "LLM_SECRET_ECHO"
    assert len(entries(trail, TrailKind.conversation)) == 3  # 本轮不记
    assert entries(trail, TrailKind.conversation_summary) == []
