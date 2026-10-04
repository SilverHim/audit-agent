"""判断引擎与输出校验 (A 负责). 规则见 开发接口文档.md 第 5 节."""

import json
from collections.abc import Iterable, Sequence
from typing import Any, Literal, cast

from pydantic import BaseModel, ValidationError

from backend.contracts import llm_outputs
from backend.contracts.knowledge import ProcedureTemplate
from backend.contracts.llm_outputs import ConfirmationExtraction, ContractExtraction, JudgmentDraft
from backend.contracts.models import (
    AuditException,
    DocumentRef,
    DocumentType,
    EvidencePointer,
    EvidenceType,
    ProcedureRun,
    ProposedAction,
    ReasoningStep,
    Reliability,
    SourceType,
    StepKind,
    TrailKind,
)
from backend.contracts.ports import AuditTrail, FinancialDataStore, LLMResponse, T_out
from backend.contracts.services import JudgmentContext, JudgmentOutcome, ValidationContext, ValidationReport
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
# 文件指针的证据种类按文件类型, 回函属外部来源; 其他类文件定不了种类, 不作证据
DOCUMENT_EVIDENCE = {DocumentType.contract: EvidenceType.contract, DocumentType.confirmation: EvidenceType.external}
# 不算行金额的金额列: 银行流水的交易后余额是累计数, 不是这一行的金额
NOT_ROW_AMOUNTS = {"balance_after_cents"}


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


def document_pointer(p: EvidencePointer, doc: DocumentRef) -> tuple[EvidencePointer | None, str]:
    """按文件登记改写指针的 type 与 reliability; 页码须在 1 到 page_count 之间. 不成立时返回 (None, 原因)."""
    if p.page is None or not 1 <= p.page <= doc.page_count:
        return None, f"页码 {p.page} 不在 1-{doc.page_count} 之间"
    evidence_type = DOCUMENT_EVIDENCE.get(doc.type)
    if evidence_type is None:
        return None, f"{doc.type} 类文件不作证据"
    return p.model_copy(update={"type": evidence_type, "reliability": doc.reliability}), ""


def judgment_semantics(draft: JudgmentDraft) -> list[str]:
    """JudgmentDraft 的语义校验, 返回错误 (空为通过)."""
    errors = [] if draft.reasoning_chain else ["推理链为空"]
    for i, step in enumerate(draft.reasoning_chain):
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
    if draft.proposed_action is ProposedAction.misstatement and draft.misstatement_draft is None:
        errors.append("建议认定错报, 但没有错报草稿")
    return errors


class PointerResolver:
    """一次校验内解析证据指针: 按 source_type 到对应存储查, 同一来源只查一次."""

    def __init__(self, validator: "OutputValidator", eid: str) -> None:
        self.validator = validator
        self.eid = eid
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}  # 解析得了的行, (行类型, 行 ID) -> 行
        # (source_type, source_id) -> (查得的对象, 查不到的原因)
        self._cache: dict[tuple[str, str], tuple[Any, str]] = {}
        self._documents: dict[str, DocumentRef] | None = None  # 项目登记的文件, 首次用到时取

    async def lookup(self, source_type: str, source_id: str) -> tuple[Any, str]:
        """取指针来源: 行, 准则片段, 文件登记或留痕. 返回 (对象, 原因), 查不到时对象为 None."""
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
        # 留痕只认外部调用, 如登记查询: 审计师直接向官方系统查得, 属外部原件 (同 ACC-04)
        return p.model_copy(update={"type": EvidenceType.external, "reliability": Reliability.external_original}), ""


class OutputValidator:
    """LLM 输出的四级校验: 语法, 结构, 引用, 语义."""

    def __init__(self, financial: FinancialDataStore, library: ProcedureLibrary, trail: AuditTrail) -> None:
        self.financial = financial  # 解析行指针与文件登记
        self.library = library  # 解析准则片段
        self.trail = trail  # 解析留痕指针

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
        unsupported: list[int] = []
        if isinstance(output, JudgmentDraft):
            output, level, unsupported, errors = await self._judgment(eid, output, ctx)
        elif isinstance(output, ContractExtraction | ConfirmationExtraction):
            output, errors = self._extraction(output, ctx.document)
            level = "reference" if errors else None
        else:
            raise NotImplementedError(f"{model.__name__} 的引用与语义校验")
        return ValidationReport(
            level_failed=level,
            retryable=False,
            unsupported_steps=unsupported,
            errors=errors,
            output=output,
        )

    async def _judgment(
        self, eid: str, draft: JudgmentDraft, ctx: ValidationContext
    ) -> tuple[JudgmentDraft, Level | None, list[int], list[str]]:
        """判断草稿的引用与语义两级. 返回 (改写指针后的草稿, 未通过的级别, 不受支持的步骤, 错误)."""
        resolver = PointerResolver(self, eid)
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

    @staticmethod
    def _extraction(
        output: ContractExtraction | ConfirmationExtraction, document: DocumentRef | None
    ) -> tuple[ContractExtraction | ConfirmationExtraction, list[str]]:
        """抽取输出的引用级, 返回 (改写指针后的输出, 错误).

        指针只能指向本文档且页码在页数内, 条款页码同样; 有一处不成立即整条失败.
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
        return output.model_copy(update={"evidence": evidence}), errors


class JudgmentEngine:
    """判断引擎: 组装上下文, 调大模型, 校验输出, 算置信度, 产出 Judgment."""

    async def judge(
        self, eid: str, template: ProcedureTemplate, run: ProcedureRun, ctx: JudgmentContext, performed_by: str
    ) -> JudgmentOutcome:
        raise NotImplementedError("组装上下文 → LLMClient → OutputValidator (重试 2 次)→ 置信度")
