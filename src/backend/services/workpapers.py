"""底稿记录 (需求 R330, Issue #50; 接口文档第 7 节 "底稿记录"): 每个程序底稿索引号一个 Workpaper (ID WP-{索引号}),
随程序执行终态与相关决定刷新; 审计师改结论, 补充说明与签署各出一版并写 decision 留痕.

- 依据 (basis_of): 本程序的执行记录 (终态), 当前执行的被测总体与例外, 本程序的判断, 本程序闸门的决定, 模板 outputs
  中材料类字段的对象 (OUTPUT_BASIS), 各取最新版本. 刷新时依据变了 (或结论草稿, 缺项变了) 才出新版本, 否则不写
  (幂等); 依据变了时已签署的改为 needs_rereview, 签署移到 last_signoff.
- 结论草稿 (Q27 选项 1, 模板拼): 规则程序列范围, 被测对象数与金额, 各检查项的结果与例外; 判断程序列各对象的建议,
  置信度与复核状态, 待复核几项, 末句 "最终结论待审计师复核后确定"; 其余程序为空 (R332, R333 补). 中英两种都写.
- 必填项 (missing_of): 底稿配置 required 中该底稿的各项 (ProcedureLibrary.required_items) 逐项按 SATISFIED 判断;
  可由补充说明补齐的 (supplementable) 另看补充说明的 item. 签署不阻塞 FIN-01 与 RPT-01.

刷新 (refresh) 在程序执行终态与决定提交之后由编排调用 (orchestration/nodes, decisions), 出错只记日志; 项目已锁定
(签字或拒绝承接) 时不刷新 (归档见 R337). 刷新本身不写留痕: 每一版都在 Store 中 (写入者为程序的执行智能体)."""

from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from backend.contracts.data import ExtractionRecord
from backend.contracts.errors import DataRejected
from backend.contracts.knowledge import ProcedureTemplate, RequiredItem
from backend.contracts.models import (
    DEFAULT_CURRENCY,
    AcceptanceScorecard,
    AnalyticsResult,
    AuditException,
    AuditObjective,
    AuditPlan,
    AuditReportVersion,
    Confirmation,
    Contract,
    DocumentRef,
    Engagement,
    EngagementTerms,
    ExceptionDisposition,
    GateDecision,
    GateId,
    GateVerdict,
    Judgment,
    LocalizedText,
    MaterialityCandidates,
    Misstatement,
    ObjectRef,
    OwnershipGraph,
    PlanStatus,
    Procedure,
    ProcedureRun,
    ProposedAction,
    ReviewRecord,
    ReviewScope,
    ReviewStatus,
    ReviewTask,
    ReviewWithdrawal,
    RiskItem,
    RunPopulation,
    RunStatus,
    RunTrigger,
    StepKind,
    TrailEntry,
    TrailKind,
    UncorrectedSchedule,
    Versioned,
    Workpaper,
    WorkpaperNote,
    WorkpaperSignoff,
    WorkpaperStatus,
)
from backend.services.currency import format_money
from backend.services.populations import distinct_items, total_cents
from backend.services.procedure_library import render_text, workpaper_kind
from backend.services.workpaper_content import build_content
from backend.services.workpaper_judgments import model_judgments

if TYPE_CHECKING:
    from backend.agents.base import AgentDeps
    from backend.orchestration.runner import GraphRunner

