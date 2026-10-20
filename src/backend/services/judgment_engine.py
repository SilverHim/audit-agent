"""判断引擎与输出校验 (A 负责). 规则见 开发接口文档.md 第 5 节."""

import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Literal, NoReturn, cast

from jinja2 import Environment, FileSystemLoader, StrictUndefined, Undefined
from pydantic import BaseModel, ValidationError

from backend.contracts import llm_outputs
from backend.contracts.knowledge import ProcedureTemplate
from backend.contracts.llm_outputs import (
    ConfirmationExtraction,
    ContractExtraction,
    CriticIndependent,
    CriticVerdictOut,
    DocumentClassification,
    JudgmentDraft,
    RegistryExtraction,
    ReportDraft,
    SheetRecognition,
)
from backend.contracts.models import (
    AgentId,
    AuditException,
    ClientProfile,
    Confirmation,
    DocumentRef,
    DocumentType,
    Engine,
    EvidencePointer,
    EvidenceType,
    Judgment,
    MisstatementDraft,
    OpinionType,
    ProcedureRun,
    ProposedAction,
    ReasoningStep,
    Reliability,
    ReviewStatus,
    SourceType,
    StepKind,
    StepStatus,
    TrailEntry,
    TrailKind,
)
from backend.contracts.ports import (
    AuditTrail,
    FinancialDataStore,
    LLMClient,
    LLMMessage,
    LLMRequest,
    LLMResponse,
    T_out,
)
from backend.contracts.services import (
    JudgmentContext,
    JudgmentOutcome,
    LLMCallResult,
    ValidationContext,
    ValidationReport,
)
from backend.services.procedure_library import ProcedureLibrary

Level = Literal["syntax", "structure", "reference", "semantic"]

# 行指针的证据种类与可靠性, 同第 5 节 RuleEngine 约定的映射表
ROW_EVIDENCE: dict[str, tuple[EvidenceType, Reliability]] = {
    "journal_entry": (EvidenceType.voucher, Reliability.internal_original),
    "sales_invoice": (EvidenceType.invoice, Reliability.internal_original),
    "delivery_note": (EvidenceType.delivery, Reliability.internal_original),
    "bank_transaction": (EvidenceType.bank, Reliability.external_copy),
    "bank_account": (EvidenceType.bank, Reliability.external_copy),
    "trial_balance": (EvidenceType.voucher, Reliability.internal_original),
}
# 文件指针的证据种类按文件类型 (第 5 节文件指针映射表): 回函与登记摘录属外部来源, 约定书与协议按合同, 对账单按银行,
# 纪要, 声明书, 函件, 治理层沟通与内部记录按凭证; 待审财报 (判断引报表行) 与 other 不作证据, 不在表中
DOCUMENT_EVIDENCE: dict[DocumentType, EvidenceType] = {
    DocumentType.contract: EvidenceType.contract,
    DocumentType.invoice: EvidenceType.invoice,
    DocumentType.confirmation: EvidenceType.external,
    DocumentType.bank_statement: EvidenceType.bank,
    DocumentType.minutes: EvidenceType.voucher,
    DocumentType.registry_extract: EvidenceType.external,
    DocumentType.engagement_letter: EvidenceType.contract,
    DocumentType.representation_letter: EvidenceType.voucher,
    DocumentType.correspondence: EvidenceType.voucher,
    DocumentType.governance_communication: EvidenceType.voucher,
    DocumentType.agreement: EvidenceType.contract,
    DocumentType.internal_record: EvidenceType.voucher,
}
# 不算行金额的金额列: 银行流水的交易后余额是累计数, 不是这一行的金额
NOT_ROW_AMOUNTS = {"balance_after_cents"}
MAX_ATTEMPTS = 3  # 首次调用加语法, 结构失败后最多 2 次重试
# 系统提示词: 要求只输出符合输出模型 JSON schema 的一个对象
SYSTEM_PROMPT = """Reply with a single JSON object that conforms to the JSON schema below. \
Write nothing before or after the object.

{schema}"""
# 语法或结构失败后追加的用户消息
RETRY_PROMPT = """Your previous reply failed validation:

{errors}

Reply again with a single JSON object that conforms to the schema."""


def output_model(template: ProcedureTemplate) -> type[BaseModel]:
    """程序模板 llm.output_model 指向的输出模型类 (contracts/llm_outputs.py).

    模板没有 llm 配置或类名不存在时抛 ValueError.
    """
    if template.llm is None:
        raise ValueError(f"{template.id} 没有 llm 配置")
    model = getattr(llm_outputs, template.llm.output_model, None)
    if not (isinstance(model, type) and issubclass(model, BaseModel)):
        raise ValueError(f"{template.id}: 输出模型 {template.llm.output_model} 不存在")
    return model


def parse_output(
    model: type[BaseModel], response: LLMResponse[Any]
) -> tuple[BaseModel | None, Level | None, list[str]]:
    """语法与结构两级: 返回 (输出, 未通过的级别, 错误).

    output 已解析时以它为准, 类型须是 model. 为空时重新解析 raw 以得到错误: 不是 JSON 为语法失败, 不合 model 为结构失败.
    这两级的错误会回传给模型重试, 用英文.
    """
    if response.output is not None:
        if isinstance(response.output, model):
            return response.output, None, []
        return None, "structure", [f"Output is {type(response.output).__name__}, expected {model.__name__}"]
    try:
        data = json.loads(response.raw)
    except ValueError as e:
        return None, "syntax", [f"Output is not valid JSON: {e}", *([response.error] if response.error else [])]
    try:
        return model.model_validate(data), None, []
    except ValidationError as e:
        return None, "structure", [f"Output does not match {model.__name__}: {e}"]


