"""复核决定. R17 校验: submit 收到 ReviewRecord 时按接口文档第 7 节的顺序校验, 不过抛对应错误码, 不写数据, 释放项目锁.
R18 写入与 confirm: 一个事务写决定, 效果, 任务 decided 与程序新版本, 提交后写留痕, 后台以决定 ID 恢复."""

from typing import Any

import pytest
from langgraph.types import Command

from backend.contracts.errors import DecisionRejected, RuleError, VersionConflict
from backend.contracts.models import (
    Assertion,
    AuditException,
    Decision,
    Direction,
    ExceptionDisposition,
    GateDecision,
    GateVerdict,
    Judgment,
    ManualResult,
    MisstatementDraft,
    MisstatementType,
    Procedure,
    ProcedureRun,
    ProcedureStatus,
    ReviewRecord,
    ReviewStatus,
    ReviewTask,
    TaskKind,
    TaskStatus,
    TrailKind,
)
from backend.orchestration.decisions import recompute_procedure
from backend.orchestration.runner import GraphRunner
from backend.services.review_queue import attribute_exceptions
from tests.fakes import (
    ENGAGEMENT,
    NOW,
    NUMBAT_PROFILE,
    FakeEngagementStore,
    FakeOutputStore,
    FakeTrail,
    ScriptA4,
    ScriptA6,
    assert_pack_synced,
    finished,
    make_procedure,
    make_runner,
    numbat_cash_agents,
    numbat_p2_agents,
    preset_rerun,
    wait_idle,
)
from tests.test_await_events import confirm, waiting_for_review
from tests.test_decisions import Started, acc01_decision, pln_decision, started
from tests.test_fieldwork import p2_agents
from tests.test_rerun import failed_at_p0, pending_task

EID = ENGAGEMENT.id
CONFIG = GraphRunner.config(EID)
NORMAL = "T-J-REV-03-1-CON-001-v1"  # REV-03 对第 1 份合同的判断, normal 任务
FAILED = "T-RUN-REV-08-1-v1"  # REV-08 整次失败, A1 建的 failed 任务 (subject_id 为空)
DRAFT = MisstatementDraft(
    type=MisstatementType.factual,
    account="Revenue",
    assertion=Assertion.Cutoff,
    amount_cents=14200000,
    direction=Direction.overstated,
)


def broken(ctx: Any) -> Any:
    raise RuleError("missing column source_id")


async def waiting_with_failure() -> Started:
    """Numbat 跑过两道闸门, REV-08 规则出错: P2 派完可派发的程序后停在 await_events.
    待决任务: REV-03 三条 normal, REV-08 一条 failed."""
    engagement_store = FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT])
    runner, store = make_runner(
        engagement_store=engagement_store,
        agents=lambda deps: {**p2_agents(deps), **numbat_p2_agents(deps), "REV-08": ScriptA6(deps, broken)},
    )
    s = Started(runner, store, engagement_store)
    await runner.start(EID)
    await wait_idle(runner, EID)
    await s.submit.submit(EID, acc01_decision(GateVerdict.approve, id="D-ACC"))
    await wait_idle(runner, EID)
    await s.submit.submit(EID, pln_decision(GateVerdict.approve))
    await wait_idle(runner, EID)
    assert await s.next_nodes() == ("await_events",)
    pending = await store.latest(EID, ReviewTask, status=TaskStatus.pending)
    assert sorted(t.id for t in pending) == sorted([FAILED, *(f"T-J-REV-03-1-CON-00{n}-v1" for n in (1, 2, 3))])
    return s


def record(task_id: str = NORMAL, decision: Decision = Decision.confirm, **fields: Any) -> ReviewRecord:
    """对任务的决定, 绑定第 1 版目标; 除 confirm 外带理由. fields 覆盖任意字段."""
    base = ReviewRecord(
        id="D-1",
        task_id=task_id,
        target_version=1,
        decision=decision,
        reason=None if decision is Decision.confirm else "复核意见",
        reviewed_by="U-1",
        decided_at=NOW,
    )
    return base.model_copy(update=fields)