# 模板 outputs 中这些字段的对象全部计入依据 (材料类产出; 例外, 判断与执行记录另取, 任务与文件不计)
OUTPUT_BASIS: dict[str, type[Versioned]] = {
    "acceptance_scorecard": AcceptanceScorecard,
    "engagement_terms": EngagementTerms,
    "materiality_candidates": MaterialityCandidates,
    "analytics": AnalyticsResult,
    "audit_plans": AuditPlan,
    "confirmations": Confirmation,
    "ownership_graph": OwnershipGraph,
    "uncorrected_schedule": UncorrectedSchedule,
    "misstatements": Misstatement,
    "report_versions": AuditReportVersion,
}
PENDING = frozenset({ExceptionDisposition.open, ExceptionDisposition.unattributed})  # 例外待处理
FINAL_SENTENCE = LocalizedText(
    zh="最终结论待审计师复核后确定.", en="The final conclusion will be determined after review."
)
ACTIONS = {  # 建议动作的写法
    ProposedAction.accept: ("接受", "accept"),
    ProposedAction.close: ("关闭事项", "close"),
    ProposedAction.misstatement: ("认定错报", "misstatement"),
    ProposedAction.more_evidence: ("补充证据", "more evidence"),
}
REVIEWS = {  # 复核状态的写法
    ReviewStatus.pending: ("待复核", "pending review"),
    ReviewStatus.confirmed: ("已确认", "confirmed"),
    ReviewStatus.overridden: ("已推翻", "overridden"),
    ReviewStatus.superseded: ("已取代", "superseded"),
}
SCOPES = {ReviewScope.full: "全部明细", ReviewScope.sample: "抽查", ReviewScope.conclusion_only: "只看结论"}


def workpaper_id(ref: str) -> str:
    """底稿记录的 ID: WP-{索引号}."""
    return f"WP-{ref}"


@dataclass(frozen=True)
class Sources:
    """一次刷新读到的 Store 内容, 都是各对象的最新版本. outputs 为 OUTPUT_BASIS 的字段 → 对象 (只读程序包用到的)."""

    currency: str = DEFAULT_CURRENCY
    engagement: Engagement | None = None
    client_name: str = ""
    people: dict[str, str] = field(default_factory=dict)
    procedures: list[Procedure] = field(default_factory=list)
    objectives: list[AuditObjective] = field(default_factory=list)
    risks: list[RiskItem] = field(default_factory=list)
    program_refs: dict[str, str] = field(default_factory=dict)
    runs: list[ProcedureRun] = field(default_factory=list)
    populations: list[RunPopulation] = field(default_factory=list)
    exceptions: list[AuditException] = field(default_factory=list)
    judgments: list[Judgment] = field(default_factory=list)
    decisions: list[GateDecision] = field(default_factory=list)
    outputs: dict[str, list[Versioned]] = field(default_factory=dict)
    contracts: list[Contract] = field(default_factory=list)
    documents: list[DocumentRef] = field(default_factory=list)
    extractions: list[ExtractionRecord] = field(default_factory=list)
    tasks: list[ReviewTask] = field(default_factory=list)
    reviews: list[ReviewRecord] = field(default_factory=list)
    withdrawals: list[ReviewWithdrawal] = field(default_factory=list)


@dataclass(frozen=True)
class View:
    """一份底稿看到的内容: 程序的终态执行 (按 run_no), 当前执行 (最后一次非增量执行与其后的增量执行), 当前执行的
    被测总体与例外, 本程序的判断, 本程序闸门的决定 (按决定时间), 材料类产出."""

    template: ProcedureTemplate
    runs: list[ProcedureRun]
    current: list[ProcedureRun]
    populations: list[RunPopulation]
    exceptions: list[AuditException]
    judgments: list[Judgment]
    decisions: list[GateDecision]
    outputs: list[Versioned]
    currency: str = DEFAULT_CURRENCY


def view_of(template: ProcedureTemplate, sources: Sources) -> View:
    """template 的底稿从 sources 里看到的内容 (见 View), 纯函数."""
    pid = template.id
    runs = sorted(
        (r for r in sources.runs if r.procedure_id == pid and r.status is not RunStatus.running), key=lambda r: r.run_no
    )
    base = max((i for i, r in enumerate(runs) if r.trigger is not RunTrigger.incremental), default=None)
    current = runs[base:] if base is not None else runs
    ids = {r.id for r in current}
    populations = [p for p in sources.populations if p.run_id in ids]
    exceptions = sorted(
        (x for x in sources.exceptions if x.procedure_id == pid and x.run_id in ids), key=lambda x: x.id
    )
    judgments = sorted((j for j in sources.judgments if j.procedure_id == pid), key=lambda j: j.id)
    decisions = sorted(
        (d for d in sources.decisions if template.gate is not None and d.gate is template.gate),
        key=lambda d: (d.decided_at, d.id),
    )
    outputs = [o for name in template.outputs if name in OUTPUT_BASIS for o in sources.outputs.get(name, [])]
    return View(template, runs, current, populations, exceptions, judgments, decisions, outputs, sources.currency)