def row_amounts(row: dict[str, Any]) -> set[int]:
    """一行的金额: 列名以 _cents 结尾的非零整数取绝对值, 不含 NOT_ROW_AMOUNTS."""
    return {
        abs(v)
        for k, v in row.items()
        if k.endswith("_cents") and k not in NOT_ROW_AMOUNTS and isinstance(v, int) and not isinstance(v, bool) and v
    }


def supported_amounts(rows: Sequence[dict[str, Any]], findings: Iterable[AuditException]) -> set[int]:
    """错报金额可取的值 (绝对值): 某条引用行的金额, 两条不同引用行的金额之差, 或归因例外的金额. 不含 0."""
    per_row = [row_amounts(r) for r in rows]
    amounts = set().union(*per_row)
    for i, a_set in enumerate(per_row):
        for b_set in per_row[i + 1 :]:
            amounts |= {abs(a - b) for a in a_set for b in b_set}
    amounts |= {abs(x.amount_cents) for x in findings if x.amount_cents is not None}
    amounts.discard(0)
    return amounts


def trail_evidence(payload: Mapping[str, Any]) -> tuple[EvidenceType, Reliability]:
    """external_call 留痕作证据时的 type 与可靠性, 按留痕记下的事实定 (需求 R289; 判断引擎解析指针与 ACC-04 写指针
    都用它, 只此一处解释): payload 的 available 为 false 是系统记下的 "没有查询" (查不了登记, 需求 R288, R289), 没有
    取得外部证据, 为内部原件 (type voucher, 同内部记录); 其余 (真查了登记, 函证收发) 是审计师直接向外部系统取得,
    属外部原件 (ASA 500 A31)."""
    if payload.get("available") is False:
        return EvidenceType.voucher, Reliability.internal_original
    return EvidenceType.external, Reliability.external_original


def document_pointer(p: EvidencePointer, doc: DocumentRef) -> tuple[EvidencePointer | None, str]:
    """按文件登记改写指针的 type 与 reliability; 页码须在 1 到 page_count 之间. 不成立时返回 (None, 原因)."""
    if p.page is None or not 1 <= p.page <= doc.page_count:
        return None, f"页码 {p.page} 不在 1-{doc.page_count} 之间"
    evidence_type = DOCUMENT_EVIDENCE.get(doc.type)
    if evidence_type is None:
        return None, f"{doc.type} 类文件不作证据"
    return p.model_copy(update={"type": evidence_type, "reliability": doc.reliability}), ""


def chain_semantics(steps: Sequence[ReasoningStep]) -> list[str]:
    """推理链的语义校验 (规格书 8.3, 8.6), 返回错误 (空为通过): 非空; fact, standard 带证据指针, standard 指向准则片段;
    inference 引用前序步骤. 判断草稿与独立复核 (GEN-09) 的推理链共用."""
    errors = [] if steps else ["推理链为空"]
    for i, step in enumerate(steps):
        if step.kind in (StepKind.fact, StepKind.standard) and not step.evidence:
            errors.append(f"第 {i} 步 {step.kind} 没有证据指针")
        elif step.kind is StepKind.standard and all(p.source_type != "standard" for p in step.evidence):
            errors.append(f"第 {i} 步 standard 没有指向准则片段")
        if step.kind is StepKind.inference:
            later = [j for j in step.from_steps if not 0 <= j < i]
            if not step.from_steps:
                errors.append(f"第 {i} 步 inference 没有引用前序步骤")
            elif later:
                errors.append(f"第 {i} 步 inference 引用了非前序步骤 {later}")
    return errors


def judgment_semantics(draft: JudgmentDraft) -> list[str]:
    """JudgmentDraft 的语义校验, 返回错误 (空为通过): 推理链 (chain_semantics), 建议错报须有错报草稿."""
    errors = chain_semantics(draft.reasoning_chain)
    if draft.proposed_action is ProposedAction.misstatement and draft.misstatement_draft is None:
        errors.append("建议认定错报, 但没有错报草稿")
    return errors


def evidence_key(p: EvidencePointer) -> tuple[str, str, int | str | None]:
    """独立复核证据白名单的比对键: 文件按 (来源, 页), 第一轮只给了所引页的文字; 客户画像按 (来源, 字段), 第一轮只给了
    所引字段的 JSON; 其余按来源, 第三项为空."""
    if p.source_type == "document":
        return p.source_type, p.source_id, p.page
    if p.source_type == "client_profile":
        return p.source_type, p.source_id, p.locator
    return p.source_type, p.source_id, None


def critic_chain_ok(steps: Sequence[ReasoningStep]) -> bool:
    """独立复核的推理链是否通过 8.6 校验: 每步都有证据支撑 (指针可解析, 且在第一轮给出的证据内), 语义无错.
    不通过的, 争议仲裁不提供 a7_upheld (规格书 6.3 GEN-09)."""
    return all(step.status is StepStatus.supported for step in steps) and not chain_semantics(steps)


def evidence_coverage(
    required: Sequence[EvidenceType], steps: Sequence[ReasoningStep], weights: Mapping[str, float]
) -> float:
    """c_evidence: evidence_required 中每种证据取已取得指针中可靠性权重最高的, 没取得记 0, 再取平均.

    只计受支持步骤里的指针: 标为 unsupported 的步骤, 其指针一个都不算. 没有可靠性的指针 (准则) 不计.
    """
    best: dict[EvidenceType, float] = {}
    for step in steps:
        if step.status is StepStatus.unsupported:
            continue
        for p in step.evidence:
            if p.reliability is not None:
                best[p.type] = max(best.get(p.type, 0.0), weights[p.reliability])
    return sum(best.get(t, 0.0) for t in required) / len(required)


