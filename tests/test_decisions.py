"""审计师决定: 效果纯函数直接调用; submit 经图与假依赖, 验证校验, 写入与越过闸门."""

import inspect
import shutil
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_asyncio
from langgraph.checkpoint.memory import InMemorySaver

from backend.adapters.llm import MockLLMClient
from backend.agents.base import AgentDeps, ProcedureAgent, RunContext
from backend.contracts.errors import DataRejected, DecisionRejected, EngagementLocked, VersionConflict
from backend.contracts.models import (
    AcceptanceScorecard,
    Annotation,
    AuditObjective,
    AuditPlan,
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
    PlanConfirmation,
    PlanStatus,
    Procedure,
    ProcedureRun,
    ProcedureStatus,
    ProfileSource,
    ReportSection,
    ReviewRecord,
    RiskItem,
    RiskLevel,
    RunStatus,
    RunTrigger,
    TrailEntry,
    TrailKind,
)
from backend.contracts.ports import LLMClient
from backend.contracts.state import EngagementState, StatePatch, fill_defaults
from backend.contracts.views import PlanMaterial, PlanView
from backend.core.config import get_settings
from backend.orchestration import runner as runner_module
from backend.orchestration.checkpoint import checkpoint_serde
from backend.orchestration.decisions import GraphDecisionSubmit
from backend.orchestration.gates import acc01_effects, gate_nodes, materiality_amounts, pln_effects
from backend.orchestration.graph import build_graph, gate_route, route_schedule
from backend.orchestration.nodes import GraphNodes
from backend.orchestration.query import GraphStateQuery
from backend.orchestration.runner import GraphRunner
from backend.services.procedure_library import ProcedureLibrary
from tests.external import needs
from tests.fakes import (
    ENGAGEMENT,
    NUMBAT_CANDIDATES,
    NUMBAT_OBJECTIVES,
    NUMBAT_PROFILE,
    NUMBAT_RISKS,
    PLAN_CONFIRMATION,
    PLAN_REJECT_ID,
    PLAN_REJECT_REASON,
    PLANNING_FEATURES,
    TYPO_PROFILE,
    EngagementWrite,
    FakeEngagementStore,
    FakeOutputStore,
    FakeRuleEngine,
    FakeTrail,
    NoTaskAsyncio,
    ScriptA4,
    ScriptAgent,
    acc01_decision,
    api_client,
    assert_pack_synced,
    make_procedure,
    make_runner,
    numbat_p1_agents,
    numbat_pln01_with,
    profile_update,
    wait_idle,
)
from tests.fixture_sets import XERO
from tests.xero_fakes import XERO17_MATERIALITY, XERO_PLAN_ROWS, XERO_RULES, make_xero_deps, xero_financial_store
from tests.xero_flow import approve_acceptance, entered_p2_by, to_acceptance_gate

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
        (acc01_decision(GateVerdict.approve, disposition=OpinionType.unqualified), "FIELD_NOT_ALLOWED"),
        # 需求 R203: PLN-15 的确认只由 PLN-15 的登记行 (R205) 允许, 其他闸门带它被拒
        (acc01_decision(GateVerdict.approve, plan_confirmation=PLAN_CONFIRMATION), "FIELD_NOT_ALLOWED"),
        (acc01_decision(GateVerdict.approve, gate=GateId.PLN_15), "NOT_AT_GATE"),  # 不在这道闸门 (planning 也关着)
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
        (
            pln_decision(
                GateVerdict.approve, materiality=None, disposition=OpinionType.qualified, edits=[edit("RISK-XXX", 9)]
            ),
            "MISSING_FIELD",
            "materiality",
        ),
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


# 需求 R358: 理由选填. 原须填理由的闸门决定 (通过以外; FIN-01 一律), 理由缺省或为空串都能提交
NO_REASON = pytest.mark.parametrize("reason", [None, ""], ids=["omitted", "empty"])


async def assert_accepted_without_reason(s: Started, decision: GateDecision) -> None:
    """提交成功: 决定照原样存 (理由为空), decision 留痕照记空理由. 调用方先 stop_after_submit, 只看这一步."""
    await s.submit.submit(EID, decision)
    assert await s.output_store.get(EID, GateDecision, decision.id) == decision
    [entry] = [e for e in decision_trail(s) if e.idempotency_key == decision.id]
    assert entry.payload["reason"] == decision.reason


