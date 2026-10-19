"""审计师决定: 效果纯函数直接调用; submit 经图与假依赖, 验证校验, 写入与越过闸门."""

import inspect
import shutil
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_asyncio

from backend.agents.base import AgentDeps, ProcedureAgent, RunContext
from backend.contracts.errors import DataRejected, DecisionRejected, EngagementLocked, VersionConflict
from backend.contracts.models import (
    AcceptanceScorecard,
    Annotation,
    AuditObjective,
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
    ProfileSource,
    ReportSection,
    ReviewRecord,
    RiskItem,
    RiskLevel,
    TrailEntry,
    TrailKind,
)
from backend.contracts.state import StatePatch
from backend.core.config import get_settings
from backend.orchestration import runner as runner_module
from backend.orchestration.decisions import GraphDecisionSubmit
from backend.orchestration.gates import acc01_effects, materiality_amounts, pln_effects
from backend.orchestration.runner import GraphRunner
from backend.services.procedure_library import ProcedureLibrary
from tests.fakes import (
    ENGAGEMENT,
    NUMBAT_CANDIDATES,
    NUMBAT_OBJECTIVES,
    NUMBAT_PROFILE,
    NUMBAT_RISKS,
    TYPO_PROFILE,
    EngagementWrite,
    FakeEngagementStore,
    FakeOutputStore,
    FakeTrail,
    NoTaskAsyncio,
    ScriptA4,
    ScriptAgent,
    acc01_decision,
    assert_pack_synced,
    make_procedure,
    make_runner,
    numbat_p1_agents,
    numbat_pln01_with,
    profile_update,
    wait_idle,
)

NOW = datetime(2026, 10, 1, tzinfo=UTC)
ANNOTATION = Annotation(section=ReportSection.opinion, comment="x")


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


Agents = Callable[[AgentDeps], dict[str, ProcedureAgent[Any]]]  # make_runner 的 agents
# 用模块共用的运行 (acc_gate, typo_gate, pln_gate) 的用例与它在同一个事件循环里
shared_loop = pytest.mark.asyncio(loop_scope="module")


async def started(
    profile: ClientProfile = NUMBAT_PROFILE, agents: Agents | None = None, library: ProcedureLibrary | None = None
) -> Started:
    engagement_store = FakeEngagementStore([profile], [ENGAGEMENT])
    runner, output_store = make_runner(engagement_store=engagement_store, agents=agents, library=library)
    await runner.start(EID)
    await wait_idle(runner, EID)
    return Started(runner, output_store, engagement_store)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def acc_gate() -> Started:
    """停在 ACC-01 闸门前的一次运行, 由只提交被拒决定的用例共用 (每次核对存储与图都没动)."""
    return await started()


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
    assert s.runner.run_error(EID) is None  # 项目已 locked, declined 不写 Store, 正常结束
    engagement = await s.engagement_store.get(EID)
    assert engagement is not None
    assert (engagement.phase, engagement.locked) == (Phase.Declined, True)
    with pytest.raises(EngagementLocked):
        await s.output_store.put(EID, [acc01_decision(GateVerdict.approve, id="D-RAW")], actor="U-1")


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
@shared_loop
async def test_submit_rejects_invalid_decision(acc_gate: Started, decision: GateDecision, code: str) -> None:
    await assert_rejected(acc_gate, decision, code)
    assert await acc_gate.next_nodes() == ("gate_acc01",)


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

    await s.submit.update_client_profile(EID, *profile_update(FIXED_PROFILE), updated_by="U-1")
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
    await s.submit.update_client_profile(EID, *profile_update(FIXED_PROFILE), updated_by="U-1")
    await wait_idle(s.runner, EID)

    await s.submit.submit(EID, acc01_decision(GateVerdict.approve, target_version=2))
    await wait_idle(s.runner, EID)

    assert await s.next_nodes() == ("gate_p1_risk",)
    engagement = await s.engagement_store.get(EID)
    assert engagement is not None
    assert engagement.phase is Phase.P1_Planning


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def typo_gate() -> Started:
    """客户画像有错字, 停在 ACC-01 闸门前的一次运行, 由只提交被拒画像更新的用例共用 (每次核对画像与图都没动)."""
    return await started(TYPO_PROFILE)


@pytest.mark.parametrize(
    ("profile", "code"),
    [
        (TYPO_PROFILE.model_copy(update={"version": 3}), "VERSION_MISMATCH"),  # 应为第 2 版
        (TYPO_PROFILE.model_copy(update={"version": 1}), "VERSION_MISMATCH"),  # 不能覆盖已有版本
        (FIXED_PROFILE.model_copy(update={"id": "CLIENT-999"}), "INVALID_VALUE"),  # 不是本项目的客户
        (FIXED_PROFILE.model_copy(update={"id": "CLIENT-999", "version": 3}), "VERSION_MISMATCH"),  # 都错: 版本在前
        (FIXED_PROFILE.model_copy(update={"abn": "68 900 042 016"}), "INVALID_VALUE"),  # ABN 校验位不对
        (FIXED_PROFILE.model_copy(update={"abn": "6890004201"}), "INVALID_VALUE"),  # ABN 不是 11 位
    ],
)
@shared_loop
async def test_update_profile_rejects_invalid(typo_gate: Started, profile: ClientProfile, code: str) -> None:
    s = typo_gate

    await assert_no_effect(
        s, lambda: s.submit.update_client_profile(EID, *profile_update(profile), updated_by="U-1"), code
    )

    assert await s.engagement_store.client_profile(ENGAGEMENT.client_id) == TYPO_PROFILE  # 没有保存
    assert await s.next_nodes() == ("gate_acc01",)


