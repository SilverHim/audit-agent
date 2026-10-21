"""知识文件的模型: 程序 YAML, 准则片段与底稿配置, 定义见 开发接口文档.md 第 8 节."""

from typing import Any, Literal, Self

from pydantic import Field, field_validator, model_validator

from backend.contracts.data import DataInput, TableKind
from backend.contracts.models import (
    AgentId,
    Assertion,
    AutonomyLevel,
    DocumentType,
    DowngradeCondition,
    Engine,
    EvidenceType,
    GateId,
    MisstatementUpdateKind,
    Model,
    OpinionType,
    Phase,
    ReportSection,
    RiskLevel,
    RuleOutcomeStatus,
)
from backend.contracts.records import AccountCategory


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


class LocalizedText(Model):
    """中英两种写法的文字 (需求 R324), 两种都须给出, 去掉首尾空白后不能为空 (原文不改)."""

    zh: str  # 中文
    en: str  # 英文

    @field_validator("zh", "en")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("不能为空或只有空白")
        return value


class RulePopulationSpec(Model):
    """emit exception 的规则的被测总体 (需求 R324): 另一段 SQL 取该规则核对的全部对象 (例外只是其中没通过的).

    SQL 每个对象一行, 列的取法同 emit rows 的返回行 (rule_engine.rule_population): source_type 为对象类型, source_id
    为行 ID, amount_cents 为金额, 其余列为识别字段; 绑定参数同规则 SQL, 只能用 params 中的与 eid (加载时校验)."""

    sql: str  # SQL, 由 B 编写
    description: LocalizedText  # 筛选说明, 可用 Jinja2 写参数占位, 按参数快照渲染 (procedure_library.render_text)


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
    # 舞弊红旗 (需求 R168, 功能 autonomy): 判断解释的例外出自标了它的规则时, 判断级降级直接降到 L1 (fraud_flag 条件)
    fraud_flag: bool = False
    # 被测总体 (需求 R324): 只给 emit exception 的规则写, emit rows 的规则以返回行为总体; 没写的 exception 规则总体记为
    # 未记录 (RulePopulation.count 为空)
    population: RulePopulationSpec | None = None
    check: LocalizedText | None = None  # 检查项名称, 如 "入账期间正确" (底稿的检查列), 不带占位


DowngradeAt = Literal["assembly", "judgment"]  # 降级的时点: 装配 (写进 Procedure) 或判断 (写进 Judgment)
# 各条件只在一个时点看 (规格书 7.4 "时点" 一列): 装配看风险等级, 判断看金额, 置信度, 推理链与舞弊迹象
DOWNGRADE_AT: dict[DowngradeCondition, DowngradeAt] = {
    DowngradeCondition.significant_risk: "assembly",
    DowngradeCondition.high_risk: "assembly",
    DowngradeCondition.amount_over_pm: "judgment",
    DowngradeCondition.low_confidence: "judgment",
    DowngradeCondition.unsupported_steps: "judgment",
    DowngradeCondition.fraud_flag: "judgment",
}


class DowngradeRule(Model):
    """自主度动态降级的一条规则 (规格书 7.4, 12.2 DowngradeRule; 需求 R168): 命中条件时降几级 (delta, 负数) 或直接降到
    哪一级 (target_level), 二者恰给一个. 默认表在 knowledge/rules.json 的 autonomy 段, 程序 YAML 的 downgrade_rules
    按条件覆盖. 只降不升: delta 须为负, target_level 高于当前级别时不改; 越过 L1 时截在 L1."""

    condition: DowngradeCondition  # 条件
    at: DowngradeAt  # 时点, 须与条件的时点一致 (DOWNGRADE_AT)
    delta: int | None = None  # 降几级, 负数
    target_level: AutonomyLevel | None = None  # 直接降到哪一级

    @model_validator(mode="after")
    def one_adjustment(self) -> Self:
        """delta 与 target_level 恰给一个, delta 为负; 时点与条件一致."""
        if (self.delta is None) == (self.target_level is None):
            raise ValueError(f"{self.condition}: delta 与 target_level 须恰给一个")
        if self.delta is not None and self.delta >= 0:
            raise ValueError(f"{self.condition}: delta 须为负数 (只降不升), 不能为 {self.delta}")
        if self.at != DOWNGRADE_AT[self.condition]:
            raise ValueError(f"{self.condition} 在 {DOWNGRADE_AT[self.condition]} 时看, 不能写 at: {self.at}")
        return self