def rule_consistency(action: ProposedAction, has_findings: bool) -> float:
    """c_rule: 规则结果与结论方向一致为 1, 否则为 0. 有规则例外时一致指建议 misstatement 或 more_evidence,
    没有例外时一致指建议 accept 或 close."""
    if has_findings:
        return 1.0 if action in (ProposedAction.misstatement, ProposedAction.more_evidence) else 0.0
    return 1.0 if action in (ProposedAction.accept, ProposedAction.close) else 0.0


# 两段首行的标题, 按意见类型 (ASA 700 第 23, 28 段, ASA 705 第 16, 20 段)
REPORT_HEADINGS = {
    OpinionType.unqualified: ("Opinion", "Basis for Opinion"),
    OpinionType.qualified: ("Qualified Opinion", "Basis for Qualified Opinion"),
}


def judgment_key(judgment: Judgment) -> str:
    """判断的引用键 (RPT-03): {程序}:{对象}; 人工判断 (ID 为 AI 判断的 ID 加 -H{n}, 第 0 节) 另加 :H{n}, 与同一对象的
    AI 判断及其他人工判断区分. 不含执行次数, 重跑后同一对象的新判断键不变. ID 不合这两种写法的另加 :{判断 ID}."""
    base = f"J-{judgment.procedure_id}-{judgment.run_id.rsplit('-', 1)[-1]}-{judgment.subject_id}"
    key = f"{judgment.procedure_id}:{judgment.subject_id}"
    if judgment.id == base:
        return key
    suffix = judgment.id.removeprefix(f"{base}-")
    return f"{key}:{suffix}" if suffix != judgment.id and re.fullmatch(r"H\d+", suffix) else f"{key}:{judgment.id}"


def citation_keys(judgments: Sequence[Judgment]) -> dict[str, Judgment]:
    """可引用的判断 (RPT-03 的 ValidationContext.judgments, 当前结果中错报所依据的已确认判断) → 引用键 (见
    judgment_key). 提示词按它列出可引用的键, 校验与把键换成判断 ID 也按它, 三处一致. 键须唯一: 同一程序同一对象
    有两条键相同的判断 (现有流程里出现不了, 如两次执行的 AI 判断都是错报依据) 时抛 ValueError, 写明键与这几条判断,
    不悄悄引错."""
    found: dict[str, list[Judgment]] = {}
    for judgment in judgments:
        found.setdefault(judgment_key(judgment), []).append(judgment)
    clashes = {key: items for key, items in found.items() if len(items) > 1}
    if clashes:
        detail = "; ".join(f"{key}: {', '.join(j.id for j in items)}" for key, items in clashes.items())
        raise ValueError(f"可引用的判断引用键重复: {detail}")
    return {key: items[0] for key, items in found.items()}


def report_checks(draft: ReportDraft, ctx: ValidationContext) -> tuple[Level | None, list[str]]:
    """报告草稿的引用与语义两级. 返回 (未通过的级别, 错误).

    引用: judgment_keys 的每个键须是可引用的判断 (ctx.judgments 的 citation_keys) 之一, 且该判断已确认.
    语义: 意见段与基础段首行为该意见类型的标题; 保留意见须引用至少一条判断. ctx.opinion 为空时抛 ValueError.
    """
    if ctx.opinion is None:
        raise ValueError("报告草稿的校验须给出意见类型 (ValidationContext.opinion)")
    citable = citation_keys(ctx.judgments)
    errors = []
    for key in draft.judgment_keys:
        judgment = citable.get(key)
        if judgment is None:
            errors.append(f"引用键 {key} 不是可引用的判断 (可用: {', '.join(citable) or '无'})")
        elif judgment.review_status is not ReviewStatus.confirmed:
            errors.append(f"引用键 {key} 的判断 {judgment.id} 尚未确认 ({judgment.review_status})")
    if errors:
        return "reference", errors
    opinion_heading, basis_heading = REPORT_HEADINGS[ctx.opinion]
    if draft.opinion.strip().splitlines()[:1] != [opinion_heading]:
        errors.append(f"意见段首行应为 {opinion_heading}")
    if draft.basis_for_opinion.strip().splitlines()[:1] != [basis_heading]:
        errors.append(f"基础段首行应为 {basis_heading}")
    if ctx.opinion is OpinionType.qualified and not draft.judgment_keys:
        errors.append("保留意见须引用形成错报的已确认判断")
    return ("semantic" if errors else None), errors


