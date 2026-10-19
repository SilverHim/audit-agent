"""RPT-01 闸门. R25: report_draft 由 A10 出报告首稿; 闸门呈报当前报告与此前的退回决定; RPT-01 决定按必填表校验.
R26: approve 签字归档并锁定项目, reject 回 report_draft 出下一修订, 信息不足重呈."""

from datetime import UTC, date, datetime
from typing import Any

import pytest
import pytest_asyncio

from backend.contracts.errors import DecisionRejected, EngagementLocked
from backend.contracts.models import (
    Annotation,
    AuditReportVersion,
    GateDecision,
    GateId,
    GateVerdict,
    JudgmentRef,
    OpinionType,
    Phase,
    Procedure,
    ProcedureRun,
    ProcedureStatus,
    ReportSection,
    ReportStatus,
    RunStatus,
    RunTrigger,
    TrailKind,
    Versioned,
)
from backend.contracts.views import ReportMaterial
from backend.orchestration.gates import rpt01_effects
from backend.orchestration.query import GraphStateQuery
from backend.orchestration.runner import GraphRunner
from tests.fakes import NOW, NUMBAT_PROFILE, EngagementWrite, FakeTrail, make_procedure, profile_update, wait_idle
from tests.test_decisions import CHOICE, Started, assert_rejected, edit, stop_after_submit
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


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def rpt_gate() -> Started:
    """停在 RPT-01 闸门前的一次运行, 由只提交被拒决定的用例共用: assert_rejected 逐次核对存储与图都没动."""
    return await at_rpt_gate()


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
    """RPT-01 退回 REPORT-1 (批注同合成数据集第 7 节第 17 行), 后台继续: report_draft 出 REPORT-2, 再停在闸门前."""
    rejected = rpt_decision(GateVerdict.reject, id="D-RPT-REJECT")
    await s.submit.submit(EID, rejected)
    await wait_idle(s.runner, EID)
    assert await s.next_nodes() == ("gate_rpt01",)
    return rejected


def latest(s: Started, model: type[Any], oid: str) -> Any:
    return max((r for r in s.output_store.rows if isinstance(r, model) and r.id == oid), key=lambda r: r.version)


def decision_trail(s: Started) -> list[tuple[str | None, str | None]]:
    """decision 留痕的 (程序编号, 防重复键), 按写入顺序."""
    trail = s.runner.deps.trail
    assert isinstance(trail, FakeTrail)
    return [(e.procedure_id, e.idempotency_key) for e in trail.entries if e.kind is TrailKind.decision]


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
@pytest.mark.asyncio(loop_scope="module")
async def test_invalid_rpt01_decision_rejected(
    rpt_gate: Started, decision: GateDecision, code: str, detail: str
) -> None:
    """验收 3: 不合规的 RPT-01 决定被拒, Store 与检查点不变, 锁释放."""
    await assert_rejected(rpt_gate, decision, code, detail)


async def test_decision_on_superseded_report_rejected() -> None:
    """验收 3: 当前已是 REPORT-2, 决定仍绑定 REPORT-1: VERSION_MISMATCH."""
    s = await at_rpt_gate()
    await reject_and_redraft(s)

    await assert_rejected(s, rpt_decision(GateVerdict.approve, id="D-RPT-2"), "VERSION_MISMATCH")


# ── R26: RPT-01 决定的效果 ──

SIGNED_AT = datetime(2026, 10, 2, 15, tzinfo=UTC)  # 悉尼已是 10 月 3 日 01:00 (夏令时 10 月 4 日才开始)


async def test_reject_returns_to_report_draft() -> None:
    """验收 1: reject 只写决定 (一次提交) 与 1 行 decision 留痕 (RPT-01 下); 下一步 report_draft; RPT-01 不变."""
    s = await at_rpt_gate()
    stop_after_submit(s)
    commits, trail = len(s.output_store.commits), len(decision_trail(s))
    rpt01 = latest(s, Procedure, "RPT-01")
    decision = rpt_decision(GateVerdict.reject)

    await s.submit.submit(EID, decision)

    assert s.output_store.commits[commits:] == [[decision]]
    assert decision_trail(s)[trail:] == [("RPT-01", "D-RPT")]
    assert await s.next_nodes() == ("report_draft",)
    assert latest(s, Procedure, "RPT-01") == rpt01


async def test_insufficient_info_stays_at_gate() -> None:
    """验收 2: 信息不足只写决定与 1 行 decision 留痕; 报告与 RPT-01 不变, 仍停在闸门; 呈报的此前退回决定不含它."""
    s = await at_rpt_gate()
    rejected = await reject_and_redraft(s)
    commits, trail = len(s.output_store.commits), len(decision_trail(s))
    report, rpt01 = latest(s, AuditReportVersion, "REPORT-2"), latest(s, Procedure, "RPT-01")
    decision = rpt_decision(GateVerdict.insufficient_info, id="D-RPT-INFO", target_id="REPORT-2")

    await s.submit.submit(EID, decision)
    await wait_idle(s.runner, EID)

    assert s.output_store.commits[commits:] == [[decision]]
    assert decision_trail(s)[trail:] == [("RPT-01", "D-RPT-INFO")]
    assert (latest(s, AuditReportVersion, "REPORT-2"), latest(s, Procedure, "RPT-01")) == (report, rpt01)
    assert await s.next_nodes() == ("gate_rpt01",)
    assert (await gate_view(s)).material.previous_decisions == [rejected]


async def approve_report_2(s: Started) -> tuple[GateDecision, int]:
    """退回一次后批准 REPORT-2 v1, 签字时刻 SIGNED_AT; 返回决定与提交前的提交数."""
    await reject_and_redraft(s)
    s.submit.now = lambda: SIGNED_AT
    commits = len(s.output_store.commits)
    decision = rpt_decision(GateVerdict.approve, id="D-RPT-APPROVE", target_id="REPORT-2")
    await s.submit.submit(EID, decision)
    await wait_idle(s.runner, EID)
    return decision, commits