class LLMSpec(Model):
    """LLM 程序的调用配置."""

    prompt: str  # knowledge/prompts/ 下的 Jinja2 模板文件名
    output_model: str  # contracts/llm_outputs.py 中的输出模型类名
    context_budget: int = 8000  # 上下文 token 预算
    # 单次调用最多输出的 token 数, 程序 YAML 可覆盖. 推理类模型 (如 gpt-6 系列) 的上限含推理 token: 4096 时 GEN-02
    # 的推理就用光了上限, 正文为空; 32768 留足余量, 只封顶, 不增加实际用量 (需求 R312)
    max_tokens: int = 32768


class ManualSpec(Model):
    """人工程序 (engine manual 且不是闸门) 交给人的要求 (需求 R92): 派发时抄进 manual 任务的请求 (ManualRequest)."""

    instructions: str  # 给执行人的说明
    result_model: str | None  # 结构化结果的模型名, 须在 contracts/manual_results.RESULT_MODELS 登记; 为空不收结构化结果
    min_evidence: int = Field(default=1, ge=0)  # 至少要附的证据文件指针数, 不小于 0
    subject: str = "RESULT"  # manual 任务的 subject_id


class WorkpaperRef(Model):
    """程序的底稿 (需求 R329): 索引号与所属区, 写在程序 YAML 的 workpaper 一行. 规则见 workpapers.yaml 与
    procedure_library.workpaper_problems."""

    ref: str  # 索引号, 如 A-2, X (FIN-04 的错报汇总), X-A
    area: str  # 区字母, 须在底稿配置的 areas 中


class ProcedureTemplate(Model):
    """审计程序模板, 对应 knowledge/procedures/ 下的一个 YAML."""

    id: str  # 程序编号
    name: str  # 程序名称
    # 范围说明 (需求 R324), 如 "期末前 20 天 (2026-09-10 至 2026-09-30) 入账的销售发票": 可用 Jinja2 写参数占位, 按
    # 参数快照渲染 (procedure_library.render_text); 为空即不写
    scope: LocalizedText | None = None
    phase: Phase  # 所属阶段
    cycle: str  # 所属循环, 如 Revenue, Cash
    owner: AgentId  # 执行它的智能体
    outputs: list[str] = []  # 本程序写的状态字段 (须在 owner 的写入范围内); 合并校验与重放只认这些
    document_types: list[DocumentType] = []  # outputs 含 documents 时本程序交回的文件类型; 重放只取这些类型的文件
    reads: list[DataInput] = []  # 本程序读的数据; 其中任一类在最近一次执行之后变过, 程序即过期 (services.staleness)
    # reads 含 documents (读文件登记) 时读哪几类文件的登记 (需求 R304), 两者须同时写; 只管过期: 带文件类型的登记变动
    # 只让读该类的程序过期. 与 document_types 各管各的: 那是产出侧 (本程序交回的文件, 只管重放), 这是读取侧. 例如
    # REV-03 读合同的登记而不交回文件, CSH-04 交回回函而不读登记
    document_reads: list[DocumentType] = []
    engine: Engine  # 执行方式
    autonomy_level: AutonomyLevel  # 默认自主度
    always_review: bool = False  # 判断一律建 normal 复核任务, 不论自主度 (CSH-02: 函证差异须人评价, ASA 505 para 14)
    manual_result_allowed: bool = False  # 单个判断对象失败时, 允许人工执行并提交结果 (manual_result)
    # 本程序的判断携带的错报更新载荷 (需求 R159): classification (FIN-03) 或 verification (FIN-09), 其余为空. 非空时以
    # 人工判断取代 AI 判断的决定须由人给出同类载荷 (decisions.review_fields), 判断确认后按载荷写错报新版本
    misstatement_update_kind: MisstatementUpdateKind | None = None
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
    manual: ManualSpec | None = None  # 人工程序交给人的要求, 人工程序须有, 其余程序不能写 (加载时校验)
    # 按条件覆盖默认降级表 (rules.json 的 autonomy 段, 需求 R168); 同一条件只写一条, 没写的条件照默认
    downgrade_rules: list[DowngradeRule] = []
    workpaper: WorkpaperRef | None = None  # 本程序的底稿 (需求 R329); 不单独出底稿的程序 (GEN-09) 为空

    @model_validator(mode="after")
    def unique_downgrade_conditions(self) -> Self:
        """downgrade_rules 中同一条件只写一条."""
        conditions = [r.condition for r in self.downgrade_rules]
        repeated = sorted({c for c in conditions if conditions.count(c) > 1})
        if repeated:
            raise ValueError(f"{self.id}: downgrade_rules 的条件重复: {', '.join(repeated)}")
        return self