@NO_REASON
@pytest.mark.parametrize("verdict", [GateVerdict.reject, GateVerdict.insufficient_info])
@pytest.mark.parametrize("gate", [GateId.ACC_01, GateId.PLN_01_03])
async def test_non_approve_without_reason_accepted(gate: GateId, verdict: GateVerdict, reason: str | None) -> None:
    """需求 R358 验收 1: ACC-01 与 PLN-01/03 的驳回与信息不足不填理由也能提交, 不报 MISSING_FIELD."""
    s = await (started() if gate is GateId.ACC_01 else at_pln_gate())
    stop_after_submit(s)
    decide = acc01_decision if gate is GateId.ACC_01 else pln_decision
    await assert_accepted_without_reason(s, decide(verdict, reason=reason))


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
    """PLN-01 同 Numbat, 但税前利润基准金额改为 base_amount_cents (亏损为负), 候选区间随之; 不标本期不适用
    (not_applicable, 需求 R351 起由 A4 标), 即旧材料的样子: 闸门按金额拒绝, 不看标记."""

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


# ── R205, R206: PLN-15 闸门, 决定校验与驳回修订 (功能 planning, Xero 演示) ──

# acceptance 不开: 没有 TERMS (ACC-08 的执行由 R200 做), 材料的 terms 为空; 带 TERMS 的材料见 test_query
XERO_P2 = ["CSH-02", "CSH-04", "CSH-05", "DOC-06", "REV-02", "REV-03", "REV-08"]
PLAN_APPROVE = GateDecision(
    id="D-PLAN",
    gate=GateId.PLN_15,
    target_id="PLAN-1",
    target_version=1,
    decision=GateVerdict.approve,
    plan_confirmation=PLAN_CONFIRMATION,
    decided_by="U-1",
    decided_at=NOW,
)
# 需要项目质量复核, 也不确认不需要审计师的专家 (需求 R206 验收 2): 只作记录, 不拦截
FIRST_YEAR_EQR = PlanConfirmation(
    eqr_required=True, eqr_basis="Firm policy for first-year audits", auditor_expert_not_required=False
)


def plan_decision(verdict: GateVerdict, **changes: Any) -> GateDecision:
    """绑定 PLAN-1 v1 的 PLN-15 决定: approve 即 PLAN_APPROVE (带确认), 其余不带确认而带理由. changes 覆盖任意字段."""
    if verdict is not GateVerdict.approve:
        changes = {"decision": verdict, "plan_confirmation": None, "reason": "Need more", **changes}
    return PLAN_APPROVE.model_copy(update=changes)


async def at_plan_gate(llm: LLMClient | None = None) -> Started:
    """Xero 演示项目 (数据已导入, 功能 critic, planning) 跑到 PLN-15 闸门: ACC-01 approve → P1 (真实
    Agent; 规则引擎为 XERO_RULES, 大模型缺省读 llm_xero) → PLN-01/03 approve (收入 1%, 50%, 5%) → assemble_pack →
    plan_draft (A4 起草 PLAN-1) → 停在 gate_p1_plan. 闸门之外另停在 fieldwork 前, 看批准后派发什么 (越过闸门后的第一个
    节点不会再静态中断, 去哪看检查点历史)."""
    deps = make_xero_deps(
        financial_store=xero_financial_store(),
        rules=FakeRuleEngine(XERO_RULES),
        llm=llm or MockLLMClient(XERO.fixtures),
        features=PLANNING_FEATURES,
    )
    app = build_graph(GraphNodes(deps, 2), deps.features).compile(
        checkpointer=InMemorySaver(serde=checkpoint_serde()),
        interrupt_before=[*gate_nodes(deps.features), "fieldwork"],
    )
    runner = GraphRunner(app, deps)
    await to_acceptance_gate(runner, EID)
    await approve_acceptance(runner, EID, ENGAGEMENT.owner_id)
    assert isinstance(deps.output_store, FakeOutputStore) and isinstance(deps.engagement_store, FakeEngagementStore)
    s = Started(runner, deps.output_store, deps.engagement_store)
    await s.submit.submit(EID, pln_decision(GateVerdict.approve, materiality=XERO17_MATERIALITY))
    await wait_idle(runner, EID)
    assert await s.next_nodes() == ("gate_p1_plan",), runner.run_error(EID)
    return s


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def plan_gate() -> Started:
    """停在 PLN-15 闸门的一次运行, 由不写入的用例共用 (被拒的决定每次核对存储与图都没动)."""
    return await at_plan_gate()


