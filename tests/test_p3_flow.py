"""需求 R160: 完成阶段骨架 (功能 misstatement 打开): 错报分诊入口, 节点与路由.

主图放在 P2 结清处: 检查点以 fan_in 身份写入, 程序包为已完成的 REV-02 与完成阶段的程序 (FIN-03, FIN-04, FIN-01 与
测试程序库副本里的桩), 状态与 Store 有一条 identified 的错报 M-D-REV02-0612 (Numbat 跨期错报, 认定自 REV-02 对
SI-2026-0612 的判断, 同 test_flow 的 D-REV02-0612). 脚本 A8 执行 FIN-03: 每条未分类的错报出一条带分类载荷的判断与
normal 任务.
桩 FIN-08 (communicate) 把已分类的错报登记为管理层拒绝更正 (uncorrected, refused): R166 起 FIN-04 只累计可累计状态
的错报, 桩让错报在 summarize 之前到达这一状态 (R160 的 R166 注). 程序库没有 FIN-09, PLN-19, FIN-02, 它们的节点为
空操作; FIN-15 的桩只在要它的用例里加.
"""

import shutil
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from backend.agents.base import AgentDeps, RunContext
from backend.agents.workpaper import unclassified
from backend.contracts.models import (
    AgentId,
    AutonomyLevel,
    Decision,
    GateVerdict,
    Judgment,
    Misstatement,
    MisstatementStatus,
    MisstatementUpdate,
    Phase,
    Procedure,
    ProcedureRun,
    ProcedureStatus,
    ProposedAction,
    ReviewTask,
    TaskKind,
    TaskStatus,
    TrailKind,
    UncorrectedSchedule,
)
from backend.contracts.state import StatePatch, initial_state
from backend.core.config import get_settings
from backend.orchestration.decisions import GraphDecisionSubmit
from backend.orchestration.runner import GraphRunner, waiting_payloads
from backend.services.features import BASELINE, MISSTATEMENT
from backend.services.procedure_library import ProcedureLibrary
from backend.services.staleness import current_results
from tests.fakes import (
    ENGAGEMENT,
    NOW,
    NUMBAT_CUTOFF_DRAFT,
    NUMBAT_PROFILE,
    EngagementWrite,
    FakeEngagementStore,
    FakeTrail,
    ScriptA8,
    finished,
    make_procedure,
    make_runner,
    wait_idle,
)
from tests.test_decisions import Started
from tests.test_fin_gate import fin_decision
from tests.test_review_decisions import record

EID = ENGAGEMENT.id
CONFIG = GraphRunner.config(EID)
FEATURES = BASELINE | {MISSTATEMENT}
CUTOFF = Misstatement(
    id="M-D-REV02-0612",
    judgment_id="J-REV-02-1-SI-2026-0612",
    exception_id=None,
    review_record_id="D-REV02-0612",
    **NUMBAT_CUTOFF_DRAFT.model_dump(),
)
TASK = f"T-J-FIN-03-1-{CUTOFF.id}-v1"
# 桩程序: 完成阶段, A8 所有 (可写判断, 复核任务与错报), 功能 misstatement; outputs 一行按各桩写的字段
STUB = "phase: P3_Completion\ncycle: Completion\nowner: A8\nfeature: misstatement\nengine: rule\nautonomy_level: L3\n"
STUB_OUTPUTS = {
    "FIN-08": "[misstatements]",
    "FIN-15": "[judgments, review_tasks]",
}
Script = Callable[[AgentDeps, RunContext], StatePatch]


