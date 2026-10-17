"""审计助理对话: GET, POST /engagements/{eid}/conversation 与 POST .../conversation/stream.

只读问答 (规格书 5.2 节, UC-08): 回答不写任何判断字段, 不改流程状态. 已锁定 (归档) 的项目照样能问.
流式接口返回 text/event-stream: delta (多条), tool (可选, 正在查什么), citations, done; 出错为 error.
项目不存在 404, 问题超长或为空 422, 正在回答上一个问题 409 CONVERSATION_BUSY, 都在开始输出之前返回.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from backend.agents.concierge_chat.service import ChatTurn, ConciergeChat, ConversationBusy, ConversationFailed
from backend.api.deps import current_user, owned_engagement
from backend.api.errors import api_error, catch_api
from backend.contracts.conversation import ConciergeAnswer, ConciergeQuestion, ConversationTurn
from backend.contracts.models import Engagement, User

router = APIRouter(prefix="/engagements/{eid}", tags=["conversation"])

# 一次性问答失败的状态码: 模型不可用等上游问题 502, 主密钥问题 503, 其余按错误码表
FAILED_STATUS = {
    "LLM_UNAVAILABLE": 502,
    "LLM_SECRET_ECHO": 502,
    "LLM_CONTEXT_OVERFLOW": 502,
    "CONVERSATION_FAILED": 500,
}


def get_concierge(request: Request) -> ConciergeChat:
    concierge: ConciergeChat = request.app.state.concierge
    return concierge


@router.get("/conversation")
async def list_conversation(
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    concierge: Annotated[ConciergeChat, Depends(get_concierge)],
) -> list[ConversationTurn]:
    """历史问答, 按时间升序; 取自 conversation 留痕, 不含压缩摘要."""
    async with catch_api():
        return await concierge.history(engagement.id)


@router.post("/conversation")
async def ask_concierge(
    body: ConciergeQuestion,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    user: Annotated[User, Depends(current_user)],
    concierge: Annotated[ConciergeChat, Depends(get_concierge)],
) -> ConciergeAnswer:
    """一次性返回回答. 与流式接口的回答和引用规则相同."""
    try:
        return await concierge.ask(engagement.id, user.id, body)
    except ConversationBusy:
        raise api_error("CONVERSATION_BUSY", "正在回答上一个问题", status=409) from None
    except ConversationFailed as e:
        raise api_error(e.code, e.message, status=FAILED_STATUS.get(e.code)) from None


@router.post(
    "/conversation/stream",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}, "description": "SSE 事件流"}},
)
async def stream_concierge(
    body: ConciergeQuestion,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    user: Annotated[User, Depends(current_user)],
    concierge: Annotated[ConciergeChat, Depends(get_concierge)],
) -> StreamingResponse:
    """流式返回回答 (事件见模块说明). 审计师中途断开时, 已生成的部分记入留痕并标 aborted."""
    try:
        turn = concierge.begin(engagement.id, user.id, body)
    except ConversationBusy:
        raise api_error("CONVERSATION_BUSY", "正在回答上一个问题", status=409) from None
    return TurnStream(turn)


class TurnStream(StreamingResponse):
    """一轮问答的 SSE 响应: 响应结束时 (正常结束, 发送失败, 响应任务被取消) 通知这一轮并关闭事件生成器.

    Starlette 在 ASGI 2.4 下发送失败 (连接已断, send 抛 OSError) 时不关闭 body_iterator, 生成器停在 yield 上,
    靠它的 finally 通知断开要等到垃圾回收. 这里不依赖它: 事件已全部送达时 client_gone 不做任何事.
    """

    def __init__(self, turn: ChatTurn) -> None:
        super().__init__(
            turn.sse(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        )
        self.turn = turn

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.turn.client_gone()
            close = getattr(self.body_iterator, "aclose", None)
            if close is not None:
                await close()