async def test_update_profile_only_at_acc01() -> None:
    s = await started()
    await s.submit.submit(EID, acc01_decision(GateVerdict.approve))
    await wait_idle(s.runner, EID)  # 已进入 P1, 停在 PLN-01/03 闸门

    with pytest.raises(DecisionRejected) as e:
        await s.submit.update_client_profile(EID, *profile_update(FIXED_PROFILE), updated_by="U-1")

    assert e.value.code == "NOT_AT_GATE"


async def test_update_profile_checks_a_changed_abn_like_new_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    """ABN 改了时另查重 (同新建客户, 同一范围): 已是别的客户 (库里的, 或还没进库的数据集客户) 的报 ABN_TAKEN, 带那个
    客户的编号与名称, 不保存; ABN 只差空格不算改, 不查重 (库里本客户自己的 ABN 不算撞上), 照常保存."""
    from backend.orchestration import decisions

    xero_like = NUMBAT_PROFILE.model_copy(
        update={"id": "CLIENT-XERO-DEMO", "legal_name": "Demo Company (AU)", "abn": "25900043021"}
    )
    monkeypatch.setattr(decisions, "dataset_profiles", lambda datasets: [NUMBAT_PROFILE, xero_like])
    s = await started(TYPO_PROFILE)
    with pytest.raises(DataRejected) as e:  # 撞上还没进库的数据集客户
        await s.submit.update_client_profile(
            EID, *profile_update(FIXED_PROFILE.model_copy(update={"abn": "25 900 043 021"})), updated_by="U-1"
        )
    assert (e.value.code, e.value.extra["client_id"]) == ("ABN_TAKEN", xero_like.id)
    wombat = NUMBAT_PROFILE.model_copy(update={"id": "CLIENT-M-0000000000aa", "legal_name": "Wombat Pty Ltd"})
    await s.engagement_store.create_client(
        wombat.model_copy(update={"abn": "44900043101"}), created_by="U-1", dataset_profiles=[]
    )
    taken = FIXED_PROFILE.model_copy(update={"abn": "44 900 043 101"})
    with pytest.raises(DataRejected) as e:
        await s.submit.update_client_profile(EID, *profile_update(taken), updated_by="U-1")
    assert (e.value.code, e.value.extra) == ("ABN_TAKEN", {"client_id": wombat.id, "legal_name": "Wombat Pty Ltd"})
    assert await s.engagement_store.client_profile(ENGAGEMENT.client_id) == TYPO_PROFILE
    assert not s.runner.is_running(EID)

    spaced = FIXED_PROFILE.model_copy(update={"abn": "68 900 042 015"})
    await s.submit.update_client_profile(EID, *profile_update(spaced), updated_by="U-1")
    await wait_idle(s.runner, EID)
    assert await s.engagement_store.client_profile(ENGAGEMENT.client_id) == spaced


BAD_YEAR_END = {"financial_year_end": "13-01"}  # 合并后不合 ClientProfile


async def test_update_profile_merges_given_fields_onto_latest() -> None:
    """changes 只给部分字段: 以最新一版为底合并, 没给的沿用; 合并后不合格报 INVALID_VALUE, 不保存."""
    s = await started(TYPO_PROFILE)
    with pytest.raises(DecisionRejected) as e:
        await s.submit.update_client_profile(EID, 2, BAD_YEAR_END, updated_by="U-1")
    assert e.value.code == "INVALID_VALUE" and "financial_year_end" in str(e.value)
    assert await s.engagement_store.client_profile(ENGAGEMENT.client_id, 2) is None

    await s.submit.update_client_profile(EID, 2, {"legal_name": FIXED_PROFILE.legal_name}, updated_by="U-1")
    await wait_idle(s.runner, EID)
    assert await s.engagement_store.client_profile(ENGAGEMENT.client_id) == FIXED_PROFILE


async def test_update_profile_checks_lock_gate_and_version_before_merging() -> None:
    """合并后不合格 (财年末 13-01) 时, 版本过期, 不在闸门与项目锁定照样先报各自的错 (第 7 节顺序)."""
    s = await started(TYPO_PROFILE)
    with pytest.raises(DecisionRejected) as e:
        await s.submit.update_client_profile(EID, 3, BAD_YEAR_END, updated_by="U-1")  # 应为第 2 版
    assert e.value.code == "VERSION_MISMATCH"

    s = await started()
    await s.submit.submit(EID, acc01_decision(GateVerdict.approve))
    await wait_idle(s.runner, EID)  # 已进入 P1
    with pytest.raises(DecisionRejected) as e:
        await s.submit.update_client_profile(EID, 2, BAD_YEAR_END, updated_by="U-1")
    assert e.value.code == "NOT_AT_GATE"

    s = await started()
    await s.submit.submit(EID, acc01_decision(GateVerdict.reject))
    await wait_idle(s.runner, EID)  # 拒绝承接, 项目锁定
    with pytest.raises(DecisionRejected) as e:
        await s.submit.update_client_profile(EID, 2, BAD_YEAR_END, updated_by="U-1")
    assert e.value.code == "ENGAGEMENT_LOCKED"


