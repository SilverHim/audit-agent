"""需求 R165: A8 执行 FIN-09 更正验证.

Xero 演示上直接调 A8 (不经主图; verify_correction 节点由 R160 接入, 两轮闭环的图测试由 R167 加): 状态为
misstatement_fakes.fin03_state 换成更正通知之后 v4 verifying 的错报 (第 1 轮两条, 通知 D-CN1; 第 2 轮三条, 通知
D-CN2, 另有其来源判断 J-FIN-15-1-{科目}, 即 misstatement_fakes.round2_sources); 真实判断引擎, 主模型缺省读 FIN-09 的
Xero fixture 正本 (R164), 对不上与部分更正的用例用脚本输出. 规则引擎的 V01 为补造更正分录接科目表的行
(misstatement_fakes.fin09_deps, xero_fakes.xero_v01_rows), 假财务存储另记这些分录 (推理链指向它们的指针才解析得了).
"""

import json
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

import pytest

from backend.agents.base import AgentDeps, RunContext
from backend.agents.workpaper import WorkpaperAgent, corrected_cents, matched_lines, to_verify, verification_update
from backend.contracts.models import (
    AgentId,
    AuditException,
    Decision,
    Direction,
    EvidenceType,
    ExceptionDisposition,
    Misstatement,
    MisstatementUpdate,
    Reliability,
    ReviewStatus,
    RunStatus,
    RunTrigger,
    TaskKind,
    TaskStatus,
)
from backend.contracts.state import EngagementState, StatePatch
from backend.orchestration.decisions import review_effects
from backend.orchestration.merge import check_patch
from backend.orchestration.nodes import after_notice, apply_patch
from backend.services.review_queue import RERUNNABLE
from tests.fakes import FakeTrail, ScriptedLLM, finished, gen05_exception, make_run
from tests.fixture_sets import XERO
from tests.misstatement_fakes import (
    CSH02,
    FIN09,
    FIRST,
    SECOND,
    SUPERSEDED,
    XJ499,
    fin03_context,
    fin03_state,
    fin09_deps,
    llm_calls,
    notice,
    round1_communicated,
    round2_communicated,
    round2_sources,
)
from tests.test_review_decisions import record
from tests.test_xero_fixtures import RecordingJudgments
from tests.xero_fakes import xero_v01_rows

pytestmark = pytest.mark.external  # Xero 快照, 补造与 FIN-09 的 Xero fixture

V02 = "X-FIN-09-1-V02-1"


def verified(mid: str, version: int, entry_no: str, cents: int) -> MisstatementUpdate:
    """全部更正的验证载荷."""
    return MisstatementUpdate(
        misstatement_id=mid,
        base_version=version,
        kind="verification",
        status="corrected",
        adjustment_entry_nos=[entry_no],
        corrected_cents=cents,
        residual_cents=0,
    )


RECOMMUNICATE = MisstatementUpdate(
    misstatement_id=XJ499, base_version=4, kind="verification", status="communicated", needs_recommunication=True
)


def round1_verifying() -> list[Misstatement]:
    """第 1 轮更正通知 D-CN1 之后的两条 (v4 verifying)."""
    return [after_notice(m, notice()) for m in round1_communicated()]


def round2_state() -> EngagementState:
    """第 2 轮: 三条薪酬错报经更正通知 D-CN2 之后为 v4 verifying, 其来源判断 (FIN-15 对科目的判断) 在状态中; FIN-09
    已执行过一次."""
    cn2 = notice("D-CN2", misstatement_ids=[], adjustment_entry_nos=SECOND)
    misstatements = [after_notice(m, cn2) for m in round2_communicated()]
    state = fin03_state()
    return state | {  # type: ignore[return-value]
        "judgments": [*state["judgments"], *round2_sources()],
        "misstatements": misstatements,
        "procedure_runs": [finished(make_run(FIN09))],
    }


async def run_fin09(deps: AgentDeps, state: EngagementState) -> tuple[StatePatch, RunContext]:
    ctx = fin03_context(deps, state, FIN09)
    return await WorkpaperAgent(deps).run(ctx), ctx


