"""审计师决定: 效果纯函数直接调用; submit 经图与假依赖, 验证校验, 写入与越过闸门."""

import inspect
from datetime import UTC, datetime
from typing import Any

import pytest

from backend.contracts.errors import DecisionRejected, VersionConflict
from backend.contracts.models import (
    AcceptanceScorecard,
    Annotation,
    ClientProfile,
    Engagement,
    GateDecision,
    GateEdit,
    GateId,
    GateVerdict,
    MaterialityCandidate,
    MaterialityCandidates,
    MaterialityChoice,
    OpinionType,
    Phase,
    Procedure,
    ProcedureStatus,
    ReportSection,
    RiskItem,
    RiskLevel,
    TrailEntry,
    TrailKind,
)
from backend.orchestration import runner as runner_module
from backend.orchestration.decisions import GraphDecisionSubmit, acc01_effects, materiality_amounts
from backend.orchestration.runner import GraphRunner
from tests.fakes import (
    ENGAGEMENT,
    NUMBAT_PROFILE,
    TYPO_PROFILE,
    EngagementWrite,
    FakeEngagementStore,
    FakeOutputStore,
    FakeTrail,
    NoTaskAsyncio,
    make_procedure,
    make_runner,
    wait_idle,
)

NOW = datetime(2026, 10, 1, tzinfo=UTC)
ANNOTATION = Annotation(section=ReportSection.opinion, comment="x")


def acc01_decision(verdict: GateVerdict, **changes: Any) -> GateDecision:
    """绑定评分卡第 1 版的 ACC-01 决定; reject 与 insufficient_info 带理由. changes 覆盖任意字段."""
    decision = GateDecision(
        id="D-1",
        gate=GateId.ACC_01,
        target_id="SCORECARD",
        target_version=1,
        decision=verdict,
        reason=None if verdict is GateVerdict.approve else "test",
        decided_by="U-1",
        decided_at=NOW,
    )
    return decision.model_copy(update=changes)


# ── 4a ACC-01 闸门决定的效果 ──

# 刚过 p0_intel 的程序包: ACC-04 已完成第 2 版, ACC-01 计划中第 1 版
PACK = [
    make_procedure("ACC-04", version=2).model_copy(update={"status": ProcedureStatus.completed}),
    make_procedure("ACC-01"),
]


def test_acc01_approve_completes_gate_and_enters_p1() -> None:
    decision = acc01_decision(GateVerdict.approve)

    patch = acc01_effects(PACK, decision)

    assert set(patch) == {"gate_decisions", "procedure_pack", "phase"}
    assert patch["gate_decisions"] == [decision]
    assert patch["phase"] == Phase.P1_Planning
    pack = patch["procedure_pack"]
    assert [p.id for p in pack] == ["ACC-04", "ACC-01"]
    assert pack[1].status == ProcedureStatus.completed
    assert pack[1].version == 2
    assert pack[0] is PACK[0]  # ACC-04 不动


def test_acc01_reject_completes_gate_and_declines() -> None:
    patch = acc01_effects(PACK, acc01_decision(GateVerdict.reject))

    assert patch["phase"] == Phase.Declined
    assert patch["procedure_pack"][1].status == ProcedureStatus.completed


def test_acc01_insufficient_info_only_records_decision() -> None:
    decision = acc01_decision(GateVerdict.insufficient_info)

    patch = acc01_effects(PACK, decision)

    assert patch == {"gate_decisions": [decision]}


# ── 需求 2 第 4 步: DecisionSubmit.submit (ACC-01) ──

EID = ENGAGEMENT.id


class Started:
    """新项目已在后台跑到 ACC-01 闸门前: 持有 runner, 各假存储与 submit 入口."""

    def __init__(self, runner: GraphRunner, output_store: FakeOutputStore, engagement_store: FakeEngagementStore):
        self.runner = runner
        self.output_store = output_store
        self.engagement_store = engagement_store
        self.submit = GraphDecisionSubmit(runner.app, runner.deps, runner)

    async def next_nodes(self) -> tuple[str, ...]:
        return (await self.runner.app.aget_state(GraphRunner.config(EID))).next

    def latest_procedure(self, pid: str) -> Procedure:
        return max(
            (r for r in self.output_store.rows if isinstance(r, Procedure) and r.id == pid), key=lambda p: p.version
        )

    def trail_kinds(self) -> list[TrailKind]:
        assert isinstance(self.runner.deps.trail, FakeTrail)
        return [e.kind for e in self.runner.deps.trail.entries]


async def started(profile: ClientProfile = NUMBAT_PROFILE) -> Started:
    engagement_store = FakeEngagementStore([profile], [ENGAGEMENT])
    runner, output_store = make_runner(engagement_store=engagement_store)
    await runner.start(EID)
    await wait_idle(runner, EID)
    return Started(runner, output_store, engagement_store)