async def test_submit_requires_resume_when_profile_ahead_of_checkpoint() -> None:
    """本项目已采用画像新版本 (Store 已提交), 检查点的 client_profile_version 还是旧的: 须先 resume."""
    s = await started(TYPO_PROFILE)
    await s.engagement_store.save_profile(FIXED_PROFILE, source=ProfileSource.update, updated_by="U-1")
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

    async def raced_save(profile: ClientProfile, **meta: Any) -> ClientProfile:
        s.engagement_store.profiles.append(FIXED_PROFILE)  # 另一个项目刚存的第 2 版
        return await real_save(profile, **meta)

    monkeypatch.setattr(s.engagement_store, "save_profile", raced_save)

    with pytest.raises(DecisionRejected) as e:
        await s.submit.update_client_profile(EID, *profile_update(FIXED_PROFILE), updated_by="U-1")

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

    await s.submit.update_client_profile(EID, *profile_update(FIXED_PROFILE), updated_by="U-1")

    [commit] = [c for c in s.output_store.commits if EngagementWrite("save_profile", "CLIENT-001", {"version": 2}) in c]
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
        await s.submit.update_client_profile(EID, *profile_update(FIXED_PROFILE), updated_by="U-1")

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
    await submit.update_client_profile(EID, *profile_update(FIXED_PROFILE), updated_by="U-1")
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

    await submit.update_client_profile(EID, *profile_update(FIXED_PROFILE), updated_by="U-1")
    await wait_idle(runner, EID)

    assert (await runner.app.aget_state(GraphRunner.config(ENG2.id))).next == ("gate_p1_risk",)
    with pytest.raises(DecisionRejected) as e:  # RESUME_REQUIRED 的检查在 NOT_AT_GATE 之前
        await submit.update_client_profile(
            ENG2.id, *profile_update(FIXED_PROFILE.model_copy(update={"version": 3})), updated_by="U-2"
        )
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


async def at_pln_gate(agents: Agents | None = None, library: ProcedureLibrary | None = None) -> Started:
    """新项目跑到 ACC-01, approve 后 P1 用 Numbat 脚本 Agent 跑完, 停在 PLN-01/03 闸门前. agents, library 见 started."""
    s = await started(agents=agents, library=library)
    await s.submit.submit(EID, acc01_decision(GateVerdict.approve, id="D-ACC"))
    await wait_idle(s.runner, EID)
    assert await s.next_nodes() == ("gate_p1_risk",)
    return s


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def pln_gate() -> Started:
    """停在 PLN-01/03 闸门前的一次运行, 由只提交被拒决定的用例共用: assert_rejected 逐次核对存储与图都没动."""
    return await at_pln_gate()


def stop_after_submit(s: Started) -> None:
    """submit 写入并越过闸门后不在后台继续执行, 释放项目锁: 只看这一步的结果. fan_in 尚未并入暂存结果,
    PLN-01/03 通过后若继续执行会反复派发同一批程序, 直到递归上限."""
    s.runner.launch = lambda eid, graph_input: s.runner.release(eid)  # type: ignore[method-assign]


async def side_effects(s: Started) -> tuple[Any, ...]:
    """被拒的提交不该改动的一切: 审计产出与各次提交, 项目与客户画像, 留痕, 检查点的状态值, 下一步与各任务的中断.
    只提交被拒决定的参数化用例共用一次运行 (如 pln_gate), 靠它保证前一个参数没给后一个留下副作用."""
    snapshot = await s.runner.app.aget_state(GraphRunner.config(EID))
    engagements, trail = s.engagement_store, s.runner.deps.trail
    assert isinstance(trail, FakeTrail)
    return (
        list(s.output_store.rows),
        list(s.output_store.commits),
        list(engagements.engagements),
        list(engagements.profiles),
        dict(engagements.profile_meta),
        list(trail.entries),
        snapshot.values,
        snapshot.next,
        [t.interrupts for t in snapshot.tasks],
    )


async def assert_no_effect(
    s: Started, submit: Callable[[], Awaitable[Any]], code: str, detail: str | None = None
) -> None:
    """submit() 被拒: 错误码对 (detail 给了时在消息里), 项目锁已释放, 什么都没写 (见 side_effects)."""
    before = await side_effects(s)

    with pytest.raises(DecisionRejected) as e:
        await submit()

    assert e.value.code == code, e.value
    if detail is not None:
        assert detail in str(e.value)
    assert not s.runner.is_running(EID)
    assert await side_effects(s) == before