def basis_of(view: View) -> list[ObjectRef]:
    """底稿的依据: 终态执行, 当前执行的被测总体与例外, 判断, 闸门决定, 材料类产出, 各为 (类名, ID, 版本), 按类名与 ID
    排序. 都没有 (程序还没执行, 闸门还没决定) 为空, 不建底稿记录."""
    objects: list[Versioned] = [
        *view.runs,
        *view.populations,
        *view.exceptions,
        *view.judgments,
        *view.decisions,
        *view.outputs,
    ]
    refs = {(type(o).__name__, o.id): o.version for o in objects}
    return [ObjectRef(kind=kind, id=oid, version=version) for (kind, oid), version in sorted(refs.items())]


def joined(parts: Iterable[LocalizedText]) -> LocalizedText | None:
    """各句以空格接成一段 (中文同样用半角标点, 句间空一格), 一句也没有为空."""
    found = list(parts)
    if not found:
        return None
    return LocalizedText(zh=" ".join(p.zh for p in found), en=" ".join(p.en for p in found))


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def rule_draft(view: View) -> LocalizedText | None:
    """规则程序的结论草稿 (Q27): 最后一次执行的范围 (模板 scope 按参数快照渲染), 被测对象数与金额 (去重, 见
    services.populations), 各执行了的规则的检查结果 (总体项数, 其中出了例外的项数, 不在该规则总体中的不作判断),
    没有执行的规则与原因, 例外数与待处理数. 没有成功的执行为空."""
    succeeded = [r for r in view.current if r.status is RunStatus.succeeded]
    if not succeeded:
        return None
    run = succeeded[-1]
    template = view.template
    parts: list[LocalizedText] = []
    if template.scope is not None:
        scope = render_text(template.scope, run.params_snapshot)
        parts.append(LocalizedText(zh=f"范围: {scope.zh}.", en=f"Scope: {scope.en}."))
    population = next((p for p in view.populations if p.run_id == run.id), None)
    items = distinct_items(population) if population is not None else None
    if items is not None:
        amount = total_cents(i.amount_cents for i in items)
        zh, en = f"被测对象 {len(items)} 项", f"{plural(len(items), 'item')} tested"
        if amount is not None:
            zh, en = (
                f"{zh}, 合计 {format_money(amount, view.currency)}",
                f"{en}, totalling {format_money(amount, view.currency)}",
            )
        parts.append(LocalizedText(zh=f"{zh}.", en=f"{en}."))
    exceptions = [x for x in view.exceptions if x.run_id == run.id]
    specs = {r.id: r for r in template.rules}
    for rule in population.rules if population is not None else []:
        spec = specs.get(rule.rule_id)
        label = spec.check if spec is not None and spec.check is not None else None
        zh_label = label.zh if label is not None else (spec.name if spec is not None else rule.rule_id)
        en_label = label.en if label is not None else f"Rule {rule.rule_id}"
        failed = len({x.source_ids[0] if x.source_ids else x.id for x in exceptions if x.rule_id == rule.rule_id})
        if spec is not None and spec.emit == "rows":
            count = rule.count or 0
            parts.append(LocalizedText(zh=f"{zh_label}: {count} 行.", en=f"{en_label}: {plural(count, 'row')}."))
            continue
        if rule.count is None:
            zh = f"{zh_label}: 例外 {failed} 项 (被测总体未记录)."
            en = f"{en_label}: {plural(failed, 'exception')} (population not recorded)."
        elif failed == 0:
            zh, en = f"{zh_label}: {rule.count} 项均符合", f"{en_label}: all {rule.count} agree"
        else:
            zh = f"{zh_label}: {rule.count} 项中 {failed} 项不符"
            en = f"{en_label}: {failed} of {rule.count} do not agree"
        if rule.count is not None:
            unassessed = len(items) - rule.count if items is not None else 0
            if unassessed > 0:
                zh, en = f"{zh}, {unassessed} 项不作判断", f"{en}, {unassessed} not assessed"
            zh, en = f"{zh}.", f"{en}."
        parts.append(LocalizedText(zh=zh, en=en))
    for outcome in run.rule_outcomes:
        parts.append(
            LocalizedText(
                zh=f"{outcome.rule_id} 未执行: {outcome.reason}.",
                en=f"{outcome.rule_id} not performed: {outcome.reason}.",
            )
        )
    pending = sum(1 for x in exceptions if x.disposition in PENDING)
    if not exceptions:
        parts.append(LocalizedText(zh="未发现例外.", en="No exceptions."))
    else:
        zh, en = (
            f"例外 {len(exceptions)} 条, 待处理 {pending} 条",
            f"{plural(len(exceptions), 'exception')}, {pending} pending",
        )
        explained = len(exceptions) - pending
        if explained:
            zh, en = f"{zh}, 已由判断解释 {explained} 条", f"{en}, {explained} explained by judgments"
        parts.append(LocalizedText(zh=f"{zh}.", en=f"{en}."))
    return joined(parts)


