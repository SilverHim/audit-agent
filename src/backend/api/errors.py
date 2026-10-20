"""把领域异常映射成开发接口文档第 10 节的 HTTP 错误."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from backend.contracts.errors import DataRejected, DecisionRejected, EngagementLocked

_STATUS: dict[str, int] = {
    "BAD_CREDENTIALS": 401,
    "NOT_FOUND": 404,
    "TASK_NOT_FOUND": 404,
    "ENGAGEMENT_BUSY": 409,
    "NOT_AT_GATE": 409,
    "RESUME_REQUIRED": 409,
    "FEATURES_MISMATCH": 409,
    "NOT_AWAITING_DATA": 409,
    "DATA_NOT_READY": 409,
    "DATA_CHECKS_FAILED": 409,
    "NOT_EDITABLE": 409,
    "TASK_NOT_PENDING": 409,
    "REQUEST_NOT_OPEN": 409,
    "NOT_AWAITING_REVIEW": 409,
    "NOT_RERUNNABLE": 409,
    "XERO_NOT_CONNECTED": 409,
    "XERO_TENANT_REQUIRED": 409,
    "XERO_RECONNECT_REQUIRED": 409,
    "PROFILE_CHECK_STALE": 409,
    "VERSION_MISMATCH": 409,
    "USERNAME_TAKEN": 409,
    "ABN_TAKEN": 409,
    "DECISION_NOT_ALLOWED": 422,
    "MISSING_FIELD": 422,
    "FIELD_NOT_ALLOWED": 422,
    "INVALID_VALUE": 422,
    "IMPORT_PARSE_ERROR": 422,
    "FILE_TOO_LARGE": 413,
    "ENGAGEMENT_LOCKED": 423,
    "SETTINGS_KEY_MISSING": 503,
    "SETTINGS_KEY_INVALID": 503,
    "XERO_NOT_CONFIGURED": 503,
    "XERO_UNAVAILABLE": 502,
}


def api_error(code: str, message: str = "", *, status: int | None = None, **extra: object) -> HTTPException:
    """错误体为 {detail: {code, message}}; extra 并入 detail, 如 DATA_NOT_READY 的 missing_tables."""
    resolved = status if status is not None else _STATUS.get(code, 400)
    return HTTPException(status_code=resolved, detail={"code": code, "message": message, **extra})


def http_for_decision(exc: DecisionRejected) -> HTTPException:
    """DecisionRejected 按错误码表映射. DataRejected 的 extra 并入 detail."""
    extra = exc.extra if isinstance(exc, DataRejected) else {}
    return api_error(exc.code, exc.message, status=None, **extra)


@asynccontextmanager
async def catch_api() -> AsyncIterator[None]:
    """路由里包住对编排与存储的调用, 把已知异常转成 HTTP 错误."""
    try:
        yield
    except DecisionRejected as exc:
        raise http_for_decision(exc) from exc
    except EngagementLocked as exc:
        raise api_error("ENGAGEMENT_LOCKED", str(exc)) from exc
    except LookupError as exc:
        raise api_error("NOT_FOUND", str(exc)) from exc


SECRET_BODY_PATHS = ("/api/v1/settings/", "/api/v1/auth/")  # 请求体可能带密钥或密码的接口: 校验失败时不回显输入


async def validation_error(request: Request, exc: Exception) -> Response:
    """请求体校验失败的 422: 沿用 FastAPI 默认格式; 但 SECRET_BODY_PATHS 下的接口去掉每条错误的 input 与 ctx,
    否则缺字段时会把整个请求体 (含 api_key 或密码) 原样回显给前端."""
    assert isinstance(exc, RequestValidationError)
    if not request.url.path.startswith(SECRET_BODY_PATHS):
        return await request_validation_exception_handler(request, exc)
    errors = [{k: v for k, v in error.items() if k not in ("input", "ctx")} for error in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})