async def assert_rejected(
    s: Started, decision: GateDecision | ReviewRecord, code: str, detail: str | None = None
) -> None:
    """提交闸门或复核决定被拒, 什么都没写 (见 assert_no_effect)."""
    await assert_no_effect(s, lambda: s.submit.submit(EID, decision), code, detail)


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
@shared_loop
async def test_pln_decision_rejected(pln_gate: Started, decision: GateDecision, code: str, detail: str | None) -> None:
    await assert_rejected(pln_gate, decision, code, detail)


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
@shared_loop
async def test_pln_errors_reported_in_section_7_order(
    pln_gate: Started, decision: GateDecision, code: str, detail: str | None
) -> None:
    """同时有几处错时按第 7 节顺序报第一处: 版本 → 字段 → 取值 → 决定 ID (Codex R09-01)."""
    await assert_rejected(pln_gate, decision, code, detail)


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
    stop_after_submit(s)
    await s.submit.submit(EID, pln_decision(GateVerdict.approve, **fresh))  # 通过校验, 不抛 DecisionRejected


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
async def test_valid_pln_decision_accepted(decision: GateDecision) -> None:
    """闭区间边界 (revenue 0.005 与 0.01, profit_before_tax 0.05, performance 0.5 与 0.75, 明显微小 0.05) 都通过校验,
    决定写入 Store."""
    s = await at_pln_gate()
    stop_after_submit(s)

    await s.submit.submit(EID, decision)

    assert await s.output_store.get(EID, GateDecision, decision.id) == decision


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


# ── R10 PLN-01/03 决定的效果 ──


def by_id(objs: list[Any]) -> dict[str, Any]:
    return {o.id: o for o in objs}


async def test_pln_approve_sets_materiality_completes_programs_and_moves_on() -> None:
    """验收 1: 三个金额进状态; PLN-01, PLN-03 completed; 决定与两条程序新版本同一次提交; 1 行 decision 留痕;
    越过闸门后进装配 (Numbat 带出的审计目标会装配出 P2 程序, fieldwork 尚为占位, 故只查越过闸门这一步)."""
    s = await at_pln_gate()
    commits_before = len(s.output_store.commits)
    trail_before = len(decision_trail(s))
    stop_after_submit(s)

    await s.submit.submit(EID, pln_decision(GateVerdict.approve))

    snapshot = await s.runner.app.aget_state(GraphRunner.config(EID))
    assert snapshot.next == ("assemble_pack",)
    values = snapshot.values
    assert (values["materiality_cents"], values["performance_materiality_cents"], values["clearly_trivial_cents"]) == (
        12000000,
        9000000,
        600000,
    )
    pack = by_id(values["procedure_pack"])
    assert (pack["PLN-01"].version, pack["PLN-01"].status) == (3, ProcedureStatus.completed)
    assert (pack["PLN-03"].version, pack["PLN-03"].status) == (2, ProcedureStatus.completed)
    [commit] = s.output_store.commits[commits_before:]
    assert [(type(o).__name__, o.id, o.version) for o in commit] == [
        ("GateDecision", "D-PLN", 1),
        ("Procedure", "PLN-01", 3),
        ("Procedure", "PLN-03", 2),
    ]
    [entry] = decision_trail(s)[trail_before:]
    assert (entry.procedure_id, entry.idempotency_key, entry.actor) == ("PLN-01", "D-PLN", "U-1")
    await assert_pack_synced(values["procedure_pack"], s.output_store)


async def test_pln_approve_with_edit_versions_risk_and_objective() -> None:
    """验收 2: 现金存在性风险 high → significant: 风险项 v2 (等级, 理由, 确认人), 目标 v2 risk_level significant;
    其余风险项与目标仍 v1; 新版本与决定同一次提交."""
    s = await at_pln_gate()
    commits_before = len(s.output_store.commits)
    raise_cash = GateEdit(
        target_id="RISK-CASH-EXI",
        target_version=1,
        field="level",
        new_value=RiskLevel.significant,
        reason="期末大额在途存款",
    )
    stop_after_submit(s)

    await s.submit.submit(EID, pln_decision(GateVerdict.approve, edits=[raise_cash]))

    values = (await s.runner.app.aget_state(GraphRunner.config(EID))).values
    risks, objectives = by_id(values["risk_register"]), by_id(values["audit_objectives"])
    cash = risks["RISK-CASH-EXI"]
    assert (cash.version, cash.level, cash.revision_reason, cash.approved_by) == (
        2,
        RiskLevel.significant,
        "期末大额在途存款",
        "U-1",
    )
    assert (objectives["OBJ-CASH-EXI"].version, objectives["OBJ-CASH-EXI"].risk_level) == (2, RiskLevel.significant)
    assert {r.id: r.version for r in values["risk_register"]} == {
        "RISK-REV-OCC": 1,
        "RISK-REV-CUT": 1,
        "RISK-CASH-EXI": 2,
    }
    assert {o.id: o.version for o in values["audit_objectives"]} == {
        "OBJ-REV-OCC": 1,
        "OBJ-REV-CUT": 1,
        "OBJ-CASH-EXI": 2,
    }
    [commit] = s.output_store.commits[commits_before:]
    assert {(type(o).__name__, o.id, o.version) for o in commit} == {
        ("GateDecision", "D-PLN", 1),
        ("Procedure", "PLN-01", 3),
        ("Procedure", "PLN-03", 2),
        ("RiskItem", "RISK-CASH-EXI", 2),
        ("AuditObjective", "OBJ-CASH-EXI", 2),
    }
    assert await s.output_store.get(EID, RiskItem, "RISK-CASH-EXI") == cash


