"""R24 FIN-01 闸门: 呈报未更正错报汇总表与全部错报;
approve 写意见类型并进 P4, reject 回 completion 重做, 信息不足重呈."""

from typing import Any

import pytest
import pytest_asyncio

from backend.contracts.models import (
    Annotation,
    GateDecision,
    GateId,
    GateVerdict,
    OpinionType,
    Phase,
    Procedure,
    ProcedureStatus,
    ReportSection,
    TrailKind,
    UncorrectedSchedule,
)
from backend.contracts.views import CompletionMaterial
from backend.orchestration.gates import fin01_effects
from backend.orchestration.query import GraphStateQuery
from backend.orchestration.runner import GraphRunner
from tests.fakes import NOW, EngagementWrite, FakeTrail, make_procedure, wait_idle
from tests.test_decisions import CHOICE, Started, assert_rejected, at_pln_gate, edit, stop_after_submit
from tests.test_flow import EID, approve_materiality, review_all, start_numbat

CONFIG = GraphRunner.config(EID)
# 合成数据集设计第 7 节第 15 行的理由
REASON = (
    "Uncorrected cutoff misstatement of AUD 142,000 exceeds overall materiality of AUD 120,000; "
    "its effects are material but not pervasive."
)


async def at_fin_gate() -> Started:
    """Numbat 走完 P2 与全部复核, 完成阶段出 SCHEDULE v1 (合计 14200000, 候选保留意见), 停在 FIN-01 闸门前."""
    s = await start_numbat()
    await approve_materiality(s)
    await wait_idle(s.runner, EID)
    await review_all(s)
    assert await s.next_nodes() == ("gate_fin",)
    return s


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def fin_gate() -> Started:
    """停在 FIN-01 闸门前的一次运行, 由只提交被拒决定的用例共用: assert_rejected 逐次核对存储与图都没动."""
    return await at_fin_gate()


def fin_decision(verdict: GateVerdict, **changes: Any) -> GateDecision:
    """绑定 SCHEDULE 第 1 版的 FIN-01 决定, 一律带理由; approve 选保留意见. changes 覆盖任意字段."""
    decision = GateDecision(
        id="D-FIN",
        gate=GateId.FIN_01,
        target_id="SCHEDULE",
        target_version=1,
        decision=verdict,
        disposition=OpinionType.qualified if verdict is GateVerdict.approve else None,
        reason=REASON,
        decided_by="U-1",
        decided_at=NOW,
    )
    return decision.model_copy(update=changes)


def latest(s: Started, model: type[Any], oid: str) -> Any:
    return max((r for r in s.output_store.rows if isinstance(r, model) and r.id == oid), key=lambda r: r.version)


async def test_gate_view_presents_schedule_and_misstatements() -> None:
    """验收 1: 闸门视图为 FIN-01, 材料为 SCHEDULE v1 与状态中全部错报, 决定须绑定 SCHEDULE v1."""
    s = await at_fin_gate()

    view = await GraphStateQuery(s.runner.app, s.runner.deps, s.runner).gate(EID)

    assert view is not None
    assert (view.gate, view.target_id, view.target_version) == (GateId.FIN_01, "SCHEDULE", 1)
    assert isinstance(view.material, CompletionMaterial) and view.material.kind == "FIN-01"
    snapshot = await s.runner.app.aget_state(CONFIG)
    assert view.material.schedule == snapshot.values["uncorrected_schedule"]
    assert view.material.schedule.candidate_opinion is OpinionType.qualified
    assert view.material.misstatements == snapshot.values["misstatements"] and len(view.material.misstatements) == 1


async def test_approve_qualified_moves_to_reporting() -> None:
    """验收 2: approve 保留意见: 决定, FIN-01 completed 新版本与阶段 P4_Reporting 同一次提交; 1 行决定留痕 (FIN-01 下);
    检查点 opinion_route 为保留, 阶段 P4_Reporting, 下一步 report_draft."""
    s = await at_fin_gate()
    stop_after_submit(s)
    commits = len(s.output_store.commits)
    decision = fin_decision(GateVerdict.approve)

    await s.submit.submit(EID, decision)

    [commit] = s.output_store.commits[commits:]
    assert decision in commit
    [fin01] = [o for o in commit if isinstance(o, Procedure)]
    assert (fin01.id, fin01.status) == ("FIN-01", ProcedureStatus.completed)
    assert EngagementWrite("update", EID, {"phase": Phase.P4_Reporting}) in commit
    trail = s.runner.deps.trail
    assert isinstance(trail, FakeTrail)
    assert [(e.procedure_id, e.idempotency_key) for e in trail.entries if e.kind is TrailKind.decision][-1] == (
        "FIN-01",
        "D-FIN",
    )
    snapshot = await s.runner.app.aget_state(CONFIG)
    assert (snapshot.values["opinion_route"], snapshot.values["phase"]) == (OpinionType.qualified, Phase.P4_Reporting)
    assert snapshot.next == ("report_draft",)


async def test_approve_may_differ_from_candidate() -> None:
    """验收 3: 候选为保留意见时审计师仍可选无保留, opinion_route 取审计师选的."""
    s = await at_fin_gate()
    stop_after_submit(s)

    await s.submit.submit(EID, fin_decision(GateVerdict.approve, disposition=OpinionType.unqualified))

    assert (await s.runner.app.aget_state(CONFIG)).values["opinion_route"] is OpinionType.unqualified