async def test_submit_approve_moves_to_p1() -> None:
    s = await started()

    await s.submit.submit(EID, acc01_decision(GateVerdict.approve))
    assert s.runner.is_running(EID)  # 立即返回, 后台继续执行
    await wait_idle(s.runner, EID)

    assert await s.next_nodes() == ("gate_p1_risk",)  # P1 节点尚为占位, 一路走到下一道闸门
    assert [d.id for d in s.output_store.rows if isinstance(d, GateDecision)] == ["D-1"]
    acc01 = s.latest_procedure("ACC-01")
    assert (acc01.version, acc01.status) == (2, ProcedureStatus.completed)
    engagement = await s.engagement_store.get(EID)
    assert engagement is not None
    assert (engagement.phase, engagement.locked) == (Phase.P1_Planning, False)
    assert TrailKind.decision in s.trail_kinds()


async def test_submit_reject_declines_and_locks() -> None:
    s = await started()

    await s.submit.submit(EID, acc01_decision(GateVerdict.reject))
    await wait_idle(s.runner, EID)

    assert await s.next_nodes() == ()  # 走到 declined 结束
    engagement = await s.engagement_store.get(EID)
    assert engagement is not None
    assert (engagement.phase, engagement.locked) == (Phase.Declined, True)


async def test_submit_insufficient_info_stays_at_gate() -> None:
    s = await started()

    await s.submit.submit(EID, acc01_decision(GateVerdict.insufficient_info))
    await wait_idle(s.runner, EID)

    assert await s.next_nodes() == ("gate_acc01",)
    assert s.runner.run_error(EID) is None
    assert s.latest_procedure("ACC-01").version == 1  # 程序包不动
    engagement = await s.engagement_store.get(EID)
    assert engagement is not None
    assert engagement.phase is Phase.P0_Acceptance
    assert [d.id for d in s.output_store.rows if isinstance(d, GateDecision)] == ["D-1"]  # 决定照样留存


@pytest.mark.parametrize(
    ("decision", "code"),
    [
        (acc01_decision(GateVerdict.approve, gate=GateId.FIN_01), "NOT_AT_GATE"),
        (acc01_decision(GateVerdict.approve, target_version=2), "VERSION_MISMATCH"),
        (acc01_decision(GateVerdict.approve, target_id="ANALYTICS"), "VERSION_MISMATCH"),
        (acc01_decision(GateVerdict.reject, reason=None), "MISSING_FIELD"),
        (acc01_decision(GateVerdict.approve, disposition=OpinionType.unqualified), "FIELD_NOT_ALLOWED"),
    ],
)
async def test_submit_rejects_invalid_decision(decision: GateDecision, code: str) -> None:
    s = await started()
    rows_before = list(s.output_store.rows)

    with pytest.raises(DecisionRejected) as e:
        await s.submit.submit(EID, decision)

    assert e.value.code == code
    assert s.output_store.rows == rows_before  # 什么都没写
    assert not s.runner.is_running(EID)  # 项目锁已释放
    assert await s.next_nodes() == ("gate_acc01",)


async def test_submit_rejects_when_busy() -> None:
    engagement_store = FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT])
    runner, _ = make_runner(engagement_store=engagement_store)
    submit = GraphDecisionSubmit(runner.app, runner.deps, runner)
    await runner.start(EID)  # 后台还在执行 ACC-04
    task = runner.tasks[EID]

    with pytest.raises(DecisionRejected) as e:
        await submit.submit(EID, acc01_decision(GateVerdict.approve))

    assert e.value.code == "ENGAGEMENT_BUSY"
    assert runner.is_running(EID)  # 被拒的提交不能放掉后台任务持有的锁
    assert runner.tasks[EID] is task and not task.done()
    await wait_idle(runner, EID)


async def test_submit_rejects_when_locked() -> None:
    s = await started()
    await s.submit.submit(EID, acc01_decision(GateVerdict.reject))
    await wait_idle(s.runner, EID)

    with pytest.raises(DecisionRejected) as e:
        await s.submit.submit(EID, acc01_decision(GateVerdict.approve, id="D-2"))

    assert e.value.code == "ENGAGEMENT_LOCKED"


async def test_submit_requires_resume_when_store_ahead_of_checkpoint() -> None:
    """上次写完 Store 后进程中断, 检查点没有这个决定: 须先 resume, 不接受新决定."""
    s = await started()
    await s.output_store.put(EID, [acc01_decision(GateVerdict.insufficient_info, id="D-0")], actor="U-1")

    with pytest.raises(DecisionRejected) as e:
        await s.submit.submit(EID, acc01_decision(GateVerdict.approve))

    assert e.value.code == "RESUME_REQUIRED"
    assert not s.runner.is_running(EID)


# ── 需求 2 第 5 步: 更新客户资料, 重跑 ACC-04 ──

FIXED_PROFILE = NUMBAT_PROFILE.model_copy(update={"version": 2})  # 名称改对的第 2 版画像