def test_pln_effects_lowering_risk_recomputes_objective_from_its_risks() -> None:
    """验收 3, 6 (纯函数): 收入截止风险 significant → medium, 目标下只有这一条风险, 目标降为 medium;
    改了等级但目标最高等级不变时目标不出新版本."""
    pack = [make_procedure("PLN-01", version=2), make_procedure("PLN-03")]
    candidates = MaterialityCandidates(id="MATERIALITY", candidates=NUMBAT_CANDIDATES)
    lower = GateEdit(target_id="RISK-REV-CUT", target_version=1, field="level", new_value=RiskLevel.medium, reason="x")
    extra = NUMBAT_RISKS[0].model_copy(update={"id": "RISK-REV-OCC-2", "level": RiskLevel.low})
    same = GateEdit(target_id="RISK-REV-OCC-2", target_version=1, field="level", new_value=RiskLevel.medium, reason="y")
    decision = pln_decision(GateVerdict.approve, edits=[lower, same])

    effects = pln_effects(pack, [*NUMBAT_RISKS, extra], NUMBAT_OBJECTIVES, candidates, decision)

    objectives = by_id(effects["audit_objectives"])
    assert (objectives["OBJ-REV-CUT"].version, objectives["OBJ-REV-CUT"].risk_level) == (2, RiskLevel.medium)
    assert objectives["OBJ-REV-OCC"] is NUMBAT_OBJECTIVES[0]  # 其下仍有 significant 的风险, 目标不变
    assert by_id(effects["risk_register"])["RISK-REV-OCC-2"].level is RiskLevel.medium
    assert [p.status for p in effects["procedure_pack"]] == [ProcedureStatus.completed, ProcedureStatus.completed]
    assert pack[0].status is ProcedureStatus.planned  # 输入不变


@pytest.mark.parametrize("verdict", [GateVerdict.reject, GateVerdict.insufficient_info])
def test_pln_effects_non_approve_only_records_decision(verdict: GateVerdict) -> None:
    decision = pln_decision(verdict)
    candidates = MaterialityCandidates(id="MATERIALITY", candidates=NUMBAT_CANDIDATES)

    assert pln_effects([], NUMBAT_RISKS, NUMBAT_OBJECTIVES, candidates, decision) == {"gate_decisions": [decision]}


async def test_pln_reject_reruns_analytics_and_old_binding_goes_stale() -> None:
    """验收 4: 驳回只写决定与留痕; 回 p1_analytics 重跑出 RUN-PLN-07-2, RUN-PLN-01-2, ANALYTICS v2, MATERIALITY v2,
    再停在闸门; 之后绑定 MATERIALITY v1 的决定报 VERSION_MISMATCH."""
    s = await at_pln_gate()
    commits_before = len(s.output_store.commits)

    await s.submit.submit(EID, pln_decision(GateVerdict.reject))
    await wait_idle(s.runner, EID)

    decision_commit = s.output_store.commits[commits_before]
    assert [(type(o).__name__, o.id) for o in decision_commit] == [("GateDecision", "D-PLN")]
    assert decision_trail(s)[-1].idempotency_key == "D-PLN"
    snapshot = await s.runner.app.aget_state(GraphRunner.config(EID))
    assert snapshot.next == ("gate_p1_risk",)
    values = snapshot.values
    assert {"RUN-PLN-07-2", "RUN-PLN-01-2"} <= {r.id for r in values["procedure_runs"]}
    assert (values["analytics"].version, values["materiality_candidates"].version) == (2, 2)
    assert values["materiality_cents"] is None
    await assert_rejected(s, pln_decision(GateVerdict.approve, id="D-PLN-2"), "VERSION_MISMATCH")
    await s.submit.submit(EID, pln_decision(GateVerdict.insufficient_info, id="D-PLN-3", target_version=2))
    await wait_idle(s.runner, EID)
    assert (await s.runner.app.aget_state(GraphRunner.config(EID))).next == ("gate_p1_risk",)


async def test_pln_insufficient_info_stays_at_gate() -> None:
    """验收 5: 信息不足只写决定与留痕, 仍停在闸门, 程序与金额都不动."""
    s = await at_pln_gate()
    commits_before = len(s.output_store.commits)

    await s.submit.submit(EID, pln_decision(GateVerdict.insufficient_info))
    await wait_idle(s.runner, EID)

    assert [[(type(o).__name__, o.id) for o in c] for c in s.output_store.commits[commits_before:]] == [
        [("GateDecision", "D-PLN")]
    ]
    snapshot = await s.runner.app.aget_state(GraphRunner.config(EID))
    assert snapshot.next == ("gate_p1_risk",)
    assert snapshot.values["materiality_cents"] is None
    assert by_id(snapshot.values["procedure_pack"])["PLN-01"].status is ProcedureStatus.planned
    assert decision_trail(s)[-1].summary == "PLN-01/03 insufficient_info"


# ── R60 审计目标覆盖缺口: 有缺口时不能通过, 只能驳回或改风险等级 ──


