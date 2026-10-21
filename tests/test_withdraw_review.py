"""需求 R363: 撤回复核决定 (DecisionSubmit.withdraw_review). Numbat 演示 (基线功能, 脚本 Agent) 走到 FIN-01 闸门前:
REV-02 对 SI-2026-0612 认定错报 D-REV02-0612 (14200000 高估), CSH-02 对 BA-01 确认 D-CSH02 (它解释两条例外), 汇总表
合计 14200000, 候选保留意见. 能不能撤回的规则直接测 (decisions.withdraw_problem); 撤回后各对象的新版本, 图回到等复核,
重新决定后汇总表随之更新; FIN-01 通过后被拒; Store 写入后检查点没跟上时恢复补做. 原决定 Xero 演示的走法见
test_review_withdrawal.
"""

from datetime import timedelta
from typing import Any

import pytest

from backend.contracts.errors import DecisionRejected
from backend.contracts.models import (
    AuditException,
    Decision,
    ExceptionDisposition,
    GateVerdict,
    Judgment,
    Misstatement,
    MisstatementStatus,
    OpinionType,
    Phase,
    Procedure,
    ProcedureStatus,
    ReviewRecord,
    ReviewStatus,
    ReviewTask,
    ReviewWithdrawal,
    TaskKind,
    TaskStatus,
    TrailKind,
    UncorrectedSchedule,
)
from backend.orchestration.decisions import withdraw_problem
from backend.orchestration.query import GraphStateQuery
from backend.orchestration.runner import GraphRunner
from backend.services.procedure_library import ProcedureLibrary
from tests.fakes import NOW, FakeTrail, wait_idle
from tests.test_decisions import Started
from tests.test_fin_gate import fin_decision
from tests.test_flow import EID, approve_materiality, review_all, start_numbat
from tests.test_review_decisions import record

CUTOFF = "J-REV-02-1-SI-2026-0612"
CUTOFF_TASK = "T-J-REV-02-1-SI-2026-0612-v1"
BA01 = "J-CSH-02-1-BA-01"
BA01_TASK = "T-J-CSH-02-1-BA-01-v1"


async def at_fin01() -> Started:
    s = await start_numbat()
    await approve_materiality(s)
    await wait_idle(s.runner, EID)
    await review_all(s)
    assert await s.next_nodes() == ("gate_fin",)
    return s


async def withdraw(s: Started, record_id: str, n: int = 1, reason: str | None = None) -> ReviewWithdrawal:
    withdrawal = await s.submit.withdraw_review(EID, f"WD-{n}", record_id, reason=reason, withdrawn_by="U-1")
    await wait_idle(s.runner, EID)
    return withdrawal


async def latest(s: Started, model: type[Any], oid: str) -> Any:
    found = await s.output_store.get(EID, model, oid)
    assert found is not None, oid
    return found


def trail_keys(s: Started) -> list[str | None]:
    trail = s.runner.deps.trail
    assert isinstance(trail, FakeTrail)
    return [e.idempotency_key for e in trail.entries if e.kind is TrailKind.decision]


TASK = ReviewTask(
    id="T-J-REV-02-1-S-v1",
    kind=TaskKind.normal,
    procedure_id="REV-02",
    target_type="Judgment",
    target_id="J-REV-02-1-S",
    target_version=1,
    run_id="RUN-REV-02-1",
    status=TaskStatus.decided,
    created_at=NOW,
)


def made(n: int, decision: Decision = Decision.confirm, minutes: int = 0) -> ReviewRecord:
    return record(TASK.id, decision, id=f"D-{n}", decided_at=NOW + timedelta(minutes=minutes))


@pytest.mark.parametrize(
    ("case", "problem"),
    [
        ("confirm", None),
        ("override", None),
        ("misstatement", None),
        ("rerun", "只能撤回执行阶段程序复核任务上的"),
        ("unattributed", "只能撤回执行阶段程序复核任务上的"),
        ("not_p2", "只能撤回执行阶段程序复核任务上的"),
        ("already_withdrawn", "已撤回"),
        ("later_decision", "不是任务"),
        ("pending_task", "不是任务"),
        ("earlier_withdrawn", None),
    ],
)
def test_withdraw_problem(case: str, problem: str | None) -> None:
    """能撤回的只有执行阶段程序 normal 任务 (目标为判断) 上的 confirm, override, misstatement, 且是任务上最近一条
    (按决定时间) 没撤回的决定, 任务已决定; 之前的决定已撤回时, 之后的那条照样能撤回."""
    from backend.core.config import get_settings

    library = ProcedureLibrary(get_settings().KNOWLEDGE_ROOT)
    target, task, records, withdrawals = made(1), TASK, [made(1)], []
    if case in ("override", "misstatement", "rerun"):
        target = made(1, Decision(case))
        records = [target]
    elif case == "unattributed":
        task = task.model_copy(update={"kind": TaskKind.unattributed, "target_type": "AuditException"})
    elif case == "not_p2":
        task = task.model_copy(update={"procedure_id": "FIN-03"})
    elif case == "already_withdrawn":
        withdrawals = [
            ReviewWithdrawal(id="WD-1", record_id="D-1", task_id=TASK.id, withdrawn_by="U-1", withdrawn_at=NOW)
        ]
    elif case == "later_decision":
        records = [made(1), made(2, minutes=5)]
    elif case == "pending_task":
        task = task.model_copy(update={"status": TaskStatus.pending})
    elif case == "earlier_withdrawn":
        target = made(2, minutes=5)
        records = [made(1), target]
        withdrawals = [
            ReviewWithdrawal(id="WD-1", record_id="D-1", task_id=TASK.id, withdrawn_by="U-1", withdrawn_at=NOW)
        ]
    found = withdraw_problem(target, task, records, withdrawals, library)
    assert (found is None) if problem is None else (found is not None and problem in found), found