def judgment_draft(view: View) -> LocalizedText | None:
    """判断程序的结论草稿 (Q27 选项 1): 判断了几个对象, 各对象的建议, 置信度与复核状态, 待复核几项, 末句 "最终结论待
    审计师复核后确定". 没有判断为空."""
    judged = model_judgments(view.judgments, view.runs)
    if not judged:
        return None
    zh_items, en_items = [], []
    for j in judged:
        action, review = ACTIONS[j.proposed_action], REVIEWS[j.review_status]
        zh_items.append(f"{j.subject_id} 建议{action[0]}, 置信度 {j.confidence:.2f}, {review[0]}")
        en_items.append(f"{j.subject_id} proposed {action[1]}, confidence {j.confidence:.2f}, {review[1]}")
    pending = sum(1 for j in judged if j.review_status is ReviewStatus.pending)
    head = LocalizedText(
        zh=f"判断对象 {len(judged)} 个: {'; '.join(zh_items)}. 待复核 {pending} 项.",
        en=f"{plural(len(judged), 'item')} judged: {'; '.join(en_items)}. {pending} pending review.",
    )
    return joined([head, FINAL_SENTENCE])


def conclusion_draft(view: View) -> LocalizedText | None:
    """按底稿类别 (procedure_library.workpaper_kind) 拼结论草稿: 规则程序 rule_draft, 判断程序 judgment_draft, 其余
    为空."""
    kind = workpaper_kind(view.template)
    if kind == "rule":
        return rule_draft(view)
    return judgment_draft(view) if kind == "judgment" else None


def noted(workpaper: Workpaper, item: str) -> bool:
    return any(n.item == item for n in workpaper.notes)


def latest_approval(decisions: Iterable[GateDecision], gate: GateId) -> GateDecision | None:
    """某闸门最后一条通过决定, 没有为空."""
    approved = [d for d in decisions if d.gate is gate and d.decision is GateVerdict.approve]
    return max(approved, key=lambda d: (d.decided_at, d.id), default=None)


def has_reason(decision: GateDecision | None) -> bool:
    return decision is not None and bool((decision.reason or "").strip())


def plans(sources: Sources) -> list[AuditPlan]:
    return [p for p in sources.outputs.get("audit_plans", []) if isinstance(p, AuditPlan)]


