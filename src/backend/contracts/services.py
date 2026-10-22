"""服务之间传递的数据对象, 定义见 开发接口文档.md 第 5 节."""

from datetime import date
from typing import Any, Generic, Literal, TypeVar

from pydantic import SerializeAsAny

from backend.contracts.llm_outputs import JudgmentDraft
from backend.contracts.models import (
    AuditException,
    AutonomyLevel,
    ClientProfile,
    Confirmation,
    DocumentRef,
    EvidencePointer,
    Judgment,
    Model,
    OpinionType,
    RuleOutcome,
    RulePopulation,
    WorkingLanguage,
)

T = TypeVar("T")


class RuleResult(Model):
    """RuleEngine 执行一条程序全部规则的结果."""

    exceptions: list[AuditException]  # emit = exception 的规则产出的例外
    rows: dict[str, list[dict[str, Any]]]  # emit = rows 的规则返回的行, 按规则编号; 没有执行的规则不在其中
    outcomes: list[RuleOutcome] = []  # 执行前检查命中而没有执行的规则, 按 YAML 顺序 (需求 R130)
    populations: list[RulePopulation] = []  # 执行了的规则的被测总体, 按 YAML 顺序 (需求 R324); 没有执行的规则不记


class JudgmentContext(Model):
    """送给 JudgmentEngine 的一个判断对象的上下文."""

    subject_id: str  # 判断对象 ID
    task: dict[str, Any]  # 任务说明: 参数, 覆盖的审计目标与风险
    data: dict[str, Any]  # 该判断需要的数据行与抽取要素
    rule_findings: list[AuditException] = []  # 该对象的规则例外 (混合程序), 本程序与前置程序的都算
    fixture_key: str  # mock 模式下取固定输出用的键
    autonomy_level: AutonomyLevel | None = None  # 判断的自主度: 程序的运行时级别 (Procedure); 为空时取模板
    # 本项目采用版本的客户画像 (需求 R128): 给了时推理链可引用它 (client_profile 指针), 校验时原样交给 OutputValidator.
    # judge 不把它放进提示词 (提示词只看 data 等变量), 调用方须同时把画像 (ID 与所需字段) 放进 data, 模型才引用得到
    client_profile: ClientProfile | None = None
    # FIN-09 (需求 R164): 规则算出的剩余额与匹配凭证各行的科目中错报科目以外的 (见 ValidationContext), judge 原样放进
    # ValidationContext; 其余程序为空
    expected_residual_cents: int | None = None
    entry_account_codes: list[str] | None = None


class JudgmentOutcome(Model):
    """JudgmentEngine 对一个判断对象的产出."""

    judgment: Judgment | None  # 判断, 失败时为空
    failure: Literal["failed", "unattributed"] | None  # 失败类型, 成功时为空
    attempts: int  # 调用大模型的次数, 含重试
    trail_ids: list[str]  # 每次大模型调用的留痕 ID
    # 通过校验的草稿 (指针已按来源改写), 失败时为空 (需求 R157). 输出模型为 JudgmentDraft 的子类时原样带回 (如 FIN-03
    # 的 MisstatementClassificationDraft), 调用方从中取判断之外的项 (错报的性质); 序列化按实际的类, 子类字段不丢
    draft: SerializeAsAny[JudgmentDraft] | None = None