async def assert_rejected(s: Started, decision: ReviewRecord, code: str, detail: str | None = None) -> None:
    """提交被拒: 错误码对, Store 无新写入, 项目锁已释放, 图不动."""
    rows, snapshot = list(s.output_store.rows), await s.runner.app.aget_state(CONFIG)

    with pytest.raises(DecisionRejected) as e:
        await s.submit.submit(EID, decision)

    assert e.value.code == code, e.value
    if detail is not None:
        assert detail in str(e.value)
    assert s.output_store.rows == rows
    assert not s.runner.is_running(EID)
    after = await s.runner.app.aget_state(CONFIG)
    assert (after.values, [t.interrupts for t in after.tasks]) == (
        snapshot.values,
        [t.interrupts for t in snapshot.tasks],
    )


class Passed(Exception):
    """校验通过, 到了写入那一步 (测试里替换 _apply_review 抛出它, 只看校验, 不写)."""


async def assert_passes(s: Started, decision: ReviewRecord) -> None:
    """通过校验: 走到写入那一步; 写入被替换, 不写 Store, 项目锁已释放."""
    rows = list(s.output_store.rows)

    async def stop(*args: Any) -> None:
        raise Passed

    s.submit._apply_review = stop  # type: ignore[method-assign]
    with pytest.raises(Passed):
        await s.submit.submit(EID, decision)

    assert s.output_store.rows == rows
    assert not s.runner.is_running(EID)


async def put(store: FakeOutputStore, *objs: Any) -> None:
    async with store.transaction(EID):
        await store.put(EID, list(objs), actor="U-1")


# ── 验收 1: 任务, 版本, 允许的决定, 字段 ──


@pytest.mark.parametrize(
    ("decision", "code", "detail"),
    [
        (record("T-NOPE"), "TASK_NOT_FOUND", "T-NOPE"),
        (record(target_version=2), "VERSION_MISMATCH", None),
        (record(decision=Decision.rerun), "DECISION_NOT_ALLOWED", None),
        (
            record(FAILED, Decision.manual_result, manual_result=ManualResult(conclusion="x")),
            "DECISION_NOT_ALLOWED",
            None,
        ),
        (record(decision=Decision.override), "MISSING_FIELD", "override_conclusion"),
        (record(decision=Decision.misstatement, reason=None, misstatement_draft=DRAFT), "MISSING_FIELD", "reason"),
        (record(misstatement_draft=DRAFT), "FIELD_NOT_ALLOWED", "misstatement_draft"),
        (
            record(FAILED, Decision.rerun, manual_result=ManualResult(conclusion="x")),
            "FIELD_NOT_ALLOWED",
            "manual_result",
        ),
    ],
)
async def test_invalid_review_record_rejected(decision: ReviewRecord, code: str, detail: str | None) -> None:
    s = await waiting_with_failure()

    await assert_rejected(s, decision, code, detail)


async def test_decided_task_rejected() -> None:
    s = await waiting_with_failure()
    task = await s.output_store.get(EID, ReviewTask, NORMAL)
    assert task is not None
    await put(s.output_store, task.model_copy(update={"version": 2, "status": TaskStatus.decided}))

    await assert_rejected(s, record(), "TASK_NOT_PENDING")


async def test_target_updated_after_task_rejected() -> None:
    """判断在 Store 已出 v2 (别的决定改过), 任务与决定仍绑定 v1: 版本不符."""
    s = await waiting_with_failure()
    judgment = await s.output_store.get(EID, Judgment, "J-REV-03-1-CON-001")
    assert judgment is not None
    await put(s.output_store, judgment.model_copy(update={"version": 2}))

    await assert_rejected(s, record(), "VERSION_MISMATCH", "第 2 版")


@pytest.mark.parametrize(
    "decision",
    [
        record(),
        record(decision=Decision.override, override_conclusion="控制权在验收时转移"),
        record(decision=Decision.misstatement, misstatement_draft=DRAFT),
        record(FAILED, Decision.rerun),
    ],
)
async def test_valid_review_record_passes(decision: ReviewRecord) -> None:
    s = await waiting_with_failure()

    await assert_passes(s, decision)


# ── 验收 2, 3: 不在等复核; 校验顺序 ──


