"""需求 R159: 错报更新随决定生效与手工结果.

Xero 演示上把主图放在 FIN-03 之前 (R160 接入 misstatement_triage 前, 完成节点改为只执行 FIN-03): 检查点以 fan_in
身份写入 P2 已结清的状态 (test_workpaper.fin03_state: 两条 identified 而未分类的错报与其来源判断; 错报与 FIN-03
也写进 Store),
真实 A8 读 FIN-03 的 Xero fixture 正本 (或给定的脚本大模型). FIN-03 为 L2, 两条判断各建 normal 任务, 完成节点内等人;
审计师经 DecisionSubmit 决定, 同一事务按判断上或人给的载荷写出错报新版本.

验证载荷 (FIN-09, R164 起) 的效果用 review_effects 直接测; 经 DecisionSubmit 的不合格验证载荷用 FIN-03 的载荷类别改为
verification 的程序库副本.
"""

import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from backend.contracts.errors import DecisionRejected
from backend.contracts.models import (
    AgentId,
    Assertion,
    AuditException,
    AutonomyLevel,
    Decision,
    Direction,
    DisputeResolution,
    ExceptionDisposition,
    Judgment,
    ManualResult,
    Misstatement,
    MisstatementDraft,
    MisstatementStatus,
    MisstatementType,
    MisstatementUpdate,
    NeitherAction,
    OpinionType,
    ProposedAction,
    ReviewRecord,
    ReviewStatus,
    ReviewTask,
    TargetType,
    TaskKind,
    TrailKind,
    UncorrectedSchedule,
    Versioned,
)
from backend.core.config import get_settings
from backend.orchestration.decisions import check_review_fields, review_effects
from backend.orchestration.gates import fin01_view
from backend.orchestration.nodes import NODE_PROCEDURES
from backend.orchestration.runner import GraphRunner, waiting_payloads
from backend.services.features import BASELINE, MISSTATEMENT
from backend.services.procedure_library import ProcedureLibrary
from backend.services.review_queue import misstatement_updates
from tests.fakes import NOW, FakeTrail, ScriptedLLM, make_ctx, wait_idle
from tests.test_decisions import Started, assert_rejected
from tests.test_review_decisions import record
from tests.test_workpaper import (
    FACTUAL,
    FIN03,
    FRAUD_REASON,
    classified,
    fin03_deps,
    fin03_state,
    fixture_raw,
    fraud_llm,
    llm_calls,
)
from tests.xero_fakes import xero_engagement
from tests.xero_flow import offline_runner

pytestmark = pytest.mark.external  # Xero 快照, 补造与 FIN-03 的 Xero fixture

EID = xero_engagement().id
CONFIG = GraphRunner.config(EID)
FEATURES = BASELINE | {MISSTATEMENT}
XJ499, CSH02 = "M-D-XJ499", "M-D-CSH02-091"
XJ499_TASK, CSH02_TASK = (f"T-J-FIN-03-1-{m}-v1" for m in (XJ499, CSH02))
FAILED_TASK = f"T-RUN-FIN-03-1-{XJ499}-v1"  # 分类模型对 XJ499 失败时的任务
# 复核人对 CSH02 改判的分类 (验收 2): 年假之外, 应计利息也属估计
JUDGMENTAL = MisstatementUpdate(
    misstatement_id=CSH02, base_version=1, kind="classification", type="judgmental", fraud_indicative=False
)
# 复核人对 XJ499 认定的错报 (验收 4): 与 M-D-XJ499 相同的重复情形
DUPLICATE = MisstatementDraft(
    type=MisstatementType.factual,
    account="Office equipment (710)",
    assertion=Assertion.Existence,
    amount_cents=75000,
    direction=Direction.overstated,
    account_code="710",
)