class PointerResolver:
    """一次校验内解析证据指针: 按 source_type 到对应存储查, 同一来源只查一次. client_profile 指针不查存储, 只认
    profile (校验上下文中本项目采用的那一版客户画像, 需求 R128)."""

    def __init__(self, validator: "OutputValidator", eid: str, profile: ClientProfile | None = None) -> None:
        self.validator = validator
        self.eid = eid
        self.profile = profile
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}  # 解析得了的行, (行类型, 行 ID) -> 行
        # (source_type, source_id) -> (查得的对象, 查不到的原因)
        self._cache: dict[tuple[str, str], tuple[Any, str]] = {}
        self._documents: dict[str, DocumentRef] | None = None  # 项目登记的文件, 首次用到时取

    async def lookup(self, source_type: str, source_id: str) -> tuple[Any, str]:
        """取指针来源: 行, 准则片段, 文件登记, 留痕或客户画像. 返回 (对象, 原因), 查不到时对象为 None."""
        key = (source_type, source_id)
        if key not in self._cache:
            self._cache[key] = await self._fetch(source_type, source_id)
        return self._cache[key]

    async def _fetch(self, source_type: str, source_id: str) -> tuple[Any, str]:
        v = self.validator
        if source_type in ROW_EVIDENCE:
            row = await v.financial.record(self.eid, cast(SourceType, source_type), source_id)
            return row, "" if row is not None else "查无此行"
        if source_type == "standard":
            fragment = v.library.fragment(source_id)
            return fragment, "" if fragment is not None else "准则片段未注册"
        if source_type == "document":
            if self._documents is None:
                self._documents = {d.id: d for d in await v.financial.documents(self.eid)}
            doc = self._documents.get(source_id)
            return doc, "" if doc is not None else "文件未登记"
        if source_type == "client_profile":
            if self.profile is None:
                return None, "校验上下文没有客户画像"
            if source_id != self.profile.id:
                return None, f"不是本项目采用的客户画像 {self.profile.id}"
            return self.profile, ""
        entry = await v.trail.get(self.eid, source_id)
        if entry is None:
            return None, "留痕不存在"
        if entry.kind is not TrailKind.external_call:
            return None, f"留痕是 {entry.kind}, 不是外部调用"
        return entry, ""

    async def resolve(self, p: EvidencePointer) -> tuple[EvidencePointer | None, str]:
        """解析一个指针, 成功时返回 type 与 reliability 按来源改写后的指针, 不采信模型填写的值."""
        found, reason = await self.lookup(p.source_type, p.source_id)
        if found is None:
            return None, reason
        if p.source_type in ROW_EVIDENCE:
            self.rows[(p.source_type, p.source_id)] = found
            evidence_type, reliability = ROW_EVIDENCE[p.source_type]
            return p.model_copy(update={"type": evidence_type, "reliability": reliability}), ""
        if p.source_type == "standard":
            return p.model_copy(update={"type": EvidenceType.standard, "reliability": None}), ""
        if p.source_type == "document":
            return document_pointer(p, found)
        if p.source_type == "client_profile":
            if p.locator not in ClientProfile.model_fields:
                return None, f"{p.locator} 不是客户画像的字段"
            # 客户画像是管理层申报的资料 (ASA 550 第 13 段), 不是外部证据: 按内部副本的凭证
            return p.model_copy(update={"type": EvidenceType.voucher, "reliability": Reliability.internal_copy}), ""
        # 留痕只认外部调用, 如登记查询; 可靠性按留痕记下的事实 (trail_evidence, 同 ACC-04)
        evidence_type, reliability = trail_evidence(found.payload)
        return p.model_copy(update={"type": evidence_type, "reliability": reliability}), ""


@dataclass(frozen=True)
class CheckResult:
    """一种输出模型的引用与语义两级校验结果."""

    output: Any  # 改写证据指针后的输出 (同一模型)
    level: Level | None = None  # 未通过的级别 (reference 或 semantic), 通过为空
    unsupported: list[int] = field(default_factory=list)  # 证据指针解析不了的推理步骤下标
    errors: list[str] = field(default_factory=list)  # 错误
    notes: list[str] = field(default_factory=list)  # 不影响通过与否的提示


# 一种输出模型的引用与语义校验: (校验器, 项目, 通过结构校验的输出, 上下文) → 结果
Check = Callable[["OutputValidator", str, Any, ValidationContext], Awaitable[CheckResult]]


Extraction = ContractExtraction | ConfirmationExtraction | RegistryExtraction  # 抽取类输出 (引用级同一套)


