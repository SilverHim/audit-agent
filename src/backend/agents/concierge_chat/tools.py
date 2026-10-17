"""审计助理的只读工具: 模型按需查询项目状态, 判断, 证据与留痕.

规则:
- 全部只读: 不写判断字段, 不改流程状态, 不作决定 (规格书 5.2 节 A10 的边界).
- 项目由服务端绑定 (ToolEnv.eid), 工具参数里没有项目 ID, 模型查不到别的项目.
- 带版本的对象取 AuditOutputStore 的最新版; 位置与重要性取 StateReader.overview, 闸门材料取 StateReader.gate.
- 只发需要的字段 (规格书 4.3 节外发数据边界). 另在 ToolBox.call 出口对全部结果统一做一次 minimize (递归): 账号,
  BSB 打码, 人员字段换稳定化名 (同一人同一化名), 各工具不再各自处理, 免得两次化名后同一人在不同工具里代号不同.
- 结果是给模型的 JSON 文本, 超过 ToolEnv.max_chars 截断; 参数不合法, 未知工具, 查不到, 工具出错都回错误文本, 不中断回答.
- 结果中出现的可引用编号进 ToolOutcome.cites, 回答提到它们时生成引用 (citations.py).
"""

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from backend.agents.base import AgentDeps
from backend.contracts.conversation import ConciergeCitation, Lang, ToolCall, ToolSpec
from backend.contracts.models import (
    AuditException,
    AuditReportVersion,
    Contract,
    EvidencePointer,
    EvidenceType,
    GateDecision,
    GateId,
    Judgment,
    Misstatement,
    Procedure,
    ProcedureRun,
    Reliability,
    ReviewTask,
    SourceType,
    TaskStatus,
    TrailEntry,
    TrailKind,
    UncorrectedSchedule,
)
from backend.contracts.views import EngagementOverview, GateView
from backend.services.deferrals import active_deferrals, target_of
from backend.services.judgment_engine import DOCUMENT_EVIDENCE, ROW_EVIDENCE
from backend.services.trail_pages import all_of_kind


class StateReader(Protocol):
    """项目位置与闸门材料的只读查询; orchestration.query.GraphStateQuery 满足它. 包内不反向导入编排层."""

    async def overview(self, eid: str) -> EngagementOverview: ...

    async def gate(self, eid: str) -> GateView | None: ...


@dataclass(frozen=True)
class ToolEnv:
    """工具的运行环境: 绑定项目与依赖, 由 ConciergeChat 每轮建一个."""

    eid: str  # 当前项目, 工具只查它
    deps: AgentDeps  # 存储与程序库
    state: StateReader  # 项目位置与闸门材料
    max_chars: int  # 单个工具结果的最多字符数


@dataclass(frozen=True)
class Found:
    """工具查到的内容: data 为给模型的 JSON 对象, cites 为其中可引用的编号 -> 引用."""

    data: Any
    cites: dict[str, ConciergeCitation] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolOutcome:
    """一次工具调用的结果."""

    content: str  # 给模型的 JSON 文本 (可能已截断)
    ok: bool  # 是否查到; 参数错误, 查不到或出错为假
    cites: dict[str, ConciergeCitation]  # 可引用的编号 -> 引用


class ToolFailed(Exception):
    """工具按预期报的错 (如查不到): 文本回给模型."""


@dataclass(frozen=True)
class Tool:
    """一个工具: 名称, 说明, 参数模型, 执行函数与进度提示文字."""

    name: str
    description: str
    args: type[BaseModel]
    run: Callable[[ToolEnv, Any], Awaitable[Found]]
    label: Mapping[Lang, str]  # 流式 tool 事件的提示, 如 "查看判断"

    def spec(self) -> ToolSpec:
        schema = self.args.model_json_schema()
        schema.pop("title", None)
        return ToolSpec(name=self.name, description=self.description, parameters=schema)


# ── 参数模型 ──


class NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Page(NoArgs):
    limit: int = Field(20, ge=1, le=50, description="最多返回几条")
    offset: int = Field(0, ge=0, description="跳过前几条, 用于翻页")