async def test_pending_task_while_at_gate_not_awaiting_review() -> None:
    """任务 pending, 但图停在 ACC-01 闸门 (静态中断, 不是节点内等待)."""
    s = await started()
    stray = ReviewTask(
        id="T-STRAY-v1",
        kind=TaskKind.failed,
        procedure_id="ACC-04",
        target_type="ProcedureRun",
        target_id="RUN-ACC-04-1",
        target_version=1,
        run_id="RUN-ACC-04-1",
        created_at=NOW,
    )
    await put(s.output_store, stray)

    await assert_rejected(s, record("T-STRAY-v1", Decision.rerun), "NOT_AWAITING_REVIEW")


async def test_locked_checked_before_task() -> None:
    s = await waiting_with_failure()
    await s.engagement_store.update(EID, locked=True)

    await assert_rejected(s, record("T-NOPE"), "ENGAGEMENT_LOCKED")


async def test_resume_required_checked_before_task() -> None:
    """Store 有检查点里没有的复核决定 (上次写完 Store 后中断): 先报须恢复, 不看任务."""
    s = await waiting_with_failure()
    await put(s.output_store, record(id="D-LOST"))

    await assert_rejected(s, record("T-NOPE"), "RESUME_REQUIRED", "D-LOST")


async def test_reused_record_id_rejected() -> None:
    """决定 ID 已用过 (已在 Store 与检查点中): 再提交时 put 会返回旧决定, 效果却按新内容算, 拒绝."""
    s = await waiting_with_failure()
    await confirm(s.output_store, 1)
    await s.runner.app.ainvoke(Command(resume="D-1"), CONFIG)

    await assert_rejected(s, record("T-J-REV-03-1-CON-002-v1"), "INVALID_VALUE", "D-1")


# ── 验收 4: P2 以外程序失败后的等待 ──


async def test_rerun_while_waiting_in_p0_passes() -> None:
    """停在 p0_intel 节点内等 ACC-04 失败的决定时, 对该 failed 任务 rerun 通过校验."""
    runner, store, _ = await failed_at_p0()
    s = Started(runner, store, runner.deps.engagement_store)  # type: ignore[arg-type]

    await assert_passes(s, record("T-RUN-ACC-04-1-v1", Decision.rerun))


# ── 验收 5: rerun_params 只能改模板已有的参数 ──


async def test_rerun_params_with_unknown_key_rejected() -> None:
    s = await waiting_with_failure()

    await assert_rejected(
        s, record(FAILED, Decision.rerun, rerun_params={"window_dayz": 25}), "INVALID_VALUE", "window_dayz"
    )


async def test_rerun_params_with_known_key_passes() -> None:
    s = await waiting_with_failure()

    await assert_passes(s, record(FAILED, Decision.rerun, rerun_params={"window_days": 25}))


# ── 审核补充: 节点内第二次等待, 决定 ID 跨两类决定, 等待的是哪条程序 ──


async def test_second_wait_in_node_accepts_next_rerun() -> None:
    """ACC-04 重跑后又失败, 节点内第二次等待: 第一个 rerun 决定已被节点读回, 但节点未返回, 还不在状态里;
    它列在等待载荷的 records 中, 不算 "须先恢复", 第二个 rerun 决定通过校验."""
    runner, store, _ = await failed_at_p0(failures=2)
    await preset_rerun(store, await pending_task(store), "D-FIRST")
    await runner.app.ainvoke(Command(resume="D-FIRST"), CONFIG)
    snapshot = await runner.app.aget_state(CONFIG)
    assert [w.value for t in snapshot.tasks for w in t.interrupts] == [
        {"awaiting": "human", "procedure_id": "ACC-04", "run_id": "RUN-ACC-04-2", "records": ["D-FIRST"]}
    ]
    assert not snapshot.values["review_records"]
    s = Started(runner, store, runner.deps.engagement_store)  # type: ignore[arg-type]
    second = await pending_task(store)

    await assert_passes(s, record(second.id, Decision.rerun, id="D-SECOND"))


async def test_second_wait_still_requires_resume_for_unread_record() -> None:
    """同上的第二次等待, 但 Store 另有一个节点没读回过的决定: 仍须先恢复."""
    runner, store, _ = await failed_at_p0(failures=2)
    await preset_rerun(store, await pending_task(store), "D-FIRST")
    await runner.app.ainvoke(Command(resume="D-FIRST"), CONFIG)
    s = Started(runner, store, runner.deps.engagement_store)  # type: ignore[arg-type]
    await put(store, record(id="D-LOST"))
    second = await pending_task(store)

    await assert_rejected(s, record(second.id, Decision.rerun, id="D-SECOND"), "RESUME_REQUIRED", "D-LOST")


