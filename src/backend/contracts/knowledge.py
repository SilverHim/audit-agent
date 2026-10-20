"""知识文件的模型: 程序 YAML 与准则片段, 定义见 开发接口文档.md 第 8 节."""

from typing import Any, Literal

from backend.contracts.data import DataInput, TableKind
from backend.contracts.models import (
    AgentId,
    Assertion,
    AutonomyLevel,
    DocumentType,
    Engine,
    EvidenceType,
    GateId,
    Model,
    Phase,
    RiskLevel,
    RuleOutcomeStatus,
)


class Trigger(Model):
    """程序的装配条件: 某科目某认定的风险等级落在 level 内时装配该程序."""

    account: str  # 科目
    assertions: list[Assertion]  # 认定
    level: list[RiskLevel]  # 触发装配的风险等级


class ProcedureInputs(Model):
    """程序执行前必须有的客户资料 (需求 R272): 缺了程序不执行, 记等待数据, 由 A3 提补交请求.

    就绪必需的三张表 (科目表, 试算表, 总账) 由 P1 等数据保证, 不写在这里; 前置程序的结果 (合同抽取, 回函) 由
    depends_on 表达. 写法在程序库加载时校验 (services.procedure_library.inputs_problems), 只有 P2 以外的程序能否
    补跑在 GraphNodes 构造时查 (orchestration.inputs.input_problems)."""

    tables: list[TableKind] = []  # 账表: 有任意一行即算有
    documents: list[DocumentType] = []  # 文件类型: 有一份该类型的文件即算有


class RuleCheck(Model):
    """规则的一项执行前检查 (需求 R130): SQL 返回行即不执行该规则, 记 status 与第一行的 reason 列."""

    status: RuleOutcomeStatus  # 命中时记的原因: skipped 或 not_executable
    sql: str  # SQL, 参数同规则 SQL; 返回行即命中, 理由取第一行的 reason 列 (固定理由写字面量)


class RuleSpec(Model):
    """程序 YAML 里的一条规则 SQL."""

    id: str  # 规则编号, 如 R01
    name: str  # 规则名称
    severity: Literal["low", "medium", "high"]  # 命中时例外的严重程度
    emit: Literal["exception", "rows"] = "exception"  # exception 每行出一条例外, rows 原样返回行
    sql: str  # SQL, 参数用 :name 绑定; 由 B 编写
    checks: list[RuleCheck] = []  # 执行前检查, 按顺序执行, 第一个返回行的决定不执行 (需求 R130); 为空即总是执行


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
    outputs: list[str] = []  # 本程序写的状态字段 (须在 owner 的写入范围内); 合并校验与重放只认这些
    document_types: list[DocumentType] = []  # outputs 含 documents 时本程序交回的文件类型; 重放只取这些类型的文件
    reads: list[DataInput] = []  # 本程序读的数据; 其中任一类在最近一次执行之后变过, 程序即过期 (services.staleness)
    engine: Engine  # 执行方式
    autonomy_level: AutonomyLevel  # 默认自主度
    always_review: bool = False  # 判断一律建 normal 复核任务, 不论自主度 (CSH-02: 函证差异须人评价, ASA 505 para 14)
    manual_result_allowed: bool = False  # 单个判断对象失败时, 允许人工执行并提交结果 (manual_result)
    gate: GateId | None = None  # 闸门程序的闸门编号, 其余为空
    feature: str | None = None  # 所属功能 (services/features.FEATURES 中的名字); 为空即基线, 总是启用
    assertions: list[Assertion] = []  # 针对的认定
    triggers: list[Trigger] = []  # 装配条件; 为空时 P2 以外为固定集, P2 只经依赖加入 (标 fixed 的除外)
    # P2 固定集: PLN-01/03 通过后不论审计目标一律装配 (所属功能未启用时不装配) 并补齐依赖, objective_ids 为空,
    # 不计入审计目标覆盖; 只用于 P2 程序, 参数不能按风险等级取值
    fixed: bool = False
    params: dict[str, Any] = {}  # 参数: 字面量, {by_risk: ...} 或 {ref: 名称}
    depends_on: list[str] = []  # 前置程序
    inputs: ProcedureInputs = ProcedureInputs()  # 执行前必须有的客户资料, 缺了等数据 (需求 R272)
    standards: list[str] = []  # 引用的准则片段 ID
    evidence_required: list[EvidenceType] = []  # 需要取得的证据种类, 用于算证据覆盖率
    rules: list[RuleSpec] = []  # 规则 SQL, 规则与混合程序用
    llm: LLMSpec | None = None  # LLM 调用配置, LLM 与混合程序用


class StandardFragment(Model):
    """准则片段: 某准则某段落的原文摘录, 供推理链引用."""

    id: str  # 片段 ID, 如 AASB15-P31
    standard: str  # 准则名, 如 AASB 15
    paragraph: str  # 段落号, 如 "33", "6(b)(i)", "Aus 13.2", "A44", "R410.15", "s307C(3)"
    subitems: list[str] = []  # 以整段为引用单位而只摘部分子项时所摘的子项 (如 [a]); paragraph 写整段号, id 不带子项
    topic: str  # 主题, 如 control_transfer
    text: str  # 原文摘录
    procedures: list[str]  # 引用它的程序