class ByProcedure(Page):
    procedure_id: str | None = Field(None, description="只看这条程序, 如 REV-02; 不填为全部")


class JudgmentArgs(NoArgs):
    judgment_id: str = Field(description="判断 ID, 如 J-REV-02-1-SI-2026-0612")


class ProcedureArgs(NoArgs):
    procedure_id: str = Field(description="程序编号, 如 CSH-02")


EvidenceSource = Literal[
    "journal_entry", "sales_invoice", "delivery_note", "bank_transaction", "bank_account", "trial_balance", "document"
]


class EvidenceArgs(NoArgs):
    source_type: EvidenceSource = Field(description="证据所在的表; trial_balance 的 source_id 为科目代码")
    source_id: str = Field(description="行 ID 或文件 ID, 取自证据指针的 source_id")


class ContractArgs(NoArgs):
    contract_id: str | None = Field(None, description="合同编号, 如 CON-001; 不填为全部合同的概要")


class StandardArgs(NoArgs):
    standard_id: str = Field(description="准则片段 ID, 如 AASB15-P31")


TrailKindArg = Literal["llm_call", "external_call", "decision", "transition", "deferral"]


class TrailArgs(Page):
    kind: TrailKindArg | None = Field(None, description="留痕类型; 不填为全部")
    procedure_id: str | None = Field(None, description="只看这条程序")


# ── 字段整理 ──

MASKED_FIELDS = frozenset({"account_no", "bsb"})  # 只留末三位
# 换成稳定化名的人员字段 (人名或用户 ID). superseded_by, attributed_by 存的是判断 ID, 不在其中
PERSON_FIELDS = frozenset(
    {"created_by", "approved_by", "signed_by", "performed_by", "reviewed_by", "decided_by", "updated_by", "deferred_by"}
)


def aud(cents: int | None) -> str | None:
    """金额 (分) 的显示: AUD 1,234.56; 空为 None. 给模型现成的写法, 免得它自己换算出错."""
    if cents is None:
        return None
    return f"AUD {Decimal(cents) / 100:,.2f}"


def pseudonym(name: str) -> str:
    """人员化名: 同一人同一化名, 能看出 "录入人与审批人是否同一人", 看不到姓名."""
    return "P-" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:6]


def minimize(value: Any) -> Any:
    """外发版本 (递归处理嵌套的对象与列表): 账号, BSB 打码; 人员字段换化名; 金额 (分) 另加 *_aud 写法;
    去掉 engagement_id. 凡是整体 model_dump 后外发的内容 (闸门材料, 例外明细, 财务数据行, 错报) 都要经过它.
    *_aud 一律以 *_cents 的换算为准: 先转完全部字段, 再写换算值, 与输入里已有的同名字段及键的顺序无关."""
    if isinstance(value, list):
        return [minimize(item) for item in value]
    if not isinstance(value, Mapping):
        return value
    out: dict[str, Any] = {}
    for key, item in value.items():
        if key == "engagement_id":
            continue
        if key in MASKED_FIELDS and isinstance(item, str):
            out[key] = "***" + item[-3:]
        elif key in PERSON_FIELDS and isinstance(item, str) and item:
            out[key] = pseudonym(item)
        else:
            out[key] = minimize(item)
    for key, item in value.items():
        if key.endswith("_cents") and isinstance(item, int) and not isinstance(item, bool):
            out[key[: -len("_cents")] + "_aud"] = aud(item)
    return out


def pointer_brief(p: EvidencePointer) -> dict[str, Any]:
    brief: dict[str, Any] = {"source_type": p.source_type, "source_id": p.source_id, "locator": p.locator}
    if p.page is not None:
        brief["page"] = p.page
    return brief


def cite_pointer(p: EvidencePointer) -> ConciergeCitation:
    label = p.source_id if p.page is None else f"{p.source_id} p.{p.page}"
    return ConciergeCitation(kind="evidence", id=p.source_id, label=label, pointer=p)


def cite_judgment(j: Judgment) -> ConciergeCitation:
    return ConciergeCitation(kind="judgment", id=j.id, label=f"{j.procedure_id} {j.subject_id}")