async def test_review_record_cannot_reuse_gate_decision_id() -> None:
    """两类决定共用 decisions 表与主键: 用已有闸门决定的 ID 提交复核决定, 拒绝."""
    s = await waiting_with_failure()
    assert await s.output_store.get(EID, GateDecision, "D-ACC") is not None

    await assert_rejected(s, record(id="D-ACC"), "INVALID_VALUE", "D-ACC")


async def test_task_of_other_program_while_waiting_in_p0_rejected() -> None:
    """停在 p0_intel 等 ACC-04 的决定时, Store 里另有 REV-08 的待决 failed 任务 (人为注入): 不是这次等待处理的,
    拒绝; 否则节点读回时会报错停下."""
    other = await waiting_with_failure()
    task = await other.output_store.get(EID, ReviewTask, FAILED)
    run = await other.output_store.get(EID, ProcedureRun, "RUN-REV-08-1")
    assert task is not None and run is not None
    runner, store, _ = await failed_at_p0()
    s = Started(runner, store, runner.deps.engagement_store)  # type: ignore[arg-type]
    await put(store, run, task)

    await assert_rejected(s, record(FAILED, Decision.rerun), "NOT_AWAITING_REVIEW", "REV-08")


async def test_later_program_in_same_node_accepts_rerun() -> None:
    """p1_analytics 先执行 PLN-07: 失败后以 D-PLN07 重跑成功; 接着 PLN-01 失败, 节点再次等待. D-PLN07 已用过, 但图节点
    未返回, 不在检查点里; 它在传给 PLN-01 的状态中, 列入等待载荷, PLN-01 的 rerun 通过校验."""

    def pln07(ctx: Any) -> Any:
        if ctx.run.run_no == 1:
            raise RuntimeError("PLN-07 第 1 次失败")
        return {"procedure_runs": [finished(ctx.run)]}

    def pln01(ctx: Any) -> Any:
        raise RuntimeError("PLN-01 失败")

    runner, store = make_runner(agents=lambda deps: {"PLN-07": ScriptA4(deps, pln07), "PLN-01": ScriptA4(deps, pln01)})
    s = Started(runner, store, runner.deps.engagement_store)  # type: ignore[arg-type]
    await runner.start(EID)
    await wait_idle(runner, EID)
    await s.submit.submit(EID, acc01_decision(GateVerdict.approve, id="D-ACC"))
    await wait_idle(runner, EID)
    await preset_rerun(store, await pending_task(store), "D-PLN07")
    await runner.app.ainvoke(Command(resume="D-PLN07"), CONFIG)
    snapshot = await runner.app.aget_state(CONFIG)
    [payload] = [w.value for t in snapshot.tasks for w in t.interrupts]
    assert (payload["procedure_id"], payload["records"]) == ("PLN-01", ["D-PLN07"])
    assert not snapshot.values["review_records"]
    second = await pending_task(store)

    await assert_passes(s, record(second.id, Decision.rerun, id="D-PLN01"))


async def test_third_wait_lists_both_records() -> None:
    """ACC-04 连续失败三次: 第三次等待的载荷列出前两个决定, 第三个 rerun 通过校验."""
    runner, store, _ = await failed_at_p0(failures=3)
    s = Started(runner, store, runner.deps.engagement_store)  # type: ignore[arg-type]
    for decision_id in ("D-FIRST", "D-SECOND"):
        await preset_rerun(store, await pending_task(store), decision_id)
        await runner.app.ainvoke(Command(resume=decision_id), CONFIG)
    snapshot = await runner.app.aget_state(CONFIG)
    assert [w.value["records"] for t in snapshot.tasks for w in t.interrupts] == [["D-FIRST", "D-SECOND"]]

    await assert_passes(s, record((await pending_task(store)).id, Decision.rerun, id="D-THIRD"))


# ── R18 复核决定写入与 confirm ──