async def test_update_profile_reruns_acc04_and_represents() -> None:
    s = await started(TYPO_PROFILE)

    await s.submit.update_client_profile(EID, FIXED_PROFILE, updated_by="U-1")
    assert s.runner.is_running(EID)
    await wait_idle(s.runner, EID)

    assert s.runner.run_error(EID) is None
    assert await s.next_nodes() == ("gate_acc01",)  # 重跑后回到闸门, 重新呈报
    assert await s.engagement_store.client_profile(ENGAGEMENT.client_id) == FIXED_PROFILE
    scorecard = max((r for r in s.output_store.rows if isinstance(r, AcceptanceScorecard)), key=lambda c: c.version)
    assert (scorecard.version, scorecard.client_profile_version, scorecard.exception_ids) == (2, 2, [])
    assert s.latest_procedure("ACC-04").version == 3  # 第 1 次跑出 v2, 重跑出 v3
    assert isinstance(s.runner.deps.trail, FakeTrail)
    decisions = [e for e in s.runner.deps.trail.entries if e.kind is TrailKind.decision]
    assert [e.actor for e in decisions] == ["U-1"]


async def test_update_profile_then_approve_reaches_p1() -> None:
    """需求 2 全流程: 名称写错 → 更新资料重跑 → 绑定评分卡第 2 版承接 → 进入 P1."""
    s = await started(TYPO_PROFILE)
    await s.submit.update_client_profile(EID, FIXED_PROFILE, updated_by="U-1")
    await wait_idle(s.runner, EID)

    await s.submit.submit(EID, acc01_decision(GateVerdict.approve, target_version=2))
    await wait_idle(s.runner, EID)

    assert await s.next_nodes() == ("gate_p1_risk",)
    engagement = await s.engagement_store.get(EID)
    assert engagement is not None
    assert engagement.phase is Phase.P1_Planning


@pytest.mark.parametrize(
    ("profile", "code"),
    [
        (TYPO_PROFILE.model_copy(update={"version": 3}), "VERSION_MISMATCH"),  # 应为第 2 版
        (TYPO_PROFILE.model_copy(update={"version": 1}), "VERSION_MISMATCH"),  # 不能覆盖已有版本
        (FIXED_PROFILE.model_copy(update={"id": "CLIENT-999"}), "INVALID_VALUE"),  # 不是本项目的客户
        (FIXED_PROFILE.model_copy(update={"id": "CLIENT-999", "version": 3}), "VERSION_MISMATCH"),  # 都错: 版本在前
    ],
)
async def test_update_profile_rejects_invalid(profile: ClientProfile, code: str) -> None:
    s = await started(TYPO_PROFILE)

    with pytest.raises(DecisionRejected) as e:
        await s.submit.update_client_profile(EID, profile, updated_by="U-1")

    assert e.value.code == code
    assert await s.engagement_store.client_profile(ENGAGEMENT.client_id) == TYPO_PROFILE  # 没有保存
    assert not s.runner.is_running(EID)
    assert await s.next_nodes() == ("gate_acc01",)


async def test_update_profile_only_at_acc01() -> None:
    s = await started()
    await s.submit.submit(EID, acc01_decision(GateVerdict.approve))
    await wait_idle(s.runner, EID)  # 已进入 P1, 停在 PLN-01/03 闸门

    with pytest.raises(DecisionRejected) as e:
        await s.submit.update_client_profile(EID, FIXED_PROFILE, updated_by="U-1")

    assert e.value.code == "NOT_AT_GATE"


async def test_submit_requires_resume_when_profile_ahead_of_checkpoint() -> None:
    """本项目已采用画像新版本 (Store 已提交), 检查点的 client_profile_version 还是旧的: 须先 resume."""
    s = await started(TYPO_PROFILE)
    await s.engagement_store.save_profile(FIXED_PROFILE)
    await s.engagement_store.update(EID, client_profile_version=2)

    with pytest.raises(DecisionRejected) as e:
        await s.submit.submit(EID, acc01_decision(GateVerdict.approve))

    assert e.value.code == "RESUME_REQUIRED"


# ── F1: 取锁后重读项目, 事务原子性, 写入失败时释放锁 ──


def describe(write: object) -> object:
    """commits 里的一项写入: 审计产出写成 (类名, id, 版本), 项目存储写入原样."""
    if isinstance(write, EngagementWrite):
        return write
    assert isinstance(write, (GateDecision, Procedure))
    return (type(write).__name__, write.id, write.version)


@pytest.mark.parametrize(
    ("verdict", "changes"),
    [
        (GateVerdict.approve, {"phase": Phase.P1_Planning}),
        (GateVerdict.reject, {"phase": Phase.Declined, "locked": True}),
    ],
)
async def test_acc01_decision_and_effects_in_one_commit(verdict: GateVerdict, changes: dict[str, Any]) -> None:
    """第 7 节: 决定, ACC-01 新版本与项目阶段 (拒绝时最后置 locked) 在一个事务内写入."""
    s = await started()

    await s.submit.submit(EID, acc01_decision(verdict))

    [commit] = [c for c in s.output_store.commits if any(isinstance(o, GateDecision) for o in c)]
    assert [describe(o) for o in commit] == [
        ("GateDecision", "D-1", 1),
        ("Procedure", "ACC-01", 2),
        EngagementWrite("update", EID, changes),
    ]
    await wait_idle(s.runner, EID)