def classify(deps: AgentDeps, ctx: RunContext) -> StatePatch:
    """脚本 FIN-03: 每条未分类的 identified 错报一条判断 (带分类载荷, 类型同错报) 与 normal 任务."""
    run = finished(ctx.run)
    judgments = [
        Judgment(
            id=f"J-FIN-03-{run.run_no}-{m.id}",
            procedure_id="FIN-03",
            run_id=run.id,
            subject_id=m.id,
            conclusion="本期事实错报, 无舞弊迹象",
            confidence=0.85,
            confidence_parts={},
            reasoning_chain=[],
            evidence=[],
            counter_argument="",
            proposed_action=ProposedAction.accept,
            misstatement_update=MisstatementUpdate(
                misstatement_id=m.id,
                base_version=m.version,
                kind="classification",
                type=m.type,
                fraud_indicative=False,
                origin="current_period",
            ),
            autonomy_level=AutonomyLevel.L2,
            performed_by=ENGAGEMENT.owner_id,
            created_at=NOW,
        )
        for m in unclassified(ctx.state)
    ]
    tasks = [deps.queue.make_task(TaskKind.normal, j, "FIN-03", run.id, NOW) for j in judgments]
    patch: StatePatch = {"procedure_runs": [run]}
    return patch | {"judgments": judgments, "review_tasks": tasks} if judgments else patch


def refuse(deps: AgentDeps, ctx: RunContext) -> StatePatch:
    """桩 FIN-08: 已分类的 identified 错报登记为管理层拒绝更正 (uncorrected, refused, 带沟通与回复)."""
    refused = [
        Misstatement.model_validate(
            m.model_dump()
            | {
                "version": m.version + 1,
                "status": MisstatementStatus.uncorrected,
                "uncorrected_basis": "refused",
                "communicated_at": NOW,
                "communicated_to": "Director",
                "responded_at": NOW,
                "management_response": "Management declines to adjust.",
            }
        )
        for m in current_results(ctx.state).misstatements
        if m.status is MisstatementStatus.identified and m.classification_judgment_id is not None
    ]
    patch: StatePatch = {"procedure_runs": [finished(ctx.run)]}
    return patch | {"misstatements": refused} if refused else patch


def proposes_misstatement(deps: AgentDeps, ctx: RunContext) -> StatePatch:
    """桩 FIN-15: 第 1 次执行出一条建议认定错报的判断 (带草稿) 与 normal 任务, 之后什么都不出."""
    run = finished(ctx.run)
    if run.run_no > 1:
        return {"procedure_runs": [run]}
    judgment = Judgment(
        id="J-FIN-15-1-826",
        procedure_id="FIN-15",
        run_id=run.id,
        subject_id="826",
        conclusion="期末应计工资少计",
        confidence=0.8,
        confidence_parts={},
        reasoning_chain=[],
        evidence=[],
        counter_argument="",
        proposed_action=ProposedAction.misstatement,
        misstatement_draft=NUMBAT_CUTOFF_DRAFT.model_copy(update={"account": "Wages payable", "amount_cents": 426120}),
        autonomy_level=AutonomyLevel.L2,
        performed_by=ENGAGEMENT.owner_id,
        created_at=NOW,
    )
    task = deps.queue.make_task(TaskKind.normal, judgment, "FIN-15", run.id, NOW)
    return {"procedure_runs": [run], "judgments": [judgment], "review_tasks": [task]}


SCRIPTS: dict[str, Script] = {"FIN-08": refuse, "FIN-15": proposes_misstatement}


def stub_library(root: Path, stubs: Sequence[str]) -> ProcedureLibrary:
    """knowledge 目录的副本另加桩程序 stubs (FIN-08, FIN-15 中的), 按 FEATURES."""
    shutil.copytree(get_settings().KNOWLEDGE_ROOT, root)
    for pid in stubs:
        text = f"id: {pid}\nname: 桩 {pid}\n{STUB}outputs: {STUB_OUTPUTS[pid]}\nrules: []\n"
        (root / "procedures" / f"{pid}.yaml").write_text(text, encoding="utf-8")
    return ProcedureLibrary(root, FEATURES)


