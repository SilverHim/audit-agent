"""GET /settings/llm; PUT /settings/llm/{slot}; POST /settings/llm/{slot}/test: 设置页的模型配置.

登录用户都能读写 (目前只有审计师一种角色). 密钥原文不返回, 也不写日志.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import APIRouter, Depends

from backend.api.deps import current_user, get_settings_service
from backend.api.errors import api_error
from backend.api.schemas import LlmSettingsView, LlmSlotIn, LlmSlotView, LlmTestResult
from backend.contracts.models import LlmSlot, User
from backend.core.security import SecretKeyError
from backend.services.llm_settings import LlmSettingsService, SettingsRejected, SlotView

router = APIRouter(prefix="/settings", tags=["settings"])


@asynccontextmanager
async def catch_settings() -> AsyncIterator[None]:
    """校验不过转 422, 主密钥不可用转 503 (见错误码表)."""
    try:
        yield
    except (SettingsRejected, SecretKeyError) as exc:
        raise api_error(exc.code, exc.message) from exc


def slot_view(view: SlotView) -> LlmSlotView:
    return LlmSlotView.model_validate(view.__dict__)


@router.get("/llm")
async def get_llm_settings(
    user: Annotated[User, Depends(current_user)],
    service: Annotated[LlmSettingsService, Depends(get_settings_service)],
) -> LlmSettingsView:
    return LlmSettingsView(
        primary=slot_view(await service.view("primary")), review=slot_view(await service.view("review"))
    )


@router.put("/llm/{slot}")
async def update_llm_slot(
    slot: LlmSlot,
    body: LlmSlotIn,
    user: Annotated[User, Depends(current_user)],
    service: Annotated[LlmSettingsService, Depends(get_settings_service)],
) -> LlmSlotView:
    """保存后下一次模型调用起用新配置. 缺主密钥时 503 SETTINGS_KEY_MISSING."""
    async with catch_settings():
        view = await service.update(
            slot,
            provider=body.provider,
            base_url=body.base_url,
            model=body.model,
            api_key=body.api_key,
            updated_by=user.id,
        )
    return slot_view(view)


@router.post("/llm/{slot}/test")
async def test_llm_slot(
    slot: LlmSlot,
    body: LlmSlotIn,
    user: Annotated[User, Depends(current_user)],
    service: Annotated[LlmSettingsService, Depends(get_settings_service)],
) -> LlmTestResult:
    """用还没保存的值测试连接; api_key 为空时用库里存的密钥."""
    async with catch_settings():
        result = await service.test(
            slot, provider=body.provider, base_url=body.base_url, model=body.model, api_key=body.api_key
        )
    return LlmTestResult.model_validate(result.__dict__)