async def test_withdrawals_reopen_the_review_and_the_schedule_follows() -> None:
    """FIN-01 闸门前撤回 D-CSH02 (确认): 判断 v3 回到待复核, 它解释的两条例外恢复为确认之前的处置 (未归因), 任务 v3
    回到待决并绑定判断 v3, CSH-02 回到等人, 阶段回执行阶段, 图停在等复核; 撤回与原决定各一条 decision 留痕, 理由照记.
    这时再撤回 D-REV02-0612 (认定错报, 停在等复核也能撤): 错报作废, 判断与任务同上. 重新决定 (BA-01 再确认, 截止性
    改为推翻) 后回到 FIN-01 闸门: 汇总表 v2 合计 0, 候选无保留, 截止性判断列在未认定一节 (14200000 不低于整体
    重要性, 决定为新的推翻). 之后 FIN-01 通过, 再撤回被拒 (NOT_WITHDRAWABLE), 什么都不写."""
    s = await at_fin01()
    before = await latest(s, AuditException, "X-CSH-05-1-B01-1")

    await withdraw(s, "D-CSH02", reason="复核时漏看了函证差异")

    ba01, task = await latest(s, Judgment, BA01), await latest(s, ReviewTask, BA01_TASK)
    assert (ba01.version, ba01.review_status, ba01.superseded_by) == (3, ReviewStatus.pending, None)
    assert (task.version, task.status, task.target_version) == (3, TaskStatus.pending, 3)
    for xid in ("X-CSH-05-1-B01-1", "X-CSH-02-1-D01-1"):
        exception = await latest(s, AuditException, xid)
        assert (exception.disposition, exception.attributed_by) == (ExceptionDisposition.open, None), xid
    assert (await latest(s, AuditException, "X-CSH-05-1-B01-1")).version == before.version + 1
    assert (await latest(s, Procedure, "CSH-02")).status is ProcedureStatus.awaiting_human
    assert (await s.engagement_store.get(EID)).phase is Phase.P2_Executing  # type: ignore[union-attr]
    assert await s.next_nodes() == ("await_events",)
    overview = await GraphStateQuery(s.runner.app, s.runner.deps, s.runner).overview(EID)
    assert (overview.awaiting_review, overview.can_withdraw, overview.pending_tasks) == (True, True, 1)
    assert trail_keys(s).count("D-CSH02") == trail_keys(s).count("WD-1") == 1

    await withdraw(s, "D-REV02-0612", n=2)
    voided = await latest(s, Misstatement, "M-D-REV02-0612")
    assert voided.status is MisstatementStatus.withdrawn
    assert (await latest(s, ReviewTask, CUTOFF_TASK)).status is TaskStatus.pending
    assert await s.next_nodes() == ("await_events",)

    await s.submit.submit(EID, record(BA01_TASK, id="D-CSH02-2", target_version=3))
    await wait_idle(s.runner, EID)
    cutoff = await latest(s, Judgment, CUTOFF)
    override = record(
        CUTOFF_TASK, Decision.override, id="D-CUT-2", target_version=cutoff.version, override_conclusion="跨期有据"
    )
    await s.submit.submit(EID, override)
    await wait_idle(s.runner, EID)

    assert await s.next_nodes() == ("gate_fin",)
    schedule = await latest(s, UncorrectedSchedule, "SCHEDULE")
    assert (schedule.version, schedule.total_cents, schedule.candidate_opinion.value) == (2, 0, "unqualified")
    [suggestion] = schedule.unrecognised
    assert (suggestion.judgment_id, suggestion.record_id, suggestion.level, suggestion.would_qualify) == (
        CUTOFF,
        "D-CUT-2",
        "material",
        True,
    )

    await s.submit.submit(EID, fin_decision(GateVerdict.approve, target_version=2, disposition=OpinionType.unqualified))
    await wait_idle(s.runner, EID)
    rows = list(s.output_store.rows)
    with pytest.raises(DecisionRejected) as e:
        await s.submit.withdraw_review(EID, "WD-3", "D-CSH02-2", reason=None, withdrawn_by="U-1")
    assert e.value.code == "NOT_WITHDRAWABLE" and s.output_store.rows == rows and not s.runner.is_running(EID)


