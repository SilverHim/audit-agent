"""知识文件的模型: 程序 YAML 与准则片段, 定义见 开发接口文档.md 第 8 节."""

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
    """程序的装配条件: 某科目某认定的风险等级落在 level 内时装配该程序."""

    account: str  # 科目
    assertions: list[Assertion]  # 认定
    level: list[RiskLevel]  # 触发装配的风险等级


class RuleSpec(Model):
    """程序 YAML 里的一条规则 SQL."""

    id: str  # 规则编号, 如 R01
    name: str  # 规则名称
    severity: Literal["low", "medium", "high"]  # 命中时例外的严重程度
    emit: Literal["exception", "rows"] = "exception"  # exception 每行出一条例外, rows 原样返回行
    sql: str  # SQL, 参数用 :name 绑定; 由 B 编写


class LLMSpec(Model):
    """LLM 程序的调用配置."""

    prompt: str  # knowledge/prompts/ 下的 Jinja2 模板文件名
    output_model: str  # contracts/llm_outputs.py 中的输出模型类名
    context_budget: int = 8000  # 上下文 token 预算
    max_tokens: int = 4096  # 单次调用最多输出的 token 数, 程序 YAML 可覆盖


class ProcedureTemplate(Model):
    """审计程序模板, 对应 knowledge/procedures/ 下的一个 YAML."""

    id: str  # 程序编号
    name: str  # 程序名称
    phase: Phase  # 所属阶段
    cycle: str  # 所属循环, 如 Revenue, Cash
    owner: AgentId  # 执行它的智能体
    engine: Engine  # 执行方式
    autonomy_level: AutonomyLevel  # 默认自主度
    always_review: bool = False  # 判断一律建 normal 复核任务, 不论自主度 (CSH-02: 函证差异须人评价, ASA 505 para 14)
    manual_result_allowed: bool = False  # 单个判断对象失败时, 允许人工执行并提交结果 (manual_result)
    gate: GateId | None = None  # 闸门程序的闸门编号, 其余为空
    assertions: list[Assertion] = []  # 针对的认定
    triggers: list[Trigger] = []  # 装配条件; 为空时 P2 以外为固定集, P2 只经依赖加入
    params: dict[str, Any] = {}  # 参数: 字面量, {by_risk: ...} 或 {ref: 名称}
    depends_on: list[str] = []  # 前置程序
    standards: list[str] = []  # 引用的准则片段 ID
    evidence_required: list[EvidenceType] = []  # 需要取得的证据种类, 用于算证据覆盖率
    rules: list[RuleSpec] = []  # 规则 SQL, 规则与混合程序用
    llm: LLMSpec | None = None  # LLM 调用配置, LLM 与混合程序用


class StandardFragment(Model):
    """准则片段: 某准则某段落的原文摘录, 供推理链引用."""

    id: str  # 片段 ID, 如 AASB15-P31
    standard: str  # 准则名, 如 AASB 15
    paragraph: str  # 段落号
    topic: str  # 主题, 如 control_transfer
    text: str  # 原文摘录
    procedures: list[str]  # 引用它的程序
