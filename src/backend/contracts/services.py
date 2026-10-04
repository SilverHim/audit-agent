"""服务之间传递的数据对象, 定义见 开发接口文档.md 第 5 节."""

from typing import Any, Generic, Literal, TypeVar

from backend.contracts.models import AuditException, Confirmation, DocumentRef, Judgment, Model

T = TypeVar("T")


class RuleResult(Model):
    """RuleEngine 执行一条程序全部规则的结果."""

    exceptions: list[AuditException]  # emit = exception 的规则产出的例外
    rows: dict[str, list[dict[str, Any]]]  # emit = rows 的规则返回的行, 按规则编号


class JudgmentContext(Model):
    """送给 JudgmentEngine 的一个判断对象的上下文."""

    subject_id: str  # 判断对象 ID
    task: dict[str, Any]  # 任务说明: 参数, 覆盖的审计目标与风险
    data: dict[str, Any]  # 该判断需要的数据行与抽取要素
    rule_findings: list[AuditException] = []  # 该对象的规则例外 (混合程序), 本程序与前置程序的都算
    fixture_key: str  # mock 模式下取固定输出用的键


class JudgmentOutcome(Model):
    """JudgmentEngine 对一个判断对象的产出."""

    judgment: Judgment | None  # 判断, 失败时为空
    failure: Literal["failed", "unattributed"] | None  # 失败类型, 成功时为空
    attempts: int  # 调用大模型的次数, 含重试
    trail_ids: list[str]  # 每次大模型调用的留痕 ID


class ValidationContext(Model):
    """OutputValidator 校验一次 LLM 输出时用到的上下文."""

    rule_findings: list[AuditException] = []  # 该判断对象的规则例外, 即判断归因的例外; 错报金额可等于其金额
    document: DocumentRef | None = None  # 抽取类程序 (DOC-04, DOC-06) 本次抽取的文件, 指针只能指向它
    judgments: list[Judgment] = []  # 状态中的判断, 供 RPT-03 校验 judgment_refs
    confirmation: Confirmation | None = None  # DOC-06 本次抽取的函证 (CSH-04 所建), 核对回函账号与回函渠道


class ValidationReport(Model, Generic[T]):
    """OutputValidator 对一次 LLM 输出的四级校验结果."""

    level_failed: Literal["syntax", "structure", "reference", "semantic"] | None  # 未通过的级别, 全部通过为空
    retryable: bool  # 能否回传错误重试: 语法, 结构失败为真
    unsupported_steps: list[int]  # 证据指针解析不了的推理步骤下标
    errors: list[str]  # 错误信息
    notes: list[str] = []  # 不影响通过与否的提示, 如回函渠道与回执不符; 随 llm_call 留痕记录
    # 通过结构校验的输出, 解析得了的证据指针 type 与 reliability 已按来源改写; 语法或结构失败为空
    output: T | None = None


class LLMCallResult(Model, Generic[T]):
    """JudgmentEngine.call_llm 的结果: 最后一次尝试的校验结果, 尝试次数与各次留痕."""

    report: ValidationReport[T]  # 最后一次尝试的校验结果, level_failed 为空即通过
    attempts: int  # 调用大模型的次数, 含重试
    trail_ids: list[str]  # 每次尝试的 llm_call 留痕 ID, 与尝试一一对应