async def test_withdrawal_caught_up_after_the_checkpoint_was_lost(monkeypatch: pytest.MonkeyPatch) -> None:
    """撤回已写进 Store, 写检查点前进程退出: 再提交任何决定报 RESUME_REQUIRED (列出撤回 ID); 新 runner recover 补做:
    以 fan_in 身份写入撤回与 Store 的最新版本, 图停在等复核, 留痕不重复; 撤回的决定也没法再撤一次."""
    s = await at_fin01()
    update = s.runner.app.aupdate_state

    async def lost(*args: Any, **kwargs: Any) -> Any:
        raise OSError("进程退出")

    monkeypatch.setattr(s.runner.app, "aupdate_state", lost)
    with pytest.raises(OSError):
        await s.submit.withdraw_review(EID, "WD-1", "D-CSH02", reason=None, withdrawn_by="U-1")
    monkeypatch.setattr(s.runner.app, "aupdate_state", update)
    with pytest.raises(DecisionRejected) as e:
        await s.submit.withdraw_review(EID, "WD-2", "D-REV02-0612", reason=None, withdrawn_by="U-1")
    assert (e.value.code, e.value.message) == ("RESUME_REQUIRED", "WD-1")

    restarted = GraphRunner(s.runner.app, s.runner.deps)
    await restarted.recover()
    await wait_idle(restarted, EID)

    snapshot = await s.runner.app.aget_state(GraphRunner.config(EID))
    assert snapshot.next == ("await_events",) and restarted.run_error(EID) is None
    assert [w.id for w in snapshot.values["review_withdrawals"]] == ["WD-1"]
    assert trail_keys(s).count("WD-1") == 1
    tasks = {t.id: t for t in snapshot.values["review_tasks"]}
    assert (tasks[BA01_TASK].version, tasks[BA01_TASK].status) == (3, TaskStatus.pending)
    s = Started(restarted, s.output_store, s.engagement_store)
    with pytest.raises(DecisionRejected) as e:
        await s.submit.withdraw_review(EID, "WD-3", "D-CSH02", reason=None, withdrawn_by="U-1")
    assert (e.value.code, e.value.message) == ("INVALID_VALUE", "D-CSH02 已撤回")


async def test_withdrawal_effects_validate_every_lifecycle_state() -> None:
    """撤回已沟通或未更正的错报时须清除只属于原状态的标记. 所有状态的作废版本都能按真实存储的方式校验读回,
    原版本与沟通, 更正事实保留; 不让 model_copy 绕过校验写成只能被假存储读回的对象. 共用一次 Numbat 开场."""
    s = await at_fin01()
    record = await latest(s, ReviewRecord, "D-REV02-0612")
    task = await latest(s, ReviewTask, record.task_id)
    original = await latest(s, Misstatement, f"M-{record.id}")
    cases = [
        {"status": MisstatementStatus.identified},
        {"status": MisstatementStatus.communicated, "communicated_at": NOW, "needs_recommunication": True},
        {"status": MisstatementStatus.verifying, "correction_notice_id": "CN-1"},
        {
            "status": MisstatementStatus.corrected,
            "corrected_cents": original.amount_cents,
            "residual_cents": 0,
            "verified_by_judgment_id": "J-VERIFY",
        },
        {
            "status": MisstatementStatus.partially_corrected,
            "corrected_cents": 1,
            "residual_cents": original.amount_cents - 1,
            "verified_by_judgment_id": "J-VERIFY",
        },
        {
            "status": MisstatementStatus.uncorrected,
            "uncorrected_basis": "refused",
            "communicated_at": NOW,
            "management_response": "We will not adjust.",
        },
        {
            "status": MisstatementStatus.uncorrected,
            "uncorrected_basis": "stopped_waiting",
            "correction_notice_id": "CN-1",
        },
        {"status": MisstatementStatus.uncorrected},
    ]
    for n, fields in enumerate(cases, start=1):
        source = Misstatement.model_validate({**original.model_dump(), **fields, "version": original.version + n})
        await s.output_store.put(EID, [source], actor="A8")
        effects = await s.submit.withdrawal_effects(EID, record, task)
        [withdrawn] = [obj for obj in effects if isinstance(obj, Misstatement)]
        assert withdrawn.status is MisstatementStatus.withdrawn
        assert withdrawn.uncorrected_basis is None and withdrawn.needs_recommunication is False
        assert Misstatement.model_validate_json(withdrawn.model_dump_json()) == withdrawn
        assert await s.output_store.get(EID, Misstatement, source.id, source.version) == source
        assert (withdrawn.communicated_at, withdrawn.management_response, withdrawn.correction_notice_id) == (
            source.communicated_at,
            source.management_response,
            source.correction_notice_id,
        )
