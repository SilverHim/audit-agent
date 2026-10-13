"""把领域异常映射成开发接口文档第 10 节的 HTTP 错误."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import HTTPException

from backend.contracts.errors import DecisionRejected

# 写入时的 NOT_AT_GATE 是 409; GET 闸门由路由单独返回 404, 不走这张表
_STATUS: dict[str, int] = {
    "BAD_CREDENTIALS": 401,
    "NOT_FOUND": 404,
    "TASK_NOT_FOUND": 404,
    "ENGAGEMENT_BUSY": 409,
    "RESUME_REQUIRED": 409,
    "TASK_NOT_PENDING": 409,
    "NOT_AWAITING_REVIEW": 409,
    "VERSION_MISMATCH": 409,
    "DECISION_NOT_ALLOWED": 422,
    "MISSING_FIELD": 422,
    "FIELD_NOT_ALLOWED": 422,
    "INVALID_VALUE": 422,
    "ENGAGEMENT_LOCKED": 423,
}


def api_error(code: str, message: str = "", *, status: int | None = None) -> HTTPException:
    """错误体为 {detail: {code, message}}."""
    resolved = status if status is not None else _STATUS.get(code, 400)
    return HTTPException(status_code=resolved, detail={"code": code, "message": message})


def http_for_decision(exc: DecisionRejected) -> HTTPException:
    """DecisionRejected 按错误码表映射; 写入路径上的 NOT_AT_GATE 为 409."""
    status = 409 if exc.code == "NOT_AT_GATE" else None
    return api_error(exc.code, exc.message, status=status)


@asynccontextmanager
async def catch_api() -> AsyncIterator[None]:
    """路由里包住对编排与存储的调用, 把已知异常转成 HTTP 错误."""
    try:
        yield
    except DecisionRejected as exc:
        raise http_for_decision(exc) from exc
    except LookupError as exc:
        raise api_error("NOT_FOUND", str(exc)) from exc
