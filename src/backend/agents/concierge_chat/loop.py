"""审计助理的对话图 (规格书 18.1 节 "A10 对话入口为独立图"): 一问一答跑一次.

    START → call_model ──有工具调用──▶ run_tools ──▶ call_model
                └──没有──▶ END

- call_model: 流式调对话模型, 逐段经 LangGraph 的自定义输出通道 (stream_mode="custom") 推 delta 事件.
- run_tools: 执行模型要求的工具 (只读, 并发), 结果作为 role=tool 消息接回; 推 tool 事件 (正在查什么).
- 步数: 每调一次模型记一步; 到 max_steps 时以 tool_choice=none 再调一次收尾, 不再调工具. recursion_limit 只作兜底.
- 回答: 各步的文本按顺序拼接, 步与步之间补一个空行; delta 事件同样补, 所以界面上看到的就是留痕里的回答.
- 不带检查点: 历史以审计留痕为准 (memory.py), 状态只活一轮.
"""

import operator
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime

from backend.adapters.llm import LLMTransportError
from backend.agents.base import gather_all
from backend.agents.concierge_chat.tools import ToolBox, ToolEnv, ToolOutcome
from backend.contracts.conversation import AssistantTurn, ChatMessage, ConciergeCitation, Lang, TextDelta, ToolCall
from backend.contracts.ports import ChatModel

STEP_SEPARATOR = "\n\n"  # 步与步之间的回答文本用空行隔开


@dataclass(frozen=True)
class ToolTrace:
    """一次工具调用的轨迹, 记入 conversation 留痕 (不含结果原文)."""

    name: str  # 工具名
    arguments: str  # 模型给的参数 JSON 原文
    ok: bool  # 是否查到
    ids: list[str]  # 结果中可引用的编号
    chars: int  # 结果字符数


def merge_cites(
    left: dict[str, ConciergeCitation], right: dict[str, ConciergeCitation]
) -> dict[str, ConciergeCitation]:
    """引用池的 reducer: 同一编号先出现的为准."""
    return {**right, **left}


class ChatState(TypedDict):
    """一轮问答的图状态."""

    messages: Annotated[list[ChatMessage], operator.add]  # 发给模型的全部消息: 系统, 历史, 本轮问题, 本轮工具往来
    steps: int  # 已调了几次模型
    texts: Annotated[list[str], operator.add]  # 各步的回答文本 (非空的)
    turns: Annotated[list[AssistantTurn], operator.add]  # 各次模型调用的结果
    traces: Annotated[list[ToolTrace], operator.add]  # 工具调用轨迹
    cites: Annotated[dict[str, ConciergeCitation], merge_cites]  # 本轮工具结果里可引用的编号
    tool_chars: int  # 本轮工具结果已用的字符数


RecordCall = Callable[[Sequence[ChatMessage], AssistantTurn, int, Literal["auto", "none"]], Awaitable[None]]


@dataclass(frozen=True)
class ChatContext:
    """一轮问答的运行环境, 经 graph.astream(..., context=) 传给节点."""

    model: ChatModel  # 当前生效的对话模型
    tools: ToolBox  # 工具箱
    env: ToolEnv  # 工具的运行环境 (绑定项目)
    lang: Lang  # 回答语言, 决定 tool 事件的提示文字
    max_steps: int  # 最多调几次带工具的模型; 到了再调一次收尾
    max_tokens: int  # 每次模型调用最多输出的 token 数
    tool_budget: int  # 本轮工具结果合计最多字符数, 超出的调用不执行, 回提示
    record_call: RecordCall  # 写一次模型调用的 llm_call 留痕: (发出的消息, 结果, 第几步, tool_choice)


def initial_state(messages: list[ChatMessage]) -> ChatState:
    return {"messages": messages, "steps": 0, "texts": [], "turns": [], "traces": [], "cites": {}, "tool_chars": 0}


def answer_of(state: ChatState) -> str:
    """本轮回答全文: 各步文本以空行拼接, 与 delta 事件的拼接相同."""
    return STEP_SEPARATOR.join(state["texts"])


async def call_model(state: ChatState, runtime: Runtime[ChatContext]) -> dict[str, Any]:
    ctx, write = runtime.context, runtime.stream_writer
    choice: Literal["auto", "none"] = "none" if state["steps"] >= ctx.max_steps else "auto"
    separate = bool(state["texts"])  # 前面的步已有文本: 本步第一段之前补空行
    turn: AssistantTurn | None = None
    async for event in ctx.model.stream_chat(
        state["messages"], ctx.tools.specs, tool_choice=choice, max_tokens=ctx.max_tokens
    ):
        if isinstance(event, TextDelta):
            if separate:
                write({"event": "delta", "data": {"text": STEP_SEPARATOR}})
                separate = False
            write({"event": "delta", "data": {"text": event.text}})
        else:
            turn = event
    if turn is None:
        raise LLMTransportError("对话模型没有给出完整结果")
    if choice == "none" and turn.tool_calls:  # 收尾时仍要调工具: 忽略, 以已有文本作答
        turn = turn.model_copy(update={"tool_calls": []})
    await ctx.record_call(state["messages"], turn, state["steps"], choice)
    message = ChatMessage(role="assistant", content=turn.text, tool_calls=turn.tool_calls)
    return {
        "messages": [message],
        "steps": state["steps"] + 1,
        "texts": [turn.text] if turn.text else [],
        "turns": [turn],
    }


def after_model(state: ChatState) -> Literal["run_tools", "__end__"]:
    last = state["messages"][-1]
    return "run_tools" if last.role == "assistant" and last.tool_calls else "__end__"


async def run_tools(state: ChatState, runtime: Runtime[ChatContext]) -> dict[str, Any]:
    ctx, write = runtime.context, runtime.stream_writer
    calls = state["messages"][-1].tool_calls
    for name in dict.fromkeys(c.name for c in calls):
        write({"event": "tool", "data": {"label": ctx.tools.label(name, ctx.lang)}})
    over = state["tool_chars"] >= ctx.tool_budget

    async def run(call: ToolCall) -> ToolOutcome:
        if over:
            note = '{"error": "本轮查询结果已超过上限, 请根据已查到的信息回答"}'
            return ToolOutcome(content=note, ok=False, cites={})
        return await ctx.tools.call(ctx.env, call)

    outcomes = await gather_all(run(c) for c in calls)
    pairs = list(zip(calls, outcomes, strict=True))
    messages = [ChatMessage(role="tool", tool_call_id=c.id, content=o.content) for c, o in pairs]
    traces = [ToolTrace(c.name, c.arguments, o.ok, sorted(o.cites), len(o.content)) for c, o in pairs]
    cites: dict[str, ConciergeCitation] = {}
    for outcome in outcomes:
        cites = merge_cites(cites, outcome.cites)
    used = sum(len(o.content) for o in outcomes)
    return {"messages": messages, "traces": traces, "cites": cites, "tool_chars": state["tool_chars"] + used}


def build_chat_graph() -> CompiledStateGraph[ChatState, ChatContext, ChatState, ChatState]:
    graph = StateGraph(ChatState, context_schema=ChatContext)
    graph.add_node("call_model", call_model)
    graph.add_node("run_tools", run_tools)
    graph.add_edge(START, "call_model")
    graph.add_conditional_edges("call_model", after_model, ["run_tools", END])
    graph.add_edge("run_tools", "call_model")
    return graph.compile()


def recursion_limit(max_steps: int) -> int:
    """兜底的递归上限: 每步两个节点 (调模型, 执行工具), 加收尾一步与余量."""
    return 2 * max_steps + 3