async def waiting_on_revenue() -> Started:
    """停在 await_events, 只剩 REV-03 的 3 个 normal 任务 (见 test_await_events.waiting_for_review)."""
    runner, store = await waiting_for_review()
    return Started(runner, store, runner.deps.engagement_store)  # type: ignore[arg-type]


def commit_after(s: Started, before: int) -> list[Any]:
    [commit] = s.output_store.commits[before:]
    return commit


def decisions_in_trail(s: Started) -> list[Any]:
    trail = s.runner.deps.trail
    assert isinstance(trail, FakeTrail)
    return [e for e in trail.entries if e.kind is TrailKind.decision]


async def test_confirm_writes_in_one_commit_and_resumes() -> None:
    """R18 验收 1: confirm 第 1 份合同: 立即返回且在后台运行; 一次提交含决定, 判断 v2 confirmed, 任务 v2 decided,
    REV-03 仍有待决任务, 不出新版本; 1 行 decision 留痕; 后台结束后图停回 await_events, 状态含该决定."""
    s = await waiting_on_revenue()
    trail_before = len(decisions_in_trail(s))
    before = len(s.output_store.commits)
    decision = record(id="D-1")

    await s.submit.submit(EID, decision)
    assert s.runner.is_running(EID)
    await wait_idle(s.runner, EID)

    commit = commit_after(s, before)
    assert [(type(o).__name__, o.id, o.version) for o in commit] == [
        ("ReviewRecord", "D-1", 1),
        ("Judgment", "J-REV-03-1-CON-001", 2),
        ("ReviewTask", NORMAL, 2),
    ]
    assert commit[1].review_status is ReviewStatus.confirmed and commit[2].status is TaskStatus.decided
    [entry] = decisions_in_trail(s)[trail_before:]
    assert (entry.actor, entry.procedure_id, entry.idempotency_key) == ("U-1", "REV-03", "D-1")
    snapshot = await s.runner.app.aget_state(CONFIG)
    assert s.runner.run_error(EID) is None
    assert snapshot.next == ("await_events",)
    assert decision in snapshot.values["review_records"]


async def test_last_confirm_completes_program_in_same_commit() -> None:
    """R18 验收 2, 6: 三个 confirm 依次提交 (每次恢复后再提交, 不报须先恢复); 第 3 个的提交含 REV-03 completed 新版本;
    图继续派发 REV-02, P2 全部完成后停在 FIN-01 闸门前; 程序包与 Store 一致."""
    s = await waiting_on_revenue()
    for n in (1, 2):
        await s.submit.submit(EID, record(f"T-J-REV-03-1-CON-00{n}-v1", id=f"D-{n}"))
        await wait_idle(s.runner, EID)
    before = len(s.output_store.commits)

    await s.submit.submit(EID, record("T-J-REV-03-1-CON-003-v1", id="D-3"))
    await wait_idle(s.runner, EID)

    rev03 = next(o for o in s.output_store.commits[before] if isinstance(o, Procedure))  # 决定那一次提交; 之后图继续跑
    assert (rev03.id, rev03.status) == ("REV-03", ProcedureStatus.completed)
    snapshot = await s.runner.app.aget_state(CONFIG)
    assert s.runner.run_error(EID) is None
    assert snapshot.next == ("gate_fin",)
    await assert_pack_synced(snapshot.values["procedure_pack"], s.output_store)