async def checkpoint_values(s: Started) -> EngagementState:
    return fill_defaults((await s.runner.app.aget_state(GraphRunner.config(EID))).values)


def stored_plans(s: Started) -> list[AuditPlan]:
    """Store 中写出的每一版计划, 按写入顺序; 各版经 JSON 往返不变 (同 Postgres 的 payload 列)."""
    plans = [r for r in s.output_store.rows if isinstance(r, AuditPlan)]
    assert all(AuditPlan.model_validate_json(p.model_dump_json()) == p for p in plans)
    return plans


async def checkpoint_matches_store(s: Started) -> None:
    """检查点中的各计划 (每个 ID 一项) 与 Store 中该计划的最新版逐个相等: 写出的每一版在检查点与 Store 中相同, 含置
    superseded 的旧修订 (审核意见 R206-02, 即 R203 普审意见 03 的检查点一侧)."""
    latest = {p.id: p for p in stored_plans(s)}  # 按写入顺序, 后写的覆盖
    assert sorted((await checkpoint_values(s))["audit_plans"], key=lambda p: p.id) == [
        latest[k] for k in sorted(latest)
    ]


def trail_of(s: Started, kind: TrailKind) -> list[TrailEntry]:
    assert isinstance(s.runner.deps.trail, FakeTrail)
    return [e for e in s.runner.deps.trail.entries if e.kind is kind]


@shared_loop
@needs("q1_snapshot", "q1_supplements")
async def test_plan_gate_stops_before_p2(plan_gate: Started) -> None:
    """验收 2: assemble_pack 写入 7 条 P2 程序第 1 版, 阶段仍 P1_Planning (项目表与检查点), 没有阶段 transition;
    plan_draft 执行 PLN-15 (RUN-PLN-15-1 succeeded, PLN-15 仍 planned), PLAN-1 v1 draft 在 Store 与检查点中相同;
    停在 gate_p1_plan 时 schedule_table 为空, 没有任何 P2 执行记录."""
    s = plan_gate
    values = await checkpoint_values(s)
    p2 = [p for p in await s.output_store.latest(EID, Procedure) if p.phase is Phase.P2_Executing]
    assert sorted(p.id for p in p2) == XERO_P2
    assert {(p.version, p.status) for p in p2} == {(1, ProcedureStatus.planned)}
    engagement = await s.engagement_store.get(EID)
    assert engagement is not None and engagement.phase is Phase.P1_Planning and values["phase"] is Phase.P1_Planning
    assert entered_p2_by(trail_of(s, TrailKind.transition)) == []
    assert s.latest_procedure("PLN-15").status is ProcedureStatus.planned
    runs = {r.id: r for r in await s.output_store.latest(EID, ProcedureRun)}
    assert runs["RUN-PLN-15-1"].status is RunStatus.succeeded
    assert not [r for r in runs.values() if r.procedure_id in XERO_P2]
    assert values["schedule_table"] == []
    [plan] = stored_plans(s)
    assert (plan.id, plan.version, plan.status, plan.run_id) == ("PLAN-1", 1, PlanStatus.draft, "RUN-PLN-15-1")
    assert values["audit_plans"] == [plan]