Check = Callable[[View, Sources, Workpaper], bool]
# 必填项代号 → 是否已记录 (底稿配置 required 的每一项都须在这里, tests/test_workpapers 核对). 可由补充说明补齐的另看
# 补充说明的 item (missing_of). ASA 300 第 12(c) 段与 ASA 320 第 14 段的 "修订" 是有条件的要求: 没有修订时算已记录
SATISFIED: dict[str, Check] = {
    "preparer": lambda v, s, w: bool(v.runs or v.decisions),  # 执行的智能体或作决定的审计师
    "completed_on": lambda v, s, w: any(r.ended_at is not None for r in v.runs) or bool(v.decisions),
    "reviewer": lambda v, s, w: w.signoff is not None,
    "reviewed_on": lambda v, s, w: w.signoff is not None,
    "review_scope": lambda v, s, w: w.signoff is not None,
    "procedures": lambda v, s, w: bool(v.runs or v.decisions),  # 模板写明性质与范围, 执行记录写明时间
    "results": lambda v, s, w: (
        (any(r.status is RunStatus.succeeded for r in v.runs) or bool(v.decisions))
        and all(
            j.evidence
            and all(step.evidence for step in j.reasoning_chain if step.kind in {StepKind.fact, StepKind.standard})
            for j in model_judgments(v.judgments, v.runs)
        )
    ),
    "conclusions": lambda v, s, w: (
        bool(w.conclusion or w.conclusion_draft)
        and all(j.reasoning_chain for j in model_judgments(v.judgments, v.runs))
    ),
    # 规则程序: 当前执行的被测总体都记录了; 判断程序: 有判断的对象
    "tested_items": lambda v, s, w: (
        bool(v.populations) and all(distinct_items(p) is not None for p in v.populations)
        if workpaper_kind(v.template) == "rule"
        else bool(v.judgments)
    ),
    "materiality_amounts": lambda v, s, w: (
        (d := latest_approval(s.decisions, GateId.PLN_01_03)) is not None and d.materiality is not None
    ),
    "materiality_factors": lambda v, s, w: has_reason(latest_approval(s.decisions, GateId.PLN_01_03)),
    "clearly_trivial": lambda v, s, w: bool(s.outputs.get("uncorrected_schedule")),
    "misstatements_accumulated": lambda v, s, w: bool(s.outputs.get("uncorrected_schedule")),
    "uncorrected_conclusion": lambda v, s, w: has_reason(latest_approval(s.decisions, GateId.FIN_01)),
    "audit_strategy": lambda v, s, w: any(p.status is PlanStatus.approved for p in plans(s)),
    "audit_plan": lambda v, s, w: any(p.status is PlanStatus.approved for p in plans(s)),
    "plan_changes": lambda v, s, w: all(p.revision == 1 or bool((p.revision_reason or "").strip()) for p in plans(s)),
}


def missing_of(required: Sequence[RequiredItem], view: View, sources: Sources, workpaper: Workpaper) -> list[str]:
    """workpaper 缺的必填项代号 (按 required 的顺序): SATISFIED 判为未记录, 且不是可补齐而已由补充说明补齐的."""
    return [
        item.id
        for item in required
        if not SATISFIED[item.id](view, sources, workpaper) and not (item.supplementable and noted(workpaper, item.id))
    ]


def unsigned(workpaper: Workpaper) -> dict[str, object]:
    """内容变了时签署的处理 (要并进新版本的字段): 已签署的改为 needs_rereview, 签署移到 last_signoff; 未签署的不变."""
    if workpaper.signoff is None:
        return {}
    return {"status": WorkpaperStatus.needs_rereview, "signoff": None, "last_signoff": workpaper.signoff}


def refreshed(
    previous: Workpaper | None, template: ProcedureTemplate, required: Sequence[RequiredItem], sources: Sources
) -> Workpaper | None:
    """按 sources 刷新 template 的底稿记录, 返回新版本; 没有依据 (还没执行或决定) 或与上一版相同时为空 (幂等).
    第 1 版为 draft; 依据变了时已签署的改为 needs_rereview (unsigned), 草稿与缺项照新算.
    草稿金额的币种或文字变了也要求重新复核;
    仅缺项变动不影响签署. 纯函数."""
    assert template.workpaper is not None
    view = view_of(template, sources)
    basis = basis_of(view)
    if not basis:
        return None
    draft = conclusion_draft(view)
    if previous is None:
        ref = template.workpaper.ref
        new = Workpaper(
            id=workpaper_id(ref), ref=ref, area=template.workpaper.area, procedure_id=template.id, basis=basis
        )
        new = new.model_copy(update={"conclusion_draft": draft})
    else:
        changes: dict[str, object] = {"basis": basis, "conclusion_draft": draft}
        new = previous.model_copy(update=changes)
    content = build_content(view, sources, new)
    if previous is not None and (
        basis != previous.basis or draft != previous.conclusion_draft or content != previous.content
    ):
        new = new.model_copy(update=unsigned(previous))
        content = build_content(view, sources, new)
    new = new.model_copy(update={"content": content})
    new = new.model_copy(update={"missing": missing_of(required, view, sources, new)})
    if previous is not None:
        if new == previous:
            return None
        new = new.model_copy(update={"version": previous.version + 1})
    return new