async def test_acc01_decision_rolled_back_when_engagement_update_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """项目阶段写不进去: 决定与 ACC-01 新版本一并回滚, 锁释放, 图仍停在闸门."""
    s = await started()

    async def broken_update(eid: str, **changes: Any) -> None:
        raise RuntimeError("engagements 写入失败")

    monkeypatch.setattr(s.engagement_store, "update", broken_update)
    rows_before = list(s.output_store.rows)

    with pytest.raises(RuntimeError, match="engagements 写入失败"):
        await s.submit.submit(EID, acc01_decision(GateVerdict.approve))

    assert s.output_store.rows == rows_before
    assert not s.runner.is_running(EID)
    assert await s.next_nodes() == ("gate_acc01",)
    assert TrailKind.decision not in s.trail_kinds()  # 留痕在提交后才写, 回滚了就没有


async def test_submit_rechecks_lock_after_acquiring(monkeypatch: pytest.MonkeyPatch) -> None:
    """取锁前读到的项目还没锁定, 取锁时已被另一个拒绝承接的决定锁定: 报 ENGAGEMENT_LOCKED, 不是 NOT_AT_GATE."""
    s = await started()
    await s.submit.submit(EID, acc01_decision(GateVerdict.reject))
    await wait_idle(s.runner, EID)  # 已走到 declined 结束
    real_get = s.engagement_store.get
    reads = 0

    async def stale_first_read(eid: str) -> Engagement | None:
        nonlocal reads
        reads += 1
        engagement = await real_get(eid)
        if reads == 1 and engagement is not None:
            return engagement.model_copy(update={"locked": False})  # 拒绝提交前读到的旧值
        return engagement

    monkeypatch.setattr(s.engagement_store, "get", stale_first_read)
    rows_before = list(s.output_store.rows)

    with pytest.raises(DecisionRejected) as e:
        await s.submit.submit(EID, acc01_decision(GateVerdict.approve, id="D-2"))

    assert e.value.code == "ENGAGEMENT_LOCKED"
    assert not s.runner.is_running(EID)
    assert s.output_store.rows == rows_before


async def test_update_profile_version_conflict_releases_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """校验通过后, 同客户的另一个项目抢先存了第 2 版: save_profile 抛 VersionConflict, 转为 VERSION_MISMATCH."""
    s = await started(TYPO_PROFILE)
    real_save = s.engagement_store.save_profile

    async def raced_save(profile: ClientProfile) -> ClientProfile:
        s.engagement_store.profiles.append(FIXED_PROFILE)  # 另一个项目刚存的第 2 版
        return await real_save(profile)

    monkeypatch.setattr(s.engagement_store, "save_profile", raced_save)

    with pytest.raises(DecisionRejected) as e:
        await s.submit.update_client_profile(EID, FIXED_PROFILE, updated_by="U-1")

    assert e.value.code == "VERSION_MISMATCH"
    assert isinstance(e.value.__cause__, VersionConflict)
    assert not s.runner.is_running(EID)
    assert await s.next_nodes() == ("gate_acc01",)
    assert TrailKind.decision not in s.trail_kinds()


async def test_submit_launch_failure_releases_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """决定已写入, 建后台任务失败: 抛出且释放锁, 不留下 ENGAGEMENT_BUSY."""
    s = await started()
    no_tasks = NoTaskAsyncio()
    monkeypatch.setattr(runner_module, "asyncio", no_tasks)

    with pytest.raises(RuntimeError, match="事件循环已关闭"):
        await s.submit.submit(EID, acc01_decision(GateVerdict.approve))

    assert not s.runner.is_running(EID)
    [coro] = no_tasks.coros
    assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED


# ── F2: 留痕在业务事务提交后写; 画像按本项目采用的版本判断 ──


def decision_trail(s: Started) -> list[TrailEntry]:
    assert isinstance(s.runner.deps.trail, FakeTrail)
    return [e for e in s.runner.deps.trail.entries if e.kind is TrailKind.decision]


async def test_decision_trail_written_after_commit_with_key() -> None:
    s = await started()

    await s.submit.submit(EID, acc01_decision(GateVerdict.approve))

    [entry] = decision_trail(s)
    assert entry.idempotency_key == "D-1"
    assert entry.payload["decision_id"] == "D-1"
    await wait_idle(s.runner, EID)


