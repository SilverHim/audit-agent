"""判断引擎与输出校验 (A 负责). 规则见 开发接口文档.md 第 5 节."""

from pydantic import BaseModel

from backend.contracts.knowledge import ProcedureTemplate
from backend.contracts.models import ProcedureRun
from backend.contracts.services import JudgmentContext, JudgmentOutcome, ValidationReport


class OutputValidator:
    """LLM 输出的四级校验: 语法, 结构, 引用, 语义."""

    async def validate(self, eid: str, template: ProcedureTemplate, output: BaseModel) -> ValidationReport:
        raise NotImplementedError("四级校验: 语法, 结构, 引用, 语义")


class JudgmentEngine:
    """判断引擎: 组装上下文, 调大模型, 校验输出, 算置信度, 产出 Judgment."""

    async def judge(
        self, eid: str, template: ProcedureTemplate, run: ProcedureRun, ctx: JudgmentContext, performed_by: str
    ) -> JudgmentOutcome:
        raise NotImplementedError("组装上下文 → LLMClient → OutputValidator (重试 2 次)→ 置信度")