def inventory(level: RiskLevel = RiskLevel.high) -> Agents:
    """PLN-01 在 Numbat 的 3 条目标外另带存货存在性目标 OBJ-INV-EXI 与其风险项 RISK-INV-EXI (等级 level).
    knowledge 中没有存货的程序, 任何等级都没有覆盖."""
    objective = AuditObjective(
        id="OBJ-INV-EXI", account="Inventory", assertion="Existence", risk_level=level, is_material=True
    )
    risk = RiskItem(
        id="RISK-INV-EXI",
        objective_id="OBJ-INV-EXI",
        assertion="Existence",
        level=level,
        fraud_related=False,
        description="Risk that inventory recorded at year end does not exist.",
    )
    return numbat_pln01_with(objective, risk)


def significant_only_library(tmp_path: Path) -> ProcedureLibrary:
    """knowledge 目录的副本, CSH-05 另加一条只在 significant 时匹配的存货存在性 trigger:
    存货目标只因风险等级没被覆盖."""
    root = tmp_path / "knowledge"
    shutil.copytree(get_settings().KNOWLEDGE_ROOT, root)
    path = root / "procedures" / "CSH-05.yaml"
    line = "  - {account: Cash, assertions: [Existence], level: [low, medium, high, significant]}\n"
    text = path.read_text(encoding="utf-8")
    assert line in text
    extra = "  - {account: Inventory, assertions: [Existence], level: [significant]}\n"
    path.write_text(text.replace(line, line + extra), encoding="utf-8")
    return ProcedureLibrary(root)


def inventory_edit(new_value: RiskLevel) -> GateEdit:
    return GateEdit(
        target_id="RISK-INV-EXI", target_version=1, field="level", new_value=new_value, reason="存货盘点差异"
    )


async def test_pln_approve_rejected_when_objective_uncovered() -> None:
    """验收 2: 存货目标没有程序覆盖, approve 报 INVALID_VALUE (消息含其 ID), 不写 Store, 图不动."""
    s = await at_pln_gate(inventory())
    await assert_rejected(s, pln_decision(GateVerdict.approve), "INVALID_VALUE", "OBJ-INV-EXI")


async def test_pln_reject_allowed_when_objective_uncovered() -> None:
    """验收 2: 有缺口时驳回照常写入, 回 p1_analytics 重新分析后再停在闸门."""
    s = await at_pln_gate(inventory())
    commits_before = len(s.output_store.commits)

    await s.submit.submit(EID, pln_decision(GateVerdict.reject))
    await wait_idle(s.runner, EID)

    assert [(type(o).__name__, o.id) for o in s.output_store.commits[commits_before]] == [("GateDecision", "D-PLN")]
    snapshot = await s.runner.app.aget_state(GraphRunner.config(EID))
    assert snapshot.next == ("gate_p1_risk",)
    assert snapshot.values["materiality_candidates"].version == 2


async def test_pln_edit_raising_risk_covers_objective(tmp_path: Path) -> None:
    """验收 3: 存货目标在 high 时没有程序覆盖, approve 被拒; 同时把其风险改到 significant 则按改后的等级算覆盖, 通过:
    风险项与目标出 v2, 越过闸门进装配."""
    s = await at_pln_gate(inventory(), significant_only_library(tmp_path))
    await assert_rejected(s, pln_decision(GateVerdict.approve), "INVALID_VALUE", "OBJ-INV-EXI")
    stop_after_submit(s)

    await s.submit.submit(EID, pln_decision(GateVerdict.approve, edits=[inventory_edit(RiskLevel.significant)]))

    snapshot = await s.runner.app.aget_state(GraphRunner.config(EID))
    assert snapshot.next == ("assemble_pack",)
    objectives = by_id(snapshot.values["audit_objectives"])
    assert (objectives["OBJ-INV-EXI"].version, objectives["OBJ-INV-EXI"].risk_level) == (2, RiskLevel.significant)


async def test_pln_edit_lowering_risk_uncovers_objective(tmp_path: Path) -> None:
    """存货目标在 significant 时有程序覆盖, 不改等级可以通过; 把其风险降到 high 后没有覆盖, 报 INVALID_VALUE."""
    s = await at_pln_gate(inventory(RiskLevel.significant), significant_only_library(tmp_path))
    lowered = pln_decision(GateVerdict.approve, edits=[inventory_edit(RiskLevel.high)])
    await assert_rejected(s, lowered, "INVALID_VALUE", "OBJ-INV-EXI")
    stop_after_submit(s)

    await s.submit.submit(EID, pln_decision(GateVerdict.approve))

    assert await s.next_nodes() == ("assemble_pack",)


async def test_no_edits_checks_objective_level_as_written(tmp_path: Path) -> None:
    """(Codex R60-01) 没有 edits 时覆盖检查不按风险重算目标等级: Store 中另有一条 significant 的同目标风险,
    目标仍为 high (效果不重算, 装配用的也是 high), 照样报缺口."""
    s = await at_pln_gate(inventory(), significant_only_library(tmp_path))
    risk = await s.output_store.get(EID, RiskItem, "RISK-INV-EXI")
    assert risk is not None
    await s.output_store.put(
        EID, [risk.model_copy(update={"id": "RISK-INV-EXI-2", "level": RiskLevel.significant})], actor="U-1"
    )
    await assert_rejected(s, pln_decision(GateVerdict.approve), "INVALID_VALUE", "OBJ-INV-EXI")