async def test_confirm_attributes_exceptions_in_same_commit() -> None:
    """R18 验收 3: confirm 函证比对的判断 J-CSH-02-1-BA-01: 它解释的 X-CSH-05-1-B01-1 与 X-CSH-02-1-D01-1 出新版本
    attributed, 与决定同一次提交; CSH-02 再无待决任务, 同一提交出 completed 新版本."""
    engagement_store = FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT])
    runner, store = make_runner(
        engagement_store=engagement_store,
        agents=lambda deps: {**p2_agents(deps), **numbat_p2_agents(deps), **numbat_cash_agents(deps)},
    )
    s = Started(runner, store, engagement_store)
    await runner.start(EID)
    await wait_idle(runner, EID)
    await s.submit.submit(EID, acc01_decision(GateVerdict.approve, id="D-ACC"))
    await wait_idle(runner, EID)
    await s.submit.submit(EID, pln_decision(GateVerdict.approve))
    await wait_idle(runner, EID)
    assert await s.next_nodes() == ("await_events",)
    before = len(store.commits)

    await s.submit.submit(EID, record("T-J-CSH-02-1-BA-01-v1", id="D-CSH"))
    await wait_idle(runner, EID)

    commit = commit_after(s, before)
    attributed = {o.id: o for o in commit if isinstance(o, AuditException)}
    assert sorted(attributed) == ["X-CSH-02-1-D01-1", "X-CSH-05-1-B01-1"]
    assert all(
        (x.version, x.disposition, x.attributed_by) == (2, ExceptionDisposition.attributed, "J-CSH-02-1-BA-01")
        for x in attributed.values()
    )
    [csh02] = [o for o in commit if isinstance(o, Procedure)]
    assert (csh02.id, csh02.status) == ("CSH-02", ProcedureStatus.completed)
    snapshot = await runner.app.aget_state(CONFIG)
    assert {x.id: x for x in snapshot.values["exceptions"]}["X-CSH-05-1-B01-1"] == attributed["X-CSH-05-1-B01-1"]


async def test_version_conflict_on_write_rejected_and_rolled_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """R18 验收 5: 写入时 put 抛 VersionConflict: 报 VERSION_MISMATCH, 整个事务回滚, Store 无新增, 锁释放, 不留痕."""
    s = await waiting_on_revenue()
    put_original = s.output_store.put

    async def conflicting(eid: str, objs: Any, *, actor: str) -> Any:
        if any(isinstance(o, Judgment) for o in objs):
            raise VersionConflict("J-REV-03-1-CON-001 v2 已存在")
        return await put_original(eid, objs, actor=actor)

    monkeypatch.setattr(s.output_store, "put", conflicting)
    trail_before = len(decisions_in_trail(s))

    await assert_rejected(s, record(), "VERSION_MISMATCH")
    assert len(decisions_in_trail(s)) == trail_before


def test_attribute_exceptions_is_pure() -> None:
    """R18 验收 4: 归因函数输入置 confirmed 的判断与当前例外, 输出例外新版本; 已归因的跳过; 缺例外抛错; 输入不变."""
    base = AuditException(
        id="X-1",
        procedure_id="CSH-05",
        run_id="RUN-CSH-05-1",
        rule_id="B01",
        severity="medium",
        source_ids=[],
        evidence=[],
    )
    done = base.model_copy(
        update={"id": "X-2", "version": 2, "disposition": ExceptionDisposition.attributed, "attributed_by": "J-0"}
    )
    other = base.model_copy(update={"id": "X-3"})
    judgment = Judgment.model_construct(id="J-1", exception_ids=["X-1", "X-2"])
    exceptions = [base, done, other]

    assert attribute_exceptions(judgment, exceptions) == [
        base.model_copy(update={"version": 2, "disposition": ExceptionDisposition.attributed, "attributed_by": "J-1"})
    ]
    assert exceptions == [base, done, other] and base.version == 1
    with pytest.raises(LookupError, match="X-9"):
        attribute_exceptions(Judgment.model_construct(id="J-1", exception_ids=["X-9"]), exceptions)


@pytest.mark.parametrize(
    ("status", "pending", "decision", "expected"),
    [
        (ProcedureStatus.awaiting_human, 1, Decision.confirm, None),
        (ProcedureStatus.awaiting_human, 0, Decision.confirm, ProcedureStatus.completed),
        (ProcedureStatus.awaiting_human, 2, Decision.rerun, ProcedureStatus.executing),
        (ProcedureStatus.executing, 0, Decision.confirm, None),
    ],
)
def test_recompute_procedure(
    status: ProcedureStatus, pending: int, decision: Decision, expected: ProcedureStatus | None
) -> None:
    """复核决定后的程序状态: rerun 为 executing; 否则有待决任务为 awaiting_human, 没有为 completed; 状态不变不出新版本;
    已是 executing (有 rerun 待派发) 时不因别的决定改掉."""
    procedure = make_procedure("REV-03", status=status)
    tasks = [ReviewTask.model_construct(id=f"T-{n}") for n in range(pending)]

    updated = recompute_procedure(procedure, tasks, decision)

    if expected is None:
        assert updated is None
    else:
        assert updated is not None and (updated.version, updated.status) == (procedure.version + 1, expected)
