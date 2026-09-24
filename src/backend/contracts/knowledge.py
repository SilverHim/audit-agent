"""知识文件的模型：程序 YAML 与准则片段，定义见 开发接口文档.md 第 8 节。"""

from typing import Any, Literal

from backend.contracts.models import (
    AgentId,
    Assertion,
    AutonomyLevel,
    Engine,
    EvidenceType,
    GateId,
    Model,
    Phase,
    RiskLevel,
)


class Trigger(Model):
    account: str
    assertions: list[Assertion]
    level: list[RiskLevel]


class RuleSpec(Model):
    id: str
    name: str
    severity: Literal["low", "medium", "high"]
    emit: Literal["exception", "rows"] = "exception"
    sql: str


class LLMSpec(Model):
    prompt: str
    output_model: str
    context_budget: int = 8000


class ProcedureTemplate(Model):
    id: str
    name: str
    phase: Phase
    cycle: str
    owner: AgentId
    engine: Engine
    autonomy_level: AutonomyLevel
    gate: GateId | None = None
    assertions: list[Assertion] = []
    triggers: list[Trigger] = []
    params: dict[str, Any] = {}
    depends_on: list[str] = []
    standards: list[str] = []
    evidence_required: list[EvidenceType] = []
    rules: list[RuleSpec] = []
    llm: LLMSpec | None = None


class StandardFragment(Model):
    id: str
    standard: str
    paragraph: str
    topic: str
    text: str
    procedures: list[str]