def cite_procedure(procedure_id: str, name: str) -> ConciergeCitation:
    return ConciergeCitation(kind="procedure", id=procedure_id, label=f"{procedure_id} {name}")


def cite_trail(entry: TrailEntry) -> ConciergeCitation:
    assert entry.id is not None  # 读回的留痕都有 ID
    return ConciergeCitation(kind="trail", id=entry.id, label=entry.summary[:60])


def add_pointers(cites: dict[str, ConciergeCitation], pointers: Sequence[EvidencePointer]) -> None:
    for p in pointers:
        cites.setdefault(p.source_id, cite_pointer(p))


def critic_fields(judgment: Judgment) -> dict[str, Any]:
    """独立复核 (A7, GEN-09) 的字段: Judgment 上以 critic_ 开头的全部字段 (结论, 理由类型, 理由, 独立结论与推理链等);
    按前缀取, 之后新增的复核字段自动带上."""
    dumped = judgment.model_dump(mode="json")
    return {k: v for k, v in dumped.items() if k.startswith("critic_")}


def judgment_brief(j: Judgment) -> dict[str, Any]:
    brief = {
        "id": j.id,
        "version": j.version,
        "procedure_id": j.procedure_id,
        "subject_id": j.subject_id,
        "conclusion": j.conclusion[:300],
        "confidence": j.confidence,
        "proposed_action": j.proposed_action.value,
        "review_status": j.review_status.value,
    }
    verdict = critic_fields(j).get("critic_verdict")
    if verdict is not None:
        brief["critic_verdict"] = verdict
    return brief


def paged(items: Sequence[Any], args: Page) -> tuple[list[Any], dict[str, int]]:
    """按 limit, offset 取一页, 另给总数."""
    page = list(items[args.offset : args.offset + args.limit])
    return page, {"total": len(items), "offset": args.offset, "returned": len(page)}


def procedure_name(env: ToolEnv, procedure_id: str) -> str:
    try:
        return env.deps.library.load(procedure_id).name
    except KeyError:
        return procedure_id


# ── 工具 ──


async def get_overview(env: ToolEnv, args: NoArgs) -> Found:
    o = await env.state.overview(env.eid)
    cites = {p.id: cite_procedure(p.id, p.name) for p in o.procedures}
    completed = sum(1 for p in o.procedures if p.status.value == "completed")
    return Found(
        {
            "client_name": o.client_name,
            "period_start": o.engagement.period_start.isoformat(),
            "period_end": o.engagement.period_end.isoformat(),
            "phase": o.engagement.phase.value,
            "locked": o.engagement.locked,
            "running": o.running,
            "run_error": o.run_error,
            "current_gate": o.current_gate.value if o.current_gate else None,
            "awaiting_review": o.awaiting_review,
            "pending_review_tasks": o.pending_tasks,
            "procedures_total": len(o.procedures),
            "procedures_completed": completed,
            "materiality": aud(o.materiality_cents),
            "performance_materiality": aud(o.performance_materiality_cents),
            "clearly_trivial_threshold": aud(o.clearly_trivial_cents),
            "opinion_route": o.opinion_route.value if o.opinion_route else None,
            "procedures": [
                {
                    "id": p.id,
                    "name": p.name,
                    "phase": p.phase.value,
                    "status": p.status.value,
                    "autonomy_level": p.autonomy_level.value,
                    "pending_review_tasks": p.pending_tasks,
                }
                for p in o.procedures
            ],
        },
        cites,
    )


async def get_gate(env: ToolEnv, args: NoArgs) -> Found:
    view = await env.state.gate(env.eid)
    if view is None:
        return Found({"current_gate": None, "note": "项目当前不在阶段决定处"})
    material = view.material.model_dump(mode="json")
    if "client_profile" in material:  # 只给名称与 ABN, 董事名单等不外发
        profile = material["client_profile"]
        material["client_profile"] = {"legal_name": profile.get("legal_name"), "abn": profile.get("abn")}
    cites: dict[str, ConciergeCitation] = {}
    for item in material.get("exceptions", []) + material.get("analytics_exceptions", []):
        add_pointers(cites, [EvidencePointer.model_validate(p) for p in item.get("evidence", [])])
    return Found({"gate": view.gate.value, "target_id": view.target_id, "material": material}, cites)