class OutputValidator:
    """LLM 输出的四级校验: 语法, 结构, 引用, 语义.

    语法与结构两级对所有输出模型相同; 引用与语义两级按输出模型查分派表 CHECKS. 各组新的输出模型用 register
    登记一行校验函数, 不改 validate.
    """

    # 输出模型 → 引用与语义校验, 见 OutputValidator 类与各校验函数之后的登记
    CHECKS: ClassVar[dict[type[BaseModel], Check]] = {}

    def __init__(self, financial: FinancialDataStore, library: ProcedureLibrary, trail: AuditTrail) -> None:
        self.financial = financial  # 解析行指针与文件登记
        self.library = library  # 解析准则片段
        self.trail = trail  # 解析留痕指针

    @classmethod
    def register(cls, model: type[BaseModel], check: Check) -> None:
        """登记一种输出模型的引用与语义校验; 已登记的覆盖."""
        cls.CHECKS[model] = check

    async def validate(
        self, eid: str, template: ProcedureTemplate, response: LLMResponse[T_out], ctx: ValidationContext
    ) -> ValidationReport[T_out]:
        """依次校验语法, 结构, 引用, 语义, 返回第一个未通过的级别; 语法与结构失败可重试, 其余不可.

        output 取通过结构校验的输出并改写证据指针; 引用失败时 unsupported_steps 与 errors 仍列出已查到的问题.
        """
        model = output_model(template)
        output, level, errors = parse_output(model, response)
        if output is None:
            return ValidationReport(level_failed=level, retryable=True, unsupported_steps=[], errors=errors)
        check = self.CHECKS.get(model)
        if check is None:
            raise NotImplementedError(f"{model.__name__} 的引用与语义校验")
        result = await check(self, eid, output, ctx)
        return ValidationReport(
            level_failed=result.level,
            retryable=False,
            unsupported_steps=result.unsupported,
            errors=result.errors,
            notes=result.notes,
            output=result.output,
        )

    async def _confirmation(
        self, eid: str, output: ConfirmationExtraction, confirmation: Confirmation
    ) -> tuple[list[str], list[str]]:
        """回函抽取的语义级: 抽出的账号须等于被函证账户的账号, 否则判失败 (回函不是这个账户的).

        回函渠道与 CSH-04 回执记录的不符时只记提示, 不判失败: 渠道以回执为准. 返回 (错误, 提示).
        """
        account = await self.financial.record(eid, "bank_account", confirmation.bank_account_id)
        if account is None:
            return [f"被函证的账户 {confirmation.bank_account_id} 不存在"], []
        errors = []
        account_id, expected_no = confirmation.bank_account_id, account["account_no"]
        if output.account_no != expected_no:
            errors.append(f"回函账号 {output.account_no} 与账户 {account_id} 的账号 {expected_no} 不符")
        notes = []
        expected, stated = confirmation.receipt_channel, output.receipt_channel
        if stated is not None and expected is not None and stated != expected:
            notes.append(f"回函写明的渠道 {stated} 与回执记录的 {expected} 不符, 以回执为准")
        return errors, notes

    async def _judgment(
        self, eid: str, draft: JudgmentDraft, ctx: ValidationContext
    ) -> tuple[JudgmentDraft, Level | None, list[int], list[str]]:
        """判断草稿的引用与语义两级. 返回 (改写指针后的草稿, 未通过的级别, 不受支持的步骤, 错误)."""
        resolver = PointerResolver(self, eid, ctx.client_profile)
        errors: list[str] = []
        valid = 0

        async def resolve_all(pointers: list[EvidencePointer], where: str) -> tuple[list[EvidencePointer], bool]:
            nonlocal valid
            out, ok = [], True
            for p in pointers:
                resolved, reason = await resolver.resolve(p)
                if resolved is None:
                    ok = False
                    errors.append(f"{where}指针 {p.source_type} {p.source_id}: {reason}")
                else:
                    valid += 1
                out.append(resolved or p)
            return out, ok

        steps: list[ReasoningStep] = []
        unsupported: list[int] = []
        for i, step in enumerate(draft.reasoning_chain):
            pointers, ok = await resolve_all(step.evidence, f"第 {i} 步")
            steps.append(step.model_copy(update={"evidence": pointers}))
            if not ok:
                unsupported.append(i)
        evidence, _ = await resolve_all(draft.evidence, "evidence 中")
        draft = draft.model_copy(update={"reasoning_chain": steps, "evidence": evidence})

        if valid == 0:
            errors.append("没有一个能解析的证据指针")
            return draft, "reference", unsupported, errors
        md = draft.misstatement_draft
        if md is not None and abs(md.amount_cents) not in supported_amounts(
            list(resolver.rows.values()), ctx.rule_findings
        ):
            errors.append(f"错报金额 {md.amount_cents} 不等于引用行的金额, 两条引用行之差或归因例外的金额")
            return draft, "reference", unsupported, errors
        semantic = judgment_semantics(draft)
        return draft, "semantic" if semantic else None, unsupported, errors + semantic

    async def _critic(
        self, eid: str, output: CriticIndependent, ctx: ValidationContext
    ) -> tuple[CriticIndependent, list[int], list[str], list[str]]:
        """独立复核推理链的引用与语义: 指针须可解析, 且在 ctx.allowed_evidence 内 (按 evidence_key, 文件须同页,
        画像须同字段), 否则该步不受支持. ctx.critic_drafts 为真 (功能 collab) 时另核对错报草稿 (_critic_draft),
        其问题与推理链的分开. 返回 (改写指针后的输出, 不受支持的步骤, 推理链的错误, 草稿的问题)."""
        resolver = PointerResolver(self, eid, ctx.client_profile)
        allowed = {evidence_key(p) for p in ctx.allowed_evidence or []}
        errors: list[str] = []
        steps: list[ReasoningStep] = []
        unsupported: list[int] = []
        for i, step in enumerate(output.reasoning_chain):
            pointers, ok = [], True
            for p in step.evidence:
                resolved, reason = await resolver.resolve(p)
                if resolved is None or evidence_key(p) not in allowed:
                    ok = False
                    errors.append(f"第 {i} 步指针 {p.source_type} {p.source_id}: {reason or '不在给出的证据内'}")
                pointers.append(resolved or p)
            steps.append(step.model_copy(update={"evidence": pointers}))
            if not ok:
                unsupported.append(i)
        draft, draft_notes = output.misstatement_draft, list[str]()
        if ctx.critic_drafts:
            draft, draft_notes = await self._critic_draft(resolver, output, ctx)
        checked = output.model_copy(update={"reasoning_chain": steps, "misstatement_draft": draft})
        return checked, unsupported, errors + chain_semantics(steps), draft_notes

    async def _critic_draft(
        self, resolver: PointerResolver, output: CriticIndependent, ctx: ValidationContext
    ) -> tuple[MisstatementDraft | None, list[str]]:
        """独立复核第一轮的错报草稿 (需求 R150, 只记不判失败): 建议 misstatement 而没有草稿, 不建议 misstatement 却给了
        草稿, 或草稿金额 (绝对值) 不在 A6 草稿的同一口径内 (supported_amounts: 第一轮给出的数据行, 即
        ctx.allowed_evidence 中的行指针的金额, 两行之差, 该判断解释的例外 ctx.rule_findings 的金额) 时记问题,
        草稿不写回. 返回 (写回的草稿, 问题)."""
        md = output.misstatement_draft
        if output.proposed_action is ProposedAction.misstatement and md is None:
            return None, ["独立复核建议认定错报, 但没有错报草稿"]
        if md is None:
            return None, []
        if output.proposed_action is None:
            return None, ["独立复核没有给建议动作, 却给了错报草稿, 草稿不采用"]
        if output.proposed_action is not ProposedAction.misstatement:
            return None, [f"独立复核建议 {output.proposed_action.value}, 却给了错报草稿, 草稿不采用"]
        rows = []
        for p in ctx.allowed_evidence or []:
            if p.source_type in ROW_EVIDENCE:
                row, _ = await resolver.lookup(p.source_type, p.source_id)
                if row is not None:
                    rows.append(row)
        if abs(md.amount_cents) not in supported_amounts(rows, ctx.rule_findings):
            detail = "不等于给出的行的金额, 两行之差或该判断解释的例外的金额, 草稿不采用"
            return None, [f"独立复核的错报金额 {md.amount_cents} {detail}"]
        return md, []

    @staticmethod
    def _extraction(output: Extraction, document: DocumentRef | None) -> tuple[Extraction, list[str]]:
        """抽取输出的引用级, 返回 (改写指针后的输出, 错误).

        指针只能指向本文档且页码在页数内, 条款页码同样; 回函的应计利息非空时须有 locator 为 accrued_interest_cents
        的本文档指针 (需求 R104); ASIC 摘录的 acn, name, status, officeholders 各须有 locator 为该字段的指针 (需求
        R290, llm_outputs.REGISTRY_POINTED); 有一处不成立即整条失败.
        """
        if document is None:
            raise ValueError("抽取类输出的校验须给出本次抽取的文件 (ValidationContext.document)")
        errors: list[str] = []
        evidence = []
        for p in output.evidence:
            if p.source_type != "document" or p.source_id != document.id:
                resolved, reason = None, f"不是本文档 {document.id}"
            else:
                resolved, reason = document_pointer(p, document)
            if resolved is None:
                errors.append(f"指针 {p.locator} ({p.source_type} {p.source_id}): {reason}")
            evidence.append(resolved or p)
        if not output.evidence:
            errors.append("没有证据指针")
        if isinstance(output, ContractExtraction):
            errors += [
                f"条款 {t.term_type} 页码 {t.page} 不在 1-{document.page_count} 之间"
                for t in output.terms
                if not 1 <= t.page <= document.page_count
            ]
        if (
            isinstance(output, ConfirmationExtraction)
            and output.accrued_interest_cents is not None
            and not any(p.locator == "accrued_interest_cents" for p in output.evidence)
        ):
            errors.append("应计利息 accrued_interest_cents 非空, 却没有 locator 为 accrued_interest_cents 的指针")
        if isinstance(output, RegistryExtraction):
            errors += [f"摘录的 {name} 没有 locator 为 {name} 的指针" for name in output.unpointed()]
        return output.model_copy(update={"evidence": evidence}), errors