@dataclass(frozen=True)
class Snapshot:
    """刷新与审计师操作读到的: 程序包里有底稿的模板 (按程序编号), 各自的必填项, Store 内容与已有的底稿记录."""

    templates: dict[str, ProcedureTemplate]
    required: dict[str, list[RequiredItem]]
    sources: Sources
    workpapers: dict[str, Workpaper]


async def load(deps: "AgentDeps", eid: str) -> Snapshot:
    """读刷新要的 Store 内容: 程序包 (Store 中各程序的最新版本) 里有底稿的模板, 执行记录, 被测总体, 例外, 判断, 闸门
    决定, 模板 outputs 用到的材料类产出, 已有的底稿记录."""
    store, library = deps.output_store, deps.library
    pack = await store.latest(eid, Procedure)
    templates = {t.id: t for t in (library.load(p.id) for p in pack) if t.workpaper is not None}
    needed = sorted({name for t in templates.values() for name in t.outputs if name in OUTPUT_BASIS})
    outputs: dict[str, list[Versioned]] = {}
    for name in needed:
        outputs[name] = list(await store.latest(eid, OUTPUT_BASIS[name]))
    engagement = await deps.engagement_store.get(eid)
    profile = (
        await deps.engagement_store.client_profile(engagement.client_id, engagement.client_profile_version)
        if engagement
        else None
    )
    papers = await store.latest(eid, Workpaper)
    people: dict[str, str] = {}
    user_ids = {p.signoff.signed_by for p in papers if p.signoff} | {engagement.owner_id if engagement else ""}
    for uid in sorted(user_ids - {""}):
        user = await deps.users.get(uid)
        if user:
            people[uid] = user.full_name
    sources = Sources(
        engagement=engagement,
        client_name=profile.legal_name if profile else "",
        people=people,
        procedures=pack,
        objectives=await store.latest(eid, AuditObjective),
        risks=await store.latest(eid, RiskItem),
        program_refs={t.id: t.workpaper.ref for t in library.all(include_disabled=True) if t.workpaper},
        currency=engagement.currency if engagement else DEFAULT_CURRENCY,
        runs=await store.latest(eid, ProcedureRun),
        populations=await store.latest(eid, RunPopulation),
        exceptions=await store.latest(eid, AuditException),
        judgments=await store.latest(eid, Judgment),
        decisions=await store.latest(eid, GateDecision),
        outputs=outputs,
        contracts=await deps.financial_store.contracts(eid),
        documents=await deps.financial_store.documents(eid),
        extractions=await deps.data.extraction_versions(eid),
        tasks=await store.latest(eid, ReviewTask),
        reviews=await store.latest(eid, ReviewRecord),
        withdrawals=await store.latest(eid, ReviewWithdrawal),
    )
    required = {pid: library.required_items(t.workpaper.ref) for pid, t in templates.items() if t.workpaper}
    workpapers = {w.id: w for w in papers}
    return Snapshot(templates, required, sources, workpapers)


def plan_refresh(snapshot: Snapshot) -> list[tuple[Workpaper, ProcedureTemplate]]:
    """snapshot 下要写的底稿新版本 (与其模板), 按程序编号; 纯函数."""
    planned = []
    for pid, template in sorted(snapshot.templates.items()):
        assert template.workpaper is not None
        previous = snapshot.workpapers.get(workpaper_id(template.workpaper.ref))
        new = refreshed(previous, template, snapshot.required[pid], snapshot.sources)
        if new is not None:
            planned.append((new, template))
    return planned


