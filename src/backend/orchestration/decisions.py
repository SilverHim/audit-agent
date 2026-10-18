"""DecisionSubmit (A 负责): 审计师决定的唯一写入口. 取锁 → 校验 → 一个事务内写入决定及其效果 → 越过闸门 → 后台继续.

校验顺序, 必填表, 决定的效果与恢复机制见 开发接口文档.md 第 7 节.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, StateSnapshot

from backend.agents.base import AgentDeps
from backend.contracts.errors import DecisionRejected, VersionConflict
from backend.contracts.models import (
    AgentId,
    AuditException,
    AutonomyLevel,
    ClientProfile,
    CriticVerdict,
    Decision,
    DisputeResolution,
    Engagement,
    EvidencePointer,
    GateDecision,
    GateId,
    Judgment,
    Misstatement,
    MisstatementDraft,
    NeitherAction,
    Phase,
    Procedure,
    ProcedureRun,
    ProcedureStatus,
    ProposedAction,
    ReviewRecord,
    ReviewStatus,
    ReviewTask,
    RiskItem,
    TargetType,
    TaskKind,
    TaskStatus,
    TrailEntry,
    TrailKind,
    Versioned,
)
from backend.contracts.state import EngagementState, StatePatch, fill_defaults
from backend.orchestration.gates import check_fields, current_gate, current_spec, spec_of
from backend.orchestration.nodes import rerun_subjects, save_outputs
from backend.orchestration.runner import GraphRunner, features_mismatch, resume_required, review_waits
from backend.orchestration.trail import phase_transition, procedure_transitions, record_transitions
from backend.services.review_queue import attribute_exceptions

LOCKING_PHASES = (Phase.Declined, Phase.Archived)  # 进入后项目置 locked, 拒绝一切写入
# 效果中会出新版本的对象列表
VERSIONED_EFFECTS = ("procedure_pack", "risk_register", "audit_objectives", "report_versions")


def new_versions(effects: StatePatch, sources: StatePatch) -> list[Versioned]:
    """效果中新出的版本: 程序包, 风险清单, 审计目标, 报告里 (id, version) 不在效果输入 (sources 同名字段) 中的对象.

    决定只写这些; 输入里本来就有的不写, 即使它们比 Store 新 (检查点领先 Store 时不顺带写出无关版本).
    """
    created: list[Versioned] = []
    for key in VERSIONED_EFFECTS:
        before = {(o.id, o.version) for o in sources.get(key, [])}
        created += [o for o in effects.get(key, []) if (o.id, o.version) not in before]
    return created


# 复核决定的必填字段与只允许在此填的字段 (第 7 节必填表); reason 是通用字段, confirm 也可以填
REVIEW_REQUIRED: dict[Decision, list[str]] = {
    Decision.confirm: [],
    Decision.override: ["reason", "override_conclusion"],
    Decision.misstatement: ["reason", "misstatement_draft"],
    Decision.rerun: ["reason"],
    Decision.manual_result: ["reason", "manual_result"],
    Decision.dispute_resolution: ["reason", "dispute_resolution"],
}
REVIEW_SPECIFIC = (
    "override_conclusion",
    "misstatement_draft",
    "rerun_params",
    "manual_result",
    "dispute_resolution",
    "neither_action",
)
REVIEW_ALLOWED: dict[Decision, set[str]] = {
    Decision.confirm: set(),
    Decision.override: {"override_conclusion"},
    Decision.misstatement: {"misstatement_draft"},
    Decision.rerun: {"rerun_params"},
    Decision.manual_result: {"manual_result"},
    # 仲裁可附错报草稿: 采用的结论认定为错报时一并登记; neither_action 与 override_conclusion 只用于 neither
    Decision.dispute_resolution: {"dispute_resolution", "neither_action", "override_conclusion", "misstatement_draft"},
}
TARGET_MODELS: dict[TargetType, type[Versioned]] = {  # 复核任务的目标类型 -> 在 Store 中的对象类型
    TargetType.Judgment: Judgment,
    TargetType.AuditException: AuditException,
    TargetType.ProcedureRun: ProcedureRun,
}


def check_review_fields(record: ReviewRecord) -> None:
    """复核决定的字段校验: 必填的缺了报 MISSING_FIELD, 不该填的填了报 FIELD_NOT_ALLOWED. None 与空值都视为未填.

    仲裁另按结果: neither 须填 neither_action (本版只有 human_conclusion) 与 override_conclusion; a6_upheld,
    a7_upheld 不能填这两项.
    """
    missing = [name for name in REVIEW_REQUIRED[record.decision] if not getattr(record, name)]
    if missing:
        raise DecisionRejected("MISSING_FIELD", ", ".join(missing))
    filled = [name for name in REVIEW_SPECIFIC if getattr(record, name) and name not in REVIEW_ALLOWED[record.decision]]
    if filled:
        raise DecisionRejected("FIELD_NOT_ALLOWED", ", ".join(filled))
    if record.decision is not Decision.dispute_resolution:
        return
    neither_fields = ("neither_action", "override_conclusion")
    if record.dispute_resolution is DisputeResolution.neither:
        missing = [name for name in neither_fields if not getattr(record, name)]
        if missing:
            raise DecisionRejected("MISSING_FIELD", ", ".join(missing))
    else:
        filled = [name for name in neither_fields if getattr(record, name)]
        if filled:
            raise DecisionRejected("FIELD_NOT_ALLOWED", f"{record.dispute_resolution} 不填 {', '.join(filled)}")


def confirm_effects(judgment: Judgment, exceptions: list[AuditException]) -> list[Versioned]:
    """confirm 的效果: 判断出新版本置 confirmed, 它解释的例外置 attributed (见 attribute_exceptions).

    judgment: 任务的目标判断 (Store 当前版本); exceptions: 其 exception_ids 所指例外的当前版本. 纯函数.
    """
    confirmed = judgment.model_copy(update={"version": judgment.version + 1, "review_status": ReviewStatus.confirmed})
    return [confirmed, *attribute_exceptions(confirmed, exceptions)]


def misstatement_effects(
    record: ReviewRecord, task: ReviewTask, target: Versioned, exceptions: list[AuditException], taken: set[str]
) -> list[Versioned]:
    """misstatement 的效果 (第 7 节): 按决定的错报草稿建 Misstatement M-{决定 ID}, 来源为一条已确认的判断.

    target 为判断 (normal 任务): 判断置 confirmed 并归因其例外 (同 confirm), 错报记该判断.
    target 为例外 (unattributed 任务): 同 override, 建人工判断 -H{n} (结论取决定理由, 建议 misstatement, 错报草稿
    取决定的, 证据取例外的, 解释该例外), 例外归因于它, 错报记该人工判断. 这样报告 (RPT-03) 引用的事实总能落到已确认
    的判断上 (R59 联调发现: 原先错报只记 exception_id, 报告无判断可引, RPT-03 反复失败).
    exceptions: target 为判断时其 exception_ids 所指例外的当前版本; taken: 检查点状态中已有的判断 ID. 纯函数.
    """
    draft = record.misstatement_draft
    assert draft is not None  # 校验时已查必填
    if isinstance(target, Judgment):
        effects = confirm_effects(target, exceptions)
        source = target.id
    else:
        assert isinstance(target, AuditException) and task.subject_id is not None  # unattributed 任务带判断对象
        assert record.reason is not None  # 校验时已查必填
        source = human_judgment_id(task, task.subject_id, taken)
        human = human_judgment(
            record,
            task,
            source,
            subject_id=task.subject_id,
            conclusion=record.reason,
            evidence=target.evidence,
            exception_ids=[target.id],
            proposed_action=ProposedAction.misstatement,
            misstatement_draft=draft,
        )
        effects = [human, *attribute_exceptions(human, [target])]
    misstatement = Misstatement(
        id=f"M-{record.id}", review_record_id=record.id, judgment_id=source, exception_id=None, **draft.model_dump()
    )
    return [*effects, misstatement]


def human_judgment_id(task: ReviewTask, subject_id: str, taken: set[str]) -> str:
    """人工建的判断的 ID (第 0 节): J-{procedure_id}-{run_no}-{subject_id}-H{n}, run_no 取任务 run_id 的编号,
    n 取 taken (检查点状态中已有的判断 ID) 中未用的最小号. 同一判断对象已有 -H1 时取 -H2; 按同一状态重算得同一 ID.
    例: 任务 run_id RUN-REV-02-1, subject_id SI-2026-0618 → J-REV-02-1-SI-2026-0618-H1.
    """
    run_no = task.run_id.rsplit("-", 1)[1]
    base = f"J-{task.procedure_id}-{run_no}-{subject_id}"
    n = 1
    while f"{base}-H{n}" in taken:
        n += 1
    return f"{base}-H{n}"


def human_judgment(
    record: ReviewRecord,
    task: ReviewTask,
    judgment_id: str,
    *,
    subject_id: str,
    conclusion: str,
    evidence: list[EvidencePointer],
    exception_ids: list[str],
    proposed_action: ProposedAction = ProposedAction.accept,
    misstatement_draft: MisstatementDraft | None = None,
) -> Judgment:
    """override, manual_result 与对例外认定错报时建的人工判断, 置 confirmed. 只填人给的 (结论, 证据, 解释的例外;
    认定错报时另有建议 misstatement 与错报草稿); 其余取固定值: 置信度 1.0, 推理链, 分项与反方论证为空,
    自主度 L1, 执行人为复核人, 时间为决定时间."""
    return Judgment(
        id=judgment_id,
        procedure_id=task.procedure_id,
        run_id=task.run_id,
        subject_id=subject_id,
        conclusion=conclusion,
        confidence=1.0,
        confidence_parts={},
        reasoning_chain=[],
        evidence=evidence,
        counter_argument="",
        proposed_action=proposed_action,
        misstatement_draft=misstatement_draft,
        exception_ids=exception_ids,
        autonomy_level=AutonomyLevel.L1,
        review_status=ReviewStatus.confirmed,
        performed_by=record.reviewed_by,
        created_at=record.decided_at,
    )


def override_effects(
    record: ReviewRecord, task: ReviewTask, target: Versioned, exceptions: list[AuditException], taken: set[str]
) -> list[Versioned]:
    """override 的效果 (第 7 节). 按 override_conclusion 建人工判断 -H{n}, 置 confirmed 并归因其例外.

    target 为判断 (normal 任务): 人工判断沿用原判断的判断对象, 证据与 exception_ids; 原判断出新版本置 overridden,
    superseded_by 记人工判断. target 为例外 (unattributed 任务): 判断对象取任务的 subject_id, 证据取例外的,
    exception_ids 为该例外. exceptions: target 为判断时其解释的例外的当前版本. 纯函数.
    """
    conclusion = record.override_conclusion
    assert conclusion is not None  # 校验时已查必填
    if isinstance(target, Judgment):
        jid = human_judgment_id(task, target.subject_id, taken)
        human = human_judgment(
            record,
            task,
            jid,
            subject_id=target.subject_id,
            conclusion=conclusion,
            evidence=target.evidence,
            exception_ids=target.exception_ids,
        )
        overridden = target.model_copy(
            update={"version": target.version + 1, "review_status": ReviewStatus.overridden, "superseded_by": jid}
        )
        return [overridden, human, *attribute_exceptions(human, exceptions)]
    assert isinstance(target, AuditException) and task.subject_id is not None  # unattributed 任务带判断对象
    jid = human_judgment_id(task, task.subject_id, taken)
    human = human_judgment(
        record,
        task,
        jid,
        subject_id=task.subject_id,
        conclusion=conclusion,
        evidence=target.evidence,
        exception_ids=[target.id],
    )
    return [human, *attribute_exceptions(human, [target])]


def manual_result_effects(record: ReviewRecord, task: ReviewTask, taken: set[str]) -> list[Versioned]:
    """manual_result 的效果 (第 7 节): 失败任务的判断对象由人给出结论与证据, 建人工判断 -H{n}, 置 confirmed. 纯函数."""
    result = record.manual_result
    assert result is not None and task.subject_id is not None  # 校验: 必填; allowed 只对带 subject_id 的任务开放
    jid = human_judgment_id(task, task.subject_id, taken)
    return [
        human_judgment(
            record,
            task,
            jid,
            subject_id=task.subject_id,
            conclusion=result.conclusion,
            evidence=result.evidence,
            exception_ids=[],
        )
    ]


def critic_judgment(record: ReviewRecord, task: ReviewTask, target: Judgment, judgment_id: str) -> Judgment:
    """a7_upheld 建的判断: 结论与推理链取独立复核第一轮的, 证据为该推理链引用的指针 (去重; 没有时沿用原判断的),
    解释的例外同原判断, 留痕指向复核的比对调用; 由审计师采纳, 置 confirmed, 执行人为复核人. 纯函数."""
    seen: set[tuple[str, str, str, int | None]] = set()
    evidence: list[EvidencePointer] = []
    for p in (p for step in target.critic_reasoning_chain for p in step.evidence):
        if (key := (p.source_type, p.source_id, p.locator, p.page)) not in seen:
            seen.add(key)
            evidence.append(p)
    adopted = human_judgment(
        record,
        task,
        judgment_id,
        subject_id=target.subject_id,
        conclusion=target.critic_conclusion or "",
        evidence=evidence or target.evidence,
        exception_ids=target.exception_ids,
    )
    return adopted.model_copy(
        update={
            "reasoning_chain": target.critic_reasoning_chain,
            "trail_ref": target.critic_trail_ref,
            "critic_verdict": CriticVerdict.not_applicable,
        }
    )


def dispute_effects(
    record: ReviewRecord, task: ReviewTask, target: Versioned, exceptions: list[AuditException], taken: set[str]
) -> list[Versioned]:
    """仲裁的效果 (规格书第 11 节争议项出口): a6_upheld 同 confirm; a7_upheld 以独立复核的结论与推理链建新判断
    -H{n} (critic_judgment), 原判断置 overridden 并记 superseded_by; neither (human_conclusion) 同 override.
    附了 misstatement_draft 时另建 Misstatement M-{决定 ID}, 来源为最终采用的判断. 纯函数.
    """
    assert isinstance(target, Judgment)  # dispute 任务的目标是判断
    resolution = record.dispute_resolution
    if resolution is DisputeResolution.a6_upheld:
        effects = confirm_effects(target, exceptions)
        adopted = effects[0]
    elif resolution is DisputeResolution.a7_upheld:
        jid = human_judgment_id(task, target.subject_id, taken)
        adopted = critic_judgment(record, task, target, jid)
        overridden = target.model_copy(
            update={"version": target.version + 1, "review_status": ReviewStatus.overridden, "superseded_by": jid}
        )
        effects = [overridden, adopted, *attribute_exceptions(adopted, exceptions)]
    else:
        assert record.neither_action is NeitherAction.human_conclusion  # 校验时已查
        effects = override_effects(record, task, target, exceptions, taken)
        adopted = effects[1]
    draft = record.misstatement_draft
    if draft is not None:
        assert isinstance(adopted, Judgment)
        effects.append(
            Misstatement(
                id=f"M-{record.id}",
                judgment_id=adopted.id,
                exception_id=None,
                review_record_id=record.id,
                **draft.model_dump(),
            )
        )
    return effects


def review_effects(
    record: ReviewRecord,
    task: ReviewTask,
    target: Versioned,
    exceptions: list[AuditException],
    taken: set[str],
) -> list[Versioned]:
    """复核决定的效果 (第 7 节决定的效果表, 不含任务 decided 与程序重算). 纯函数.

    task: 决定处理的任务; target: 任务目标的当前版本 (判断, 例外或执行记录);
    exceptions: target 为判断时其解释的例外的当前版本; taken: 检查点状态中已有的判断 ID, 人工判断据此取号.
    """
    if record.decision is Decision.confirm:
        assert isinstance(target, Judgment)  # confirm 只允许用于 normal 任务, 目标是判断
        return confirm_effects(target, exceptions)
    if record.decision is Decision.misstatement:
        return misstatement_effects(record, task, target, exceptions, taken)
    if record.decision is Decision.override:
        return override_effects(record, task, target, exceptions, taken)
    if record.decision is Decision.manual_result:
        return manual_result_effects(record, task, taken)
    if record.decision is Decision.dispute_resolution:
        return dispute_effects(record, task, target, exceptions, taken)
    assert record.decision is Decision.rerun  # rerun: 只改程序 (见 rerun_procedure), 不改判断与例外
    return []


def recompute_procedure(procedure: Procedure, pending: list[ReviewTask]) -> Procedure | None:
    """rerun 以外的复核决定写入后目标程序的新版本 (第 7 节): 仍有待决任务为 awaiting_human, 没有为 completed.
    程序已是 executing (之前的 rerun 决定已置, 尚待派发) 时保持不变, 否则会把待重跑的程序置为完成.
    状态不变时返回 None, 不出新版本.

    pending: 该程序在本决定之后仍待决的任务 (不含本决定处理的任务).
    """
    if procedure.status is ProcedureStatus.executing:
        return None
    status = ProcedureStatus.awaiting_human if pending else ProcedureStatus.completed
    if status is procedure.status:
        return None
    return procedure.model_copy(update={"version": procedure.version + 1, "status": status})


def rerun_group(task: ReviewTask, pending: list[ReviewTask]) -> list[ReviewTask]:
    """rerun 决定关闭的一组 failed 任务 (Q17 一轮失败算一组, 整组重跑): 被决定的任务在前, 其后为同程序其余待决的
    failed 任务 (都来自该程序最近一次执行), 按 ID 排序. pending 为该程序当前全部待决任务; normal, unattributed
    任务不在组内."""
    siblings = [
        t for t in pending if t.kind is TaskKind.failed and t.procedure_id == task.procedure_id and t.id != task.id
    ]
    return [task, *sorted(siblings, key=lambda t: t.id)]


def rerun_procedure(procedure: Procedure, record: ReviewRecord, group: list[ReviewTask]) -> Procedure:
    """rerun 决定写出的程序新版本 (第 7 节): 置 executing (不论还有无别的待决任务), params 合并 rerun_params,
    rerun_subject_ids 取整组任务的对象 (见 nodes.rerun_subjects; 组内有整次失败的任务时为空, 整条重跑).
    整组在同一事务里关闭, 程序处于 executing 时已没有待决的 failed 任务, 不会再来第二个 rerun.
    P2 程序由下一轮派发重跑, P2 以外的程序由所在节点读回这一版后重跑 (nodes.read_rerun 取等待版本 + 1).
    例: REV-03 第 1 次 CON-002, CON-003 失败, 对其中任一个 rerun → REV-03 新版本 executing,
    rerun_subject_ids [CON-002, CON-003].
    """
    return procedure.model_copy(
        update={
            "version": procedure.version + 1,
            "status": ProcedureStatus.executing,
            "params": {**procedure.params, **(record.rerun_params or {})},
            "rerun_subject_ids": rerun_subjects(group),
        }
    )


def review_trail(eid: str, record: ReviewRecord, task: ReviewTask, closed: list[str]) -> TrailEntry:
    """复核决定的 decision 留痕, 决定提交与恢复补写共用. closed: rerun 时随同关闭的同组任务 ID (不含 task)."""
    payload: dict[str, Any] = {"decision_id": record.id, "task_id": task.id, "reason": record.reason}
    if record.dispute_resolution is not None:
        payload["dispute_resolution"] = record.dispute_resolution.value
    if closed:
        payload["closed_task_ids"] = closed
    return TrailEntry(
        engagement_id=eid,
        kind=TrailKind.decision,
        actor=record.reviewed_by,
        procedure_id=task.procedure_id,
        summary=f"{task.id} {record.decision.value}" + (f", 同组 {len(closed)} 个任务一并关闭" if closed else ""),
        idempotency_key=record.id,
        payload=payload,
    )


class GraphDecisionSubmit:
    """DecisionSubmit 的实现: 审计师所有决定的唯一写入口.

    写入顺序: 先在一个事务内写 Store (决定, 程序新版本, 项目阶段), 提交后写 decision 留痕 (独立事务),
    再以闸门节点身份写入检查点, 最后交给 runner 在后台继续执行. 项目锁从校验前一直持有到后台执行停下.
    """

    def __init__(
        self,
        app: CompiledStateGraph[EngagementState],
        deps: AgentDeps,
        runner: GraphRunner,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.app = app  # 编译好的主图, 用于读检查点与 aupdate_state
        self.deps = deps  # 外部依赖
        self.runner = runner  # 项目锁与后台执行
        self.now = now  # 当前时刻 (UTC), RPT-01 签字时刻与报告日取它; 测试可注入固定时刻

    async def _engagement(self, eid: str) -> Engagement:
        engagement = await self.deps.engagement_store.get(eid)
        if engagement is None:
            raise LookupError(f"Engagement {eid} not found")
        return engagement

    async def _check_resume_required(self, engagement: Engagement, snapshot: StateSnapshot) -> None:
        """Store 已写入, 检查点却没跟上 (上次写完 Store 后中断) 时须先 resume 补做, 不接受新的决定.

        判断见 runner.resume_required.
        """
        missing = await resume_required(self.deps, engagement, snapshot)
        if missing is not None:
            raise DecisionRejected("RESUME_REQUIRED", missing)

    @asynccontextmanager
    async def _deciding(self, eid: str, resume: str | None = None) -> AsyncIterator[tuple[Engagement, StateSnapshot]]:
        """submit 与 update_client_profile 共用的外壳, 用法: async with self._deciding(eid) as (engagement, snapshot).

        进入时: 查项目锁定, 取项目锁, 重读项目再查一次锁定, 查功能集 (FEATURES_MISMATCH), 读检查点, 查 RESUME_REQUIRED.
        块内出错: 释放项目锁后抛出 (写入时的 VersionConflict 转为 DecisionRejected("VERSION_MISMATCH")).
        块正常结束: 交给 runner 从检查点在后台继续执行, 锁由后台任务结束时释放. resume 为复核决定 ID 时以
        Command(resume=resume) 恢复停在节点内 interrupt() 的节点; 闸门决定与更新客户资料为 None (已越过闸门).
        """
        engagement = await self._engagement(eid)
        if engagement.locked:  # 第 7 节校验顺序: 锁定先于项目锁 (ENGAGEMENT_BUSY)
            raise DecisionRejected("ENGAGEMENT_LOCKED", eid)
        self.runner.acquire(eid)  # 取不到抛 ENGAGEMENT_BUSY
        try:
            # 取锁前读到的项目可能已过时: 读完到取锁之间, 另一个决定可能刚拒绝承接并锁定了项目
            engagement = await self._engagement(eid)
            if engagement.locked:
                raise DecisionRejected("ENGAGEMENT_LOCKED", eid)
            mismatch = features_mismatch(self.deps, engagement)  # 第 7 节校验顺序: 先于 RESUME_REQUIRED
            if mismatch is not None:
                raise DecisionRejected("FEATURES_MISMATCH", mismatch)
            snapshot = await self.app.aget_state(GraphRunner.config(eid))
            await self._check_resume_required(engagement, snapshot)
            yield engagement, snapshot
        except VersionConflict as e:
            self.runner.release(eid)
            raise DecisionRejected("VERSION_MISMATCH", str(e)) from e
        except BaseException:
            self.runner.release(eid)  # 后台任务尚未启动, 须在此释放
            raise
        self.runner.launch(eid, None if resume is None else Command(resume=resume))

    async def _check_decision_id(self, eid: str, decision_id: str) -> None:
        """决定 ID 未被用过, 否则 INVALID_VALUE. 闸门决定与复核决定共用一张 decisions 表 (第 9 节), 两类都查.

        同一 ID 再提交 (内容可能不同) 时, put 会按 (id, version) 返回旧决定, 效果却按新内容算.
        """
        store = self.deps.output_store
        if await store.get(eid, GateDecision, decision_id) or await store.get(eid, ReviewRecord, decision_id):
            raise DecisionRejected("INVALID_VALUE", f"决定 ID {decision_id} 已用过")

    async def check_gate_decision(self, eid: str, decision: GateDecision) -> Versioned:
        """第 7 节校验顺序中 NOT_AT_GATE 之后的部分: 材料与修改项的版本, 字段 (登记表的 fields), 取值 (登记表的
        check), 决定 ID 未用过.

        返回决定绑定的材料 (Store 中的当前版本). 不过抛 DecisionRejected.
        """
        spec = spec_of(decision.gate)
        store = self.deps.output_store
        # 决定须绑定材料的当前版本 (读 Store): 审计师看材料期间材料可能已重跑出新版
        target = await spec.stored(store, eid)
        if target is None or (decision.target_id, decision.target_version) != (target.id, target.version):
            raise DecisionRejected("VERSION_MISMATCH", f"{decision.gate} 的呈报材料已更新, 请刷新后重新决定")
        risks = {r.id: r for r in await store.latest(eid, RiskItem)} if decision.edits else {}
        for edit in decision.edits:  # 不存在的风险项留给专项校验的 INVALID_VALUE
            risk = risks.get(edit.target_id)
            if risk is not None and risk.version != edit.target_version:
                raise DecisionRejected("VERSION_MISMATCH", f"风险项 {risk.id} 已是第 {risk.version} 版")
        check_fields(spec, decision)
        if spec.check is not None:
            await spec.check(self.deps, eid, decision, target)
        await self._check_decision_id(eid, decision.id)
        return target

    async def check_review_record(self, eid: str, record: ReviewRecord, snapshot: StateSnapshot) -> ReviewTask:
        """第 7 节校验顺序中 RESUME_REQUIRED 之后的部分, 对复核决定: 任务存在且待决, 图在节点内等待, 版本,
        允许的决定, 字段, 取值. 任务与目标对象读 Store 的最新版本. 返回所处理的任务. 不过抛 DecisionRejected.
        """
        store = self.deps.output_store
        task = await store.get(eid, ReviewTask, record.task_id)
        if task is None:
            raise DecisionRejected("TASK_NOT_FOUND", record.task_id)
        if task.status is not TaskStatus.pending:
            raise DecisionRejected("TASK_NOT_PENDING", f"{task.id} 已决定")
        waits = review_waits(snapshot)  # 等数据就绪不收复核决定
        if not waits:
            raise DecisionRejected("NOT_AWAITING_REVIEW", "当前不在等复核")
        # 等待中的节点上次恢复时出错 (如收到不在 Store 中的决定 ID): 它此后每次恢复都取回旧值再出错, 新决定送不到它,
        # 写进 Store 只会成为"须先恢复"的决定. 按检查点判断 (任务带 error), 重启后照样成立
        broken = [task for task in snapshot.tasks if task.interrupts and task.error]
        if broken:
            raise DecisionRejected(
                "NOT_AWAITING_REVIEW",
                f"等待中的节点 {broken[0].name} 上次恢复时出错, 须先按错误信息恢复: {broken[0].error}",
            )
        # P2 以外程序失败后的等待只处理该程序的任务; 别的程序的决定送进去, 节点读回时会报错停下
        if not any(not isinstance(w, dict) or w.get("procedure_id") in (None, task.procedure_id) for w in waits):
            raise DecisionRejected("NOT_AWAITING_REVIEW", f"当前不在等 {task.procedure_id} 的决定")
        # 决定须绑定任务记的目标版本, 且目标此后没有出新版本 (审计师看材料期间可能被别的决定改过)
        target = await store.get(eid, TARGET_MODELS[task.target_type], task.target_id)
        if record.target_version != task.target_version or target is None or target.version != task.target_version:
            current = target.version if target is not None else None
            raise DecisionRejected(
                "VERSION_MISMATCH",
                f"{task.target_id} 当前为第 {current} 版, 任务与决定绑定的是第 {record.target_version} 版",
            )
        if record.decision not in self.deps.queue.allowed(task):
            raise DecisionRejected("DECISION_NOT_ALLOWED", f"{task.kind} 任务不能 {record.decision}")
        check_review_fields(record)
        if (
            record.dispute_resolution is DisputeResolution.a7_upheld
            and isinstance(target, Judgment)
            and DisputeResolution.a7_upheld not in self.deps.queue.resolutions(task, target)
        ):
            raise DecisionRejected("INVALID_VALUE", "独立复核的推理链没有通过校验, 不能采用其结论 (a7_upheld)")
        # rerun_params 只能改模板已有的参数: build_run 把它叠加到解析结果上, 不查键名, 拼错的键会悄悄无效
        params = self.deps.library.load(task.procedure_id).params
        unknown = sorted(set(record.rerun_params or {}) - set(params))
        if unknown:
            raise DecisionRejected("INVALID_VALUE", f"{task.procedure_id} 没有参数 {', '.join(unknown)}")
        await self._check_decision_id(eid, record.id)
        return task

    async def submit(self, eid: str, decision: ReviewRecord | GateDecision) -> None:
        """提交一个决定: 校验通过则写入并在后台继续执行, 立即返回; 校验不过抛 DecisionRejected, 什么都不写."""
        if isinstance(decision, ReviewRecord):
            async with self._deciding(eid, resume=decision.id) as (_, snapshot):
                task = await self.check_review_record(eid, decision, snapshot)
                await self._apply_review(eid, decision, task, snapshot)
            return
        async with self._deciding(eid) as (_, snapshot):
            values = fill_defaults(snapshot.values)
            spec = current_spec(snapshot.next, values)
            if spec is None or spec.gate is not decision.gate:
                raise DecisionRejected("NOT_AT_GATE", f"当前不在 {decision.gate} 闸门")
            target = await self.check_gate_decision(eid, decision)
            # 效果的输入: 程序包取检查点, 其余由登记表的 effects 读 (如 PLN-01/03 的风险清单与审计目标取 Store)
            pack = values["procedure_pack"]
            effects, sources = await spec.effects(self.deps, eid, decision, target, pack, self.now())
            created = new_versions(effects, sources)
            written = await self._write_gate(eid, decision, effects, created, values["phase"])
            # 材料一并写入: 决定绑定的是 Store 中的版本, 路由按状态中的材料判断决定是否过期, 两者须一致.
            # 效果已出材料的新版本时 (RPT-01 签字) 只写新版本, 路由另行放行 (spec.passed)
            if spec.material_field not in written:
                written[spec.material_field] = [target] if spec.is_list else target
            # 以闸门节点身份写入: 检查点越过闸门, 下一步由节点的路由按刚写入的决定选出口
            await self.app.aupdate_state(GraphRunner.config(eid), written, as_node=spec.node)

    async def _apply_review(self, eid: str, record: ReviewRecord, task: ReviewTask, snapshot: StateSnapshot) -> None:
        """写入已通过校验的复核决定: 一个事务内写决定, 效果, 任务 decided 与程序新版本, 提交后写 decision 留痕.
        rerun 时连同同程序其余待决的 failed 任务一并置 decided (一轮失败算一组, 见 rerun_group).

        效果按 Store 当前版本计算 (与校验读的一致). 决定的写入者为复核人, 其余为 A1. Store 若已有其中某个
        (id, version) 而内容不同, put 会返回旧对象, 效果就丢了: 此时抛 VersionConflict 整体回滚
        (_deciding 转为 VERSION_MISMATCH). 不写检查点: 节点恢复后从 Store 读回 (await_events, 第 7 节恢复机制).
        snapshot: 当前检查点, 人工判断按其中已有的判断取号 (-H{n}).
        """
        store = self.deps.output_store
        target = await store.get(eid, TARGET_MODELS[task.target_type], task.target_id)
        assert target is not None  # 校验时已按任务目标取到
        exceptions: list[AuditException] = []
        if isinstance(target, Judgment):
            found = [await store.get(eid, AuditException, x) for x in target.exception_ids]
            exceptions = [x for x in found if x is not None]
        taken = {j.id for j in fill_defaults(snapshot.values)["judgments"]}
        effects = review_effects(record, task, target, exceptions, taken)
        procedure = await store.get(eid, Procedure, task.procedure_id)
        assert procedure is not None  # 任务所属程序在程序包中, 已入库
        pending = await store.latest(eid, ReviewTask, procedure_id=task.procedure_id, status=TaskStatus.pending)
        if record.decision is Decision.rerun:
            group = rerun_group(task, pending)
            updated: Procedure | None = rerun_procedure(procedure, record, group)
        else:
            group = [task]
            updated = recompute_procedure(procedure, [t for t in pending if t.id != task.id])
        decided = [t.model_copy(update={"version": t.version + 1, "status": TaskStatus.decided}) for t in group]
        created = [*effects, *decided, *([updated] if updated else [])]
        async with store.transaction(eid):
            await store.put(eid, [record], actor=record.reviewed_by)
            saved = await store.put(eid, created, actor=AgentId.A1)
            if saved != created:
                clashes = [f"{o.id} v{o.version}" for o, c in zip(saved, created, strict=True) if o != c]
                raise VersionConflict(f"Store 已有内容不同的同版本: {', '.join(clashes)}")
        # 留痕在业务事务提交后写, 防重复键为决定 ID; 写失败时再提交报 RESUME_REQUIRED (同闸门决定)
        await self.deps.trail.record(review_trail(eid, record, task, [t.id for t in group[1:]]))
        await record_transitions(self.deps.trail, await procedure_transitions(store, eid, saved))

    async def _write_gate(
        self, eid: str, decision: GateDecision, effects: StatePatch, created: list[Versioned], phase: Phase
    ) -> StatePatch:
        """在一个事务内写入闸门决定及其效果, 提交后写 decision 留痕与 transition 留痕 (程序状态, 阶段由 phase 变为
        效果中的阶段). 返回要写入状态的片段 (决定换成 Store 返回的).

        决定的写入者为决定人; created 为效果新出的版本 (见 new_versions), 写入者为 A1. Store 若已有其中某个
        (id, version) 而内容不同, put 会返回旧对象, 效果就丢了: 此时抛 VersionConflict 整体回滚
        (_deciding 转为 VERSION_MISMATCH). 阶段变化同步到 engagements 表, 进入 Declined 或 Archived 时最后置 locked
        (排在全部 put 之后: 置 locked 后 put 抛 EngagementLocked).
        """
        output_store = self.deps.output_store
        async with output_store.transaction(eid):
            stored = await save_outputs(output_store, eid, {"gate_decisions": [decision]}, actor=decision.decided_by)
            saved = await output_store.put(eid, created, actor=AgentId.A1)
            if saved != created:
                clashes = [f"{o.id} v{o.version}" for o, c in zip(saved, created, strict=True) if o != c]
                raise VersionConflict(f"Store 已有内容不同的同版本: {', '.join(clashes)}")
            if "phase" in effects:
                locking = effects["phase"] in LOCKING_PHASES
                await self.deps.engagement_store.update(eid, phase=effects["phase"], locked=True if locking else None)
        # 留痕是独立事务, 不随业务回滚: 提交成功后再写, 才不会留下业务已回滚的决定.
        # 防重复键为决定 ID. 写失败时 Store 已提交而检查点未跟上: 再提交报 RESUME_REQUIRED
        # (拒绝承接已置 locked 的报 ENGAGEMENT_LOCKED), 由 resume/recover 补做并补写留痕
        procedure_id = spec_of(decision.gate).procedure_id
        await self.deps.trail.record(
            TrailEntry(
                engagement_id=eid,
                kind=TrailKind.decision,
                actor=decision.decided_by,
                procedure_id=procedure_id,
                summary=f"{decision.gate.value} {decision.decision.value}",
                idempotency_key=decision.id,
                payload={"decision_id": decision.id, "reason": decision.reason},
            )
        )
        entries = await procedure_transitions(output_store, eid, saved)
        if "phase" in effects:
            entries += phase_transition(eid, phase, effects["phase"], procedure_id)
        await record_transitions(self.deps.trail, entries)
        return {**effects, **stored}

    async def update_client_profile(self, eid: str, profile: ClientProfile, *, updated_by: str) -> None:
        """更新客户资料: 只在停在 ACC-01 闸门时可用. 保存新版本画像后, 路由回 p0_intel 重跑 ACC-04 再重新呈报.

        profile.id 须为项目 client_id, version 须为该客户画像的最新版本 +1 (画像跨项目共用, 版本号全局递增).
        保存画像与改项目采用的版本在一个事务内, 提交后写 decision 留痕. updated_by 为操作用户 ID, 写入留痕.
        """
        async with self._deciding(eid) as (engagement, snapshot):
            if current_gate(snapshot.next, fill_defaults(snapshot.values)) is not GateId.ACC_01:
                raise DecisionRejected("NOT_AT_GATE", "只能在 ACC-01 闸门更新客户资料")
            # 第 7 节校验顺序: VERSION_MISMATCH 先于 INVALID_VALUE. 版本号按项目客户的最新画像算, 不按 profile.id
            latest = await self.deps.engagement_store.client_profile(engagement.client_id)
            expected = latest.version + 1 if latest is not None else 1
            if profile.version != expected:
                raise DecisionRejected("VERSION_MISMATCH", f"客户画像应为第 {expected} 版")
            if profile.id != engagement.client_id:
                raise DecisionRejected("INVALID_VALUE", f"profile.id 应为 {engagement.client_id}")

            async with self.deps.output_store.transaction(eid):  # 画像新版本与本项目采用的版本一起提交
                saved = await self.deps.engagement_store.save_profile(profile)
                await self.deps.engagement_store.update(eid, client_profile_version=saved.version)
            await self.deps.trail.record(  # 提交后写, 理由同 _write_acc01
                TrailEntry(
                    engagement_id=eid,
                    kind=TrailKind.decision,
                    actor=updated_by,
                    procedure_id="ACC-01",
                    summary=f"客户资料更新至第 {saved.version} 版",
                    idempotency_key=f"{saved.id}-v{saved.version}",
                    payload={"client_id": saved.id, "version": saved.version},
                )
            )
            # 只写新版本号: ACC-01 的路由发现评分卡依据的版本落后, 选 rerun 回 p0_intel
            await self.app.aupdate_state(
                GraphRunner.config(eid),
                {"client_profile_version": saved.version},
                as_node=spec_of(GateId.ACC_01).node,
            )