async def test_trail_failure_after_commit_requires_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    """决定已提交, 写留痕失败: 抛出并释放锁, 检查点没越过闸门; 再提交报 RESUME_REQUIRED, 由 resume 补做与补写留痕."""
    s = await started()

    async def broken_record(entry: TrailEntry) -> str:
        raise RuntimeError("留痕写入失败")

    monkeypatch.setattr(s.runner.deps.trail, "record", broken_record)

    with pytest.raises(RuntimeError, match="留痕写入失败"):
        await s.submit.submit(EID, acc01_decision(GateVerdict.approve))

    assert [d.id for d in s.output_store.rows if isinstance(d, GateDecision)] == ["D-1"]
    assert not s.runner.is_running(EID)
    assert await s.next_nodes() == ("gate_acc01",)
    with pytest.raises(DecisionRejected) as e:
        await s.submit.submit(EID, acc01_decision(GateVerdict.approve, id="D-2"))
    assert e.value.code == "RESUME_REQUIRED"


async def test_reject_trail_failure_reports_locked(monkeypatch: pytest.MonkeyPatch) -> None:
    """拒绝承接已提交 (项目置 locked), 写留痕失败: 再提交按校验顺序先报 ENGAGEMENT_LOCKED, 不是 RESUME_REQUIRED.

    检查点仍停在 gate_acc01, 由 recover/resume 补做 (第 7 节: 已锁定但检查点停在闸门的不跳过) 并补写留痕.
    """
    s = await started()

    async def broken_record(entry: TrailEntry) -> str:
        raise RuntimeError("留痕写入失败")

    monkeypatch.setattr(s.runner.deps.trail, "record", broken_record)

    with pytest.raises(RuntimeError, match="留痕写入失败"):
        await s.submit.submit(EID, acc01_decision(GateVerdict.reject))

    engagement = await s.engagement_store.get(EID)
    assert engagement is not None and engagement.locked
    assert await s.next_nodes() == ("gate_acc01",)
    assert not s.runner.is_running(EID)
    with pytest.raises(DecisionRejected) as e:
        await s.submit.submit(EID, acc01_decision(GateVerdict.approve, id="D-2"))
    assert e.value.code == "ENGAGEMENT_LOCKED"


async def test_update_profile_adopts_version_in_one_commit() -> None:
    """画像新版本与本项目采用的版本在同一次提交; 留痕在提交后写, 防重复键为 客户-v版本."""
    s = await started(TYPO_PROFILE)

    await s.submit.update_client_profile(EID, FIXED_PROFILE, updated_by="U-1")

    [commit] = [c for c in s.output_store.commits if any(isinstance(w, EngagementWrite) for w in c)]
    assert commit == [
        EngagementWrite("save_profile", "CLIENT-001", {"version": 2}),
        EngagementWrite("update", EID, {"client_profile_version": 2}),
    ]
    engagement = await s.engagement_store.get(EID)
    assert engagement is not None and engagement.client_profile_version == 2
    [entry] = decision_trail(s)
    assert entry.idempotency_key == "CLIENT-001-v2"
    await wait_idle(s.runner, EID)


async def test_update_profile_rolled_back_when_adoption_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """改项目采用的版本失败: 已暂存的画像一并回滚, 不写留痕, 锁释放, 仍停在 ACC-01."""
    s = await started(TYPO_PROFILE)
    real_update = s.engagement_store.update

    async def broken_update(eid: str, **changes: Any) -> None:
        if "client_profile_version" in changes:
            raise RuntimeError("engagements 写入失败")
        await real_update(eid, **changes)

    monkeypatch.setattr(s.engagement_store, "update", broken_update)

    with pytest.raises(RuntimeError, match="engagements 写入失败"):
        await s.submit.update_client_profile(EID, FIXED_PROFILE, updated_by="U-1")

    assert await s.engagement_store.client_profile("CLIENT-001") == TYPO_PROFILE
    assert decision_trail(s) == []
    assert not s.runner.is_running(EID)
    assert await s.next_nodes() == ("gate_acc01",)


ENG2 = ENGAGEMENT.model_copy(update={"id": "ENG-2026-002"})  # 同一客户 CLIENT-001 的另一个项目


async def test_shared_client_profile_update_does_not_block_other_engagement() -> None:
    """项目 1 在 ACC-01 把画像改到第 2 版; 项目 2 建于其后, 仍采用第 1 版 (数据集画像).

    项目 2 的 ACC-04 按第 1 版核查 (名称写错, 出 name_match 例外); 它的决定不报 RESUME_REQUIRED, 正常进入 P1.
    """
    engagement_store = FakeEngagementStore([TYPO_PROFILE], [ENGAGEMENT, ENG2])
    runner, output_store = make_runner(engagement_store=engagement_store)
    submit = GraphDecisionSubmit(runner.app, runner.deps, runner)
    await runner.start(EID)
    await wait_idle(runner, EID)
    await submit.update_client_profile(EID, FIXED_PROFILE, updated_by="U-1")
    await wait_idle(runner, EID)

    await runner.start(ENG2.id)
    await wait_idle(runner, ENG2.id)
    card2 = await output_store.get(ENG2.id, AcceptanceScorecard, "SCORECARD")
    assert card2 is not None
    assert (card2.client_profile_version, card2.exception_ids) == (1, ["X-ACC-04-1-name_match-1"])

    await submit.submit(ENG2.id, acc01_decision(GateVerdict.approve, id="D-2"))
    await wait_idle(runner, ENG2.id)

    snapshot2 = await runner.app.aget_state(GraphRunner.config(ENG2.id))
    assert snapshot2.next == ("gate_p1_risk",)
    assert runner.run_error(ENG2.id) is None
    e1, e2 = await engagement_store.get(EID), await engagement_store.get(ENG2.id)
    assert e1 is not None and e2 is not None
    assert (e1.client_profile_version, e2.client_profile_version) == (2, 1)