@shared_loop
@needs("q1_snapshot", "q1_supplements")
async def test_plan_gate_view_and_overview(plan_gate: Started) -> None:
    """验收 3: gate() 为 GateView(PLN-15, PLAN-1, 1), 材料 kind PLN-15: plan 为 PLAN-1 v1, terms 为空 (acceptance 关),
    重要性三项 32431, 16216, 1622, 此前的驳回决定为空 (首次); 总览的 current_gate 为 PLN-15."""
    s = plan_gate
    query = GraphStateQuery(s.runner.app, s.runner.deps, s.runner)
    view = await query.gate(EID)
    assert view is not None and (view.gate, view.target_id, view.target_version) == (GateId.PLN_15, "PLAN-1", 1)
    material = view.material
    assert isinstance(material, PlanMaterial)
    assert material.plan == stored_plans(s)[0] and material.terms is None and material.previous_decisions == []
    assert (material.materiality_cents, material.performance_materiality_cents, material.clearly_trivial_cents) == (
        32431,
        16216,
        1622,
    )
    assert (await query.overview(EID)).current_gate is GateId.PLN_15


# 其他闸门的专属字段 (GATE_SPECIFIC 中除 plan_confirmation 外的), PLN-15 的任何决定都不能带
OTHER_GATE_FIELDS = {
    "materiality": CHOICE,
    "edits": [edit("RISK-OTHER")],  # 不存在的风险项: 版本校验跳过, 落到字段校验
    "disposition": OpinionType.qualified,
    "annotations": [ANNOTATION],
}


async def plan_page(s: Started) -> PlanView:
    """经 HTTP 取审计计划页 (GET /plan), 与 StateQuery.plan 的结果相同."""
    async with api_client(s.runner) as client:
        response = await client.get(f"/api/v1/engagements/{EID}/plan")
    assert response.status_code == 200, response.text
    view = PlanView.model_validate(response.json())
    assert view == await GraphStateQuery(s.runner.app, s.runner.deps, s.runner).plan(EID)
    return view


@shared_loop
@needs("q1_snapshot", "q1_supplements")
async def test_plan_page_at_gate(plan_gate: Started) -> None:
    """需求 R314 验收 2: 停在 gate_p1_plan 时 GET /plan: 当前计划 PLAN-1, 修订 [PLAN-1 v1 draft], 还没有决定, 特别风险
    [RISK-REV-OCC, RISK-REV-CUT], 计划涉及的三个审计目标, 程序表 9 行都是全量."""
    view = await plan_page(plan_gate)
    assert view.current_plan_id == "PLAN-1" and view.decisions == []
    assert [(p.id, p.version, p.status) for p in view.plans] == [("PLAN-1", 1, PlanStatus.draft)]
    assert [r.id for r in view.risks] == ["RISK-REV-OCC", "RISK-REV-CUT"]
    assert [o.id for o in view.objectives] == ["OBJ-REV-OCC", "OBJ-REV-CUT", "OBJ-CASH-EXI"]
    assert [(r.objective_id, r.procedure_id) for r in view.rows] == XERO_PLAN_ROWS
    assert {(r.extent, r.sample_size) for r in view.rows} == {("full", None)}


@pytest.mark.parametrize(
    ("decision", "code", "detail"),
    [
        # R205 验收 5: 效果要用确认, 属最小路径; 绑定的不是当前计划的当前版本
        (plan_decision(GateVerdict.approve, plan_confirmation=None), "MISSING_FIELD", "plan_confirmation"),
        (plan_decision(GateVerdict.approve, target_version=2), "VERSION_MISMATCH", None),
        (plan_decision(GateVerdict.approve, target_id="PLAN-2"), "VERSION_MISMATCH", None),
        # 审核意见 R206-01: 绑定的版本不对时驳回与信息不足同样被拒, 不只 approve
        (plan_decision(GateVerdict.reject, target_version=2), "VERSION_MISMATCH", None),
        (plan_decision(GateVerdict.insufficient_info, target_id="PLAN-2"), "VERSION_MISMATCH", None),
        # R206 验收 1
        *[
            (
                plan_decision(
                    GateVerdict.approve, plan_confirmation=PLAN_CONFIRMATION.model_copy(update={"eqr_basis": b})
                ),
                "INVALID_VALUE",
                "eqr_basis",
            )
            for b in ("", " \n ")
        ],
        *[
            (plan_decision(v, plan_confirmation=PLAN_CONFIRMATION), "FIELD_NOT_ALLOWED", "plan_confirmation")
            for v in (GateVerdict.reject, GateVerdict.insufficient_info)
        ],
        *[
            (plan_decision(v, **{name: value}), "FIELD_NOT_ALLOWED", name)
            for v in GateVerdict
            for name, value in OTHER_GATE_FIELDS.items()
        ],
    ],
)
@shared_loop
@needs("q1_snapshot", "q1_supplements")
async def test_plan_decision_rejected_without_writing(
    plan_gate: Started, decision: GateDecision, code: str, detail: str | None
) -> None:
    """R205 验收 5, R206 验收 1: 停在 gate_p1_plan (PLAN-1 v1) 时被拒的 PLN-15 决定, 错误码与消息所指的字段; 都什么都
    不写, 锁已释放. 其他闸门的决定带 plan_confirmation 见 test_submit_rejects_invalid_decision (ACC-01 approve)."""
    await assert_rejected(plan_gate, decision, code, detail)