ANNOTATION = Annotation(section=ReportSection.basis_for_opinion, comment="x")


@pytest.mark.parametrize(
    ("decision", "code", "detail"),
    [
        (fin_decision(GateVerdict.approve, disposition=None), "MISSING_FIELD", "disposition"),
        (fin_decision(GateVerdict.approve, reason=None), "MISSING_FIELD", "reason"),
        # 消息列出可选范围与理由 (services.opinion.allowed_opinions, 需求 R184): SCHEDULE v1 合计 14200000,
        # 重要性 12000000
        (
            fin_decision(GateVerdict.approve, disposition=OpinionType.adverse),
            "INVALID_VALUE",
            "只能选 unqualified, qualified, 不能选 adverse; 金额路径: 未更正错报合计 14200000 分不低于整体重要性 "
            "12000000 分, 金额路径候选 qualified (可由审计师推翻, ASA 450 para 11)",
        ),
        (
            fin_decision(GateVerdict.approve, disposition=OpinionType.disclaimer),
            "INVALID_VALUE",
            "只能选 unqualified, qualified, 不能选 disclaimer",
        ),
        (fin_decision(GateVerdict.reject, disposition=OpinionType.qualified), "FIELD_NOT_ALLOWED", "disposition"),
        (fin_decision(GateVerdict.approve, materiality=CHOICE), "FIELD_NOT_ALLOWED", "materiality"),
        (fin_decision(GateVerdict.reject, edits=[edit()]), "FIELD_NOT_ALLOWED", "edits"),
        (fin_decision(GateVerdict.insufficient_info, annotations=[ANNOTATION]), "FIELD_NOT_ALLOWED", "annotations"),
        (fin_decision(GateVerdict.approve, target_id="MATERIALITY"), "VERSION_MISMATCH", None),
        (fin_decision(GateVerdict.approve, target_version=2), "VERSION_MISMATCH", None),
    ],
)
@pytest.mark.asyncio(loop_scope="module")
async def test_invalid_fin01_decision_rejected(
    fin_gate: Started, decision: GateDecision, code: str, detail: str | None
) -> None:
    """验收 4: 不合规的 FIN-01 决定被拒, Store 与检查点不变, 锁释放."""
    await assert_rejected(fin_gate, decision, code, detail)


async def test_fin01_decision_not_at_gate_rejected() -> None:
    """验收 4: 不在 FIN-01 闸门 (停在 PLN-01/03) 时提交 FIN-01 决定: NOT_AT_GATE."""
    s = await at_pln_gate()

    await assert_rejected(s, fin_decision(GateVerdict.approve), "NOT_AT_GATE")


async def test_reject_redoes_schedule() -> None:
    """验收 5: reject 后下一步 completion; 继续执行后 SCHEDULE v2, 再停在 FIN-01 闸门, 决定须绑定 v2;
    FIN-01 仍 planned."""
    s = await at_fin_gate()
    launch = s.runner.launch
    stop_after_submit(s)

    await s.submit.submit(EID, fin_decision(GateVerdict.reject))

    assert await s.next_nodes() == ("completion",)
    s.runner.launch = launch  # type: ignore[method-assign]
    s.runner.acquire(EID)
    s.runner.launch(EID, None)
    await wait_idle(s.runner, EID)
    assert await s.next_nodes() == ("gate_fin",)
    view = await GraphStateQuery(s.runner.app, s.runner.deps, s.runner).gate(EID)
    assert view is not None and (view.target_id, view.target_version) == ("SCHEDULE", 2)
    assert latest(s, UncorrectedSchedule, "SCHEDULE").version == 2
    assert latest(s, Procedure, "FIN-01").status is ProcedureStatus.planned


async def test_insufficient_info_stays_at_gate() -> None:
    """验收 6: 信息不足只写决定, 仍停在 FIN-01 闸门."""
    s = await at_fin_gate()
    commits = len(s.output_store.commits)
    decision = fin_decision(GateVerdict.insufficient_info)

    await s.submit.submit(EID, decision)
    await wait_idle(s.runner, EID)

    assert s.output_store.commits[commits:] == [[decision]]
    assert await s.next_nodes() == ("gate_fin",)


def test_fin01_effects_is_pure() -> None:
    """验收 7: 效果是纯函数: approve 出 FIN-01 completed 新版本, 意见类型与阶段; 驳回与信息不足只有决定; 输入不变."""
    pack = [make_procedure("FIN-01", Phase.P3_Completion), make_procedure("FIN-04", Phase.P3_Completion)]
    approve = fin_decision(GateVerdict.approve, disposition=OpinionType.unqualified)

    effects = fin01_effects(pack, approve)

    assert effects["gate_decisions"] == [approve]
    assert [(p.id, p.version, p.status) for p in effects["procedure_pack"]] == [
        ("FIN-01", 2, ProcedureStatus.completed),
        ("FIN-04", 1, ProcedureStatus.planned),
    ]
    assert (effects["opinion_route"], effects["phase"]) == (OpinionType.unqualified, Phase.P4_Reporting)
    assert pack[0].version == 1
    for verdict in (GateVerdict.reject, GateVerdict.insufficient_info):
        decision = fin_decision(verdict)
        assert fin01_effects(pack, decision) == {"gate_decisions": [decision]}
