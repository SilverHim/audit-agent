"""LLM 客户端（A 负责）。mock 模式读 fixtures，live 模式接真实提供商（选型未定）。"""

from pathlib import Path

from pydantic import BaseModel, ValidationError

from backend.contracts.ports import LLMClient, LLMRequest, LLMResponse, T_out
from backend.core.config import Settings


class MockLLMClient:
    """读 {fixtures}/{procedure_id}/{fixture_key}.json，找不到读 default.json。"""

    def __init__(self, fixtures_root: Path) -> None:
        self.root = fixtures_root

    async def complete(self, req: LLMRequest, output_model: type[T_out]) -> LLMResponse[T_out]:
        folder = self.root / req.procedure_id
        path = next((p for p in (folder / f"{req.fixture_key}.json", folder / "default.json") if p.exists()), None)
        if path is None:
            return LLMResponse[output_model](  # type: ignore[valid-type]
                output=None, raw="", model="mock", input_tokens=0, output_tokens=0, error="fixture_not_found"
            )
        raw = path.read_text(encoding="utf-8")
        try:
            output: BaseModel | None = output_model.model_validate_json(raw)
            error = None
        except ValidationError as e:
            output, error = None, str(e)
        return LLMResponse[output_model](  # type: ignore[valid-type]
            output=output, raw=raw, model="mock", input_tokens=0, output_tokens=0, error=error
        )


class LiveLLMClient:
    """真实提供商，M3 前定选型后实现。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def complete(self, req: LLMRequest, output_model: type[T_out]) -> LLMResponse[T_out]:
        raise NotImplementedError("LLM 选型未定")


def make_llm_client(settings: Settings) -> LLMClient:
    if settings.LLM_MODE == "mock":
        return MockLLMClient(settings.LLM_FIXTURES_ROOT)
    return LiveLLMClient(settings)