async def test_profile_update_elsewhere_does_not_pull_back_engagement_in_p1() -> None:
    """项目 2 已过 ACC-01 停在 PLN-01/03 闸门, 项目 1 随后改画像: 项目 2 的检查点不动, 也不须 resume."""
    engagement_store = FakeEngagementStore([TYPO_PROFILE], [ENGAGEMENT, ENG2])
    runner, _ = make_runner(engagement_store=engagement_store)
    submit = GraphDecisionSubmit(runner.app, runner.deps, runner)
    for eid in (EID, ENG2.id):
        await runner.start(eid)
        await wait_idle(runner, eid)
    await submit.submit(ENG2.id, acc01_decision(GateVerdict.approve, id="D-2"))
    await wait_idle(runner, ENG2.id)

    await submit.update_client_profile(EID, FIXED_PROFILE, updated_by="U-1")
    await wait_idle(runner, EID)

    assert (await runner.app.aget_state(GraphRunner.config(ENG2.id))).next == ("gate_p1_risk",)
    with pytest.raises(DecisionRejected) as e:  # RESUME_REQUIRED 的检查在 NOT_AT_GATE 之前
        await submit.update_client_profile(ENG2.id, FIXED_PROFILE.model_copy(update={"version": 3}), updated_by="U-2")
    assert e.value.code == "NOT_AT_GATE"


# ── R09 闸门决定按闸门分派, PLN-01/03 校验 ──

CHOICE = MaterialityChoice(basis="revenue", pct=0.01, performance_pct=0.75, clearly_trivial_pct=0.05)


def pln_decision(verdict: GateVerdict, **changes: Any) -> GateDecision:
    """绑定 MATERIALITY 第 1 版的 PLN-01/03 决定: approve 带合成数据集第 7 节的选择, 其余带理由.

    changes 覆盖任意字段.
    """
    decision = GateDecision(
        id="D-PLN",
        gate=GateId.PLN_01_03,
        target_id="MATERIALITY",
        target_version=1,
        decision=verdict,
        materiality=CHOICE if verdict is GateVerdict.approve else None,
        reason=None if verdict is GateVerdict.approve else "收入口径应剔除关联方销售",
        decided_by="U-1",
        decided_at=NOW,
    )
    return decision.model_copy(update=changes)


def edit(target_id: str = "RISK-CASH-EXI", target_version: int = 1) -> GateEdit:
    return GateEdit(
        target_id=target_id, target_version=target_version, field="level", new_value=RiskLevel.significant, reason="x"
    )


async def at_pln_gate() -> Started:
    """新项目跑到 ACC-01, approve 后 P1 用 Numbat 脚本 Agent 跑完, 停在 PLN-01/03 闸门前."""
    s = await started()
    await s.submit.submit(EID, acc01_decision(GateVerdict.approve, id="D-ACC"))
    await wait_idle(s.runner, EID)
    assert await s.next_nodes() == ("gate_p1_risk",)
    return s


async def assert_rejected(s: Started, decision: GateDecision, code: str, detail: str | None = None) -> None:
    """提交被拒: 错误码对, Store 无新写入, 项目锁已释放, 图不动."""
    rows_before, next_before = list(s.output_store.rows), await s.next_nodes()

    with pytest.raises(DecisionRejected) as e:
        await s.submit.submit(EID, decision)

    assert e.value.code == code, e.value
    if detail is not None:
        assert detail in str(e.value)
    assert s.output_store.rows == rows_before
    assert not s.runner.is_running(EID)
    assert await s.next_nodes() == next_before


