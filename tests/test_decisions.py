"""审计师决定: 效果纯函数直接调用; submit 经图与假依赖, 验证校验, 写入与越过闸门."""

from datetime import UTC, datetime
from typing import Any

import pytest

from backend.contracts.errors import DecisionRejected
from backend.contracts.models import (
    AcceptanceScorecard,
    ClientProfile,
    GateDecision,
    GateId,
    GateVerdict,
    OpinionType,
    Phase,
    Procedure,
    ProcedureStatus,
    TrailKind,
)
from backend.orchestration.decisions import GraphDecisionSubmit, acc01_effects
from backend.orchestration.runner import GraphRunner
from tests.fakes import (
    ENGAGEMENT,
    NUMBAT_PROFILE,
    TYPO_PROFILE,
    FakeEngagementStore,
    FakeOutputStore,
    FakeTrail,
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

    with pytest.raises(DecisionRejected) as e:
        await submit.submit(EID, acc01_decision(GateVerdict.approve))

    assert e.value.code == "ENGAGEMENT_BUSY"
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
    """画像新版本已保存, 检查点的 client_profile_version 还是旧的: 须先 resume."""
    s = await started(TYPO_PROFILE)
    await s.engagement_store.save_profile(FIXED_PROFILE)

    with pytest.raises(DecisionRejected) as e:
        await s.submit.submit(EID, acc01_decision(GateVerdict.approve))

    assert e.value.code == "RESUME_REQUIRED"