class Case(Started):
    """停在 P2 结清处后, 后台跑到第一次停下. runs 为各桩与 FIN-03 收到的执行 (procedure_id, run_no)."""

    runs: list[tuple[str, int]]

    async def path(self) -> list[str]:
        """从写入检查点起依次经过的图节点 (相邻重复的合并: 节点内每次等待各存一个检查点)."""
        history = [s async for s in self.runner.app.aget_state_history(CONFIG)]
        steps = [s.next[0] for s in reversed(history) if s.next]
        return [n for i, n in enumerate(steps) if i == 0 or steps[i - 1] != n]

    def latest(self, model: type[Any], oid: str) -> Any:
        return max((r for r in self.output_store.rows if isinstance(r, model) and r.id == oid), key=lambda r: r.version)

    async def submit_and_wait(self, decision: Any) -> None:
        await self.submit.submit(EID, decision)
        await wait_idle(self.runner, EID)
        assert self.runner.run_error(EID) is None

    async def pending(self) -> list[ReviewTask]:
        return await self.output_store.latest(EID, ReviewTask, status=TaskStatus.pending)

    def phase_runs(self) -> set[str]:
        """写阶段的各次提交里的执行记录: 节点每次恢复都从头执行, 重放第 1 次分诊时同值再写, 别的执行不写阶段."""
        return {
            o.id
            for c in self.output_store.commits
            if any(isinstance(o, EngagementWrite) and "phase" in o.changes for o in c)
            for o in c
            if isinstance(o, ProcedureRun)
        }


async def at_p3(
    tmp_path: Path, stubs: Sequence[str] = ("FIN-08",), misstatements: Sequence[Misstatement] = (CUTOFF,)
) -> Case:
    """见模块 docstring."""
    runs: list[tuple[str, int]] = []

    def agents(deps: AgentDeps) -> dict[str, Any]:
        def scripted(pid: str, script: Script) -> ScriptA8:
            def run(ctx: RunContext) -> StatePatch:
                runs.append((pid, ctx.run.run_no))
                return script(deps, ctx)

            return ScriptA8(deps, run)

        return {pid: scripted(pid, script) for pid, script in [("FIN-03", classify), *SCRIPTS.items()]}

    engagement_store = FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT])
    runner, store = make_runner(
        engagement_store=engagement_store,
        agents=agents,
        library=stub_library(tmp_path / "knowledge", stubs),
        features=FEATURES,
    )
    pack = [
        make_procedure("REV-02", Phase.P2_Executing, status=ProcedureStatus.completed),
        *(make_procedure(pid, Phase.P3_Completion) for pid in ("FIN-03", "FIN-04", "FIN-01", *stubs)),
    ]
    async with store.transaction(EID):
        await store.put(EID, [*pack, *misstatements], actor=AgentId.A1)
    state = initial_state(ENGAGEMENT, pack, NUMBAT_PROFILE.version) | {
        "phase": Phase.P2_Executing,
        "misstatements": list(misstatements),
        "materiality_cents": 12000000,
        "performance_materiality_cents": 9000000,
        "clearly_trivial_cents": 600000,
    }
    await runner.app.aupdate_state(CONFIG, state, as_node="fan_in")
    case = Case(runner, store, engagement_store)
    case.runs = runs
    runner.acquire(EID)
    runner.launch(EID, None)
    await wait_idle(runner, EID)
    assert runner.run_error(EID) is None
    return case


def fin03_payload(run_no: int = 1, records: Sequence[str] = ()) -> dict[str, Any]:
    return {"awaiting": "human", "procedure_id": "FIN-03", "run_id": f"RUN-FIN-03-{run_no}", "records": list(records)}


# 完成阶段走完一轮 (不回流) 经过的节点
ONE_ROUND = [
    "communicate",
    "verify_correction",
    "p3_reassess",
    "summarize",
    "final_review",
    "completion",
    "gate_fin",
]