@pytest.mark.parametrize(
    ("decision", "code", "detail"),
    [
        (pln_decision(GateVerdict.approve, materiality=None), "MISSING_FIELD", "materiality"),
        (pln_decision(GateVerdict.reject, reason=None), "MISSING_FIELD", "reason"),
        (pln_decision(GateVerdict.insufficient_info, reason=""), "MISSING_FIELD", "reason"),
        (pln_decision(GateVerdict.reject, materiality=CHOICE), "FIELD_NOT_ALLOWED", "materiality"),
        (pln_decision(GateVerdict.reject, edits=[edit()]), "FIELD_NOT_ALLOWED", "edits"),
        (pln_decision(GateVerdict.approve, disposition=OpinionType.qualified), "FIELD_NOT_ALLOWED", "disposition"),
        (pln_decision(GateVerdict.approve, annotations=[ANNOTATION]), "FIELD_NOT_ALLOWED", "annotations"),
        (pln_decision(GateVerdict.approve, target_version=2), "VERSION_MISMATCH", None),
        (pln_decision(GateVerdict.approve, target_id="ANALYTICS"), "VERSION_MISMATCH", None),
        (pln_decision(GateVerdict.approve, edits=[edit(target_version=2)]), "VERSION_MISMATCH", "RISK-CASH-EXI"),
        (
            pln_decision(GateVerdict.approve, materiality=CHOICE.model_copy(update={"pct": 0.02})),
            "INVALID_VALUE",
            "pct",
        ),
        (
            pln_decision(GateVerdict.approve, materiality=CHOICE.model_copy(update={"pct": 0.004})),
            "INVALID_VALUE",
            "pct",
        ),
        (
            pln_decision(GateVerdict.approve, materiality=CHOICE.model_copy(update={"performance_pct": 0.8})),
            "INVALID_VALUE",
            "performance_pct",
        ),
        (
            pln_decision(GateVerdict.approve, materiality=CHOICE.model_copy(update={"clearly_trivial_pct": 0.0})),
            "INVALID_VALUE",
            "clearly_trivial_pct",
        ),
        (
            pln_decision(GateVerdict.approve, materiality=CHOICE.model_copy(update={"clearly_trivial_pct": 0.06})),
            "INVALID_VALUE",
            "clearly_trivial_pct",
        ),
        (pln_decision(GateVerdict.approve, edits=[edit("RISK-XXX")]), "INVALID_VALUE", "RISK-XXX"),
        (pln_decision(GateVerdict.approve, edits=[edit(), edit()]), "INVALID_VALUE", "RISK-CASH-EXI"),
        (pln_decision(GateVerdict.approve, id="D-ACC"), "INVALID_VALUE", "D-ACC"),
    ],
    ids=[
        "approve_without_materiality",
        "reject_without_reason",
        "insufficient_info_empty_reason",
        "reject_with_materiality",
        "reject_with_edits",
        "disposition",
        "annotations",
        "stale_target_version",
        "wrong_target",
        "stale_edit_version",
        "pct_above_range",
        "pct_below_range",
        "performance_pct",
        "clearly_trivial_zero",
        "clearly_trivial_above_candidate",
        "unknown_risk",
        "same_risk_twice",
        "reused_decision_id",
    ],
)
async def test_pln_decision_rejected(decision: GateDecision, code: str, detail: str | None) -> None:
    s = await at_pln_gate()

    await assert_rejected(s, decision, code, detail)


@pytest.mark.parametrize(
    ("decision", "code", "detail"),
    [
        (pln_decision(GateVerdict.approve, target_version=2, materiality=None), "VERSION_MISMATCH", None),
        (pln_decision(GateVerdict.reject, edits=[edit(target_version=2)]), "VERSION_MISMATCH", "RISK-CASH-EXI"),
        (
            pln_decision(GateVerdict.approve, edits=[edit("RISK-XXX", 9), edit(target_version=2)]),
            "VERSION_MISMATCH",
            "RISK-CASH-EXI",
        ),
        (pln_decision(GateVerdict.reject, edits=[edit("RISK-XXX", 9)]), "FIELD_NOT_ALLOWED", "edits"),
        (pln_decision(GateVerdict.reject, reason=None, edits=[edit("RISK-XXX", 9)]), "MISSING_FIELD", "reason"),
        (pln_decision(GateVerdict.approve, id="D-ACC", materiality=None), "MISSING_FIELD", "materiality"),
        (
            pln_decision(GateVerdict.approve, id="D-ACC", materiality=CHOICE.model_copy(update={"pct": 0.02})),
            "INVALID_VALUE",
            "materiality.pct",
        ),
        (pln_decision(GateVerdict.approve, id="D-ACC", edits=[edit(), edit()]), "INVALID_VALUE", "同一风险项"),
    ],
    ids=[
        "stale_target_before_missing_field",
        "stale_edit_before_field_not_allowed",
        "stale_edit_before_unknown_edit",
        "field_not_allowed_before_unknown_edit",
        "missing_field_first_among_fields",
        "missing_field_before_reused_id",
        "bad_pct_before_reused_id",
        "repeated_edit_before_reused_id",
    ],
)
async def test_pln_errors_reported_in_section_7_order(decision: GateDecision, code: str, detail: str | None) -> None:
    """同时有几处错时按第 7 节顺序报第一处: 版本 → 字段 → 取值 → 决定 ID (Codex R09-01)."""
    s = await at_pln_gate()

    await assert_rejected(s, decision, code, detail)