def recording(deps: AgentDeps) -> tuple[RecordingJudgments, AgentDeps]:
    """判断引擎外包一层记下判断上下文."""
    recorder = RecordingJudgments(deps.judgments)
    return recorder, replace(deps, judgments=recorder)


def fixture(name: str, **changes: Any) -> str:
    """FIN-09 的 Xero fixture 正本 (原文), 改 changes 中的字段."""
    raw = json.loads((XERO.fixtures / FIN09 / f"{name}.json").read_text(encoding="utf-8"))
    return json.dumps(raw | changes)


def edited(changes: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """第 1 轮的 V01 行, 按行 ID 改 changes 中的列."""
    return [row | changes.get(row["id"], {}) for row in xero_v01_rows(FIRST)]


def update_of(patch: StatePatch, mid: str) -> MisstatementUpdate | None:
    return next(j.misstatement_update for j in patch["judgments"] if j.subject_id == mid)


async def test_fin09_verifies_corrected_misstatements() -> None:
    """验收 1, 2: 两条 v4 verifying 各一条判断 (第 1 版, 待复核) 与 normal 任务, 执行成功; 判断带验证载荷 (依据 v4):
    XJ499 由 AJE-X01 贷 710 全额更正 (75000), CSH02 由 AJE-X05 贷 270 全额更正 (4853), 剩余额都为 0; 都没有规则例外.
    置信度 evidence 0.7, rule 1.0, llm 0.9, 合为 0.8625. 2 行 llm_call, actor A8, fixture_key 为来源判断的对象. 片段经
    A8 的合并校验通过."""
    trail = FakeTrail()
    deps = fin09_deps(trail)
    patch, ctx = await run_fin09(deps, fin03_state(misstatements=round1_verifying()))

    assert set(patch) == {"procedure_runs", "judgments", "review_tasks"}
    check_patch(
        patch, writer=AgentId.A8, procedure_id=FIN09, run_id=ctx.run.id, state=ctx.state, outputs=ctx.template.outputs
    )
    [run] = patch["procedure_runs"]
    assert (run.id, run.status) == ("RUN-FIN-09-1", RunStatus.succeeded)
    judgments = patch["judgments"]
    assert [(j.id, j.version, j.review_status, j.exception_ids) for j in judgments] == [
        (f"J-FIN-09-1-{m}", 1, ReviewStatus.pending, []) for m in (XJ499, CSH02)
    ]
    assert [j.misstatement_update for j in judgments] == [
        verified(XJ499, 4, "AJE-X01", 75000),
        verified(CSH02, 4, "AJE-X05", 4853),
    ]
    assert [(t.id, t.kind) for t in patch["review_tasks"]] == [
        (f"T-J-FIN-09-1-{m}-v1", TaskKind.normal) for m in (XJ499, CSH02)
    ]
    for j in judgments:
        assert (j.confidence_parts, j.confidence) == ({"evidence": 0.7, "rule": 1.0, "llm": 0.9}, 0.8625)
    assert [(e.actor, e.run_id, e.payload["fixture_key"]) for e in llm_calls(trail)] == [
        (AgentId.A8, "RUN-FIN-09-1", key) for key in ("XJ-499", "BA-091")
    ]


async def test_fin09_context_lists_matched_entries() -> None:
    """验收 1: 上下文为错报, 来源判断 (结论, 不带证据), 匹配行, 匹配行所在凭证的全部行, 已更正额与剩余额 (键同
    FIN-09.md.j2 头注释); 校验上下文另有剩余额与凭证中错报科目以外的科目."""
    recorder, deps = recording(fin09_deps(FakeTrail()))
    await run_fin09(deps, fin03_state(misstatements=round1_verifying()))

    xj499, csh02 = recorder.contexts
    assert set(xj499.data) == {
        "misstatement", "source_judgment", "matched_line_ids", "entry_lines", "corrected_cents", "residual_cents"
    }  # fmt: skip
    assert set(xj499.data["source_judgment"]) == {"id", "procedure_id", "subject_id", "conclusion"}
    assert (xj499.data["matched_line_ids"], [r["id"] for r in xj499.data["entry_lines"]]) == (
        ["AJE-X01-2"],
        ["AJE-X01-1", "AJE-X01-2", "AJE-X01-3"],
    )
    assert (xj499.data["corrected_cents"], xj499.data["residual_cents"]) == (75000, 0)
    assert (xj499.expected_residual_cents, xj499.entry_account_codes) == (0, ["711", "820"])
    assert (csh02.data["matched_line_ids"], [r["id"] for r in csh02.data["entry_lines"]]) == (
        ["AJE-X05-2"],
        ["AJE-X05-1", "AJE-X05-2"],
    )
    assert (csh02.expected_residual_cents, csh02.entry_account_codes) == (0, ["620"])
    assert [c.rule_findings for c in recorder.contexts] == [[], []]


async def test_without_account_code_matches_by_amount() -> None:
    """验收 3: XJ499 没有 account_code: 匹配借方或贷方等于错报金额的行 AJE-X01-2 (贷 75000), 结果同验收 1; 凭证的
    科目全部进校验上下文 (fixture 的 other_accounts 在其中)."""
    xj499, csh02 = round1_verifying()
    recorder, deps = recording(fin09_deps(FakeTrail()))
    patch, _ = await run_fin09(
        deps, fin03_state(misstatements=[xj499.model_copy(update={"account_code": None}), csh02])
    )

    assert update_of(patch, XJ499) == verified(XJ499, 4, "AJE-X01", 75000)
    assert recorder.contexts[0].data["matched_line_ids"] == ["AJE-X01-2"]
    assert recorder.contexts[0].entry_account_codes == ["710", "711", "820"]


async def test_round2_verifies_payroll_misstatements() -> None:
    """验收 4: 第 2 轮三条 (FIN-09 第 2 次执行): M-D-F03 配 AJE-X02-2 (贷 826), M-D-F04 配 AJE-X03, M-D-F05 配 AJE-X04,
    都全额更正; 判断 J-FIN-09-2-{错报}, fixture_key 为科目代码."""
    trail = FakeTrail()
    patch, _ = await run_fin09(fin09_deps(trail, entries=FIRST + SECOND), round2_state())

    assert [(j.id, j.misstatement_update) for j in patch["judgments"]] == [
        ("J-FIN-09-2-M-D-F03", verified("M-D-F03", 4, "AJE-X02", 426120)),
        ("J-FIN-09-2-M-D-F04", verified("M-D-F04", 4, "AJE-X03", 201000)),
        ("J-FIN-09-2-M-D-F05", verified("M-D-F05", 4, "AJE-X04", 268736)),
    ]
    assert [e.payload["fixture_key"] for e in llm_calls(trail)] == ["826", "804", "805"]


async def test_partial_correction() -> None:
    """验收 5: AJE-X01-2 改为贷 710 50000, 脚本模型判未完整更正: XJ499 partially_corrected, 已更正 50000, 剩余 25000."""
    llm = ScriptedLLM({"XJ-499": [fixture("XJ-499", fully_corrected=False)], "BA-091": [fixture("BA-091")]})
    deps = fin09_deps(FakeTrail(), llm, rows=edited({"AJE-X01-2": {"credit_cents": 50000}}))
    patch, _ = await run_fin09(deps, fin03_state(misstatements=round1_verifying()))

    assert update_of(patch, XJ499) == MisstatementUpdate(
        misstatement_id=XJ499,
        base_version=4,
        kind="verification",
        status="partially_corrected",
        adjustment_entry_nos=["AJE-X01"],
        corrected_cents=50000,
        residual_cents=25000,
    )


MISMATCH: dict[str, Any] = {"fully_corrected": False, "other_accounts": [], "proposed_action": "more_evidence"}


def mismatch_llm() -> ScriptedLLM:
    """对不上时的脚本输出: XJ499 判未更正, 不涉及其他科目, 要更多证据 (MISMATCH); CSH02 照 fixture 正本."""
    return ScriptedLLM({"XJ-499": [fixture("XJ-499", **MISMATCH)], "BA-091": [fixture("BA-091")]})


OTHER_SOURCE = {f"AJE-X01-{n}": {"source_doc": "XJ-498"} for n in (1, 2, 3)}


@pytest.mark.parametrize(
    ("rows", "changes", "source_ids", "corrected", "amount"),
    [
        (OTHER_SOURCE, {}, ["XJ-499"], 0, 75000),  # 来源对象对不上: 没有候选行
        ({}, {"account_code": "720"}, ["XJ-499"], 0, 75000),  # 科目对不上
        ({"AJE-X01-2": {"credit_cents": 90000}}, {}, ["AJE-X01-2"], 90000, 75000),  # 超额
        ({"AJE-X01-2": {"debit_cents": 75000, "credit_cents": 0}}, {}, ["AJE-X01-2"], -75000, 75000),  # 方向相反
        ({}, {"account_code": None, "amount_cents": 50000}, ["XJ-499"], 0, 50000),  # 没有科目, 金额对不上
    ],
    ids=["other_source", "other_account", "excess", "reversed", "amount_only"],
)
async def test_mismatch_raises_v02_and_recommunicates(
    rows: dict[str, dict[str, Any]], changes: dict[str, Any], source_ids: list[str], corrected: int, amount: int
) -> None:
    """验收 6: 对不上时出 X-FIN-09-1-V02-1 (本次第 1 版, open; 涉及匹配行, 没有时为来源对象; 金额与 detail 的
    expected_cents 为错报金额), 作 XJ499 判断的规则例外 (exception_ids); 载荷为 communicated 且须重新沟通. CSH02
    照常."""
    xj499, csh02 = round1_verifying()
    deps = fin09_deps(FakeTrail(), mismatch_llm(), rows=edited(rows))
    patch, ctx = await run_fin09(deps, fin03_state(misstatements=[xj499.model_copy(update=changes), csh02]))

    check_patch(
        patch, writer=AgentId.A8, procedure_id=FIN09, run_id=ctx.run.id, state=ctx.state, outputs=ctx.template.outputs
    )
    [exception] = patch["exceptions"]
    assert (exception.id, exception.version, exception.rule_id, exception.severity, exception.disposition) == (
        V02,
        1,
        "V02",
        "medium",
        ExceptionDisposition.open,
    )
    assert (exception.source_ids, exception.amount_cents) == (source_ids, amount)
    matched = [s for s in source_ids if s.startswith("AJE-")]
    assert exception.detail == {
        "misstatement_id": XJ499,
        "expected_cents": amount,
        "corrected_cents": corrected,
        "matched_line_ids": matched,
    }
    assert [(p.type, p.source_type, p.source_id, p.locator, p.reliability) for p in exception.evidence] == [
        (EvidenceType.voucher, "journal_entry", lid, "debit_cents, credit_cents", Reliability.internal_original)
        for lid in matched
    ]
    [judgment] = [j for j in patch["judgments"] if j.subject_id == XJ499]
    assert (judgment.exception_ids, judgment.misstatement_update) == ([V02], RECOMMUNICATE)
    assert update_of(patch, CSH02) == verified(CSH02, 4, "AJE-X05", 4853)


@pytest.mark.parametrize(
    ("rows", "kind", "task_id"),
    [({}, TaskKind.failed, f"T-RUN-FIN-09-1-{XJ499}-v1"), (OTHER_SOURCE, TaskKind.unattributed, f"T-{V02}-v1")],
    ids=["matched", "mismatch"],
)
async def test_model_failure_hands_over(rows: dict[str, dict[str, Any]], kind: TaskKind, task_id: str) -> None:
    """验收 8: 大模型连续 3 次结构失败 (重试耗尽): 对得上时建带对象的 failed 任务, 允许 rerun 与 manual_result
    (FIN-09 开 manual_result_allowed, 需求 R165); 对不上时 V02 第 1 版置 unattributed, 以它为目标建 unattributed 任务.
    CSH02 照常判断."""
    llm = ScriptedLLM({"XJ-499": ["not json"] * 3, "BA-091": [fixture("BA-091")]})
    deps = fin09_deps(FakeTrail(), llm, rows=edited(rows))
    patch, _ = await run_fin09(deps, fin03_state(misstatements=round1_verifying()))

    assert [j.subject_id for j in patch["judgments"]] == [CSH02]
    task = next(t for t in patch["review_tasks"] if t.kind is kind)
    assert (task.id, task.subject_id) == (task_id, XJ499)
    if kind is TaskKind.failed:
        assert deps.queue.allowed(task) == {Decision.rerun, Decision.manual_result}
        assert "exceptions" not in patch
    else:
        assert [(x.id, x.version, x.disposition) for x in patch["exceptions"]] == [
            (V02, 1, ExceptionDisposition.unattributed)
        ]


async def failed_with_v02(
    rows: dict[str, dict[str, Any]] = OTHER_SOURCE, failing: Sequence[str] = ("XJ-499",)
) -> tuple[EngagementState, StatePatch]:
    """第 1 次执行: V01 按 rows 改 (缺省 XJ499 的分录来源对象对不上, 出 V02), 模型对 failing 中的来源对象连续 3 次
    结构失败: 其 V02 v1 unattributed, 建 unattributed 任务. 返回执行时的状态与片段."""
    llm = ScriptedLLM({key: ["not json"] * 3 if key in failing else [fixture(key)] for key in ("XJ-499", "BA-091")})
    state = fin03_state(misstatements=round1_verifying())
    patch, _ = await run_fin09(fin09_deps(FakeTrail(), llm, rows=edited(rows)), state)
    return state, patch


async def rerun(
    state: EngagementState,
    first: StatePatch,
    rows: dict[str, dict[str, Any]],
    llm: Any,
    subjects: Sequence[str] = (XJ499,),
    later: Sequence[AuditException] = (),
) -> StatePatch:
    """第 1 次的片段并入状态, 其可重跑任务 (RERUNNABLE: failed 与 unattributed) 已决定 rerun (一组, decided), later
    为之后的例外版本; 第 2 次执行只重判 subjects (rerun_subject_ids), V01 按 rows 改."""
    decided = [
        t.model_copy(update={"version": 2, "status": TaskStatus.decided})
        for t in first["review_tasks"]
        if t.kind in RERUNNABLE
    ]
    state = apply_patch(apply_patch(state, first), {"review_tasks": decided, "exceptions": list(later)})
    deps = fin09_deps(FakeTrail(), llm, rows=edited(rows))
    ctx = fin03_context(deps, state, FIN09)
    ctx = replace(ctx, procedure=ctx.procedure.model_copy(update={"rerun_subject_ids": list(subjects)}))
    return await WorkpaperAgent(deps).run(ctx)


@pytest.mark.parametrize(
    ("rows", "reply"),
    [(OTHER_SOURCE, MISMATCH), ({}, {})],
    ids=["still_mismatched", "now_matched"],
)
async def test_rerun_reuses_unattributed_v02(rows: dict[str, dict[str, Any]], reply: dict[str, Any]) -> None:
    """审核 R165-07 (需求 R347 起 unattributed 任务可以 rerun, 规则同 CSH-02 的 D01, D04): 第 1 次 V02 v1
    unattributed; 对其任务 rerun 后第 2 次只重判 XJ499, 沿用 X-FIN-09-1-V02-1 作规则例外, 不建 X-FIN-09-2-V02-1
    (片段里没有例外); 本次对得上也沿用 (不看本次比对). 确认该判断时它出 v2 attributed."""
    state, first = await failed_with_v02()
    assert [(x.id, x.version, x.disposition) for x in first["exceptions"]] == [
        (V02, 1, ExceptionDisposition.unattributed)
    ]
    second = await rerun(state, first, rows, ScriptedLLM({"XJ-499": [fixture("XJ-499", **reply)]}))

    [judgment] = second["judgments"]
    assert (judgment.exception_ids, "exceptions" in second) == ([V02], False)
    [task] = second["review_tasks"]
    misstatements = {XJ499: round1_verifying()[0]}
    effects = review_effects(
        record(task.id, id="D-C"), task, judgment, first["exceptions"], set(), misstatements=misstatements
    )
    assert [(x.id, x.version, x.disposition) for x in effects if isinstance(x, AuditException)] == [
        (V02, 2, ExceptionDisposition.attributed)
    ]


async def test_attributed_earlier_v02_not_reused() -> None:
    """已归因的旧 V02 (第 1 次的, v2 attributed) 不沿用: 第 2 次仍对不上时按本次比对新建 X-FIN-09-2-V02-1 作规则例外."""
    state, first = await failed_with_v02()
    [v02] = first["exceptions"]
    attributed = v02.model_copy(
        update={"version": 2, "disposition": ExceptionDisposition.attributed, "attributed_by": "J-FIN-09-1-H1"}
    )
    second = await rerun(state, first, OTHER_SOURCE, mismatch_llm(), later=[attributed])

    assert [(x.id, x.version) for x in second["exceptions"]] == [("X-FIN-09-2-V02-1", 1)]
    [judgment] = second["judgments"]
    assert judgment.exception_ids == ["X-FIN-09-2-V02-1"]


async def test_group_rerun_reuses_each_own_v02() -> None:
    """两条都对不上且都判断失败 (V02-1 属 XJ499, V02-2 属 CSH02, 都 v1 unattributed): 一组重跑时各沿用自己的 V02
    (按 detail.misstatement_id 对上), 不新建."""
    other = {row["id"]: {"source_doc": "OTHER"} for row in xero_v01_rows(FIRST)}
    state, first = await failed_with_v02(other, ("XJ-499", "BA-091"))
    llm = ScriptedLLM({key: [fixture(key, **MISMATCH)] for key in ("XJ-499", "BA-091")})
    second = await rerun(state, first, other, llm, (XJ499, CSH02))

    assert [j.exception_ids for j in second["judgments"]] == [["X-FIN-09-1-V02-1"], ["X-FIN-09-1-V02-2"]]
    assert "exceptions" not in second


async def test_new_v02_numbered_without_reused() -> None:
    """审核 R165-09: 同一次执行里一条沿用, 一条新建. 第 1 次只有 XJ499 对不上, 两条都结构失败 (XJ499 的 V02-1
    unattributed, CSH02 为 failed 任务); 整组 rerun 后第 2 次 CSH02 也对不上: XJ499 沿用 X-FIN-09-1-V02-1, CSH02
    新建的是 X-FIN-09-2-V02-1 (编号不计沿用的), 不是 V02-2."""
    state, first = await failed_with_v02(failing=("XJ-499", "BA-091"))
    other = {row["id"]: {"source_doc": "OTHER"} for row in xero_v01_rows(FIRST)}
    llm = ScriptedLLM({key: [fixture(key, **MISMATCH)] for key in ("XJ-499", "BA-091")})
    second = await rerun(state, first, other, llm, (XJ499, CSH02))

    assert [x.id for x in second["exceptions"]] == ["X-FIN-09-2-V02-1"]
    assert [j.exception_ids for j in second["judgments"]] == [["X-FIN-09-1-V02-1"], ["X-FIN-09-2-V02-1"]]


async def test_nothing_to_verify_runs_empty() -> None:
    """验收 7: 没有 verifying 的错报 (两条都还是 v3 communicated): 不执行规则, 不调大模型, 执行成功, 片段只有执行
    记录."""
    trail = FakeTrail()
    deps = fin09_deps(trail)
    patch, _ = await run_fin09(deps, fin03_state(misstatements=round1_communicated()))

    assert (llm_calls(trail), deps.rules.calls) == ([], [])  # type: ignore[attr-defined]
    assert list(patch) == ["procedure_runs"]
    assert patch["procedure_runs"][0].status is RunStatus.succeeded


async def test_source_judgment_must_be_in_state() -> None:
    """来源判断不在状态中: 抛 LookupError (执行失败), 不静默跳过."""
    deps = fin09_deps(FakeTrail())
    state = fin03_state(misstatements=round1_verifying(), judgments=[])
    with pytest.raises(LookupError, match="J-GEN-02-1-XJ-499"):
        await WorkpaperAgent(deps).execute(fin03_context(deps, state, FIN09))


@pytest.mark.parametrize(
    ("entries", "expected"),
    [
        (FIRST, [("X-FIN-09-1-V02-1", XJ499), ("X-FIN-09-1-V02-2", CSH02)]),
        (["AJE-X05"], [("X-FIN-09-1-V02-1", CSH02)]),
    ],
    ids=["both", "csh02_only"],
)
async def test_v02_numbered_by_subject(entries: list[str], expected: list[tuple[str, str]]) -> None:
    """所匹配凭证 (entries) 的来源对象改了的错报对不上: V02 的 n 在对不上的错报中按判断对象的顺序从 1 编号, 各作自己
    判断的规则例外; 只有 CSH02 对不上时它的是 V02-1."""
    other = {row["id"]: {"source_doc": "OTHER"} for row in xero_v01_rows(FIRST) if row["entry_no"] in entries}
    sources = {"XJ-499": "AJE-X01", "BA-091": "AJE-X05"}  # 来源对象 → 匹配的凭证
    llm = ScriptedLLM({key: [fixture(key, **(MISMATCH if no in entries else {}))] for key, no in sources.items()})
    deps = fin09_deps(FakeTrail(), llm, rows=edited(other))
    patch, _ = await run_fin09(deps, fin03_state(misstatements=round1_verifying()))

    assert [(x.id, x.detail["misstatement_id"]) for x in patch["exceptions"]] == expected
    by_subject = [[x for x, m in expected if m == mid] for mid in (XJ499, CSH02)]
    assert [j.exception_ids for j in patch["judgments"]] == by_subject


async def test_rerun_subjects_only() -> None:
    """rerun_subject_ids 非空时只验证其中的错报 (in_scope, 同 FIN-03), 先筛再取数: 只调一次大模型, 范围外的 XJ499
    即使对不上也不出 V02."""
    trail = FakeTrail()
    deps = fin09_deps(trail, rows=edited(OTHER_SOURCE))
    ctx = fin03_context(deps, fin03_state(misstatements=round1_verifying()), FIN09)
    ctx = replace(ctx, procedure=ctx.procedure.model_copy(update={"rerun_subject_ids": [CSH02]}))
    patch = await WorkpaperAgent(deps).run(ctx)

    assert [j.subject_id for j in patch["judgments"]] == [CSH02]
    assert "exceptions" not in patch
    assert [e.payload["fixture_key"] for e in llm_calls(trail)] == ["BA-091"]


def test_to_verify_takes_current_results() -> None:
    """判断对象只取当前结果中的 verifying: 来源执行被过期重跑取代的不算; communicated 的不算."""
    xj499, csh02 = round1_verifying()
    assert [m.id for m in to_verify(fin03_state(misstatements=[xj499, csh02], rerun_requests=[SUPERSEDED]))] == [CSH02]
    assert to_verify(fin03_state(misstatements=round1_communicated())) == []


def line(
    lid: str,
    account_type: str | None,
    debit: int = 0,
    credit: int = 0,
    entry_no: str = "AJE-X01",
    source_doc: str = "XJ-499",
) -> dict[str, Any]:
    """一行 V01 (科目 710; 来源对象缺省为 XJ-499)."""
    return {
        "id": lid,
        "entry_no": entry_no,
        "account_code": "710",
        "account_type": account_type,
        "debit_cents": debit,
        "credit_cents": credit,
        "source_doc": source_doc,
    }


def test_matched_by_amount_takes_first_line_by_id() -> None:
    """没有 account_code 时按金额匹配, 有几行时取行 ID 最小的一行 (与 V01 的顺序无关); 只在来源对象的行中取, 别的
    来源对象的行 ID 更小也不取."""
    xj499 = round1_verifying()[0].model_copy(update={"account_code": None})
    rows = [
        line("AJE-X02-1", "asset", debit=75000, entry_no="AJE-X02"),
        line("AJE-X01-2", "asset", credit=75000),
        line("AJE-X01-1", "asset", credit=75000, source_doc="XJ-498"),
    ]
    assert [r["id"] for r in matched_lines(xj499, "XJ-499", rows)] == ["AJE-X01-2"]


@pytest.mark.parametrize(
    ("account_type", "direction", "debit", "credit", "corrected"),
    [
        ("asset", Direction.overstated, 0, 75000, 75000),
        ("expense", Direction.overstated, 0, 75000, 75000),
        ("expense", Direction.understated, 75000, 0, 75000),
        ("liability", Direction.understated, 0, 75000, 75000),
        ("equity", Direction.overstated, 75000, 0, 75000),
        ("equity", Direction.understated, 0, 75000, 75000),
        ("revenue", Direction.overstated, 75000, 0, 75000),
        ("asset", Direction.overstated, 75000, 0, -75000),  # 方向相反
    ],
)
def test_correction_direction_by_account_type(
    account_type: str, direction: Direction, debit: int, credit: int, corrected: int
) -> None:
    """有 account_code 时按匹配行的科目类型定更正方向: 资产, 费用高估以贷方更正, 低估以借方; 负债, 权益, 收入相反."""
    xj499 = round1_verifying()[0].model_copy(update={"direction": direction})
    assert corrected_cents(xj499, [line("AJE-X01-2", account_type, debit, credit)]) == corrected


def test_line_outside_chart_of_accounts_fails() -> None:
    """匹配行的科目不在科目表 (account_type 为空): 抛 LookupError, 不猜方向."""
    with pytest.raises(LookupError, match="AJE-X01-2"):
        corrected_cents(round1_verifying()[0], [line("AJE-X01-2", None, credit=75000)])


def test_entry_numbers_deduplicated() -> None:
    """同一凭证的两行都匹配时凭证号只记一次; 合计等于错报金额为全部更正."""
    xj499 = round1_verifying()[0]
    lines = [line("AJE-X01-2", "asset", credit=50000), line("AJE-X01-4", "asset", credit=25000)]
    assert verification_update(xj499, lines, corrected_cents(xj499, lines)) == verified(XJ499, 4, "AJE-X01", 75000)


async def test_increment_exceptions_join_rule_findings() -> None:
    """需求 R194 认领: GEN-05 增量执行在匹配凭证 (AJE-X01) 上未归因的例外接在 XJ499 判断的规则例外中; 别的凭证上的,
    初始执行引出的, 被过期重跑取代的增量执行引出的与已归因的不进; CSH02 的匹配凭证 (AJE-X05) 上没有."""
    increment, superseded = (
        finished(make_run("GEN-05", n).model_copy(update={"trigger": RunTrigger.incremental})) for n in (2, 3)
    )
    initial = finished(make_run("GEN-05"))
    on_entry = gen05_exception(increment, "R02", "medium", "AJE-X01-2", 75000, {"entry_no": "AJE-X01"})
    attributed = gen05_exception(increment, "R09", "high", "AJE-X01-1", 82500, {"entry_no": "AJE-X01"})
    attributed = attributed.model_copy(update={"disposition": ExceptionDisposition.attributed, "attributed_by": "J-1"})
    elsewhere = gen05_exception(increment, "R02", "medium", "AJE-X02-1", 426120, {"entry_no": "AJE-X02"}, number=2)
    earlier = gen05_exception(initial, "R02", "medium", "AJE-X01-2", 75000, {"entry_no": "AJE-X01"})
    stale = gen05_exception(superseded, "R02", "medium", "AJE-X01-2", 75000, {"entry_no": "AJE-X01"})
    rerun = SUPERSEDED.model_copy(update={"procedure_ids": ["GEN-05"], "superseded_run_ids": [superseded.id]})
    state = fin03_state(
        misstatements=round1_verifying(),
        procedure_runs=[initial, increment, superseded],
        exceptions=[on_entry, attributed, elsewhere, earlier, stale],
        rerun_requests=[rerun],
    )
    patch, _ = await run_fin09(fin09_deps(FakeTrail()), state)

    assert [j.exception_ids for j in patch["judgments"]] == [[on_entry.id], []]
