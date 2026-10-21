"""审计助理对话服务 ConciergeChat: 对话接口 (api/routes/conversation.py) 调它.

一轮问答在独立的后台任务里跑 (ChatTurn), 经队列把事件交给 SSE:
1. 取当前生效的对话模型 (设置页主模型; mock 模式为规则模型) 与项目总览.
2. 从审计留痕重建历史; 估算超预算时压缩 (memory.py), 摘要写 conversation_summary 留痕.
3. 跑对话图 (loop.py): delta, tool 事件依次进队列. 上游报上下文超限且还没输出时, 加大力度压缩后重试一次.
4. 结束: 从回答里匹配引用, 写 conversation 留痕, 发 citations 与 done. 出错发 error 后结束, 不写 conversation 留痕.

取消 (审计师断开, ask 的调用方被取消, shutdown): 只同步地取消后台任务, 不在被取消的作用域里 await. 收尾挂在任务的
完成回调上 (_finish, 任务没开始执行就被取消也会做): 释放项目, 另起独立任务写 aborted 留痕 (已发出的部分回答,
已执行的工具轨迹, 从部分回答匹配的引用), shutdown 等这些写入完成. 流式接口在响应结束时 (含发送失败) 也通知取消,
不依赖 SSE 生成器被关闭. 还没开始读就断开而又没有发送失败的, 后台任务照常跑完并写留痕. 每次模型调用写 llm_call 留痕.
三种留痕 (conversation, conversation_summary, llm_call) 的 procedure_id 都记 GEN-10 (PROCEDURE_ID).

同一项目同时只答一个问题 (内存标记, 单 worker), 忙时 begin 抛 ConversationBusy; 与主图的项目锁无关,
主图在执行时也能提问.
"""

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator, Callable, Coroutine, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, cast

from langchain_core.runnables import RunnableConfig

from backend.adapters.chat_llm import ContextOverflow, LiveChatSource, SecretEcho
from backend.adapters.llm import LLMTransportError
from backend.agents.base import AgentDeps
from backend.agents.concierge_chat.citations import citation_pool, match_citations
from backend.agents.concierge_chat.loop import (
    ChatContext,
    ChatState,
    ToolTrace,
    answer_of,
    build_chat_graph,
    initial_state,
    recursion_limit,
)
from backend.agents.concierge_chat.memory import (
    History,
    Summary,
    Turn,
    all_turns,
    collect,
    context_tokens,
    history_messages,
    load_history,
    summary_material,
)
from backend.agents.concierge_chat.rule_model import RuleChatSource
from backend.agents.concierge_chat.tools import StateReader, ToolBox, ToolEnv
from backend.contracts.conversation import (
    AssistantTurn,
    ChatMessage,
    ConciergeAnswer,
    ConciergeCitation,
    ConciergeQuestion,
    ConversationTurn,
    Lang,
    ToolSpec,
)
from backend.contracts.models import AgentId, TrailEntry, TrailKind
from backend.contracts.ports import ChatModel, ChatModelSource, XeroConnections
from backend.contracts.views import EngagementOverview
from backend.core.config import Settings
from backend.core.security import SecretKeyError
from backend.services.judgment_engine import prompt_env
from backend.services.llm_settings import LlmSettingsService

log = logging.getLogger(__name__)
PING_SECONDS = 15.0  # 这么久没有事件就发一行 SSE 注释保活, 免得代理断开空闲连接
LOGGED_TOOL_CHARS = 2000  # llm_call 留痕里每条工具结果最多保留的字符数
SUMMARY_SYSTEM = "You keep the running summary of a conversation for an audit file. Never continue the conversation."
PROCEDURE_ID = "GEN-10"  # 进度, 交付物与数据来源解释 (规格书 GEN-10); 不建程序文件, 只用作对话留痕的程序编号