async def list_open_items(env: ToolEnv, args: NoArgs) -> Found:
    """待人处理的事项, 按 阶段决定 → 复核任务 的顺序 (补交请求接口落地后排在两者之间)."""
    deps, eid = env.deps, env.eid
    overview = await env.state.overview(eid)
    tasks = {t.id: t for t in await deps.output_store.latest(eid, ReviewTask)}
    decisions = await deps.output_store.latest(eid, GateDecision)
    entries = await all_of_kind(deps.trail, eid, TrailKind.deferral)
    deferred = {target_of(e) for e in active_deferrals(entries, tasks, overview.current_gate, decisions)}
    cites: dict[str, ConciergeCitation] = {}
    items: list[dict[str, Any]] = []
    if overview.current_gate is not None:
        gate = overview.current_gate
        items.append({"type": "gate_decision", "gate": gate.value, "deferred": ("gate", gate.value) in deferred})
    pending = sorted((t for t in tasks.values() if t.status is TaskStatus.pending), key=lambda t: t.created_at)
    judgments = {j.id: j for j in await deps.output_store.latest(eid, Judgment)}
    for task in pending:
        item: dict[str, Any] = {
            "type": "review_task",
            "task_id": task.id,
            "kind": task.kind.value,
            "procedure_id": task.procedure_id,
            "procedure_name": procedure_name(env, task.procedure_id),
            "target_type": task.target_type.value,
            "target_id": task.target_id,
            "subject_id": task.subject_id,
            "deferred": ("review_task", task.id) in deferred,
        }
        judgment = judgments.get(task.target_id)
        if judgment is not None:
            item["subject_id"] = judgment.subject_id
            item["conclusion"] = judgment.conclusion[:200]
            cites[judgment.id] = cite_judgment(judgment)
        cites.setdefault(task.procedure_id, cite_procedure(task.procedure_id, item["procedure_name"]))
        items.append(item)
    return Found({"items": items, "count": len(items)}, cites)


async def list_judgments(env: ToolEnv, args: ByProcedure) -> Found:
    filters = {"procedure_id": args.procedure_id} if args.procedure_id else {}
    judgments = sorted(await env.deps.output_store.latest(env.eid, Judgment, **filters), key=lambda j: j.id)
    page, meta = paged(judgments, args)
    return Found({"judgments": [judgment_brief(j) for j in page], **meta}, {j.id: cite_judgment(j) for j in page})


async def get_judgment(env: ToolEnv, args: JudgmentArgs) -> Found:
    judgment = await env.deps.output_store.get(env.eid, Judgment, args.judgment_id)
    if judgment is None:
        raise ToolFailed(f"没有判断 {args.judgment_id}")
    cites = {judgment.id: cite_judgment(judgment)}
    steps = []
    for i, step in enumerate(judgment.reasoning_chain):
        add_pointers(cites, step.evidence)
        steps.append(
            {
                "step": i,
                "kind": step.kind.value,
                "text": step.text,
                "status": step.status.value,
                "from_steps": step.from_steps,
                "evidence": [pointer_brief(p) for p in step.evidence],
            }
        )
    add_pointers(cites, judgment.evidence)
    draft = judgment.misstatement_draft
    data = {
        **judgment_brief(judgment),
        "conclusion": judgment.conclusion,
        "confidence_parts": judgment.confidence_parts,
        "autonomy_level": judgment.autonomy_level.value,
        "counter_argument": judgment.counter_argument,
        "reasoning_chain": steps,
        "evidence": [pointer_brief(p) for p in judgment.evidence],
        "misstatement_draft": (
            {**draft.model_dump(mode="json"), "amount_aud": aud(draft.amount_cents)} if draft is not None else None
        ),
        "exception_ids": judgment.exception_ids,
        "superseded_by": judgment.superseded_by,
        "created_at": judgment.created_at.isoformat(),
        **critic_fields(judgment),
    }
    return Found(data, cites)


