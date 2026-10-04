"""R25 RPT-01 闸门: report_draft 由 A10 出报告首稿; 闸门呈报当前报告与此前的退回决定; RPT-01 决定按必填表校验."""

from typing import Any

import pytest

from backend.contracts.models import (
    Annotation,
    AuditReportVersion,
    GateDecision,
    GateId,
    GateVerdict,
    JudgmentRef,
    OpinionType,
    ProcedureRun,
    ReportSection,
    RunStatus,
    RunTrigger,
)
from backend.contracts.views import ReportMaterial
from backend.orchestration.graph import GATE_NODES
from backend.orchestration.query import GraphStateQuery
from backend.orchestration.runner import GraphRunner
from tests.fakes import NOW, wait_idle
from tests.test_decisions import CHOICE, Started, assert_rejected, edit
from tests.test_fin_gate import at_fin_gate, fin_decision
from tests.test_flow import EID

CONFIG = GraphRunner.config(EID)
# 合成数据集设计第 7 节第 17 行的退回批注
ANNOTATION = Annotation(
    section=ReportSection.basis_for_opinion,
    comment="Cite the contract clause that sets the point at which control transfers.",
)


async def at_rpt_gate() -> Started:
    """Numbat 走到 FIN-01 闸门并以保留意见通过; report_draft 出 REPORT-1, 停在 RPT-01 闸门前."""
    s = await at_fin_gate()
    await s.submit.submit(EID, fin_decision(GateVerdict.approve))
    await wait_idle(s.runner, EID)
    assert await s.next_nodes() == ("gate_rpt01",)
    return s


def rpt_decision(verdict: GateVerdict, **changes: Any) -> GateDecision:
    """绑定 REPORT-1 第 1 版的 RPT-01 决定; 驳回带批注与理由, 信息不足带理由. changes 覆盖任意字段."""
    decision = GateDecision(
        id="D-RPT",
        gate=GateId.RPT_01,
        target_id="REPORT-1",
        target_version=1,
        decision=verdict,
        annotations=[ANNOTATION] if verdict is GateVerdict.reject else [],
        reason=None if verdict is GateVerdict.approve else "基础段须引用控制权转移的合同条款",
        decided_by="U-1",
        decided_at=NOW,
    )
    return decision.model_copy(update=changes)


async def gate_view(s: Started) -> Any:
    view = await GraphStateQuery(s.runner.app, s.runner.deps, s.runner).gate(EID)
    assert view is not None
    return view


async def reject_and_redraft(s: Started) -> GateDecision:
    """模拟 RPT-01 退回 (效果由 R26 实现, 这里直接以闸门身份写入决定), 继续执行: report_draft 出 REPORT-2."""
    rejected = rpt_decision(GateVerdict.reject, id="D-RPT-REJECT")
    await s.runner.app.aupdate_state(CONFIG, {"gate_decisions": [rejected]}, as_node=GATE_NODES[GateId.RPT_01])
    s.runner.acquire(EID)
    s.runner.launch(EID, None)
    await wait_idle(s.runner, EID)
    assert await s.next_nodes() == ("gate_rpt01",)
    return rejected


async def test_report_draft_writes_first_revision() -> None:
    """验收 1: FIN-01 通过后 report_draft 经 execute 调 A10: REPORT-1 v1 与 RUN-RPT-03-1 同一次提交, 并入状态,
    停在 RPT-01 闸门; 报告为保留意见, 引用已确认的跨期判断."""
    s = await at_fin_gate()
    commits = len(s.output_store.commits)
    await s.submit.submit(EID, fin_decision(GateVerdict.approve))
    await wait_idle(s.runner, EID)

    draft = [c for c in s.output_store.commits[commits:] if any(isinstance(o, AuditReportVersion) for o in c)]
    [commit] = draft
    [report] = [o for o in commit if isinstance(o, AuditReportVersion)]
    [run] = [o for o in commit if isinstance(o, ProcedureRun)]
    assert (report.id, report.version, report.revision, report.opinion_type) == (
        "REPORT-1",
        1,
        1,
        OpinionType.qualified,
    )
    assert report.judgment_refs == [JudgmentRef(id="J-REV-02-1-SI-2026-0612", version=2)]
    for section in (ReportSection.opinion, ReportSection.basis_for_opinion):
        assert all(fact in report.sections[section] for fact in ("142,000", "2026-06-28", "2026-07-03"))
    assert (run.id, run.status) == ("RUN-RPT-03-1", RunStatus.succeeded)
    assert (await s.runner.app.aget_state(CONFIG)).values["report_versions"] == [report]
    assert await s.next_nodes() == ("gate_rpt01",)