@dataclass(frozen=True)
class ChatLimits:
    """对话的各项上限, 取自配置 CONCIERGE_*."""

    max_steps: int = 6  # 一轮最多调几次带工具的模型; 到了再调一次收尾
    context_budget: int = 24000  # 一轮开始时系统提示 + 历史 + 问题的估算 token 上限, 超过即压缩
    turn_reserve: int = 8000  # 本轮工具结果的 token 预算 (按每 token 3 个字符折算)
    keep_turns: int = 4  # 压缩时保留原文的最近轮数
    tool_result_chars: int = 6000  # 单个工具结果最多字符数
    max_tokens: int = 2000  # 每次模型调用最多输出的 token 数
    summary_max_tokens: int = 1200  # 压缩摘要最多输出的 token 数

    @classmethod
    def from_settings(cls, settings: Settings) -> "ChatLimits":
        return cls(
            max_steps=settings.CONCIERGE_MAX_STEPS,
            context_budget=settings.CONCIERGE_CONTEXT_BUDGET,
            turn_reserve=settings.CONCIERGE_TURN_RESERVE,
            keep_turns=settings.CONCIERGE_KEEP_TURNS,
            tool_result_chars=settings.CONCIERGE_TOOL_RESULT_CHARS,
        )


class ConversationBusy(Exception):
    """该项目正在回答上一个问题."""


class ConversationFailed(Exception):
    """这轮问答失败. code: LLM_SECRET_ECHO, LLM_CONTEXT_OVERFLOW, LLM_UNAVAILABLE, SETTINGS_KEY_MISSING,
    SETTINGS_KEY_INVALID, NOT_FOUND, CONVERSATION_FAILED. message 不含密钥."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def failure_of(error: Exception) -> ConversationFailed:
    if isinstance(error, SecretEcho):
        return ConversationFailed("LLM_SECRET_ECHO", "模型输出中出现了密钥, 已中止, 本轮不记录")
    if isinstance(error, ContextOverflow):
        return ConversationFailed("LLM_CONTEXT_OVERFLOW", "对话内容超出模型的上下文长度, 请换个更具体的问法")
    if isinstance(error, LLMTransportError):
        return ConversationFailed("LLM_UNAVAILABLE", str(error))
    if isinstance(error, SecretKeyError):
        return ConversationFailed(error.code, error.message)
    if isinstance(error, LookupError):
        return ConversationFailed("NOT_FOUND", str(error))
    return ConversationFailed("CONVERSATION_FAILED", f"{type(error).__name__}: {error}")


def chat_prompt_hash(messages: Sequence[ChatMessage], tools: Sequence[ToolSpec]) -> str:
    """送给对话模型的完整输入 (消息与工具说明) 的 sha256: 规范 JSON, 键排序, 不留空白, 非 ASCII 原样."""
    canonical = json.dumps(
        {"messages": [m.model_dump() for m in messages], "tools": [t.model_dump() for t in tools]},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def logged_message(message: ChatMessage) -> dict[str, Any]:
    """llm_call 留痕里的一条消息: 工具结果截短."""
    dumped = message.model_dump(mode="json", exclude_defaults=True)
    if message.role == "tool" and len(message.content) > LOGGED_TOOL_CHARS:
        dumped["content"] = message.content[:LOGGED_TOOL_CHARS] + "…"
    return dumped


def sse_frame(event: str, data: dict[str, Any]) -> str:
    """一个 SSE 事件. json.dumps 会转义换行, data 总是一行; 只用 LF (前端按 \\n\\n 切分)."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@dataclass(eq=False)  # 按对象身份比较与哈希, 放进 ConciergeChat.turns