@pytest.fixture(autouse=True)
def completion_runs_fin03(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """完成节点只执行 FIN-03 (recovery 与 nodes 共用这张表)."""
    monkeypatch.setitem(NODE_PROCEDURES, "completion", (FIN03,))
    yield


async def at_fin03(llm: Any = None, library: ProcedureLibrary | None = None) -> Started:
    """见模块 docstring: 停在完成节点内等 FIN-03 的复核. llm 为空时读 Xero fixture 正本; library 为空时按 FEATURES."""
    deps = fin03_deps(FakeTrail(), llm, features=FEATURES, library=library)
    state = fin03_state()
    procedure = make_ctx(deps, FIN03, engagement=xero_engagement()).procedure
    store = deps.output_store
    async with store.transaction(EID):
        await store.put(EID, [procedure, *state["misstatements"]], actor=AgentId.A1)
    runner = offline_runner(deps)
    await runner.app.aupdate_state(CONFIG, state | {"procedure_pack": [procedure]}, as_node="fan_in")
    runner.acquire(EID)
    runner.launch(EID, None)
    await wait_idle(runner, EID)
    assert runner.run_error(EID) is None
    payload = {"awaiting": "human", "procedure_id": FIN03, "run_id": "RUN-FIN-03-1", "records": []}
    assert waiting_payloads(await runner.app.aget_state(CONFIG)) == [payload]
    return Started(runner, store, deps.engagement_store)  # type: ignore[arg-type]


async def submit(s: Started, decision: ReviewRecord) -> list[Any]:
    """提交并等后台停下; 返回这次决定的那次提交写入的对象."""
    before = len(s.output_store.commits)
    await s.submit.submit(EID, decision)
    await wait_idle(s.runner, EID)
    assert s.runner.run_error(EID) is None
    return list(s.output_store.commits[before])


def written(commit: list[Any]) -> set[tuple[str, str, int]]:
    return {(type(o).__name__, o.id, o.version) for o in commit if isinstance(o, Versioned)}


def one(commit: list[Any], model: type[Versioned], oid: str) -> Any:
    [found] = [o for o in commit if isinstance(o, model) and o.id == oid]
    return found


def misstatement_v1(oid: str) -> Misstatement:
    return next(m for m in fin03_state()["misstatements"] if m.id == oid)


def decision_rows(s: Started, decision_id: str) -> list[Any]:
    trail = s.runner.deps.trail
    assert isinstance(trail, FakeTrail)
    return [e for e in trail.entries if e.kind is TrailKind.decision and e.idempotency_key == decision_id]


@pytest.fixture
async def waiting() -> Started:
    return await at_fin03()


async def test_confirm_writes_classified_misstatement_in_same_commit(waiting: Started) -> None:
    """验收 1: confirm D-C1 (XJ499 的任务): 同一提交写判断 v2 confirmed, M-D-XJ499 v2 (按判断上的分类载荷: factual,
    无舞弊迹象, 本期, 无审计目标, 作分类的判断为它, 仍为 identified) 与任务 v2 decided; FIN-03 另有待决任务, 不出新版本;
    1 行 decision 留痕."""
    commit = await submit(waiting, record(XJ499_TASK, id="D-C1"))

    judgment = f"J-FIN-03-1-{XJ499}"
    assert written(commit) == {
        ("ReviewRecord", "D-C1", 1),
        ("Judgment", judgment, 2),
        ("Misstatement", XJ499, 2),
        ("ReviewTask", XJ499_TASK, 2),
    }
    assert one(commit, Judgment, judgment).review_status is ReviewStatus.confirmed
    assert one(commit, Misstatement, XJ499) == classified(misstatement_v1(XJ499), **FACTUAL)
    assert one(commit, Misstatement, XJ499).status is MisstatementStatus.identified
    assert len(decision_rows(waiting, "D-C1")) == 1


async def test_override_takes_reviewers_update(waiting: Started) -> None:
    """验收 2: override CSH02 的判断须带人给的载荷 (缺了 MISSING_FIELD, 不写); 带了: 人工判断 -H1 (confirmed, L1, 载荷
    取人给的), 原判断 v2 overridden 并记 superseded_by, M-D-CSH02-091 v2 按人给的载荷 (judgmental, 作分类的判断为 -H1;
    载荷没写来源与审计目标, 不改)."""
    override = record(CSH02_TASK, Decision.override, id="D-O", override_conclusion="应计利息依赖银行的计息估计")
    await assert_rejected(waiting, override, "MISSING_FIELD", "misstatement_update")

    commit = await submit(waiting, override.model_copy(update={"misstatement_update": JUDGMENTAL}))

    human_id = f"J-FIN-03-1-{CSH02}-H1"
    human = one(commit, Judgment, human_id)
    assert (human.review_status, human.autonomy_level, human.misstatement_update) == (
        ReviewStatus.confirmed,
        AutonomyLevel.L1,
        JUDGMENTAL,
    )
    original = one(commit, Judgment, f"J-FIN-03-1-{CSH02}")
    assert (original.version, original.review_status, original.superseded_by) == (2, ReviewStatus.overridden, human_id)
    expected = classified(misstatement_v1(CSH02), human_id, type=MisstatementType.judgmental, fraud_indicative=False)
    assert one(commit, Misstatement, CSH02) == expected


@pytest.mark.parametrize(
    ("update", "code", "detail"),
    [
        (JUDGMENTAL.model_copy(update={"base_version": 2}), "VERSION_MISMATCH", "当前为第 1 版"),
        (
            MisstatementUpdate(misstatement_id=CSH02, base_version=1, kind="verification", needs_recommunication=True),
            "INVALID_VALUE",
            "classification, 不是 verification",
        ),
        (JUDGMENTAL.model_copy(update={"misstatement_id": XJ499}), "INVALID_VALUE", f"本任务的错报 {CSH02}"),
    ],
    ids=["base_version", "kind", "other_misstatement"],
)
async def test_reviewers_update_checked_before_writing(
    waiting: Started, update: MisstatementUpdate, code: str, detail: str
) -> None:
    """验收 3: 人给的载荷依据的不是错报当前版本 → VERSION_MISMATCH; 类别与模板 misstatement_update_kind 不符, 或不是本
    任务的错报 → INVALID_VALUE; 都不写."""
    override = record(CSH02_TASK, Decision.override, id="D-O", override_conclusion="改判", misstatement_update=update)
    await assert_rejected(waiting, override, code, detail)


async def test_confirm_rejected_when_misstatement_moved_on(waiting: Started) -> None:
    """验收 3: 判断之后错报已出新版本 (这里直接写进 Store), 判断上的载荷依据的是旧版本: confirm → VERSION_MISMATCH,
    不写."""
    store = waiting.output_store
    moved = misstatement_v1(XJ499).model_copy(update={"version": 2})
    async with store.transaction(EID):
        await store.put(EID, [moved], actor=AgentId.A1)
    await assert_rejected(waiting, record(XJ499_TASK, id="D-C1"), "VERSION_MISMATCH", f"{XJ499} 当前为第 2 版")


async def test_misstatement_on_classification_applies_judgments_update(waiting: Started) -> None:
    """验收 4: FIN-03 的 normal 任务允许 {confirm, override, misstatement}. 对 XJ499 认定错报 D-C2 (与 M-D-XJ499 相同的
    重复情形, 用户已知): 同一提交写判断 v2 confirmed, M-D-XJ499 v2 (按判断上的载荷, 同 confirm), M-D-C2 v1 identified
    (来源为该判断, 未分类), 任务 v2 decided. 再带人给的载荷 → FIELD_NOT_ALLOWED (载荷取判断上的)."""
    task = await waiting.output_store.get(EID, ReviewTask, XJ499_TASK)
    assert task is not None
    assert waiting.runner.deps.queue.allowed(task) == {Decision.confirm, Decision.override, Decision.misstatement}
    decision = record(XJ499_TASK, Decision.misstatement, id="D-C2", misstatement_draft=DUPLICATE)
    update = MisstatementUpdate(misstatement_id=XJ499, base_version=1, kind="classification", type="judgmental")
    await assert_rejected(
        waiting, decision.model_copy(update={"misstatement_update": update}), "FIELD_NOT_ALLOWED", "misstatement_update"
    )

    commit = await submit(waiting, decision)

    judgment = f"J-FIN-03-1-{XJ499}"
    assert written(commit) == {
        ("ReviewRecord", "D-C2", 1),
        ("Judgment", judgment, 2),
        ("Misstatement", XJ499, 2),
        ("Misstatement", "M-D-C2", 1),
        ("ReviewTask", XJ499_TASK, 2),
    }
    assert one(commit, Misstatement, XJ499) == classified(misstatement_v1(XJ499), **FACTUAL)
    new = one(commit, Misstatement, "M-D-C2")
    assert (new.status, new.judgment_id, new.classification_judgment_id) == (
        MisstatementStatus.identified,
        judgment,
        None,
    )


async def test_manual_result_on_failed_classification_takes_reviewers_update() -> None:
    """验收 6: 分类模型对 XJ499 连续 3 次结构失败, 其 failed 任务提交 manual_result: 缺载荷 → MISSING_FIELD; 带了:
    建人工判断 -H1 (带人给的载荷), M-D-XJ499 v2 的作分类的判断为它; 节点读回后仍等 CSH02 的任务."""
    s = await at_fin03(ScriptedLLM({"XJ-499": ["not json"] * 3, "BA-091": [fixture_raw("BA-091")]}))
    manual = record(FAILED_TASK, Decision.manual_result, id="D-H", manual_result=ManualResult(conclusion="人工分类"))
    await assert_rejected(s, manual, "MISSING_FIELD", "misstatement_update")

    update = MisstatementUpdate(misstatement_id=XJ499, base_version=1, kind="classification", **FACTUAL)
    commit = await submit(s, manual.model_copy(update={"misstatement_update": update}))

    human_id = f"J-FIN-03-1-{XJ499}-H1"
    assert one(commit, Judgment, human_id).misstatement_update == update
    assert one(commit, Misstatement, XJ499) == classified(misstatement_v1(XJ499), human_id, **FACTUAL)
    payload = {"awaiting": "human", "procedure_id": FIN03, "run_id": "RUN-FIN-03-1", "records": ["D-H"]}
    assert waiting_payloads(await s.runner.app.aget_state(CONFIG)) == [payload]


async def test_both_confirmed_node_replays_latest_misstatements(waiting: Started) -> None:
    """验收 9: 两条都确认后节点结束, 走到 FIN-01 闸门前. 节点每次恢复都从头执行, RUN-FIN-03-1 已是终态, 按 outputs 从
    Store 重放 (不再调大模型, 全程 2 行 llm_call), 取回两条错报的最新版本: 状态中为 v2."""
    await submit(waiting, record(XJ499_TASK, id="D-C1"))
    await submit(waiting, record(CSH02_TASK, id="D-C3"))

    snapshot = await waiting.runner.app.aget_state(CONFIG)
    assert snapshot.next == ("gate_fin",)
    assert len(llm_calls(waiting.runner.deps.trail)) == 2  # type: ignore[arg-type]
    assert sorted((m.id, m.version, m.classification_judgment_id) for m in snapshot.values["misstatements"]) == [
        (CSH02, 2, f"J-FIN-03-1-{CSH02}"),
        (XJ499, 2, f"J-FIN-03-1-{XJ499}"),
    ]


async def test_confirmed_fraud_sample_flagged_at_fin01() -> None:
    """R158 验收 6 后半句 (协调者在 R159 记的用例): 模型给 XJ-499 有舞弊迹象与理由, 确认该判断: 同一提交写出的
    M-D-XJ499 v2 带舞弊标记与理由; 两条都确认后在 FIN-01 闸门材料 CompletionMaterial.misstatements 中可见, 材料照常
    生成, 流程照常走到闸门, 不阻断 (汇总表照 R158 的用例给定)."""
    s = await at_fin03(fraud_llm())
    commit = await submit(s, record(XJ499_TASK, id="D-C1"))
    flagged = one(commit, Misstatement, XJ499)
    assert (flagged.version, flagged.fraud_indicative, flagged.fraud_reason) == (2, True, FRAUD_REASON)
    await submit(s, record(CSH02_TASK, id="D-C3"))

    snapshot = await s.runner.app.aget_state(CONFIG)
    assert snapshot.next == ("gate_fin",)
    values = snapshot.values
    schedule = UncorrectedSchedule(
        id="SCHEDULE",
        items=[],
        excluded_trivial_ids=[],
        total_cents=0,
        materiality_cents=32431,
        candidate_opinion=OpinionType.unqualified,
    )
    _, material = await fin01_view(s.runner.deps, EID, values | {"uncorrected_schedule": schedule})
    assert [(m.id, m.fraud_indicative, m.fraud_reason) for m in material.misstatements] == [  # type: ignore[union-attr]
        (XJ499, True, FRAUD_REASON),
        (CSH02, False, None),
    ]


async def test_inconsistent_verification_update_rejected(tmp_path: Path) -> None:
    """人给的验证载荷按它写出的新版本不合格 (已更正额加剩余额不等于错报金额) → INVALID_VALUE, 不写. FIN-09 尚未落地,
    用载荷类别改为 verification 的 FIN-03 (程序库副本) 代它."""
    root = tmp_path / "knowledge"
    shutil.copytree(get_settings().KNOWLEDGE_ROOT, root)
    path = root / "procedures" / f"{FIN03}.yaml"
    yaml = path.read_text(encoding="utf-8")
    path.write_text(yaml.replace("misstatement_update_kind: classification", "misstatement_update_kind: verification"))
    s = await at_fin03(library=ProcedureLibrary(root, FEATURES))
    update = MisstatementUpdate(
        misstatement_id=CSH02,
        base_version=1,
        kind="verification",
        status="corrected",
        corrected_cents=100,
        residual_cents=0,
    )
    override = record(CSH02_TASK, Decision.override, id="D-O", override_conclusion="已更正", misstatement_update=update)
    await assert_rejected(s, override, "INVALID_VALUE", "已更正额加剩余额须等于错报金额")


# ── 验收 7: 验证载荷 (FIN-09 的判断, R164 起) 的效果, 纯函数 ──

V02 = "X-FIN-09-1-V02-1"  # 分录与错报对不上的例外
VERIFYING = Misstatement(
    id=XJ499,
    version=4,
    judgment_id="J-GEN-02-1-XJ-499",
    exception_id=None,
    review_record_id="D-XJ499",
    type=MisstatementType.factual,
    account="Office equipment (710)",
    assertion=Assertion.Existence,
    amount_cents=75000,
    direction=Direction.overstated,
    account_code="710",
    status=MisstatementStatus.verifying,
    communicated_at=NOW,
    correction_notice_id="CN-1",
)


def verification(**fields: Any) -> MisstatementUpdate:
    return MisstatementUpdate(misstatement_id=XJ499, base_version=4, kind="verification", **fields)


def fin09_judgment(update: MisstatementUpdate, exception_ids: list[str]) -> Judgment:
    return Judgment(
        id=f"J-FIN-09-1-{XJ499}",
        procedure_id="FIN-09",
        run_id="RUN-FIN-09-1",
        subject_id=XJ499,
        conclusion="更正分录核对结果",
        confidence=0.86,
        confidence_parts={},
        reasoning_chain=[],
        evidence=[],
        counter_argument="",
        proposed_action=ProposedAction.accept,
        misstatement_update=update,
        exception_ids=exception_ids,
        autonomy_level=AutonomyLevel.L2,
        performed_by="U-1",
        created_at=NOW,
    )


def v02(disposition: ExceptionDisposition = ExceptionDisposition.open) -> AuditException:
    return AuditException(
        id=V02,
        version=1 if disposition is ExceptionDisposition.open else 2,
        procedure_id="FIN-09",
        run_id="RUN-FIN-09-1",
        rule_id="V02",
        severity="medium",
        source_ids=["XJ-499"],
        amount_cents=75000,
        evidence=[],
        disposition=disposition,
    )


def fin09_task(target: Versioned, kind: TaskKind) -> ReviewTask:
    is_judgment = isinstance(target, Judgment)
    return ReviewTask(
        id=f"T-{target.id}-v{target.version}",
        kind=kind,
        procedure_id="FIN-09",
        target_type=TargetType.Judgment if is_judgment else TargetType.AuditException,
        target_id=target.id,
        target_version=target.version,
        run_id="RUN-FIN-09-1",
        subject_id=None if is_judgment else XJ499,
        created_at=NOW,
    )


RECOMMUNICATE = verification(status="communicated", needs_recommunication=True)


@pytest.mark.parametrize(
    ("update", "exceptions", "changes"),
    [
        (
            verification(status="corrected", adjustment_entry_nos=["AJE-X01"], corrected_cents=75000, residual_cents=0),
            [],
            {"adjustment_entry_nos": ["AJE-X01"], "corrected_cents": 75000, "residual_cents": 0},
        ),
        (
            verification(
                status="partially_corrected",
                adjustment_entry_nos=["AJE-X01"],
                corrected_cents=50000,
                residual_cents=25000,
            ),
            [],
            {"adjustment_entry_nos": ["AJE-X01"], "corrected_cents": 50000, "residual_cents": 25000},
        ),
        (verification(status="communicated"), [V02], {"needs_recommunication": True}),
    ],
    ids=["corrected", "partially_corrected", "communicated"],
)
def test_confirmed_verification_writes_misstatement(
    update: MisstatementUpdate, exceptions: list[str], changes: dict[str, Any]
) -> None:
    """验收 7: 确认 FIN-09 的判断: 错报按验证载荷出新版本; corrected 与 partially_corrected 记作验证的判断与更正分录,
    已更正与剩余金额; communicated (对不上) 置 needs_recommunication (载荷不写也置), 不记作验证的判断; 判断解释的 V02
    例外照常归因."""
    judgment = fin09_judgment(update, exceptions)
    found = [v02()] if exceptions else []
    effects = review_effects(
        record(f"T-{judgment.id}-v1", id="D-V"),
        fin09_task(judgment, TaskKind.normal),
        judgment,
        found,
        set(),
        misstatements={XJ499: VERIFYING},
    )

    status = update.status
    verified = judgment.id if status in ("corrected", "partially_corrected") else None
    expected = VERIFYING.model_copy(
        update={"version": 5, "status": status, "verified_by_judgment_id": verified, **changes}
    )
    assert [o for o in effects if isinstance(o, Misstatement)] == [expected]
    attributed = [o for o in effects if isinstance(o, AuditException)]
    assert [(x.id, x.attributed_by) for x in attributed] == [(V02, judgment.id) for _ in exceptions]


@pytest.mark.parametrize("decision", [Decision.override, Decision.misstatement])
def test_unattributed_v02_takes_reviewers_verification(decision: Decision) -> None:
    """验收 7: FIN-09 的 unattributed 任务 (目标为 V02 例外) 的 override 与 misstatement 须带人给的载荷 (缺了
    MISSING_FIELD); 带了: 人工判断 -H1 带该载荷, 原错报按载荷出新版本 (communicated, 须重新沟通); misstatement 另建
    M-{决定 ID} identified, 来源为该人工判断."""
    exception = v02(ExceptionDisposition.unattributed)
    task = fin09_task(exception, TaskKind.unattributed)
    fields: dict[str, Any] = {"override_conclusion": "分录冲回的科目不对"} if decision is Decision.override else {}
    if decision is Decision.misstatement:
        fields["misstatement_draft"] = DUPLICATE.model_copy(update={"amount_cents": 25000})
    bare = record(task.id, decision, id="D-U", **fields)
    with pytest.raises(DecisionRejected) as e:
        check_review_fields(bare, task.kind, "verification")
    assert (e.value.code, e.value.message) == ("MISSING_FIELD", "misstatement_update")

    decided = bare.model_copy(update={"misstatement_update": RECOMMUNICATE})
    check_review_fields(decided, task.kind, "verification")
    effects = review_effects(decided, task, exception, [], set(), lifecycle=True, misstatements={XJ499: VERIFYING})

    human_id = f"J-FIN-09-1-{XJ499}-H1"
    [human] = [o for o in effects if isinstance(o, Judgment)]
    assert (human.id, human.misstatement_update) == (human_id, RECOMMUNICATE)
    misstatements = {m.id: m for m in effects if isinstance(m, Misstatement)}
    assert misstatements.pop(XJ499) == VERIFYING.model_copy(
        update={"version": 5, "status": "communicated", "needs_recommunication": True}
    )
    if decision is Decision.misstatement:
        new = misstatements.pop("M-D-U")
        assert (new.status, new.judgment_id) == (MisstatementStatus.identified, human_id)
    assert misstatements == {}


@pytest.mark.parametrize(
    ("resolution", "fields", "updated"),
    [
        (DisputeResolution.a6_upheld, {}, [5]),
        (
            DisputeResolution.neither,
            {"neither_action": NeitherAction.human_conclusion, "override_conclusion": "两方都不采纳"},
            [],
        ),
    ],
    ids=["a6_upheld", "neither"],
)
def test_upheld_dispute_applies_judgments_update(
    resolution: DisputeResolution, fields: dict[str, Any], updated: list[int]
) -> None:
    """仲裁 a6_upheld 同 confirm, 按判断上的载荷写出错报新版本; neither 建的人工判断不带载荷 (仲裁不收载荷), 错报
    不写."""
    judgment = fin09_judgment(RECOMMUNICATE, [])
    task = fin09_task(judgment, TaskKind.dispute)
    decision = record(task.id, Decision.dispute_resolution, id="D-A", dispute_resolution=resolution, **fields)
    effects = review_effects(decision, task, judgment, [], set(), misstatements={XJ499: VERIFYING})
    assert [m.version for m in effects if isinstance(m, Misstatement)] == updated


def test_update_needs_its_misstatement() -> None:
    """misstatement_updates 只认置 confirmed 的判断 (待复核的不写); 载荷所指的错报不在给定的错报中抛 LookupError."""
    pending = fin09_judgment(RECOMMUNICATE, [])
    assert misstatement_updates([pending], {XJ499: VERIFYING}) == []
    confirmed = pending.model_copy(update={"version": 2, "review_status": ReviewStatus.confirmed})
    with pytest.raises(LookupError, match=XJ499):
        misstatement_updates([confirmed], {})