# ── 各输出模型的引用与语义校验 (分派表 OutputValidator.CHECKS) ──


async def check_judgment(
    validator: OutputValidator, eid: str, output: JudgmentDraft, ctx: ValidationContext
) -> CheckResult:
    """判断草稿: 证据指针解析, 错报金额对得上, 推理链语义 (见 _judgment)."""
    draft, level, unsupported, errors = await validator._judgment(eid, output, ctx)
    return CheckResult(draft, level, unsupported, errors)


async def check_extraction(
    validator: OutputValidator, eid: str, output: Extraction, ctx: ValidationContext
) -> CheckResult:
    """抽取输出: 指针只能指向本文档且页码在页数内; 回函抽取 (ctx.confirmation 非空) 另核对账号, 渠道不符只记提示."""
    extracted, errors = validator._extraction(output, ctx.document)
    if errors:
        return CheckResult(extracted, "reference", errors=errors)
    if isinstance(extracted, ConfirmationExtraction) and ctx.confirmation is not None:
        errors, notes = await validator._confirmation(eid, extracted, ctx.confirmation)
        return CheckResult(extracted, "semantic" if errors else None, errors=errors, notes=notes)
    return CheckResult(extracted)


async def check_report(
    validator: OutputValidator, eid: str, output: ReportDraft, ctx: ValidationContext
) -> CheckResult:
    """报告草稿: 引用的判断须为当前已确认的版本, 两段标题与意见类型相符 (见 report_checks)."""
    level, errors = report_checks(output, ctx)
    return CheckResult(output, level, errors=errors)


async def check_critic(
    validator: OutputValidator, eid: str, output: CriticIndependent, ctx: ValidationContext
) -> CheckResult:
    """独立复核第一轮: 引用与语义问题只记进 errors, 不判失败; 推理链照存, 仲裁时 critic_chain_ok 据此不提供
    a7_upheld. 错报草稿的问题 (功能 collab 打开时才核对) 记进 notes, 与推理链的分开, 草稿不写回 (见 _critic_draft)."""
    checked, unsupported, errors, notes = await validator._critic(eid, output, ctx)
    return CheckResult(checked, None, unsupported, errors, notes)