@needs("q1_snapshot", "q1_supplements")
async def test_plan_approve_enters_p2_in_one_commit() -> None:
    """验收 4, 6: approve (D-PLAN, 不需要项目质量复核与审计师的专家): PLAN-1 v2 approved (批准人 U-1, 批准时间为决定
    时刻, 三项确认写入, 其余同 v1, run_id 沿用), PLN-15 completed 新版本, 阶段 P2_Executing, 与决定同一次提交; 1 行
    decision 留痕 (PLN-15); transition 留痕 PLN-15 planned → completed 与项目 P1_Planning → P2_Executing (PLN-15).
    决定绑定 PLAN-1 v1 而状态中已是 v2, 路由仍为 approve, 下一节点 schedule (检查点历史); 第 0 层派发 REV-08, REV-03,
    CSH-04, CSH-05. 写出的每一版计划在 Store 与检查点中相同."""
    s = await at_plan_gate()
    before = len(s.output_store.commits)

    await s.submit.submit(EID, PLAN_APPROVE)
    await wait_idle(s.runner, EID)

    config = GraphRunner.config(EID)
    assert s.runner.run_error(EID) is None and await s.next_nodes() == ("fieldwork", "fieldwork")
    history = [snapshot.next async for snapshot in s.runner.app.aget_state_history(config)]  # 新的在前
    assert history[:3] == [("fieldwork", "fieldwork"), ("schedule",), ("gate_p1_plan",)]
    draft, approved = stored_plans(s)
    confirmation = PLAN_CONFIRMATION.model_dump()
    assert (approved.id, approved.version, approved.status, approved.run_id) == (
        "PLAN-1",
        2,
        PlanStatus.approved,
        draft.run_id,
    )
    assert (approved.approved_by, approved.approved_at) == ("U-1", NOW)
    assert approved.model_dump(include=set(confirmation)) == confirmation
    changed = {"version", "status", "approved_by", "approved_at", *confirmation}
    assert approved.model_dump(exclude=changed) == draft.model_dump(exclude=changed)
    values = await checkpoint_values(s)
    assert values["audit_plans"] == [approved] and values["phase"] is Phase.P2_Executing
    assert gate_route(GateId.PLN_15)(values) == "approve"  # 验收 6: 决定绑定 v1, 状态中已是 v2
    [commit] = s.output_store.commits[before:]
    pln15 = s.latest_procedure("PLN-15")
    assert pln15.status is ProcedureStatus.completed
    assert sorted((type(o).__name__, o.id) for o in commit if not isinstance(o, EngagementWrite)) == [
        ("AuditPlan", "PLAN-1"),
        ("GateDecision", "D-PLAN"),
        ("Procedure", "PLN-15"),
    ]
    assert [w for w in commit if isinstance(w, EngagementWrite)] == [
        EngagementWrite("update", EID, {"phase": Phase.P2_Executing})
    ]
    engagement = await s.engagement_store.get(EID)
    assert engagement is not None and engagement.phase is Phase.P2_Executing
    [decided] = [e for e in trail_of(s, TrailKind.decision) if e.idempotency_key == "D-PLAN"]
    assert decided.procedure_id == "PLN-15"
    moves = [
        (e.procedure_id, e.payload["id"], e.payload["from"], e.payload["to"]) for e in trail_of(s, TrailKind.transition)
    ]
    assert ("PLN-15", "PLN-15", "planned", "completed") in moves
    assert ("PLN-15", EID, "P1_Planning", "P2_Executing") in moves
    sends = route_schedule(GraphNodes(s.runner.deps, 2))(values)  # schedule 之后的派发
    assert isinstance(sends, list)
    assert sorted(p for send in sends for p in send.arg["procedure_ids"]) == ["CSH-04", "CSH-05", "REV-03", "REV-08"]