async def test_p2_settled_enters_triage_then_runs_one_round_to_gate_fin(tmp_path: Path) -> None:
    """验收 2, 7: P2 结清后进 misstatement_triage, 阶段 P3_Completion 与 RUN-FIN-03-1 在同一提交写入, 阶段迁移留痕记在
    FIN-03 下; 停在节点内等 FIN-03 的复核. confirm 后依次经 communicate (桩 FIN-08 登记拒绝), verify_correction,
    p3_reassess (空操作), summarize (FIN-04 出 SCHEDULE v1, 不写阶段), final_review, completion (空操作), 停在 FIN-01
    闸门. FIN-01 驳回回 summarize: FIN-04 出 SCHEDULE v2, 再经 final_review, completion 回到闸门; 不重跑 FIN-03 与
    FIN-08."""
    s = await at_p3(tmp_path)
    assert waiting_payloads(await s.runner.app.aget_state(CONFIG)) == [fin03_payload()]
    [commit] = [c for c in s.output_store.commits if any(getattr(o, "id", None) == "RUN-FIN-03-1" for o in c)]
    assert EngagementWrite("update", EID, {"phase": Phase.P3_Completion}) in commit
    trail = s.runner.deps.trail
    assert isinstance(trail, FakeTrail)
    phases = [e for e in trail.entries if e.kind is TrailKind.transition and e.payload["object"] == "engagement"]
    assert [(e.payload["from"], e.payload["to"], e.procedure_id) for e in phases] == [
        ("P2_Executing", "P3_Completion", "FIN-03")
    ]

    await s.submit_and_wait(record(TASK, id="D-C1"))

    assert await s.path() == ["misstatement_triage", *ONE_ROUND]
    assert s.runs == [("FIN-03", 1), ("FIN-08", 1)]
    schedule = s.latest(UncorrectedSchedule, "SCHEDULE")
    assert (schedule.version, [i.misstatement_id for i in schedule.items]) == (1, [CUTOFF.id])
    assert s.phase_runs() == {"RUN-FIN-03-1"}  # 只随第 1 次分诊写阶段

    await s.submit_and_wait(fin_decision(GateVerdict.reject))

    assert (await s.path())[-4:] == ["summarize", "final_review", "completion", "gate_fin"]
    assert s.latest(UncorrectedSchedule, "SCHEDULE").version == 2
    runs = {r.id for r in await s.output_store.latest(EID, ProcedureRun)}
    assert {"RUN-FIN-04-2", "RUN-FIN-03-1", "RUN-FIN-08-1"} <= runs and not {"RUN-FIN-03-2", "RUN-FIN-08-2"} & runs


async def test_misstatements_found_late_loop_back_to_triage(tmp_path: Path) -> None:
    """验收 4: 桩 FIN-15 (final_review) 第 1 次出建议认定错报的判断, 审计师认定后回 misstatement_triage: 不再写阶段,
    FIN-03 以 RUN-FIN-03-2 只判新错报; 这一轮 summarize 以 RUN-FIN-04-2 出 SCHEDULE v2, FIN-15 第 2 次没有新错报, 经
    completion 到 FIN-01 闸门. 每轮 summarize 一次."""
    s = await at_p3(tmp_path, stubs=("FIN-08", "FIN-15"))
    await s.submit_and_wait(record(TASK, id="D-C1"))
    [proposal] = await s.pending()
    assert proposal.procedure_id == "FIN-15"
    found = NUMBAT_CUTOFF_DRAFT.model_copy(update={"account": "Wages payable", "amount_cents": 426120})
    await s.submit_and_wait(record(proposal.id, Decision.misstatement, id="D-F15", misstatement_draft=found))
    [second] = await s.pending()
    assert second.id == "T-J-FIN-03-2-M-D-F15-v1"  # 只判新错报
    await s.submit_and_wait(record(second.id, id="D-C2"))

    loop = ["communicate", "verify_correction", "p3_reassess", "summarize", "final_review"]
    assert await s.path() == ["misstatement_triage", *loop, "misstatement_triage", *ONE_ROUND]  # 回流一次
    assert [r for r in s.runs if r[0] in ("FIN-03", "FIN-15")] == [
        ("FIN-03", 1),
        ("FIN-15", 1),
        ("FIN-03", 2),
        ("FIN-15", 2),
    ]
    assert s.latest(UncorrectedSchedule, "SCHEDULE").version == 2
    assert s.phase_runs() == {"RUN-FIN-03-1"}


async def test_completion_does_not_write_phase_when_fin02_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """功能打开时 completion 不写阶段 (第 1 轮审核 R160-03): 程序库副本加桩 FIN-02, completion 执行它, 走完一轮到
    FIN-01 闸门; 写阶段的提交里只有 RUN-FIN-03-1, RUN-FIN-02-1 的提交不写阶段."""
    monkeypatch.setitem(STUB_OUTPUTS, "FIN-02", "[misstatements]")
    monkeypatch.setitem(SCRIPTS, "FIN-02", lambda deps, ctx: {"procedure_runs": [finished(ctx.run)]})
    s = await at_p3(tmp_path, stubs=("FIN-08", "FIN-02"))
    await s.submit_and_wait(record(TASK, id="D-C1"))

    assert (await s.runner.app.aget_state(CONFIG)).next == ("gate_fin",)
    assert ("FIN-02", 1) in s.runs
    assert s.phase_runs() == {"RUN-FIN-03-1"}