async def refresh(deps: "AgentDeps", eid: str) -> list[Workpaper]:
    """刷新项目的全部底稿记录 (见模块说明), 在一个事务内写入变了的 (写入者为程序的执行智能体), 返回写入的. 项目不存在
    或已锁定 (签字或拒绝承接) 时不刷新, 返回空."""
    engagement = await deps.engagement_store.get(eid)
    if engagement is None or engagement.locked:
        return []
    planned = plan_refresh(await load(deps, eid))
    written: list[Workpaper] = []
    if planned:
        async with deps.output_store.transaction(eid):
            for workpaper, template in planned:
                [stored] = await deps.output_store.put(eid, [workpaper], actor=template.owner)
                assert isinstance(stored, Workpaper)
                written.append(stored)
    return written


def workpaper_trail(
    eid: str, workpaper: Workpaper, actor: str, action: str, summary: str, **details: object
) -> TrailEntry:
    """审计师对底稿一次操作的 decision 留痕: payload 为 {action, ref, version, ...details}, 记在所属程序下, 防重复键
    {eid}:workpaper:{索引号}:v{版本}."""
    return TrailEntry(
        engagement_id=eid,
        kind=TrailKind.decision,
        actor=actor,
        procedure_id=workpaper.procedure_id,
        summary=summary,
        idempotency_key=f"{eid}:workpaper:{workpaper.ref}:v{workpaper.version}",
        payload={"action": action, "ref": workpaper.ref, "version": workpaper.version, **details},
    )