async def structure_only(
    validator: OutputValidator, eid: str, output: BaseModel, ctx: ValidationContext
) -> CheckResult:
    """只做语法与结构两级: 独立复核第二轮 CriticVerdictOut; 上传文件分类 DocumentClassification; 上传账表识别
    SheetRecognition.

    文件分类的类型取值已由结构校验限定, 没有证据指针. 账表识别的方案在结构校验里自洽 (TablePlan 的模型校验); 与工作表
    对不对得上在套用时查 (table_plan.check_plan), 不算大模型输出失败.
    """
    return CheckResult(output)


for _model, _check in (
    (JudgmentDraft, check_judgment),
    (ContractExtraction, check_extraction),
    (ConfirmationExtraction, check_extraction),
    (RegistryExtraction, check_extraction),
    (ReportDraft, check_report),
    (CriticIndependent, check_critic),
    (CriticVerdictOut, structure_only),
    (DocumentClassification, structure_only),
    (SheetRecognition, structure_only),
):
    OutputValidator.register(_model, _check)


def json_default(value: object) -> NoReturn:
    """tojson 遇到不能序列化的对象时调用: 缺变量 (StrictUndefined) 抛 UndefinedError, 其余仍抛 TypeError.

    StrictUndefined 只在取值, 转字符串等操作时报错, json.dumps 只会说它不能序列化, 看不出是缺了变量.
    """
    if isinstance(value, Undefined):
        str(value)  # StrictUndefined 转字符串即抛 UndefinedError, 消息写明缺的是哪个变量
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def prompt_env(root: Path) -> Environment:
    """提示词模板的 Jinja2 环境: 去掉块标签所在行的换行与行首空白, 缺变量即报错 (含经 tojson 输出的), 不做 HTML 转义."""
    env = Environment(
        loader=FileSystemLoader(root), trim_blocks=True, lstrip_blocks=True, undefined=StrictUndefined, autoescape=False
    )
    env.policies["json.dumps_kwargs"] = {"sort_keys": True, "default": json_default}  # sort_keys 同 Jinja2 缺省
    return env