async def get_procedure(env: ToolEnv, args: ProcedureArgs) -> Found:
    deps, eid, pid = env.deps, env.eid, args.procedure_id
    procedure = await deps.output_store.get(eid, Procedure, pid)
    if procedure is None:
        raise ToolFailed(f"本项目没有程序 {pid}")
    try:
        template = deps.library.load(pid)
    except KeyError:
        raise ToolFailed(f"程序库没有 {pid}") from None
    runs = sorted(await deps.output_store.latest(eid, ProcedureRun, procedure_id=pid), key=lambda r: r.run_no)
    exceptions = await deps.output_store.latest(eid, AuditException, procedure_id=pid)
    judgments = await deps.output_store.latest(eid, Judgment, procedure_id=pid)
    data = {
        "id": pid,
        "name": template.name,
        "phase": procedure.phase.value,
        "status": procedure.status.value,
        "engine": template.engine.value,
        "autonomy_level": procedure.autonomy_level.value,
        "objective_ids": procedure.objective_ids,
        "depends_on": list(template.depends_on),
        "standards": list(template.standards),
        "runs": [
            {
                "run_no": r.run_no,
                "trigger": r.trigger.value,
                "status": r.status.value,
                "started_at": r.started_at.isoformat(),
                "ended_at": r.ended_at.isoformat() if r.ended_at else None,
                "failure_reason": r.failure_reason,
            }
            for r in runs
        ],
        "exceptions": len(exceptions),
        "judgments": [judgment_brief(j) for j in sorted(judgments, key=lambda j: j.id)],
    }
    cites = {pid: cite_procedure(pid, template.name), **{j.id: cite_judgment(j) for j in judgments}}
    return Found(data, cites)


async def list_exceptions(env: ToolEnv, args: ByProcedure) -> Found:
    filters = {"procedure_id": args.procedure_id} if args.procedure_id else {}
    exceptions = sorted(await env.deps.output_store.latest(env.eid, AuditException, **filters), key=lambda x: x.id)
    page, meta = paged(exceptions, args)
    cites: dict[str, ConciergeCitation] = {}
    items = []
    for x in page:
        add_pointers(cites, x.evidence)
        items.append(
            {
                "id": x.id,
                "procedure_id": x.procedure_id,
                "rule_id": x.rule_id,
                "severity": x.severity,
                "source_ids": x.source_ids,
                "amount": aud(x.amount_cents),
                "disposition": x.disposition.value,
                "attributed_by": x.attributed_by,
                "detail": x.detail,
                "evidence": [pointer_brief(p) for p in x.evidence],
            }
        )
    return Found({"exceptions": items, **meta}, cites)


async def get_evidence(env: ToolEnv, args: EvidenceArgs) -> Found:
    if args.source_type == "document":
        documents = {d.id: d for d in await env.deps.financial_store.documents(env.eid)}
        document = documents.get(args.source_id)
        if document is None:
            raise ToolFailed(f"没有文件 {args.source_id}")
        data = document.model_dump(mode="json", include={"id", "file_name", "type", "page_count", "status"})
        pointer = EvidencePointer(
            type=DOCUMENT_EVIDENCE.get(document.type, EvidenceType.external),
            source_type="document",
            source_id=document.id,
            locator="document",
            reliability=document.reliability,
        )
        return Found(data, {document.id: cite_pointer(pointer)})
    source_type: SourceType = args.source_type
    row = await env.deps.financial_store.record(env.eid, source_type, args.source_id)
    if row is None:
        raise ToolFailed(f"{args.source_type} 中没有 {args.source_id}")
    evidence_type, reliability = ROW_EVIDENCE[source_type]
    pointer = EvidencePointer(
        type=evidence_type, source_type=source_type, source_id=args.source_id, locator="row", reliability=reliability
    )
    return Found({"source_type": source_type, "row": row}, {args.source_id: cite_pointer(pointer)})


def contract_pointer(contract: Contract, page: int | None = None) -> EvidencePointer:
    return EvidencePointer(
        type=EvidenceType.contract,
        source_type="document",
        source_id=contract.document_id,
        locator="contract",
        page=page,
        reliability=Reliability.internal_original,
    )