def pln15_runs(s: Started) -> list[str]:
    return sorted(r.id for r in s.output_store.rows if isinstance(r, ProcedureRun) and r.procedure_id == "PLN-15")


def plan_versions(s: Started) -> list[tuple[str, int, PlanStatus]]:
    return [(p.id, p.version, p.status) for p in stored_plans(s)]


async def plan_gate_view(s: Started) -> tuple[str, int, list[str]]:
    """停在 PLN-15 闸门时呈报的 (计划, 版本, 此前的驳回决定)."""
    view = await GraphStateQuery(s.runner.app, s.runner.deps, s.runner).gate(EID)
    assert view is not None and isinstance(view.material, PlanMaterial)
    return view.target_id, view.target_version, [d.id for d in view.material.previous_decisions]


@needs("q1_snapshot", "q1_supplements")
async def test_plan_info_reject_revise_restart_and_approve() -> None:
    """R206 验收 2, 3, 4, 5, 6(a) (R205 审核意见 02 的探针在此补断言), 一次运行依次:
    信息不足只写决定与留痕, 仍停闸门, 呈报仍是 PLAN-1 v1, 计划, PLN-15 与阶段都不变;
    驳回 D-P15R: 决定与 decision 留痕 (PLN-15) 写入, plan_draft 以 rerun 执行 RUN-PLN-15-2, 真实 A4 出 PLAN-2 v1
    (第 2 修订, 回应 D-P15R, 修订理由为驳回理由) 并把 PLAN-1 置 superseded; 再停闸门呈报 PLAN-2 v1, 此前的驳回只有
    D-P15R (信息不足不列); 阶段仍 P1_Planning, PLN-15 仍 planned; 最后一条决定绑定 PLAN-1 v1 而当前为 PLAN-2 v1,
    路由为 represent;
    此时进程重启 (新 runner, 同一检查点): recover 不推进 (检查点不变), 不重跑 PLN-15; 绑 PLAN-1 v1 的决定报
    VERSION_MISMATCH;
    批准 PLAN-2 v1 (需要项目质量复核, 未确认不需要专家: 只作记录, 不拦截): PLAN-2 v2 approved, 三项照写入,
    PLN-15 completed, 阶段 P2_Executing, 越过闸门后下一节点为 schedule."""
    s = await at_plan_gate()
    await checkpoint_matches_store(s)

    async def decide(decision: GateDecision) -> EngagementState:
        await s.submit.submit(EID, decision)
        await wait_idle(s.runner, EID)
        assert s.runner.run_error(EID) is None, s.runner.run_error(EID)
        return await checkpoint_values(s)

    def decided(key: str) -> list[str | None]:
        return [e.procedure_id for e in trail_of(s, TrailKind.decision) if e.idempotency_key == key]

    asked = plan_decision(GateVerdict.insufficient_info, id="D-P15I", reason="Awaiting partner availability")
    commits = len(s.output_store.commits)
    values = await decide(asked)
    assert s.output_store.commits[commits:] == [[asked]] and decided("D-P15I") == ["PLN-15"]
    assert await s.next_nodes() == ("gate_p1_plan",) and await plan_gate_view(s) == ("PLAN-1", 1, [])
    assert plan_versions(s) == [("PLAN-1", 1, PlanStatus.draft)] and values["phase"] is Phase.P1_Planning
    assert s.latest_procedure("PLN-15").status is ProcedureStatus.planned
    await checkpoint_matches_store(s)

    reject = plan_decision(GateVerdict.reject, id=PLAN_REJECT_ID, reason=PLAN_REJECT_REASON)
    values = await decide(reject)
    assert await s.output_store.get(EID, GateDecision, PLAN_REJECT_ID) == reject and decided(PLAN_REJECT_ID) == [
        "PLN-15"
    ]
    assert await s.next_nodes() == ("gate_p1_plan",) and await plan_gate_view(s) == ("PLAN-2", 1, [PLAN_REJECT_ID])
    assert plan_versions(s) == [
        ("PLAN-1", 1, PlanStatus.draft),
        ("PLAN-2", 1, PlanStatus.draft),
        ("PLAN-1", 2, PlanStatus.superseded),
    ]
    runs = {r.id: r for r in await s.output_store.latest(EID, ProcedureRun) if r.procedure_id == "PLN-15"}
    assert sorted(runs) == ["RUN-PLN-15-1", "RUN-PLN-15-2"] and runs["RUN-PLN-15-2"].trigger is RunTrigger.rerun
    [plan2] = [p for p in values["audit_plans"] if p.id == "PLAN-2"]
    assert (plan2.revision, plan2.addresses_decision_id, plan2.revision_reason, plan2.run_id) == (
        2,
        PLAN_REJECT_ID,
        PLAN_REJECT_REASON,
        "RUN-PLN-15-2",
    )
    assert values["phase"] is Phase.P1_Planning and s.latest_procedure("PLN-15").status is ProcedureStatus.planned
    assert gate_route(GateId.PLN_15)(values) == "represent"  # 验收 5: 最后一条决定绑定的计划已被取代
    await checkpoint_matches_store(s)

    config = GraphRunner.config(EID)
    before = (await s.runner.app.aget_state(config)).config, len(s.output_store.rows)
    restarted = GraphRunner(s.runner.app, s.runner.deps)
    await restarted.recover()
    await wait_idle(restarted, EID)
    s = Started(restarted, s.output_store, s.engagement_store)
    assert restarted.run_error(EID) is None and await s.next_nodes() == ("gate_p1_plan",)
    assert ((await restarted.app.aget_state(config)).config, len(s.output_store.rows)) == before
    assert pln15_runs(s) == ["RUN-PLN-15-1", "RUN-PLN-15-2"]
    await checkpoint_matches_store(s)
    for verdict in GateVerdict:  # 绑定已被取代的 PLAN-1 v1: 哪种决定都被拒 (审核意见 R206-01)
        await assert_rejected(s, plan_decision(verdict, id=f"D-OLD-{verdict.value}"), "VERSION_MISMATCH")

    values = await decide(
        plan_decision(GateVerdict.approve, id="D-PLAN2", target_id="PLAN-2", plan_confirmation=FIRST_YEAR_EQR)
    )
    assert await s.next_nodes() == ("fieldwork", "fieldwork") and values["phase"] is Phase.P2_Executing
    history = [snapshot.next async for snapshot in restarted.app.aget_state_history(config)]  # 新的在前
    assert history[1:3] == [("schedule",), ("gate_p1_plan",)]
    approved = stored_plans(s)[-1]
    assert (approved.id, approved.version, approved.status) == ("PLAN-2", 2, PlanStatus.approved)
    assert approved.model_dump(include=set(FIRST_YEAR_EQR.model_dump())) == FIRST_YEAR_EQR.model_dump()
    assert approved in values["audit_plans"] and s.latest_procedure("PLN-15").status is ProcedureStatus.completed
    await checkpoint_matches_store(s)
    # R314 验收 3: 进入执行阶段后 GET /plan 照样返回, 当前计划为批准的 PLAN-2, 三项确认可读出
    page = await plan_page(s)
    assert page.current_plan_id == "PLAN-2" and page.plans[-1] == approved
    confirmed = page.plans[-1].model_dump(include={"eqr_required", "eqr_basis", "auditor_expert_not_required"})
    assert confirmed == FIRST_YEAR_EQR.model_dump()  # R206 验收 2: 三项确认经 GET /plan 读得出
    assert [(p.id, p.version, p.status) for p in page.plans] == [
        ("PLAN-1", 2, PlanStatus.superseded),
        ("PLAN-2", 2, PlanStatus.approved),
    ]
    assert [(d.id, d.decision, d.reason) for d in page.decisions] == [
        ("D-P15I", GateVerdict.insufficient_info, "Awaiting partner availability"),
        (PLAN_REJECT_ID, GateVerdict.reject, PLAN_REJECT_REASON),
        ("D-PLAN2", GateVerdict.approve, None),
    ]