def prompt_hash(system: str, messages: Sequence[LLMMessage]) -> str:
    """送给大模型的完整输入 (system 与 messages) 的 sha256: 规范 JSON, 键排序, 不留空白, 非 ASCII 字符原样."""
    canonical = json.dumps(
        {"system": system, "messages": [m.model_dump() for m in messages]},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class JudgmentEngine:
    """判断引擎: 组装上下文, 调大模型, 校验输出, 算置信度, 产出 Judgment.

    调大模型的公共部分 (渲染, 重试, 留痕) 在 call_llm, judge 与抽取类程序 (DOC-04, DOC-06), 报告 (RPT-03) 共用.
    """

    def __init__(
        self, llm: LLMClient, validator: OutputValidator, trail: AuditTrail, library: ProcedureLibrary
    ) -> None:
        self.llm = llm  # 大模型客户端
        self.validator = validator  # 四级校验
        self.trail = trail  # 写 llm_call 留痕
        self.library = library  # 取准则片段
        self.prompts = prompt_env(library.root / "prompts")  # 提示词模板
        rules = json.loads((library.root / "rules.json").read_text(encoding="utf-8"))
        self.confidence_weights: dict[str, float] = rules["confidence_weights"]  # 置信度各分项的权重
        self.reliability_weights: dict[str, float] = rules["reliability_weights"]  # 证据可靠性的权重

    def render(self, template: ProcedureTemplate, variables: Mapping[str, Any]) -> str:
        """按程序模板 llm.prompt 渲染提示词. 缺变量抛 jinja2.UndefinedError, 属调用方的错, 不算大模型失败."""
        if template.llm is None:
            raise ValueError(f"{template.id} 没有 llm 配置")
        return self.prompts.get_template(template.llm.prompt).render(variables)

    async def call_llm(
        self,
        eid: str,
        template: ProcedureTemplate,
        run: ProcedureRun,
        variables: Mapping[str, Any],
        ctx: ValidationContext,
        actor: AgentId,
        fixture_key: str = "default",
        *,
        llm: LLMClient | None = None,
        system: str = SYSTEM_PROMPT,
    ) -> LLMCallResult[Any]:
        """渲染提示词, 调大模型并校验输出; 语法或结构失败时把原文与错误追加进对话重试, 最多共 MAX_ATTEMPTS 次.

        引用与语义失败不重试. 每次尝试写一行 llm_call 留痕. variables 为模板变量 (对象须先 model_dump(mode="json")),
        ctx 为校验上下文, actor 为调用方 Agent, fixture_key 为判断对象或文件 (mock 模式据此取固定输出).
        请求与留痕的 procedure_id 取模板编号 (GEN-09 在 A6 那次执行里调用, run_id 仍是 A6 的执行).
        llm: 换用的客户端 (独立复核用复核模型), 缺省为本引擎的; system: 系统提示词模板, 须含 {schema}.
        """
        model = output_model(template)
        assert template.llm is not None  # output_model 已确认
        client = llm if llm is not None else self.llm
        system = system.format(schema=json.dumps(model.model_json_schema(), indent=2))
        messages = [LLMMessage(role="user", content=self.render(template, variables))]
        trail_ids: list[str] = []
        for attempt in range(1, MAX_ATTEMPTS + 1):
            request = LLMRequest(
                procedure_id=template.id,
                run_id=run.id,
                system=system,
                messages=messages,
                max_tokens=template.llm.max_tokens,
                fixture_key=fixture_key,
                eid=eid,
            )
            response = await client.complete(request, model)
            report = await self.validator.validate(eid, template, response, ctx)
            trail_ids.append(await self.record(eid, actor, request, response, report, attempt))
            if not report.retryable or attempt == MAX_ATTEMPTS:
                break
            messages = [
                *messages,
                LLMMessage(role="assistant", content=response.raw),
                LLMMessage(role="user", content=RETRY_PROMPT.format(errors="\n".join(report.errors))),
            ]
        return LLMCallResult(report=report, attempts=attempt, trail_ids=trail_ids, model=response.model)

    async def record(
        self,
        eid: str,
        actor: AgentId,
        request: LLMRequest,
        response: LLMResponse[Any],
        report: ValidationReport[Any],
        attempt: int,
    ) -> str:
        """写一次尝试的 llm_call 留痕, 返回留痕 ID. payload 存提示词全文与原文, 供复查."""
        outcome = report.level_failed or "passed"
        return await self.trail.record(
            TrailEntry(
                engagement_id=eid,
                kind=TrailKind.llm_call,
                actor=actor,
                procedure_id=request.procedure_id,
                run_id=request.run_id,
                summary=f"{request.procedure_id} {request.fixture_key} 第 {attempt} 次调用大模型: {outcome}",
                model=response.model,
                prompt_hash=prompt_hash(request.system, request.messages),
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                payload={
                    "attempt": attempt,
                    "fixture_key": request.fixture_key,
                    "system": request.system,
                    "messages": [m.model_dump() for m in request.messages],
                    "raw": response.raw,
                    "level_failed": report.level_failed,
                    "errors": report.errors,
                    "notes": report.notes,
                },
            )
        )

    def standards(self, template: ProcedureTemplate) -> list[dict[str, Any]]:
        """程序引用的准则片段 (提示词变量 standards, JSON 形式). 未注册的片段抛 LookupError, 在调用大模型之前."""
        fragments = []
        for fragment_id in template.standards:
            fragment = self.library.fragment(fragment_id)
            if fragment is None:
                raise LookupError(f"{template.id}: 准则片段 {fragment_id} 未注册")
            fragments.append(fragment.model_dump(mode="json"))
        return fragments

    async def judge(
        self, eid: str, template: ProcedureTemplate, run: ProcedureRun, ctx: JudgmentContext, performed_by: str
    ) -> JudgmentOutcome:
        """对一个判断对象调大模型, 把通过校验的草稿补全为 Judgment (review_status pending, 由调用方分类).

        失败 (重试耗尽, 引用或语义失败) 时 judgment 为空: hybrid 程序为 unattributed (保留规则结果), 其余为 failed.
        调用方 Agent 取模板 owner; performed_by 为项目负责人.
        """
        variables = {
            "subject_id": ctx.subject_id,
            "task": ctx.task,
            "data": ctx.data,
            "rule_findings": [x.model_dump(mode="json") for x in ctx.rule_findings],
            "standards": self.standards(template),
        }
        result = await self.call_llm(
            eid,
            template,
            run,
            variables,
            ValidationContext(rule_findings=ctx.rule_findings, client_profile=ctx.client_profile),
            template.owner,
            ctx.fixture_key,
        )
        report = result.report
        if report.level_failed is not None:
            failure: Literal["failed", "unattributed"] = (
                "unattributed" if template.engine is Engine.hybrid else "failed"
            )
            return JudgmentOutcome(judgment=None, failure=failure, attempts=result.attempts, trail_ids=result.trail_ids)
        draft = cast(JudgmentDraft, report.output)
        steps = [
            step.model_copy(
                update={"status": StepStatus.unsupported if i in report.unsupported_steps else StepStatus.supported}
            )
            for i, step in enumerate(draft.reasoning_chain)
        ]
        parts = self.confidence_parts(template, draft, steps, bool(ctx.rule_findings))
        judgment = Judgment(
            id=f"J-{template.id}-{run.run_no}-{ctx.subject_id}",
            procedure_id=template.id,
            run_id=run.id,
            subject_id=ctx.subject_id,
            conclusion=draft.conclusion,
            confidence=self.confidence(parts),
            confidence_parts={k: round(v, 4) for k, v in parts.items()},
            reasoning_chain=steps,
            evidence=draft.evidence,
            counter_argument=draft.counter_argument,
            proposed_action=draft.proposed_action,
            misstatement_draft=draft.misstatement_draft,
            exception_ids=[x.id for x in ctx.rule_findings],
            autonomy_level=ctx.autonomy_level or template.autonomy_level,
            performed_by=performed_by,
            created_at=datetime.now(UTC),
            trail_ref=result.trail_ids[-1],
        )
        return JudgmentOutcome(judgment=judgment, failure=None, attempts=result.attempts, trail_ids=result.trail_ids)

    def confidence_parts(
        self, template: ProcedureTemplate, draft: JudgmentDraft, steps: Sequence[ReasoningStep], has_findings: bool
    ) -> dict[str, float]:
        """置信度的适用分项: evidence (模板有 evidence_required 时), rule (仅 hybrid), llm (模型自报)."""
        parts: dict[str, float] = {}
        if template.evidence_required:
            parts["evidence"] = evidence_coverage(template.evidence_required, steps, self.reliability_weights)
        if template.engine is Engine.hybrid:
            parts["rule"] = rule_consistency(draft.proposed_action, has_findings)
        parts["llm"] = draft.self_confidence
        return parts

    def confidence(self, parts: Mapping[str, float]) -> float:
        """confidence = Σ w_i · c_i / Σ w_i, 只对 parts 中的分项求和, 四位小数."""
        weights = {k: self.confidence_weights[k] for k in parts}
        return round(sum(weights[k] * parts[k] for k in parts) / sum(weights.values()), 4)