async def get_contract(env: ToolEnv, args: ContractArgs) -> Found:
    """DOC-04 抽取出的合同: 不给编号时为全部合同的概要, 给了为该合同的要素与条款原文 (带页码)."""
    contracts = sorted(await env.deps.financial_store.contracts(env.eid), key=lambda c: c.id)
    if args.contract_id is None:
        items = [
            {
                "id": c.id,
                "counterparty": c.counterparty,
                "amount": aud(c.amount_cents),
                "sign_date": c.sign_date.isoformat() if c.sign_date else None,
                "terms": len(c.terms),
            }
            for c in contracts
        ]
        return Found({"contracts": items}, {c.id: cite_contract(c) for c in contracts})
    contract = next((c for c in contracts if c.id == args.contract_id), None)
    if contract is None:
        raise ToolFailed(f"没有合同 {args.contract_id} (合同由合同抽取程序从 PDF 抽出, 可能尚未抽取)")
    cites = {contract.id: cite_contract(contract)}
    for t in contract.terms:
        pointer = contract_pointer(contract, t.page)
        cites[t.id] = ConciergeCitation(kind="evidence", id=t.id, label=f"{t.id} p.{t.page}", pointer=pointer)
    data = {
        "id": contract.id,
        "counterparty": contract.counterparty,
        "counterparty_abn": contract.counterparty_abn,
        "amount": aud(contract.amount_cents),
        "sign_date": contract.sign_date.isoformat() if contract.sign_date else None,
        "terms": [{"id": t.id, "type": t.term_type.value, "page": t.page, "text": t.text} for t in contract.terms],
        "evidence": [pointer_brief(p) for p in contract.evidence],
    }
    return Found(data, cites)


def cite_contract(contract: Contract) -> ConciergeCitation:
    label = f"{contract.id} {contract.counterparty}"
    return ConciergeCitation(kind="evidence", id=contract.id, label=label, pointer=contract_pointer(contract))


async def get_misstatements(env: ToolEnv, args: NoArgs) -> Found:
    deps, eid = env.deps, env.eid
    items = sorted(await deps.output_store.latest(eid, Misstatement), key=lambda m: m.id)
    schedule = await deps.output_store.get(eid, UncorrectedSchedule, "SCHEDULE")
    overview = await env.state.overview(eid)
    judgments = {j.id: j for j in await deps.output_store.latest(eid, Judgment)}
    cites = {m.judgment_id: cite_judgment(judgments[m.judgment_id]) for m in items if m.judgment_id in judgments}
    data = {
        "misstatements": [
            {**m.model_dump(mode="json", exclude={"review_record_id"}), "amount_aud": aud(m.amount_cents)}
            for m in items
        ],
        "schedule": (
            {
                **schedule.model_dump(mode="json"),
                "total_aud": aud(schedule.total_cents),
                "materiality_aud": aud(schedule.materiality_cents),
            }
            if schedule is not None
            else None
        ),
        "materiality": aud(overview.materiality_cents),
        "opinion_route": overview.opinion_route.value if overview.opinion_route else None,
    }
    return Found(data, {k: v for k, v in cites.items() if k is not None})


async def get_report(env: ToolEnv, args: NoArgs) -> Found:
    deps, eid = env.deps, env.eid
    versions = await deps.output_store.latest(eid, AuditReportVersion)
    if not versions:
        return Found({"report": None, "note": "还没有报告草稿"})
    report = max(versions, key=lambda r: r.revision)
    decisions = await deps.output_store.latest(eid, GateDecision, gate=GateId.RPT_01)
    judgments = {j.id: j for j in await deps.output_store.latest(eid, Judgment)}
    cites = {r.id: cite_judgment(judgments[r.id]) for r in report.judgment_refs if r.id in judgments}
    data = {
        "revision": report.revision,
        "version": report.version,
        "opinion_type": report.opinion_type.value,
        "status": report.status.value,
        "report_date": report.report_date.isoformat() if report.report_date else None,
        "sections": {k.value: v for k, v in report.sections.items()},
        "judgment_refs": [r.model_dump(mode="json") for r in report.judgment_refs],
        "rpt01_decisions": [
            {
                "decision": d.decision.value,
                "reason": d.reason,
                "annotations": [a.model_dump(mode="json") for a in d.annotations],
                "decided_at": d.decided_at.isoformat(),
            }
            for d in sorted(decisions, key=lambda d: d.decided_at)
        ],
    }
    return Found(data, cites)