async def test_approve_syncs_store_objectives_so_assembly_matches_check(tmp_path: Path) -> None:
    """(Codex R60-02) Store 中存货风险与目标已是 significant v2 (检查点仍为 high v1): 覆盖检查按 Store 通过,
    效果把 Store 的完整目标写入状态, 装配按 significant 进行, 不再卡住."""
    s = await at_pln_gate(inventory(), significant_only_library(tmp_path))
    risk = await s.output_store.get(EID, RiskItem, "RISK-INV-EXI")
    objective = await s.output_store.get(EID, AuditObjective, "OBJ-INV-EXI")
    assert risk is not None and objective is not None
    raised = [
        risk.model_copy(update={"version": 2, "level": RiskLevel.significant}),
        objective.model_copy(update={"version": 2, "risk_level": RiskLevel.significant}),
    ]
    await s.output_store.put(EID, raised, actor="U-1")

    await s.submit.submit(EID, pln_decision(GateVerdict.approve))
    await wait_idle(s.runner, EID)

    assert s.runner.run_error(EID) is None
    values = (await s.runner.app.aget_state(GraphRunner.config(EID))).values
    assert by_id(values["audit_objectives"])["OBJ-INV-EXI"] == raised[1]
    assert values["phase"] is Phase.P2_Executing


def test_pln_effects_without_edits_carry_current_lists_unchanged() -> None:
    """没有 edits 时效果仍带完整风险清单与审计目标 (原对象, 不重算), 用来把 Store 的版本同步进状态."""
    candidates = MaterialityCandidates(id="MATERIALITY", candidates=NUMBAT_CANDIDATES)
    inconsistent = [NUMBAT_OBJECTIVES[0].model_copy(update={"risk_level": RiskLevel.low}), *NUMBAT_OBJECTIVES[1:]]
    effects = pln_effects([], NUMBAT_RISKS, inconsistent, candidates, pln_decision(GateVerdict.approve))
    assert effects["risk_register"] == NUMBAT_RISKS and effects["audit_objectives"] == inconsistent


async def test_pln_coverage_checked_after_other_invalid_values() -> None:
    """覆盖检查在其余取值校验之后: 重要性比例越界与缺口同时存在时先报比例."""
    s = await at_pln_gate(inventory())
    bad = pln_decision(GateVerdict.approve, materiality=CHOICE.model_copy(update={"pct": 0.5}))
    await assert_rejected(s, bad, "INVALID_VALUE", "materiality.pct")


# ── Codex R10-01, R10-02: Store 与检查点不一致时, 效果按 Store 算, 材料同步进状态, 不顺带写无关版本 ──


async def test_edit_applied_on_store_version_when_checkpoint_behind() -> None:
    """Store 中现金风险已是 v2 (检查点 v1), 决定绑定 v2: 出 v3 并真正应用修改, 状态同步为 Store 的版本."""
    s = await at_pln_gate()
    current = await s.output_store.get(EID, RiskItem, "RISK-CASH-EXI")
    assert current is not None
    await s.output_store.put(EID, [current.model_copy(update={"version": 2})], actor="U-1")
    stop_after_submit(s)

    await s.submit.submit(EID, pln_decision(GateVerdict.approve, edits=[edit(target_version=2)]))

    cash = await s.output_store.get(EID, RiskItem, "RISK-CASH-EXI")
    assert cash is not None
    assert (cash.version, cash.level, cash.revision_reason, cash.approved_by) == (3, RiskLevel.significant, "x", "U-1")
    objective = await s.output_store.get(EID, AuditObjective, "OBJ-CASH-EXI")
    assert objective is not None and objective.risk_level is RiskLevel.significant
    snapshot = await s.runner.app.aget_state(GraphRunner.config(EID))
    assert by_id(snapshot.values["risk_register"])["RISK-CASH-EXI"] == cash
    assert snapshot.next == ("assemble_pack",)


async def test_objective_recomputed_with_store_only_peer_risk() -> None:
    """目标下另有一条只在 Store 中的 significant 风险: 把原风险降为 medium 后目标仍为 significant."""
    s = await at_pln_gate()
    current = await s.output_store.get(EID, RiskItem, "RISK-CASH-EXI")
    assert current is not None
    peer = current.model_copy(update={"id": "RISK-CASH-EXI-2", "level": RiskLevel.significant})
    await s.output_store.put(EID, [peer], actor="U-1")
    lower = GateEdit(target_id=current.id, target_version=1, field="level", new_value=RiskLevel.medium, reason="y")
    stop_after_submit(s)

    await s.submit.submit(EID, pln_decision(GateVerdict.approve, edits=[lower]))

    objective = await s.output_store.get(EID, AuditObjective, "OBJ-CASH-EXI")
    assert objective is not None and objective.risk_level is RiskLevel.significant
    values = (await s.runner.app.aget_state(GraphRunner.config(EID))).values
    assert peer.id in by_id(values["risk_register"])