class WorkpaperService:
    """底稿记录的读取与审计师的三种操作 (需求 R330, 接口 tag workpapers). 操作持项目锁 (锁定 423, 忙 409): 先刷新,
    再在最新一版上改出新版本, 一个事务内写入, 提交后写 decision 留痕. 索引号没有底稿记录 (程序还没执行) 时 404."""

    def __init__(self, deps: "AgentDeps", runner: "GraphRunner") -> None:
        self.deps = deps
        self.runner = runner

    async def list(self, eid: str) -> list[Workpaper]:
        """项目的底稿记录 (各最新版本), 按档案顺序 (ProcedureLibrary.workpaper_order)."""
        found = await self.deps.output_store.latest(eid, Workpaper)
        return sorted(found, key=lambda w: self.deps.library.workpaper_order(w.ref))

    @asynccontextmanager
    async def holding(self, eid: str) -> AsyncIterator[Snapshot]:
        """查项目 (不存在 LookupError, 锁定 ENGAGEMENT_LOCKED), 取项目锁 (忙 ENGAGEMENT_BUSY), 刷新后交出最新的
        snapshot; 块结束释放锁."""
        engagement = await self.deps.engagement_store.get(eid)
        if engagement is None:
            raise LookupError(f"Engagement {eid} not found")
        if engagement.locked:
            raise DataRejected("ENGAGEMENT_LOCKED", eid)
        self.runner.acquire(eid)
        try:
            engagement = await self.deps.engagement_store.get(eid)
            if engagement is None:
                raise LookupError(f"Engagement {eid} not found")
            if engagement.locked:
                raise DataRejected("ENGAGEMENT_LOCKED", eid)
            await refresh(self.deps, eid)
            yield await load(self.deps, eid)
        finally:
            self.runner.release(eid)

    async def _write(self, eid: str, snapshot: Snapshot, new: Workpaper, actor: str, trail: TrailEntry) -> Workpaper:
        template = snapshot.templates[new.procedure_id]
        view = view_of(template, snapshot.sources)
        new = new.model_copy(
            update={"missing": missing_of(snapshot.required[template.id], view, snapshot.sources, new)}
        )
        new = new.model_copy(update={"content": build_content(view, snapshot.sources, new)})
        async with self.deps.output_store.transaction(eid):
            [stored] = await self.deps.output_store.put(eid, [new], actor=actor)
        assert isinstance(stored, Workpaper)
        await self.deps.trail.record(trail)
        return stored

    @staticmethod
    def current(snapshot: Snapshot, ref: str) -> Workpaper:
        found = snapshot.workpapers.get(workpaper_id(ref))
        if found is None:
            raise LookupError(f"底稿 {ref} 还没有记录 (程序没有执行或闸门没有决定)")
        return found

    async def conclude(self, eid: str, ref: str, text: str, actor: str) -> Workpaper:
        """审计师改写结论 (POST .../conclusion): 新版本记结论, 改写人与时间, 草稿照留; 已签署的改为须重新复核.
        文字去首尾空白后为空 MISSING_FIELD; 与当前结论相同时返回当前版本, 不再写."""
        text = text.strip()
        if not text:
            raise DataRejected("MISSING_FIELD", "text")
        async with self.holding(eid) as snapshot:
            workpaper = self.current(snapshot, ref)
            if workpaper.conclusion == text:
                return workpaper
            changes = {"conclusion": text, "conclusion_by": actor, "conclusion_at": datetime.now(UTC)}
            new = workpaper.model_copy(update={**changes, **unsigned(workpaper), "version": workpaper.version + 1})
            trail = workpaper_trail(eid, new, actor, "workpaper_conclusion", f"审计师改写底稿 {ref} 的结论", text=text)
            return await self._write(eid, snapshot, new, actor, trail)

    async def note(self, eid: str, ref: str, text: str, item: str | None, actor: str) -> Workpaper:
        """审计师补充说明 (POST .../notes): 新版本追加一条; item 为它补齐的必填项代号, 须是该底稿可由补充说明补齐的
        (底稿配置 supplementable), 否则 INVALID_VALUE; 补齐后该项从 missing 去掉. 已签署的改为须重新复核. 文字去首尾
        空白后为空 MISSING_FIELD."""
        text = text.strip()
        if not text:
            raise DataRejected("MISSING_FIELD", "text")
        async with self.holding(eid) as snapshot:
            workpaper = self.current(snapshot, ref)
            if item is not None:
                allowed = [r.id for r in snapshot.required[workpaper.procedure_id] if r.supplementable]
                if item not in allowed:
                    detail = f"{item} 不是底稿 {ref} 可由补充说明补齐的必填项 ({', '.join(allowed) or '没有'})"
                    raise DataRejected("INVALID_VALUE", detail)
            added = WorkpaperNote(text=text, item=item, added_by=actor, added_at=datetime.now(UTC))
            changes = {"notes": [*workpaper.notes, added], **unsigned(workpaper), "version": workpaper.version + 1}
            new = workpaper.model_copy(update=changes)
            summary = f"审计师补充底稿 {ref} 的说明" + (f" (补齐 {item})" if item else "")
            trail = workpaper_trail(eid, new, actor, "workpaper_note", summary, item=item, text=text)
            return await self._write(eid, snapshot, new, actor, trail)

    async def sign(self, eid: str, ref: str, scope: ReviewScope, opinion: str, actor: str) -> Workpaper:
        """审计师签署 (POST .../signoff): 新版本 status signed, signoff 记签署人, 时间, 复核范围与意见 (选填, 去首尾
        空白). 同一人以同样的范围与意见再签已签署的返回当前版本, 不再写. 缺必填项也能签 (只记录, 不阻断)."""
        opinion = opinion.strip()
        async with self.holding(eid) as snapshot:
            workpaper = self.current(snapshot, ref)
            done = workpaper.signoff
            if done is not None and (done.signed_by, done.scope, done.opinion) == (actor, scope, opinion):
                return workpaper
            signoff = WorkpaperSignoff(signed_by=actor, signed_at=datetime.now(UTC), scope=scope, opinion=opinion)
            new = workpaper.model_copy(
                update={"status": WorkpaperStatus.signed, "signoff": signoff, "version": workpaper.version + 1}
            )
            summary = f"审计师签署底稿 {ref} ({SCOPES[scope]})"
            trail = workpaper_trail(eid, new, actor, "workpaper_signoff", summary, scope=scope.value, opinion=opinion)
            return await self._write(eid, snapshot, new, actor, trail)
