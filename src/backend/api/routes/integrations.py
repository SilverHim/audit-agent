"""外部系统的授权回调 (需求 R279): Xero 授权后浏览器回到这里. 不要求登录, 靠 state 认出项目与发起人."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse

from backend.api.deps import get_xero
from backend.services.xero_link import CALLBACK_PATH, XeroLink

router = APIRouter(prefix="/integrations", tags=["integrations"])


@router.get("/xero/callback", response_class=RedirectResponse, status_code=302)
async def xero_callback(
    xero: Annotated[XeroLink, Depends(get_xero)],
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    """Xero 授权回调 (XERO_REDIRECT_URI 指向这里): 核对 state (10 分钟内, 只能用一次), 换令牌, 保存该项目的连接
    (令牌加密存库). 成功 302 到前端 /engagements/{eid}/data?xero=connected; 失败 302 到同一页 (认不出项目时为前端
    首页) 带 ?xero=error&reason=<原因代码>, 代码见 services.xero_link.CALLBACK_REASONS. 跳转地址不带 code 与 state."""
    return RedirectResponse(await xero.callback(code, state, error), status_code=302)


class RedactCallbackQuery(logging.Filter):
    """访问日志 (uvicorn.access) 里抹掉授权回调的查询串 (带 code 与 state), 其余请求原样记. 按去掉查询串后的路径
    以回调路径结尾判断: uvicorn 记的路径含 root_path, 部署在反向代理前缀下 (--root-path) 时前面多一段."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str) and "?" in args[2]:
            path = args[2].split("?", 1)[0]
            if path.endswith(CALLBACK_PATH):
                record.args = (*args[:2], path + "?<redacted>", *args[3:])
        return True


def install_log_filter() -> None:
    """给 uvicorn.access 装上 RedactCallbackQuery (只装一次)."""
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactCallbackQuery) for f in access.filters):
        access.addFilter(RedactCallbackQuery())