async def test_no_misstatements_runs_through_without_waiting(tmp_path: Path) -> None:
    """验收 6: P2 没有错报: FIN-03 空执行 (RUN-FIN-03-1 成功, 程序 completed, 不等人), 经各节点到 summarize, SCHEDULE
    没有项目, 候选意见 unqualified, 停在 FIN-01 闸门."""
    s = await at_p3(tmp_path, misstatements=())

    assert await s.path() == ["misstatement_triage", *ONE_ROUND]
    assert s.latest(Procedure, "FIN-03").status is ProcedureStatus.completed
    schedule = s.latest(UncorrectedSchedule, "SCHEDULE")
    assert (schedule.items, schedule.candidate_opinion) == ([], "unqualified")


async def test_reviewer_misstatement_on_classification_reclassifies_new_one(tmp_path: Path) -> None:
    """验收 10: 复核人对 FIN-03 的任务认定错报 (草稿与原错报相同, T12 不限制): 原错报按载荷出 v2 (已分类), 另有
    M-D-M1 v1 identified 未分类; 回 misstatement_triage (不再写阶段), FIN-03 以 RUN-FIN-03-2 只判它, 确认后走完一轮
    到 FIN-01 闸门; 到 summarize 时没有未分类的 identified 错报."""
    s = await at_p3(tmp_path)
    await s.submit_and_wait(record(TASK, Decision.misstatement, id="D-M1", misstatement_draft=NUMBAT_CUTOFF_DRAFT))
    assert s.latest(Misstatement, CUTOFF.id).classification_judgment_id == f"J-FIN-03-1-{CUTOFF.id}"
    [task] = await s.pending()
    assert task.id == "T-J-FIN-03-2-M-D-M1-v1"
    await s.submit_and_wait(record(task.id, id="D-C2"))

    assert await s.path() == ["misstatement_triage", *ONE_ROUND]  # 回流时节点名相同, 相邻合并
    assert [r for r in s.runs if r[0] == "FIN-03"] == [("FIN-03", 1), ("FIN-03", 2)]
    history = [h async for h in s.runner.app.aget_state_history(CONFIG)]
    at_summarize = next(h for h in history if h.next == ("summarize",))
    assert unclassified(at_summarize.values) == []
    assert s.phase_runs() == {"RUN-FIN-03-1"}


async def test_decision_at_triage_recovered_after_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """验收 8: 停在 misstatement_triage 时, FIN-03 的复核决定写进 Store 而后台没继续 (检查点未跟上, 进程随即退出);
    新 runner 启动时 recover 按节点表 (功能 misstatement 的) 把决定交回分诊节点, 继续走完一轮到 FIN-01 闸门, 结果同
    验收 2."""
    s = await at_p3(tmp_path)
    with monkeypatch.context() as m:
        m.setattr(s.runner, "launch", lambda eid, graph_input: s.runner.release(eid))
        await s.submit.submit(EID, record(TASK, id="D-C1"))
    assert waiting_payloads(await s.runner.app.aget_state(CONFIG)) == [fin03_payload()]

    restarted = GraphRunner(s.runner.app, s.runner.deps)
    await restarted.recover()
    await wait_idle(restarted, EID)

    assert restarted.run_error(EID) is None
    assert await s.path() == ["misstatement_triage", *ONE_ROUND]
    assert s.latest(UncorrectedSchedule, "SCHEDULE").version == 1
    submit = GraphDecisionSubmit(restarted.app, restarted.deps, restarted)
    await submit.submit(EID, fin_decision(GateVerdict.reject))  # 恢复后的项目照常收决定
    await wait_idle(restarted, EID)
    assert s.latest(UncorrectedSchedule, "SCHEDULE").version == 2