async def test_gate_view_before_and_after_reject() -> None:
    """验收 2: 首次呈报 REPORT-1 v1, 无此前的退回; 退回一次并出 REPORT-2 后, 呈报 REPORT-2 v1 与那次退回决定;
    REPORT-2 由第 2 次执行 (rerun) 写出, 回应那次退回."""
    s = await at_rpt_gate()

    first = await gate_view(s)
    assert (first.gate, first.target_id, first.target_version) == (GateId.RPT_01, "REPORT-1", 1)
    assert isinstance(first.material, ReportMaterial) and first.material.kind == "RPT-01"
    assert first.material.previous_decisions == []

    rejected = await reject_and_redraft(s)

    second = await gate_view(s)
    assert (second.target_id, second.target_version) == ("REPORT-2", 1)
    assert second.material.previous_decisions == [rejected]
    assert second.material.report.addresses_decision_id == rejected.id
    run = await s.output_store.get(EID, ProcedureRun, "RUN-RPT-03-2")
    assert run is not None and run.trigger is RunTrigger.rerun


@pytest.mark.parametrize(
    ("decision", "code", "detail"),
    [
        (rpt_decision(GateVerdict.reject, annotations=[]), "MISSING_FIELD", "annotations"),
        (rpt_decision(GateVerdict.reject, reason=None), "MISSING_FIELD", "reason"),
        (rpt_decision(GateVerdict.insufficient_info, reason=None), "MISSING_FIELD", "reason"),
        (rpt_decision(GateVerdict.approve, annotations=[ANNOTATION]), "FIELD_NOT_ALLOWED", "annotations"),
        (rpt_decision(GateVerdict.approve, disposition=OpinionType.qualified), "FIELD_NOT_ALLOWED", "disposition"),
        (rpt_decision(GateVerdict.reject, materiality=CHOICE), "FIELD_NOT_ALLOWED", "materiality"),
        (rpt_decision(GateVerdict.approve, edits=[edit()]), "FIELD_NOT_ALLOWED", "edits"),
    ],
)
async def test_invalid_rpt01_decision_rejected(decision: GateDecision, code: str, detail: str) -> None:
    """验收 3: 不合规的 RPT-01 决定被拒, Store 与检查点不变, 锁释放."""
    s = await at_rpt_gate()

    await assert_rejected(s, decision, code, detail)


async def test_decision_on_superseded_report_rejected() -> None:
    """验收 3: 当前已是 REPORT-2, 决定仍绑定 REPORT-1: VERSION_MISMATCH."""
    s = await at_rpt_gate()
    await reject_and_redraft(s)

    await assert_rejected(s, rpt_decision(GateVerdict.approve, id="D-RPT-2"), "VERSION_MISMATCH")


@pytest.mark.parametrize("verdict", [GateVerdict.approve, GateVerdict.reject, GateVerdict.insufficient_info])
async def test_valid_rpt01_decision_not_yet_applied(verdict: GateVerdict) -> None:
    """验收 4: 校验通过的 RPT-01 决定暂抛 NotImplementedError (效果由下一条需求实现), 不写 Store, 锁释放."""
    s = await at_rpt_gate()
    rows = list(s.output_store.rows)

    with pytest.raises(NotImplementedError, match="RPT-01"):
        await s.submit.submit(EID, rpt_decision(verdict))

    assert s.output_store.rows == rows
    assert not s.runner.is_running(EID)