async def test_approve_signs_report_and_locks_last() -> None:
    """验收 3: approve 一次提交写决定, REPORT-2 v2 (signed, 报告日 2026-10-03, 签字人为决定人, 签字时刻为 now)
    与 RPT-01 completed 新版本; 阶段 Archived 与 locked 在同一提交, 排在全部 put 之后."""
    s = await at_rpt_gate()
    rpt01 = latest(s, Procedure, "RPT-01")

    decision, commits = await approve_report_2(s)

    [commit] = s.output_store.commits[commits:]
    *puts, last = commit
    assert all(isinstance(o, Versioned) for o in puts)
    assert last == EngagementWrite("update", EID, {"phase": Phase.Archived, "locked": True})
    assert decision in puts
    [report] = [o for o in puts if isinstance(o, AuditReportVersion)]
    assert (report.id, report.version, report.status) == ("REPORT-2", 2, ReportStatus.signed)
    assert (report.report_date, report.approved_by, report.approved_at) == (date(2026, 10, 3), "U-1", SIGNED_AT)
    assert report.sections == latest(s, AuditReportVersion, "REPORT-2").sections  # 签字不改正文
    [procedure] = [o for o in puts if isinstance(o, Procedure)]
    assert (procedure.id, procedure.version, procedure.status) == (
        "RPT-01",
        rpt01.version + 1,
        ProcedureStatus.completed,
    )
    assert decision_trail(s)[-1] == ("RPT-01", "D-RPT-APPROVE")


async def test_approve_routes_to_archive_and_ends() -> None:
    """验收 4: 签字后报告已是 v2, 决定绑定 v1, 仍放行到 archive; archive 不写 Store, 图结束, 无后台错误.
    检查点: REPORT-1 v1 draft, REPORT-2 v2 signed, 阶段 Archived."""
    s = await at_rpt_gate()

    decision, commits = await approve_report_2(s)

    assert len(s.output_store.commits) == commits + 1  # 只有决定那一次提交, archive 没有写入
    snapshot = await s.runner.app.aget_state(CONFIG)
    assert snapshot.next == () and s.runner.run_error(EID) is None
    assert [(r.id, r.version, r.status) for r in snapshot.values["report_versions"]] == [
        ("REPORT-1", 1, ReportStatus.draft),
        ("REPORT-2", 2, ReportStatus.signed),
    ]
    assert snapshot.values["phase"] is Phase.Archived
    assert snapshot.values["gate_decisions"][-1] == decision


async def test_signed_engagement_is_read_only() -> None:
    """验收 5: 签字后项目 locked: 再提交决定与更新客户资料报 ENGAGEMENT_LOCKED; 直接写 Store 抛 EngagementLocked."""
    s = await at_rpt_gate()
    await approve_report_2(s)
    rows = list(s.output_store.rows)

    with pytest.raises(DecisionRejected) as e:
        await s.submit.submit(EID, rpt_decision(GateVerdict.approve, id="D-RPT-AGAIN", target_id="REPORT-2"))
    assert e.value.code == "ENGAGEMENT_LOCKED"
    profile = NUMBAT_PROFILE.model_copy(update={"version": NUMBAT_PROFILE.version + 1})
    with pytest.raises(DecisionRejected) as e:
        await s.submit.update_client_profile(EID, *profile_update(profile), updated_by="U-1")
    assert e.value.code == "ENGAGEMENT_LOCKED"
    with pytest.raises(EngagementLocked):
        await s.output_store.put(EID, [rpt_decision(GateVerdict.approve, id="D-RAW")], actor="U-1")
    assert s.output_store.rows == rows
    assert not s.runner.is_running(EID)


@pytest.mark.parametrize(
    ("now", "report_date"),
    [
        (SIGNED_AT, date(2026, 10, 3)),
        (datetime(2026, 10, 2, 13, 59, tzinfo=UTC), date(2026, 10, 2)),  # 悉尼 23:59, 仍是 10 月 2 日
    ],
)
def test_rpt01_effects_is_pure(now: datetime, report_date: date) -> None:
    """验收 6: 效果是纯函数: approve 出报告签字版, RPT-01 completed 新版本与阶段 Archived, 报告日按悉尼取当天;
    驳回与信息不足只有决定; 输入不变."""
    pack = [make_procedure("RPT-03", Phase.P4_Reporting), make_procedure("RPT-01", Phase.P4_Reporting)]
    report = AuditReportVersion(
        id="REPORT-2", revision=2, opinion_type=OpinionType.qualified, sections={}, judgment_refs=[]
    )
    approve = rpt_decision(GateVerdict.approve, target_id="REPORT-2")

    effects = rpt01_effects(pack, approve, report, now)

    assert effects["gate_decisions"] == [approve]
    assert [(p.id, p.version, p.status) for p in effects["procedure_pack"]] == [
        ("RPT-03", 1, ProcedureStatus.planned),
        ("RPT-01", 2, ProcedureStatus.completed),
    ]
    [signed] = effects["report_versions"]
    assert signed == report.model_copy(
        update={
            "version": 2,
            "status": ReportStatus.signed,
            "report_date": report_date,
            "approved_by": "U-1",
            "approved_at": now,
        }
    )
    assert effects["phase"] is Phase.Archived
    assert (pack[1].version, report.version, report.status) == (1, 1, ReportStatus.draft)
    for verdict in (GateVerdict.reject, GateVerdict.insufficient_info):
        decision = rpt_decision(verdict)
        assert rpt01_effects(pack, decision, report, now) == {"gate_decisions": [decision]}