class StandardFragment(Model):
    """准则片段: 某准则某段落的原文摘录, 供推理链引用."""

    id: str  # 片段 ID, 如 AASB15-P31
    standard: str  # 准则名, 如 AASB 15
    paragraph: str  # 段落号, 如 "33", "6(b)(i)", "Aus 13.2", "A44", "R410.15", "s307C(3)"
    subitems: list[str] = []  # 以整段为引用单位而只摘部分子项时所摘的子项 (如 [a]); paragraph 写整段号, id 不带子项
    topic: str  # 主题, 如 control_transfer
    text: str  # 原文摘录
    procedures: list[str]  # 引用它的程序


# 按程序模板分的底稿类别 (procedure_library.workpaper_kind): rule 为规则程序, judgment 为判断程序
WorkpaperKind = Literal["rule", "judgment"]


class WorkpaperArea(Model):
    """底稿的区 (需求 R329)."""

    id: str  # 区字母, 如 A
    name: LocalizedText
    # 程序底稿索引号的序号写法: digits 为 {区}-1 起, letters 为 {区}-A 起 (错报区 X: X-数字留给错报汇总表中的错报编号).
    # 区字母本身 (如 X) 也可作索引号, 与系统底稿不重复即可
    numbering: Literal["digits", "letters"] = "digits"


class SystemWorkpaper(Model):
    """系统生成, 不对应程序的底稿 (00 索引, 导引表 C, D, S 准则对照)."""

    ref: str  # 索引号
    name: LocalizedText
    area: str | None = None  # 导引表属其区 (索引号即区字母); 索引与准则对照不属任何区
    categories: list[AccountCategory] = []  # 导引表取试算表中这些标准分类的科目 (Q29); 其余为空


class RequiredItem(Model):
    """各类底稿必须记录的一项 (ASA 230 第 8, 9 段与各准则). kinds 与 refs 都为空即全部底稿, 否则为其并集
    (ProcedureLibrary.required_items)."""

    id: str  # 代号, 如 preparer
    name: LocalizedText
    standard: str  # 出处的准则, 如 ASA 230
    paragraph: str  # 出处的段号 (现行), 如 9(b)
    supplementable: bool  # 缺了时能否由审计师的补充说明补齐 (需求 R330): 理由, 考虑因素与结论可以; 编制, 复核与事实不行
    kinds: list[WorkpaperKind] = []  # 只对这几类程序的底稿
    refs: list[str] = []  # 只对这几份底稿 (索引号)


class StandardRequirement(Model):
    """准则文档要求对照的一行 (ASA 230 附录 1 所列, 段号按各准则现行文本)."""

    standard: str  # 准则, 如 ASA 240
    paragraphs: str  # 现行段号, 如 45-48
    content: LocalizedText  # 要求记录的内容
    refs: list[str] = []  # 对应的底稿 (索引号)
    areas: list[str] = []  # 另对应这些区的全部底稿 (程序底稿与导引表, ProcedureLibrary.standard_refs)
    note: LocalizedText | None = None  # 说明 (如 ASA 330 第 30 段由导引表与试算表的勾稽支持)
    not_applicable: LocalizedText | None = None  # 不适用的条件 (如 ASA 600: 单一主体)


class WorkpaperConfig(Model):
    """底稿配置 (knowledge/workpapers.yaml, 需求 R329, 接口文档第 8.1 节 "底稿配置")."""

    areas: list[WorkpaperArea]  # 区, 按档案中的顺序
    system: list[SystemWorkpaper]  # 不对应程序的系统底稿
    required: list[RequiredItem]  # 必填项
    standards_note: LocalizedText  # 准则对照的说明一行
    standards: list[StandardRequirement]  # 准则对照, 按准则编号


class ReportHeadings(Model):
    """审计报告意见段与形成意见的基础段首行的标题 (ASA 700 第 23, 28 段; ASA 705 第 16, 20(a) 段)."""

    opinion: str  # 意见段, 如 Opinion, Qualified Opinion
    basis_for_opinion: str  # 形成意见的基础段, 如 Basis for Opinion


class ReportTemplate(Model):
    """templates/report.yaml 中一种工作语言的一份 (需求 R341; 文件按 WorkingLanguage 各一份, 结构相同, 见
    ProcedureLibrary). RPT-03 的提示词与校验取 headings, 报告正文的日期按 date 写, 模型写两段时的名称按 terms 写,
    拼在模型写的两段之后的固定段落取 sections."""

    headings: dict[OpinionType, ReportHeadings]  # 意见类型 → 两段的标题
    date: str  # 报告中的日期写法: str.format 的模板, d 为日期, 如 "{d.day} {d:%B %Y}"
    # 模型写的两段里的名称, 与固定段落的写法相同 (R341 审核 03): 键 → 写法, 如 corporations_act → Corporations Act 2001
    terms: dict[str, str]
    sections: dict[OpinionType, dict[ReportSection, str]]  # 意见类型 → 固定段落 (每段首行为标题, 签名块除外; 含占位符)