class LostCheckpoint:
    """写完 Store 与留痕之后, 写检查点 (aupdate_state) 时进程中断."""

    def __init__(self, app: Any) -> None:
        self.app = app

    def __getattr__(self, name: str) -> Any:
        return getattr(self.app, name)

    async def aupdate_state(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("crash before checkpoint")


@NO_REASON
@needs("q1_snapshot", "q1_supplements")
async def test_plan_info_and_reject_without_reason(reason: str | None) -> None:
    """需求 R358: PLN-15 的信息不足与驳回不填理由都能提交, 决定与留痕照记空理由; 驳回后 A4 照样起草 PLAN-2
    (revision_reason 为空), 提示词有 "## Revision 2" 而不列审计师的理由 (发给大模型的输入里理由为空时略去)."""
    s = await at_plan_gate()
    asked = plan_decision(GateVerdict.insufficient_info, id="D-P15I", reason=reason)
    reject = plan_decision(GateVerdict.reject, id=PLAN_REJECT_ID, reason=reason)
    for decision in (asked, reject):
        await s.submit.submit(EID, decision)
        await wait_idle(s.runner, EID)
        assert s.runner.run_error(EID) is None, s.runner.run_error(EID)
        assert await s.output_store.get(EID, GateDecision, decision.id) == decision
        [entry] = [e for e in decision_trail(s) if e.idempotency_key == decision.id]
        assert entry.payload["reason"] == reason

    assert await plan_gate_view(s) == ("PLAN-2", 1, [PLAN_REJECT_ID])
    [plan2] = [p for p in stored_plans(s) if p.id == "PLAN-2"]
    assert (plan2.revision, plan2.addresses_decision_id, plan2.revision_reason) == (2, PLAN_REJECT_ID, reason)
    *_, prompt = (
        "\n".join(m["content"] for m in e.payload["messages"])
        for e in trail_of(s, TrailKind.llm_call)
        if e.procedure_id == "PLN-15"
    )
    assert "## Revision 2" in prompt and "Redraft it from the inputs above." in prompt
    assert "Reason given by the auditor" not in prompt


@needs("q1_snapshot", "q1_supplements")
async def test_plan_approve_caught_up_after_lost_checkpoint() -> None:
    """审核意见 R205-02 (原探针): 批准写完 Store 后检查点没跟上: 新决定报 RESUME_REQUIRED; resume 补做 (登记行的
    rebuild) 后检查点里是 Store 的批准版 PLAN-1 v2, 阶段 P2_Executing, PLN-15 completed, 越过闸门到 fieldwork 前;
    decision 留痕与 P1 → P2 的阶段迁移各一行, 不重复."""
    s = await at_plan_gate()
    broken = GraphDecisionSubmit(LostCheckpoint(s.runner.app), s.runner.deps, s.runner)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="crash before checkpoint"):
        await broken.submit(EID, PLAN_APPROVE)
    assert await s.next_nodes() == ("gate_p1_plan",)
    with pytest.raises(DecisionRejected) as e:
        await s.submit.submit(EID, PLAN_APPROVE.model_copy(update={"id": "D-PLAN-2"}))
    assert e.value.code == "RESUME_REQUIRED"

    await s.runner.resume(EID)
    await wait_idle(s.runner, EID)

    assert s.runner.run_error(EID) is None and await s.next_nodes() == ("fieldwork", "fieldwork")
    values = await checkpoint_values(s)
    approved = stored_plans(s)[-1]
    assert (approved.id, approved.version, approved.status) == ("PLAN-1", 2, PlanStatus.approved)
    assert values["audit_plans"] == [approved] and values["phase"] is Phase.P2_Executing
    assert next(p for p in values["procedure_pack"] if p.id == "PLN-15").status is ProcedureStatus.completed
    assert [e.idempotency_key for e in trail_of(s, TrailKind.decision)].count("D-PLAN") == 1
    moves = [
        (e.payload["from"], e.payload["to"])
        for e in trail_of(s, TrailKind.transition)
        if e.payload["object"] == "engagement"
    ]
    assert moves.count(("P1_Planning", "P2_Executing")) == 1
