"""审计师决定: 效果纯函数直接调用; submit 经图与假依赖, 验证校验, 写入与越过闸门."""

import inspect
from datetime import UTC, datetime
from typing import Any

import pytest

from backend.contracts.errors import DecisionRejected, VersionConflict
from backend.contracts.models import (
    AcceptanceScorecard,
    ClientProfile,
    Engagement,
    GateDecision,
    GateId,
    GateVerdict,
    OpinionType,
    Phase,
    Procedure,
    ProcedureStatus,
    TrailEntry,
    TrailKind,
)
from backend.orchestration import runner as runner_module
from backend.orchestration.decisions import GraphDecisionSubmit, acc01_effects
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