class ValidationContext(Model):
    """OutputValidator 校验一次 LLM 输出时用到的上下文."""

    rule_findings: list[AuditException] = []  # 该判断对象的规则例外, 即判断归因的例外; 错报金额可等于其金额
    document: DocumentRef | None = None  # 抽取类程序 (DOC-04, DOC-06) 本次抽取的文件, 指针只能指向它
    # RPT-03 可引用的判断 (当前结果中错报所依据的已确认判断), 引用键据此生成; FIN-02 为状态中的判断, judgment_refs 须为
    # 其中当前版且已确认的 (需求 R183)
    judgments: list[Judgment] = []
    required_refs: list[str] = []  # FIN-02 的 judgment_refs 必须含的判断 ID (当前有效的持续经营评价判断, 需求 R183)
    confirmation: Confirmation | None = None  # DOC-06 本次抽取的函证 (CSH-04 所建), 核对回函账号与回函渠道
    opinion: OpinionType | None = None  # RPT-03 应出的意见类型 (FIN-01 选定), 核对两段的标题
    # 输出的语言 (需求 R341): JudgmentEngine.call_llm 按执行记录的 language 填, 调用方不填; RPT-03 按它取两段的标题
    # (templates/report.yaml). 不经 call_llm 直接校验时缺省 en
    language: WorkingLanguage = "en"
    # 记账币种 (需求 R365, R366): 判断草稿的结论里的金额须按它写; call_llm 按项目当前币种填, 直接校验时缺省澳元
    currency: str = "AUD"
    # GEN-09 第一轮给独立复核的证据指针: 其推理链只能引用这些 (按来源比对, 文件还须同页, 画像还须同字段), 否则该步
    # unsupported
    allowed_evidence: list[EvidencePointer] | None = None
    # GEN-09 第一轮是否核对复核方的建议动作与错报草稿 (功能 collab 打开时, 需求 R150); 问题记进报告的 notes
    critic_drafts: bool = False
    # 本项目采用版本的客户画像 (需求 R128): client_profile 指针只认它 (ID 相同, locator 为其顶层字段名), 不读
    # EngagementStore; 为空时这类指针解析不了
    client_profile: ClientProfile | None = None
    # PLN-15 的计划叙述 (AuditPlanDraft, 需求 R204): procedure_notes 须覆盖这些程序 (计划 items 的程序编号; 多出的
    # 不算失败, 合成计划时不收, 需求 R316), 时间表不早于 period_end (审计期间止). 其余输出不用, 为空
    expected_procedure_ids: list[str] | None = None
    period_end: date | None = None
    # FIN-09 的更正验证草稿 (CorrectionVerificationDraft, 需求 R164): fully_corrected 须与 "规则算出的剩余额为 0" 一致,
    # other_accounts 须在 entry_account_codes 内且不重复. entry_account_codes 为匹配凭证各行的科目中错报科目以外的
    # (错报没有科目代码时为全部). 其余输出不用, 为空; FIN-09 缺任一项时 call_llm 在调用大模型之前抛 ValueError
    expected_residual_cents: int | None = None
    entry_account_codes: list[str] | None = None
    # PLN-07 第 3 步 (AnalyticsScreeningDraft, 需求 R371): 第 2 步规则命中的科目代码, 每个须恰好一条 decision. 其余输出
    # 不用, 为空; 直接校验 screen 输出而没给时 check_analytics_screening 抛 ValueError
    analytics_hits: list[str] | None = None
    # 判断与 GEN-09 第一轮引用的准则片段须在该程序 YAML 的 standards 内 (GEN-09 为被复核的程序, 需求 R301), 否则该指针
    # 同未登记的片段一样解析不了 (所在步骤 unsupported); 为 None 不核对 (抽取, 分类, 报告与 GEN-09 第二轮等不按引用级
    # 解析准则片段的输出). 必填, 不给缺省值: 新调用方漏传时类型检查即报, 不会静默不核对
    standards_of: str | None


class ValidationReport(Model, Generic[T]):
    """OutputValidator 对一次 LLM 输出的四级校验结果."""

    level_failed: Literal["syntax", "structure", "reference", "semantic"] | None  # 未通过的级别, 全部通过为空
    unsupported_steps: list[int]  # 证据指针解析不了的推理步骤下标
    errors: list[str]  # 错误信息
    notes: list[str] = []  # 不影响通过与否的提示, 如回函渠道与回执不符; 随 llm_call 留痕记录
    # 通过结构校验的输出, 解析得了的证据指针 type 与 reliability 已按来源改写; 语法或结构失败为空
    output: T | None = None

    @property
    def retryable(self) -> bool:
        """能否回传错误重试: 没通过 (四级中任一级) 即可 (需求 R348). 只标 unsupported 的步骤不算没通过."""
        return self.level_failed is not None


class LLMCallResult(Model, Generic[T]):
    """JudgmentEngine.call_llm 的结果: 最后一次尝试的校验结果, 尝试次数与各次留痕."""

    report: ValidationReport[T]  # 最后一次尝试的校验结果, level_failed 为空即通过
    attempts: int  # 调用大模型的次数, 含重试
    trail_ids: list[str]  # 每次尝试的 llm_call 留痕 ID, 与尝试一一对应
    model: str = ""  # 最后一次尝试实际用的模型名 (响应里的)