async def get_standard(env: ToolEnv, args: StandardArgs) -> Found:
    fragment = env.deps.library.fragment(args.standard_id)
    if fragment is None:
        raise ToolFailed(f"没有准则片段 {args.standard_id}")
    pointer = EvidencePointer(
        type=EvidenceType.standard, source_type="standard", source_id=fragment.id, locator=fragment.paragraph
    )
    data = fragment.model_dump(mode="json", include={"id", "standard", "paragraph", "topic", "text"})
    return Found(data, {fragment.id: cite_pointer(pointer)})


CONVERSATION_KINDS = frozenset({TrailKind.conversation, TrailKind.conversation_summary})


async def search_trail(env: ToolEnv, args: TrailArgs) -> Found:
    """留痕检索: 只回摘要等元数据, 不回 payload; 不含对话本身的留痕. 按写入时间倒序 (最近的在前)."""
    if args.kind is not None:
        entries = await all_of_kind(env.deps.trail, env.eid, TrailKind(args.kind))
    else:
        entries = []
        while True:
            page = await env.deps.trail.list(env.eid, None, limit=500, offset=len(entries))
            entries += page
            if len(page) < 500:
                break
    hits = [
        e
        for e in entries
        if e.kind not in CONVERSATION_KINDS and (args.procedure_id is None or e.procedure_id == args.procedure_id)
    ]
    hits.reverse()
    page_entries, meta = paged(hits, args)
    items = [
        {
            "id": e.id,
            "kind": e.kind.value,
            "actor": e.actor,
            "procedure_id": e.procedure_id,
            "summary": e.summary,
            "model": e.model,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in page_entries
    ]
    return Found({"entries": items, **meta}, {cast(str, e.id): cite_trail(e) for e in page_entries})


TOOLS: tuple[Tool, ...] = (
    Tool(
        "get_overview",
        "项目总览: 客户, 审计期间, 所处阶段, 是否在执行, 停在哪个阶段决定, 各程序状态, 待复核数, 重要性水平, 意见类型.",
        NoArgs,
        get_overview,
        {"zh": "查看项目进度", "en": "Checking engagement progress"},
    ),
    Tool(
        "get_gate",
        "当前阶段决定 (承接, 风险与重要性, 意见类型, 报告签发) 的呈报材料; 不在阶段决定处时说明.",
        NoArgs,
        get_gate,
        {"zh": "查看阶段决定材料", "en": "Checking the decision material"},
    ),
    Tool(
        "list_open_items",
        "待审计师处理的事项, 依次为阶段决定, 复核任务; 标明哪些已标记稍后处理.",
        NoArgs,
        list_open_items,
        {"zh": "查看待处理事项", "en": "Checking open items"},
    ),
    Tool(
        "list_judgments",
        "AI 判断列表 (结论摘要, 置信度, 建议, 复核状态); 可按程序过滤, 可翻页.",
        ByProcedure,
        list_judgments,
        {"zh": "查看判断列表", "en": "Listing judgments"},
    ),
    Tool(
        "get_judgment",
        "一条判断的完整内容: 结论, 置信度及分项, 推理链 (每步的证据指针), 反方论证, 建议的错报, 独立复核结论.",
        JudgmentArgs,
        get_judgment,
        {"zh": "查看判断", "en": "Reading a judgment"},
    ),
    Tool(
        "get_procedure",
        "一条审计程序: 名称, 状态, 执行方式, 自主度, 覆盖的审计目标, 依赖, 历次执行与失败原因, 产出的判断.",
        ProcedureArgs,
        get_procedure,
        {"zh": "查看审计程序", "en": "Reading a procedure"},
    ),
    Tool(
        "list_exceptions",
        "规则查出的例外: 规则, 严重程度, 涉及的数据行, 金额, 是否已被判断解释; 可按程序过滤, 可翻页.",
        ByProcedure,
        list_exceptions,
        {"zh": "查看例外", "en": "Listing exceptions"},
    ),
    Tool(
        "get_evidence",
        "按证据指针取一行财务数据 (分录, 发票, 发运单, 银行流水, 银行账户, 试算表科目) 或一份文件的登记信息.",
        EvidenceArgs,
        get_evidence,
        {"zh": "查看证据", "en": "Reading evidence"},
    ),
    Tool(
        "get_contract",
        "销售合同 (从合同 PDF 抽取): 不给编号时列出全部合同概要; 给编号时为对方, 金额, 签订日与各条款原文及页码.",
        ContractArgs,
        get_contract,
        {"zh": "查看合同", "en": "Reading a contract"},
    ),
    Tool(
        "get_misstatements",
        "已认定的错报, 未更正错报汇总表, 整体重要性与意见类型.",
        NoArgs,
        get_misstatements,
        {"zh": "查看错报", "en": "Checking misstatements"},
    ),
    Tool(
        "get_report",
        "当前审计报告草稿 (最新一轮修订) 的各段落, 引用的判断, 以及报告签发环节的退回意见.",
        NoArgs,
        get_report,
        {"zh": "查看审计报告", "en": "Reading the audit report"},
    ),
    Tool(
        "get_standard",
        "准则片段原文 (如 AASB 15, ASA 505 的某一段), ID 取自证据指针或程序的 standards.",
        StandardArgs,
        get_standard,
        {"zh": "查看准则原文", "en": "Reading the standard"},
    ),
    Tool(
        "search_trail",
        "审计留痕检索 (大模型调用, 外部调用, 人的决定, 状态迁移, 稍后处理), 只给摘要, 最近的在前; 可按类型与程序过滤.",
        TrailArgs,
        search_trail,
        {"zh": "检索审计留痕", "en": "Searching the audit trail"},
    ),
)


UNKNOWN_LABEL: dict[Lang, str] = {"zh": "查询资料", "en": "Looking something up"}


class ToolBox:
    """工具箱: 给模型的工具说明, 以及按模型的调用执行工具."""

    def __init__(self, tools: Sequence[Tool] = TOOLS) -> None:
        self.tools = {t.name: t for t in tools}
        self.specs = [t.spec() for t in tools]

    def label(self, name: str, lang: Lang) -> str:
        """tool 事件的提示文字. 未知工具用固定文字, 不回显模型给的工具名 (未经校验, 可能是任何内容)."""
        tool = self.tools.get(name)
        return tool.label[lang] if tool is not None else UNKNOWN_LABEL[lang]

    async def call(self, env: ToolEnv, call: ToolCall) -> ToolOutcome:
        """执行一个调用. 参数 JSON 不合法, 不合参数模型, 未知工具, 查不到 (ToolFailed) 与其他异常都转成错误文本.
        取消 (CancelledError) 照常上抛."""
        tool = self.tools.get(call.name)
        if tool is None:
            return failure(f"没有这个工具; 可用的工具: {', '.join(self.tools)}")
        try:
            raw = json.loads(call.arguments) if call.arguments.strip() else {}
        except ValueError as e:
            return failure(f"参数不是合法的 JSON: {e}")
        try:
            args = tool.args.model_validate(raw)
        except ValidationError as e:
            return failure(f"参数不合要求: {e.errors(include_url=False, include_input=False)}")
        try:
            found = await tool.run(env, args)
        except ToolFailed as e:
            return failure(str(e))
        except LookupError as e:
            return failure(f"查不到: {e}")
        except Exception as e:  # 工具出错不中断回答; 只给类型与消息, 由模型换个查法或如实说明
            return failure(f"查询出错 ({type(e).__name__}): {e}")
        content = json.dumps(minimize(found.data), ensure_ascii=False, default=str)
        if len(content) > env.max_chars:
            content = content[: env.max_chars] + "…[结果过长, 已截断; 可用 limit, offset 翻页或换更具体的查询]"
        return ToolOutcome(content=content, ok=True, cites=found.cites)


def failure(message: str) -> ToolOutcome:
    return ToolOutcome(content=json.dumps({"error": message}, ensure_ascii=False), ok=False, cites={})