class ChatTurn:
    """一轮问答: 后台任务, 给 SSE 的事件队列, 以及中断时写 aborted 留痕要用的进度."""

    eid: str  # 项目
    user_id: str  # 提问的审计师
    question: ConciergeQuestion  # 问题
    queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = field(default_factory=asyncio.Queue)
    parts: list[str] = field(default_factory=list)  # 已发出的回答片段, 断开时拼成部分回答
    traces: list[ToolTrace] = field(default_factory=list)  # 已执行的工具调用轨迹, 随图状态更新
    cites: dict[str, ConciergeCitation] = field(default_factory=dict)  # 已执行的工具结果里的编号池, 随图状态更新
    history_pool: dict[str, ConciergeCitation] = field(default_factory=dict)  # 历史中可引用的编号
    model: str = ""  # 最近一次调用的模型名
    # 本轮的回答语言 (项目的工作语言, 需求 R342): 读总览时定下, 之后改了工作语言也不影响这一轮; 还没读到时为空
    answer_lang: Lang | None = None
    input_tokens: int = 0  # 本轮各次调用的输入 token 合计
    output_tokens: int = 0  # 本轮各次调用的输出 token 合计
    record: "asyncio.Task[Any] | None" = None  # 写这一轮 conversation 留痕 (正常或 aborted) 的独立任务, 只建一次
    gone: bool = False  # 审计师已断开
    delivered: bool = False  # SSE 已把全部事件送到结束标记: 之后的关闭不算断开
    closed: bool = False  # 结束标记已放进队列
    finished: bool = False  # 收尾 (ConciergeChat._finish) 已做
    task: "asyncio.Task[ConciergeAnswer | ConversationFailed] | None" = None

    def emit(self, event: str, data: dict[str, Any]) -> None:
        self.queue.put_nowait((event, data))

    def close(self) -> None:
        """放结束标记, 可重复调用."""
        if not self.closed:
            self.closed = True
            self.queue.put_nowait(None)

    def count(self, result: AssistantTurn) -> None:
        self.model = result.model
        self.input_tokens += result.input_tokens
        self.output_tokens += result.output_tokens

    async def sse(self) -> AsyncIterator[str]:
        """SSE 输出: 依次为 delta (多条), tool (可选), citations, done; 出错为 error. 生成器被关闭或取消 (审计师断开)
        时只同步地通知后台任务, 不 await."""
        finished = False
        try:
            while True:
                try:
                    item = await asyncio.wait_for(self.queue.get(), PING_SECONDS)
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                if item is None:
                    finished = self.delivered = True
                    return
                yield sse_frame(*item)
        finally:
            if not finished:
                self.client_gone()

    def client_gone(self) -> None:
        """审计师断开: 同步取消后台任务 (不 await), 可重复调用; 事件已全部送达时不算断开."""
        if self.delivered:
            return
        self.gone = True
        if self.task is not None and not self.task.done():
            self.task.cancel()

    async def result(self) -> ConciergeAnswer:
        """等这轮结束, 返回回答; 失败抛 ConversationFailed."""
        assert self.task is not None
        outcome = await self.task
        if isinstance(outcome, ConversationFailed):
            raise outcome
        return outcome