@pytest.mark.parametrize("kind", ["materiality", "risk"])
async def test_pln_versions_read_from_store_not_checkpoint(kind: str) -> None:
    """Store 中的材料或风险项已是第 2 版而检查点仍是第 1 版: 绑定第 1 版报 VERSION_MISMATCH, 绑定第 2 版通过校验."""
    s = await at_pln_gate()
    model, target_id = (MaterialityCandidates, "MATERIALITY") if kind == "materiality" else (RiskItem, "RISK-CASH-EXI")
    current = await s.output_store.get(EID, model, target_id)
    assert current is not None
    await s.output_store.put(EID, [current.model_copy(update={"version": 2})], actor="U-1")
    stale = {} if kind == "materiality" else {"edits": [edit()]}
    fresh = {"target_version": 2} if kind == "materiality" else {"edits": [edit(target_version=2)]}

    await assert_rejected(s, pln_decision(GateVerdict.approve, **stale), "VERSION_MISMATCH")
    with pytest.raises(NotImplementedError, match="效果尚未实现"):
        await s.submit.submit(EID, pln_decision(GateVerdict.approve, **fresh))


async def test_pln_decision_at_acc01_not_at_gate() -> None:
    s = await started()

    await assert_rejected(s, pln_decision(GateVerdict.approve), "NOT_AT_GATE")


async def test_acc01_decision_at_pln_gate_not_at_gate() -> None:
    s = await at_pln_gate()

    await assert_rejected(s, acc01_decision(GateVerdict.approve, id="D-9"), "NOT_AT_GATE")


@pytest.mark.parametrize(
    "decision",
    [
        pln_decision(GateVerdict.approve),
        pln_decision(GateVerdict.approve, materiality=CHOICE.model_copy(update={"pct": 0.005})),
        pln_decision(
            GateVerdict.approve, materiality=CHOICE.model_copy(update={"basis": "profit_before_tax", "pct": 0.05})
        ),
        pln_decision(GateVerdict.approve, materiality=CHOICE.model_copy(update={"performance_pct": 0.5})),
        pln_decision(GateVerdict.approve, edits=[edit(), edit("RISK-REV-CUT")]),
        pln_decision(GateVerdict.reject),
        pln_decision(GateVerdict.insufficient_info),
    ],
    ids=["demo_choice", "revenue_pct_low", "pbt_pct", "performance_low", "two_edits", "reject", "insufficient_info"],
)
async def test_valid_pln_decision_passes_checks_then_effects_not_implemented(decision: GateDecision) -> None:
    """闭区间边界 (revenue 0.005 与 0.01, profit_before_tax 0.05, performance 0.5 与 0.75, 明显微小 0.05) 都通过校验;
    效果留给下一步, 暂抛 NotImplementedError, 什么都不写, 锁已释放."""
    s = await at_pln_gate()
    rows_before = list(s.output_store.rows)

    with pytest.raises(NotImplementedError, match="效果尚未实现"):
        await s.submit.submit(EID, decision)

    assert s.output_store.rows == rows_before
    assert not s.runner.is_running(EID)


async def test_reused_acc01_decision_id_rejected() -> None:
    """ACC-01 信息不足 D-1 留在闸门, 再以 D-1 提交 approve: 拒绝, 不按新内容写效果 (BASE-02)."""
    s = await started()
    await s.submit.submit(EID, acc01_decision(GateVerdict.insufficient_info))
    await wait_idle(s.runner, EID)

    await assert_rejected(s, acc01_decision(GateVerdict.approve), "INVALID_VALUE", "D-1")
    assert s.latest_procedure("ACC-01").status is ProcedureStatus.planned


# ── 重要性三项 ──


@pytest.mark.parametrize(
    ("base", "choice", "expected"),
    [
        (1200000000, CHOICE, (12000000, 9000000, 600000)),
        # 1234567 × 0.005 = 6172.835 → 6173; 6173 × 0.75 = 4629.75 → 4630; 6173 × 0.05 = 308.65 → 309
        (1234567, CHOICE.model_copy(update={"pct": 0.005}), (6173, 4630, 309)),
        # 整体 5000 × 0.01 = 50; 执行 50 × 0.57 按十进制为 28.5 → 29 (浮点为 28.499999999999996, 会得 28);
        # 明显微小 50 × 0.05 = 2.5 → 3 (Python 的 round 按银行家舍入会得 2)
        (5000, CHOICE.model_copy(update={"performance_pct": 0.57}), (50, 29, 3)),
    ],
)
def test_materiality_amounts(base: int, choice: MaterialityChoice, expected: tuple[int, int, int]) -> None:
    candidates = MaterialityCandidates(
        id="MATERIALITY",
        candidates=[
            MaterialityCandidate(
                basis="revenue", base_amount_cents=base, pct_low=0, pct_high=1, low_cents=0, high_cents=base
            )
        ],
    )

    assert materiality_amounts(candidates, choice) == expected