async def test_approve_on_store_candidates_reaches_assembly() -> None:
    """Store 中候选已是 v2 (金额翻倍), 检查点 v1: 绑定 v2 的通过按 v2 算金额, 状态同步 v2, 路由不判过期."""
    s = await at_pln_gate()
    current = await s.output_store.get(EID, MaterialityCandidates, "MATERIALITY")
    assert current is not None
    doubled = [c.model_copy(update={"base_amount_cents": c.base_amount_cents * 2}) for c in current.candidates]
    await s.output_store.put(EID, [current.model_copy(update={"version": 2, "candidates": doubled})], actor="U-1")
    stop_after_submit(s)

    await s.submit.submit(EID, pln_decision(GateVerdict.approve, target_version=2))

    snapshot = await s.runner.app.aget_state(GraphRunner.config(EID))
    assert snapshot.next == ("assemble_pack",)
    assert snapshot.values["materiality_candidates"].version == 2
    assert snapshot.values["materiality_cents"] == 24000000


async def test_checkpoint_ahead_versions_not_written_along() -> None:
    """检查点里 PLN-07 比 Store 新 (人为制造): 决定只写自己新出的版本, 不顺带写 PLN-07."""
    s = await at_pln_gate()
    config = GraphRunner.config(EID)
    pack = (await s.runner.app.aget_state(config)).values["procedure_pack"]
    ahead = [p.model_copy(update={"version": p.version + 1}) if p.id == "PLN-07" else p for p in pack]
    await s.runner.app.aupdate_state(config, {"procedure_pack": ahead}, as_node="p1_analytics")
    commits_before = len(s.output_store.commits)
    stop_after_submit(s)

    await s.submit.submit(EID, pln_decision(GateVerdict.approve))

    [commit] = s.output_store.commits[commits_before:]
    assert [(type(o).__name__, o.id) for o in commit] == [
        ("GateDecision", "D-PLN"),
        ("Procedure", "PLN-01"),
        ("Procedure", "PLN-03"),
    ]


async def test_same_version_with_different_content_in_store_rejected() -> None:
    """Store 已有 PLN-01 的下一版 (内容不同), 效果算出的同号版本会被 put 当作已存在而丢掉: 整体回滚,
    VERSION_MISMATCH, 不写决定与留痕."""
    s = await at_pln_gate()
    stored = await s.output_store.get(EID, Procedure, "PLN-01")
    assert stored is not None
    await s.output_store.put(EID, [stored.model_copy(update={"version": stored.version + 1})], actor="U-1")
    trail_before = len(decision_trail(s))

    await assert_rejected(s, pln_decision(GateVerdict.approve), "VERSION_MISMATCH", "PLN-01")

    assert await s.output_store.get(EID, GateDecision, "D-PLN") is None
    assert len(decision_trail(s)) == trail_before


# ── R61 基准金额不为正的重要性候选: 照常列出, 不能以它通过 (Q18) ──


def loss_making(base_amount_cents: int) -> Agents:
    """PLN-01 同 Numbat, 但税前利润基准金额改为 base_amount_cents (亏损为负), 候选区间随之."""

    def agents(deps: AgentDeps) -> dict[str, ProcedureAgent[Any]]:
        numbat = cast(ScriptAgent, numbat_p1_agents(deps)["PLN-01"]).script

        def pln01(ctx: RunContext) -> StatePatch:
            patch = numbat(ctx)
            candidates = patch["materiality_candidates"]
            assert candidates is not None
            loss = [
                c.model_copy(
                    update={
                        "base_amount_cents": base_amount_cents,
                        "low_cents": round(base_amount_cents * c.pct_low),
                        "high_cents": round(base_amount_cents * c.pct_high),
                    }
                )
                if c.basis == "profit_before_tax"
                else c
                for c in candidates.candidates
            ]
            return {**patch, "materiality_candidates": candidates.model_copy(update={"candidates": loss})}

        return {"PLN-01": ScriptA4(deps, pln01)}

    return agents


PBT = MaterialityChoice(basis="profit_before_tax", pct=0.05, performance_pct=0.75, clearly_trivial_pct=0.05)


@pytest.mark.parametrize("base_amount_cents", [-150000000, 0])
async def test_pln_approve_rejected_for_non_positive_basis(base_amount_cents: int) -> None:
    """验收 1, 2: 税前亏损 (或为零) 时候选照常列出; 以税前利润为基准通过报 INVALID_VALUE (消息含基准名), 不写 Store."""
    s = await at_pln_gate(loss_making(base_amount_cents))
    candidates = (await s.runner.app.aget_state(GraphRunner.config(EID))).values["materiality_candidates"]
    pbt = next(c for c in candidates.candidates if c.basis == "profit_before_tax")
    assert pbt.base_amount_cents == base_amount_cents

    await assert_rejected(s, pln_decision(GateVerdict.approve, materiality=PBT), "INVALID_VALUE", "profit_before_tax")


async def test_pln_other_basis_approves_when_loss_making() -> None:
    """验收 1: 税前亏损时选收入基准照常通过, 重要性按收入算."""
    s = await at_pln_gate(loss_making(-150000000))
    stop_after_submit(s)

    await s.submit.submit(EID, pln_decision(GateVerdict.approve))

    snapshot = await s.runner.app.aget_state(GraphRunner.config(EID))
    assert snapshot.next == ("assemble_pack",)
    assert snapshot.values["materiality_cents"] == 12000000