class ConciergeChat:
    """审计助理的对话服务 (见模块说明). 组装根在 lifespan 里构造, 挂在 app.state.concierge."""

    def __init__(
        self,
        deps: AgentDeps,
        state: StateReader,
        source: ChatModelSource,
        limits: ChatLimits | None = None,
        tools: ToolBox | None = None,
    ) -> None:
        self.deps = deps  # 存储, 留痕, 程序库
        self.state = state  # 项目位置与闸门材料 (GraphStateQuery)
        self.source = source  # 每轮取当前生效的对话模型
        self.limits = limits if limits is not None else ChatLimits()
        self.tools = tools if tools is not None else ToolBox()
        self.prompts = prompt_env(deps.library.root / "prompts" / "concierge")
        self.graph = build_chat_graph()
        self.busy: dict[str, ChatTurn] = {}  # 正在回答的项目 -> 那一轮; 只由那一轮自己释放 (release)
        self.turns: set[ChatTurn] = set()  # 进行中的各轮, shutdown 时取消
        self.writes: set[asyncio.Task[Any]] = set()  # 写 conversation 留痕的独立任务, shutdown 时等它们写完
        self.persisting: dict[str, asyncio.Task[Any]] = {}  # 项目 -> 最近一轮还在写的留痕; 下一轮读历史前等它
        self.xero: XeroConnections | None = None  # Xero 连接记录, 组装根在建好 XeroLink 后接上 (待办的组织信息核对)

    async def history(self, eid: str) -> list[ConversationTurn]:
        """项目的全部问答, 按时间升序 (不含压缩摘要)."""
        return [t.view() for t in await all_turns(self.deps.trail, eid)]

    def begin(self, eid: str, user_id: str, question: ConciergeQuestion) -> ChatTurn:
        """开始一轮问答 (后台任务). 该项目正在回答时抛 ConversationBusy.

        收尾挂在任务的完成回调上 (_finish), 任务在开始执行之前就被取消时也会做.
        """
        if eid in self.busy:
            raise ConversationBusy(eid)
        turn = ChatTurn(eid, user_id, question)
        self.busy[eid] = turn
        turn.task = asyncio.create_task(self._run(turn))
        self.turns.add(turn)
        turn.task.add_done_callback(lambda _: self._finish(turn))
        return turn

    async def ask(self, eid: str, user_id: str, question: ConciergeQuestion) -> ConciergeAnswer:
        """一次性问答: 等回答结束再返回. 调用方被取消时, 这轮随之取消 (写 aborted 留痕)."""
        return await self.begin(eid, user_id, question).result()

    async def shutdown(self) -> None:
        """取消进行中的各轮, 等它们结束, 再等 aborted 留痕写完 (之后才能关闭数据库)."""
        turns = list(self.turns)
        tasks = [t.task for t in turns if t.task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for turn in turns:  # 完成回调可能还没轮到; _finish 可重复调用
            self._finish(turn)
        await self.settle()

    async def settle(self) -> None:
        """等已请求取消的各轮结束, 并等全部留痕写入完成.

        每轮先让出一次事件循环, 再对已结束而还没收尾的轮次主动收尾 (_finish 可重复调用; 不靠完成回调何时轮到);
        只等还没完成的任务: 对已完成的任务 gather 或 wait 不会让出事件循环, 回调轮不上, 循环会空转.
        """
        while True:
            await asyncio.sleep(0)
            for turn in list(self.turns):
                if turn.task is not None and turn.task.done():
                    self._finish(turn)
            stopping = [t.task for t in self.turns if t.task is not None and t.task.cancelling() and not t.task.done()]
            pending = [w for w in self.writes if not w.done()]
            if not stopping and not pending:
                return
            await asyncio.wait([*stopping, *pending])

    def _finish(self, turn: ChatTurn) -> None:
        """一轮结束后的收尾, 可重复调用: 被取消且还没有留痕写入的, 安排 aborted 留痕; 然后释放项目, 放结束标记.

        先安排写入再释放: 下一轮读历史前会等这一写入 (persisting). 留痕不在被取消的任务里写, 那里再被取消一次
        (如断开后紧接着 shutdown) 就会打断写入. 正常留痕已在写 (record 已建) 时不补写 aborted, 免得同一轮记两条.
        """
        if turn.finished:
            return
        turn.finished = True
        if turn.task is not None and turn.task.cancelled() and turn.record is None:
            turn.record = self._persist(turn.eid, self._record_aborted(turn))
        self.release(turn)
        self.turns.discard(turn)
        turn.close()

    def _persist(self, eid: str, write: Coroutine[Any, Any, Any]) -> "asyncio.Task[Any]":
        """在独立任务里写留痕 (不随这一轮的取消而中断): 记进 writes 供 shutdown 等待, 记为该项目最近的写入.

        同一项目的写入串成链: 新写入先等此前登记的那次写完 (前一次失败也照常往下写), 再写自己. 所以最近的写入
        完成即代表此前全部写入都已完成, 下一轮只等它一个; 留痕也按轮次的先后写入.
        """
        before = self.persisting.get(eid)

        async def chained() -> Any:
            try:
                if before is not None and not before.done():
                    await asyncio.wait([before])  # 只等完成, 不传播它的异常
            except BaseException:
                write.close()  # 没轮到就被取消: 关掉未开始的协程, 免得 "never awaited" 警告
                raise
            return await write

        task = asyncio.create_task(chained())
        self.writes.add(task)
        self.persisting[eid] = task

        def done(finished: "asyncio.Task[Any]") -> None:
            self.writes.discard(finished)
            if self.persisting.get(eid) is finished:
                del self.persisting[eid]

        task.add_done_callback(done)
        return task

    async def _record_aborted(self, turn: ChatTurn) -> None:
        """中断的一轮: 已发出的部分回答, 已执行的工具轨迹, 从部分回答里匹配到的引用."""
        answer = "".join(turn.parts)
        citations = match_citations(answer, citation_pool(turn.cites.values(), turn.history_pool.values()))
        try:
            await self._record_turn(turn, turn.eid, turn.user_id, turn.question, answer, citations, aborted=True)
        except Exception as e:  # 留痕写不进去 (如库不可用) 只记日志
            log.warning("审计助理中断的一轮没能写留痕: %s", type(e).__name__)

    async def _run(self, turn: ChatTurn) -> ConciergeAnswer | ConversationFailed:
        """后台任务. 取消 (断开, 调用方取消, shutdown) 照常上抛, 由 _finish 安排 aborted 留痕后再释放项目;
        其余失败发 error 事件. 不是取消的, 结束时立即释放项目, 不等完成回调."""
        cancelled = False
        try:
            return await self._answer(turn, turn.eid, turn.user_id, turn.question)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception as e:
            failed = failure_of(e)
            if not isinstance(e, SecretEcho):
                log.warning("审计助理回答失败 (%s): %s", turn.eid, failed.code)
            turn.emit("error", {"code": failed.code, "message": failed.message})
            return failed
        finally:
            if not cancelled:
                self.release(turn)
            turn.close()

    def release(self, turn: ChatTurn) -> None:
        """释放项目, 只在登记的仍是这一轮时: 它释放之后可能已有新一轮开始, 不能把新一轮的登记清掉."""
        if self.busy.get(turn.eid) is turn:
            del self.busy[turn.eid]

    async def _answer(self, turn: ChatTurn, eid: str, user_id: str, question: ConciergeQuestion) -> ConciergeAnswer:
        previous = self.persisting.get(eid)
        if previous is not None and not previous.done():  # 上一轮的留痕还在写 (如中断后立即追问): 等它进历史
            await asyncio.wait([previous])
        model = await self.source.current()
        overview = await self.state.overview(eid)
        # 回答语言取项目的工作语言 (需求 R342); 提问带的 lang 是界面语言, 只管工具提示 (见 _converse)
        answer_lang: Lang = overview.engagement.working_language
        turn.answer_lang = answer_lang

        def system(summary: Summary | None) -> str:
            return self.system_prompt(overview, answer_lang, summary.text if summary else "")

        history = await load_history(self.deps.trail, eid)
        history = await self._fit(eid, model, history, system, question, answer_lang, force=False)
        turn.history_pool = history.pool()
        try:
            state = await self._converse(turn, eid, model, history, system, question)
        except ContextOverflow:
            if turn.parts:  # 已经输出了一部分, 不能重来
                raise
            history = await self._fit(eid, model, history, system, question, answer_lang, force=True)
            turn.history_pool = history.pool()
            state = await self._converse(turn, eid, model, history, system, question)
        answer = answer_of(state)
        citations = match_citations(answer, citation_pool(state["cites"].values(), turn.history_pool.values()))
        # 正常留痕也在独立任务里写: 写入途中这一轮被取消时照样写完, _finish 见 record 已建就不再补写 aborted
        write = self._record_turn(turn, eid, user_id, question, answer, citations, aborted=False)
        turn.record = self._persist(eid, write)
        trail_id = cast(str, await asyncio.shield(turn.record))
        turn.emit("citations", {"citations": [c.model_dump(mode="json") for c in citations]})
        turn.emit("done", {"trail_id": trail_id})
        return ConciergeAnswer(answer=answer, citations=citations, trail_id=trail_id)

    def system_prompt(self, overview: EngagementOverview, answer_lang: Lang, summary: str) -> str:
        """系统提示词; answer_lang 为回答语言 (项目的工作语言), 写在 "Language:" 一行与末尾的语言要求里."""
        engagement = overview.engagement
        return self.prompts.get_template("system.md.j2").render(
            client_name=overview.client_name,
            period_start=engagement.period_start.isoformat(),
            period_end=engagement.period_end.isoformat(),
            answer_lang=answer_lang,
            summary=summary,
        )

    async def _converse(
        self,
        turn: ChatTurn,
        eid: str,
        model: ChatModel,
        history: History,
        system: Callable[[Summary | None], str],
        question: ConciergeQuestion,
    ) -> ChatState:
        """跑一次对话图, 事件进队列, 返回最终状态."""
        messages = [
            ChatMessage(role="system", content=system(history.summary)),
            *history_messages(history.turns),
            ChatMessage(role="user", content=question.question),
        ]
        logged = 0  # 已记入 llm_call 留痕的消息数: 每次只记新增的

        async def record_call(
            sent: Sequence[ChatMessage], result: AssistantTurn, step: int, choice: Literal["auto", "none"]
        ) -> None:
            nonlocal logged
            new, logged = sent[logged:], len(sent) + 1  # +1: 本次的 assistant 消息记在 text 与 tool_calls 里
            turn.count(result)
            await self._record_llm(eid, sent, new, result, "conversation", step, choice)

        limits = self.limits
        ctx = ChatContext(
            model=model,
            tools=self.tools,
            env=ToolEnv(eid, self.deps, self.state, limits.tool_result_chars, self.xero),
            ui_lang=question.lang,  # 界面语言: 工具提示文字按它, 回答语言另在系统提示里
            max_steps=limits.max_steps,
            max_tokens=limits.max_tokens,
            tool_budget=limits.turn_reserve * 3,
            record_call=record_call,
        )
        final: ChatState | None = None
        config: RunnableConfig = {"recursion_limit": recursion_limit(limits.max_steps)}
        modes: list[Literal["custom", "values"]] = ["custom", "values"]
        stream = self.graph.astream(initial_state(messages), config, context=ctx, stream_mode=modes)
        async for mode, chunk in cast(AsyncIterator[tuple[str, Any]], stream):
            if mode == "custom":
                event, data = chunk["event"], chunk["data"]
                if event == "delta":
                    turn.parts.append(data["text"])
                turn.emit(event, data)
            else:
                final = cast(ChatState, chunk)
                turn.traces = list(final["traces"])  # 中途断开时, aborted 留痕记到已执行的工具为止
                turn.cites = dict(final["cites"])
        assert final is not None  # 图至少产出初始状态
        return final

    async def _fit(
        self,
        eid: str,
        model: ChatModel,
        history: History,
        system: Callable[[Summary | None], str],
        question: ConciergeQuestion,
        answer_lang: Lang,
        *,
        force: bool,
    ) -> History:
        """历史超预算 (或 force: 上游已报超限) 时压缩: 较早的轮连同上一份摘要写成新摘要 (用这一轮的回答语言写),
        保留最近几轮原文. force 时保留的轮数减半. 摘要失败只截掉较早的轮 (不写摘要)."""
        size = context_tokens(system(history.summary), history.turns, question.question, self.tools.specs)
        if not force and size <= self.limits.context_budget:
            return history
        keep = self.limits.keep_turns if not force else min(self.limits.keep_turns, len(history.turns) // 2)
        cut = len(history.turns) - keep
        if cut <= 0:
            return history
        old, kept = history.turns[:cut], history.turns[cut:]
        try:
            text = await self._summarize(eid, model, history.summary, old, answer_lang)
        except (asyncio.CancelledError, SecretEcho):  # 密钥回显不是普通的压缩失败: 整轮按安全失败处理
            raise
        except Exception as e:
            log.warning("审计助理对话历史压缩失败, 本轮只截去较早的 %d 轮: %s", len(old), type(e).__name__)
            return History(history.summary, kept)
        previous: list[ConciergeCitation] = history.summary.cite_pool if history.summary else []
        pool = list(citation_pool(previous, *(t.citations for t in old)).values())
        trail_id = await self.deps.trail.record(
            TrailEntry(
                engagement_id=eid,
                kind=TrailKind.conversation_summary,
                procedure_id=PROCEDURE_ID,
                actor=AgentId.A10,
                summary=f"审计助理压缩对话历史: {len(old)} 轮问答并入摘要",
                payload={
                    "summary": text,
                    "covers_through": old[-1].trail_id,
                    "cite_pool": [c.model_dump(mode="json") for c in pool],
                    "tokens_before": size,
                    "turns": len(old),
                },
                created_at=datetime.now(UTC),
            )
        )
        return History(Summary(trail_id, text, old[-1].trail_id, pool), kept)

    async def _summarize(
        self, eid: str, model: ChatModel, previous: Summary | None, old: Sequence[Turn], answer_lang: Lang
    ) -> str:
        """把 old 连同上一份摘要写成新摘要; answer_lang 为摘要的语言 (这一轮的回答语言, 需求 R342)."""
        prompt = self.prompts.get_template("summary.md.j2").render(
            previous=previous.text if previous else "", turns=summary_material(old), answer_lang=answer_lang
        )
        messages = [ChatMessage(role="system", content=SUMMARY_SYSTEM), ChatMessage(role="user", content=prompt)]
        result = await collect(model, messages, self.limits.summary_max_tokens)
        await self._record_llm(eid, messages, messages, result, "summary", 0, "none", tools=[])
        return result.text.strip()

    async def _record_llm(
        self,
        eid: str,
        sent: Sequence[ChatMessage],
        new: Sequence[ChatMessage],
        result: AssistantTurn,
        purpose: Literal["conversation", "summary"],
        step: int,
        choice: Literal["auto", "none"],
        *,
        tools: Sequence[ToolSpec] | None = None,
    ) -> None:
        """一次模型调用的 llm_call 留痕: 提示词哈希覆盖完整输入, payload 只存本次新增的消息 (之前的在上一条留痕里)."""
        specs = self.tools.specs if tools is None else tools
        what = "回答" if purpose == "conversation" else "压缩对话历史"
        await self.deps.trail.record(
            TrailEntry(
                engagement_id=eid,
                kind=TrailKind.llm_call,
                procedure_id=PROCEDURE_ID,
                actor=AgentId.A10,
                summary=f"审计助理{what}: 第 {step + 1} 次调用大模型",
                model=result.model,
                prompt_hash=chat_prompt_hash(sent, specs),
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                payload={
                    "purpose": purpose,
                    "step": step,
                    "tool_choice": choice,
                    "messages": [logged_message(m) for m in new],
                    "text": result.text,
                    "tool_calls": [c.model_dump() for c in result.tool_calls],
                    "finish_reason": result.finish_reason,
                },
                created_at=datetime.now(UTC),
            )
        )

    async def _working_language(self, eid: str) -> Lang:
        """项目当前的工作语言; 查不到项目为 en (缺省值, 同 Engagement.working_language). 查项目出错时记日志并取 en:
        只在还没读到总览就中断的一轮里用到, 不能因为它写不成这一轮的留痕 (问题本身总要留痕)."""
        try:
            engagement = await self.deps.engagement_store.get(eid)
        except Exception as e:
            log.warning("审计助理留痕时查项目 %s 的工作语言出错, 回答语言记 en: %s", eid, type(e).__name__)
            return "en"
        return engagement.working_language if engagement is not None else "en"

    async def _record_turn(
        self,
        turn: ChatTurn,
        eid: str,
        user_id: str,
        question: ConciergeQuestion,
        answer: str,
        citations: Sequence[ConciergeCitation],
        *,
        aborted: bool,
    ) -> str:
        """这轮问答的 conversation 留痕: 执行者为审计师; payload 含问, 答, 界面语言 (ui_lang), 回答语言 (answer_lang,
        需求 R342), 引用, 工具轨迹 (不含结果原文). 还没读到总览就中断的, 回答语言取项目当时的工作语言."""
        answer_lang = turn.answer_lang or await self._working_language(eid)
        payload: dict[str, Any] = {
            "question": question.question,
            "answer": answer,
            "ui_lang": question.lang,
            "answer_lang": answer_lang,
            "citations": [c.model_dump(mode="json") for c in citations],
            "model": turn.model,
            "tools": [asdict(t) for t in turn.traces],
        }
        if aborted:
            payload["aborted"] = True
        trail_id = await self.deps.trail.record(
            TrailEntry(
                engagement_id=eid,
                kind=TrailKind.conversation,
                procedure_id=PROCEDURE_ID,
                actor=user_id,
                summary=question.question[:80],
                model=turn.model or None,
                input_tokens=turn.input_tokens,
                output_tokens=turn.output_tokens,
                payload=payload,
                created_at=datetime.now(UTC),
            )
        )
        return trail_id


def make_chat_source(settings: Settings, llm_settings: LlmSettingsService | None) -> ChatModelSource:
    """mock 模式为规则模型; live 模式按设置页主模型的生效配置 (没有设置服务时取 .env)."""
    if settings.LLM_MODE == "mock":
        return RuleChatSource()
    if llm_settings is not None:
        service = llm_settings
        return LiveChatSource(lambda: service.config("primary"))

    async def from_env() -> tuple[Any, str, str, str]:
        provider = "custom" if settings.LLM_BASE_URL else "openai"
        return provider, settings.LLM_BASE_URL, settings.LLM_MODEL, settings.LLM_API_KEY

    return LiveChatSource(from_env)
