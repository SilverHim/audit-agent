"""R87 P2 以外节点的通用等待 (orchestration/waits.py, 接口文档第 7 节恢复机制).

测试程序 TST-J01 放在临时 knowledge 目录 (完成阶段, owner A6, hybrid, L2; 失败任务的 manual_result 一节 owner 改为 A8,
同 FIN-03), 完成节点改为先执行它再执行 FIN-04.
它的脚本 Agent 按轮次出判断与任务: 有待决任务时节点内等人, 决定读回后从 Store 整体并入, 与提交顺序无关;
rerun 仍按 Q17 整组重跑. 另测恢复补做, DecisionSubmit 按载荷拒绝, 重放与载荷登记.
"""

import shutil
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from langgraph.types import Command
from pydantic import BaseModel

from backend.agents.base import AgentDeps, RunContext
from backend.contracts.models import (
    AgentId,
    AuditException,
    AutonomyLevel,
    Decision,
    DisputeResolution,
    ExceptionDisposition,
    GateVerdict,
    Judgment,
    ManualResult,
    Misstatement,
    Phase,
    Procedure,
    ProcedureRun,
    ProcedureStatus,
    ProposedAction,
    ReviewRecord,
    ReviewStatus,
    ReviewTask,
    RunStatus,
    TargetType,
    TaskKind,
    TaskStatus,
    TrailEntry,
    TrailKind,
    Versioned,
)
from backend.contracts.state import StatePatch
from backend.core.config import get_settings
from backend.orchestration import decisions
from backend.orchestration.nodes import NODE_PROCEDURES, GraphNodes
from backend.orchestration.query import GraphStateQuery
from backend.orchestration.runner import GraphRunner, waiting_payloads
from backend.orchestration.waits import WaitKind, accepts, data_payload, human_payload, kind_of, review_payload
from backend.services.features import BASELINE
from backend.services.procedure_library import ProcedureLibrary
from tests.fakes import (
    ENGAGEMENT,
    NOW,
    NUMBAT_PROFILE,
    FakeEngagementStore,
    FakeOutputStore,
    FakeTrail,
    ScriptA6,
    ScriptA8,
    finished,
    make_procedure,
    make_runner,
    numbat_all_p2_agents,
    wait_idle,
)
from tests.test_decisions import Started, acc01_decision
from tests.test_fin_gate import fin_decision
from tests.test_flow import approve_materiality, review_all
from tests.test_rerun import CrashAfterCommit
from tests.test_review_decisions import DRAFT, assert_rejected, record

EID = ENGAGEMENT.id
CONFIG = GraphRunner.config(EID)
COLLAB = BASELINE | {"collab"}
TST = "TST-J01"
TST_YAML = (
    f"id: {TST}\nname: 自测判断\nphase: P3_Completion\ncycle: Completion\nowner: A6\n"
    "engine: hybrid\nautonomy_level: L2\n"
)
TST_OUTPUTS = "outputs: [judgments, exceptions, review_tasks]\n"
# 每轮执行的脚本: run_no → [(对象, 任务种类)]; normal 出判断与 normal 任务, failed 只出 failed 任务, unattributed 只出
# 该对象的规则例外 (第 1 版即 unattributed) 与以它为目标, 带对象的 unattributed 任务 (带规则例外的对象判断失败),
# None 只出判断
Plan = dict[int, list[tuple[str, TaskKind | None]]]
ONE: Plan = {1: [("S1", TaskKind.normal)]}
TWO: Plan = {1: [("S1", TaskKind.normal), ("S2", TaskKind.normal)]}
MIXED: Plan = {1: [("S1", TaskKind.normal), ("S2", TaskKind.failed)], 2: [("S2", None)]}


@pytest.fixture(autouse=True)
def completion_runs_tst(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """完成节点先执行 TST-J01 再执行 FIN-04 (recovery 与 nodes 共用这张表)."""
    monkeypatch.setitem(NODE_PROCEDURES, "completion", (TST, "FIN-04"))
    yield


def tst_library(
    root: Path,
    extra: str = "",
    outputs: str = TST_OUTPUTS,
    features: frozenset[str] | None = None,
    owner: AgentId = AgentId.A6,
) -> ProcedureLibrary:
    """knowledge 目录的副本另加 TST-J01; extra 为追加到其 YAML 的行 (如 gate: FIN-01), outputs 为其产出声明一行;
    features 为程序库按的功能集 (缺省为基线); owner 为模板 owner."""
    shutil.copytree(get_settings().KNOWLEDGE_ROOT, root)
    yaml = TST_YAML.replace("owner: A6", f"owner: {owner.value}")
    (root / "procedures" / f"{TST}.yaml").write_text(yaml + outputs + extra, encoding="utf-8")
    return ProcedureLibrary(root) if features is None else ProcedureLibrary(root, features)


def tst_exception(run: ProcedureRun, subject: str) -> AuditException:
    """TST-J01 规则部分对 subject 的例外, 由该对象的判断引用."""
    return AuditException(
        id=f"X-{TST}-{run.run_no}-R1-{subject}",
        procedure_id=TST,
        run_id=run.id,
        rule_id="R1",
        severity="medium",
        source_ids=[subject],
        evidence=[],
    )


def tst_judgment(run: ProcedureRun, subject: str) -> Judgment:
    return Judgment(
        id=f"J-{TST}-{run.run_no}-{subject}",
        procedure_id=TST,
        run_id=run.id,
        subject_id=subject,
        conclusion="测试结论",
        confidence=0.8,
        confidence_parts={"evidence": 0.8, "rule": 1.0, "llm": 0.7},
        reasoning_chain=[],
        evidence=[],
        counter_argument="测试反方论证",
        proposed_action=ProposedAction.accept,
        autonomy_level=AutonomyLevel.L2,
        performed_by=ENGAGEMENT.owner_id,
        created_at=NOW,
    )


class Case(Started):
    """Numbat 走完 P2 与全部复核后进入完成阶段, 停在 TST-J01 的节点内等待. agent 为 TST-J01 的脚本 Agent,
    seen 为它各次收到的 (run_no, rerun_subject_ids)."""

    agent: ScriptA6
    seen: list[tuple[int, list[str]]]

    def latest(self, model: type[Versioned], oid: str) -> Any:
        return max((r for r in self.output_store.rows if isinstance(r, model) and r.id == oid), key=lambda r: r.version)

    async def waits(self) -> list[Any]:
        return waiting_payloads(await self.runner.app.aget_state(CONFIG))

    async def task(self, subject: str, run_no: int = 1) -> ReviewTask:
        [task] = [
            t
            for t in await self.output_store.latest(EID, ReviewTask, procedure_id=TST)
            if t.run_id == f"RUN-{TST}-{run_no}" and (t.subject_id or t.target_id.rsplit("-", 1)[-1]) == subject
        ]
        return task

    def transitions(self, oid: str) -> list[tuple[str, str]]:
        trail = self.runner.deps.trail
        assert isinstance(trail, FakeTrail)
        return [
            (e.payload["from"], e.payload["to"])
            for e in trail.entries
            if e.kind is TrailKind.transition and e.payload["id"] == oid
        ]

    def decision_payload(self, key: str) -> dict[str, Any]:
        """决定 key 的 decision 留痕的 payload (只有一行)."""
        trail = self.runner.deps.trail
        assert isinstance(trail, FakeTrail)
        [entry] = [e for e in trail.entries if e.kind is TrailKind.decision and e.idempotency_key == key]
        return entry.payload

    def decision_keys(self) -> list[str | None]:
        trail = self.runner.deps.trail
        assert isinstance(trail, FakeTrail)
        return [e.idempotency_key for e in trail.entries if e.kind is TrailKind.decision]


async def at_tst(
    tmp_path: Path,
    plan: Plan,
    output_store: FakeOutputStore | None = None,
    cited: frozenset[str] = frozenset(),
    yaml: str = "",
    explains: str | None = None,
    outputs: str = TST_OUTPUTS,
    features: frozenset[str] | None = None,
    owner: AgentId = AgentId.A6,
) -> Case:
    """见 Case. output_store 可换成会在某次提交后出错的存储 (如 CrashAfterCommit). cited 中的对象另出一条规则例外,
    由其判断引用; explains 为各判断另外解释的已有例外 (别的程序的); yaml, outputs 见 tst_library; owner 为模板 owner
    与脚本 Agent 的身份 (A6 或 A8)."""
    seen: list[tuple[int, list[str]]] = []
    holder: dict[str, ScriptA6] = {}

    def agents(deps: AgentDeps) -> dict[str, Any]:
        def script(ctx: RunContext) -> StatePatch:
            seen.append((ctx.run.run_no, list(ctx.procedure.rerun_subject_ids)))
            run = finished(ctx.run)
            judgments, tasks, exceptions = [], [], []
            for subject, kind in plan.get(ctx.run.run_no, []):
                if kind is TaskKind.failed:
                    tasks.append(deps.queue.make_task(kind, run, TST, run.id, NOW, subject_id=subject))
                    continue
                if kind is TaskKind.unattributed:
                    found = tst_exception(run, subject)
                    exceptions.append(found.model_copy(update={"disposition": ExceptionDisposition.unattributed}))
                    tasks.append(deps.queue.make_task(kind, exceptions[-1], TST, run.id, NOW, subject_id=subject))
                    continue
                judgment = tst_judgment(run, subject)
                if subject in cited:
                    exceptions.append(tst_exception(run, subject))
                    judgment = judgment.model_copy(update={"exception_ids": [exceptions[-1].id]})
                if explains is not None:
                    judgment = judgment.model_copy(update={"exception_ids": [*judgment.exception_ids, explains]})
                judgments.append(judgment)
                if kind is not None:
                    tasks.append(deps.queue.make_task(kind, judgment, TST, run.id, NOW))
            patch: StatePatch = {"procedure_runs": [run], "judgments": judgments, "review_tasks": tasks}
            return {**patch, "exceptions": exceptions} if exceptions else patch

        holder["agent"] = (ScriptA8 if owner is AgentId.A8 else ScriptA6)(deps, script)
        return {**numbat_all_p2_agents(deps), TST: holder["agent"]}

    engagement_store = FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT])
    runner, store = make_runner(
        engagement_store=engagement_store,
        output_store=output_store,
        agents=agents,
        library=tst_library(tmp_path / "knowledge", yaml, outputs, features, owner),
        features=features,
    )
    case = Case(runner, store, engagement_store)
    case.agent, case.seen = holder["agent"], seen
    await runner.start(EID)
    await wait_idle(runner, EID)
    await case.submit.submit(EID, acc01_decision(GateVerdict.approve, id="D-ACC"))
    await wait_idle(runner, EID)
    await approve_materiality(case)
    await wait_idle(runner, EID)
    await review_all(case)
    return case


async def submit(case: Case, decision: ReviewRecord) -> None:
    await case.submit.submit(EID, decision)
    await wait_idle(case.runner, EID)
    assert case.runner.run_error(EID) is None


def confirm(task: ReviewTask, decision_id: str) -> ReviewRecord:
    return record(task.id, id=decision_id)


def override(task: ReviewTask, decision_id: str) -> ReviewRecord:
    return record(task.id, Decision.override, id=decision_id, override_conclusion="人工结论")


def rerun(task: ReviewTask, decision_id: str) -> ReviewRecord:
    return record(task.id, Decision.rerun, id=decision_id, target_version=task.target_version)


def tst_payload(records: list[str], run_no: int = 1) -> dict[str, Any]:
    return {"awaiting": "human", "procedure_id": TST, "run_id": f"RUN-{TST}-{run_no}", "records": records}


def stable(value: Any) -> Any:
    """比较两个项目用: 各次运行取的当前时间 (执行的开始与结束, 功能集冻结时刻) 换成占位, 脚本给的 NOW 保留."""
    if isinstance(value, BaseModel):
        return type(value).__name__, stable(value.model_dump())
    if isinstance(value, dict):
        return {k: "<now>" if k == "frozen_at" else stable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [stable(v) for v in value]
    if isinstance(value, datetime) and value != NOW:
        return "<now>"
    return value


def store_state(store: FakeOutputStore) -> list[Any]:
    """Store 全部对象, 不计写入顺序."""
    return [stable(r) for r in sorted(store.rows, key=lambda r: (type(r).__name__, r.id, r.version))]


async def graph_state(case: Case) -> Any:
    return stable((await case.runner.app.aget_state(CONFIG)).values)


# ── 验收 2: 判断与 normal 任务使节点等待 ──


async def test_normal_task_waits_in_node(tmp_path: Path) -> None:
    case = await at_tst(tmp_path, ONE)

    assert await case.waits() == [tst_payload([])]  # 键与失败等待相同
    assert (await GraphStateQuery(case.runner.app, case.runner.deps, case.runner).overview(EID)).awaiting_review
    [commit] = [c for c in case.output_store.commits if any(getattr(o, "id", None) == f"RUN-{TST}-1" for o in c)]
    written = {(type(o).__name__, o.id, o.version) for o in commit if isinstance(o, Versioned)}
    assert {
        ("ProcedureRun", f"RUN-{TST}-1", 1),
        ("Judgment", f"J-{TST}-1-S1", 1),
        ("ReviewTask", f"T-J-{TST}-1-S1-v1", 1),
        ("Procedure", TST, 2),
    } <= written
    assert case.latest(ProcedureRun, f"RUN-{TST}-1").status is RunStatus.succeeded
    assert case.latest(ReviewTask, f"T-J-{TST}-1-S1-v1").kind is TaskKind.normal
    assert case.latest(Procedure, TST).status is ProcedureStatus.awaiting_human
    assert case.transitions(TST) == [("planned", "awaiting_human")]


@pytest.mark.parametrize(("features", "waiting"), [(COLLAB, "in_dispute"), (None, "awaiting_human")])
async def test_dispute_task_in_node_is_in_dispute_with_collab(
    tmp_path: Path, features: frozenset[str] | None, waiting: str
) -> None:
    """R151 验收 8: P2 以外节点的通用等待同样认争议中. TST-J01 出一条判断与 dispute 任务: collab 打开时程序为
    in_dispute (关闭时照现状为 awaiting_human), 节点内等待; 仲裁 a6_upheld 读回后程序 completed, 继续到 FIN-01 闸门."""
    case = await at_tst(tmp_path, {1: [("S1", TaskKind.dispute)]}, features=features)

    assert await case.waits() == [tst_payload([])]
    assert case.latest(Procedure, TST).status == waiting
    task = await case.task("S1")
    await submit(
        case,
        record(task.id, Decision.dispute_resolution, id="D-ARB", dispute_resolution=DisputeResolution.a6_upheld),
    )
    snapshot = await case.runner.app.aget_state(CONFIG)
    assert snapshot.next == ("gate_fin",)
    procedure = case.latest(Procedure, TST)
    assert procedure.status is ProcedureStatus.completed
    assert next(p for p in snapshot.values["procedure_pack"] if p.id == TST) == procedure  # 取 Store 最新版本
    assert case.transitions(TST) == [("planned", waiting), (waiting, "completed")]


async def test_dispute_left_after_other_task_decided_stays_in_dispute(tmp_path: Path) -> None:
    """R151 验收 3: collab 打开, TST-J01 出一条 normal 与一条 dispute 任务, 程序为 in_dispute; 先确认 normal 的,
    仍有待决争议, 程序不出新版本 (仍为 in_dispute); 再仲裁, 程序 completed."""
    case = await at_tst(tmp_path, {1: [("S1", TaskKind.normal), ("S2", TaskKind.dispute)]}, features=COLLAB)
    assert case.latest(Procedure, TST).status is ProcedureStatus.in_dispute
    before = case.latest(Procedure, TST).version

    await submit(case, confirm(await case.task("S1"), "D-1"))
    assert (case.latest(Procedure, TST).version, case.latest(Procedure, TST).status) == (
        before,
        ProcedureStatus.in_dispute,
    )
    task = await case.task("S2")
    await submit(
        case,
        record(task.id, Decision.dispute_resolution, id="D-ARB", dispute_resolution=DisputeResolution.a6_upheld),
    )
    assert (await case.runner.app.aget_state(CONFIG)).next == ("gate_fin",)
    assert case.transitions(TST) == [("planned", "in_dispute"), ("in_dispute", "completed")]


# ── 验收 3: confirm 后节点整体读回并继续 ──


async def test_confirm_completes_and_continues_to_fin_gate(tmp_path: Path) -> None:
    case = await at_tst(tmp_path, ONE)

    await submit(case, confirm(await case.task("S1"), "D-1"))

    snapshot = await case.runner.app.aget_state(CONFIG)
    assert snapshot.next == ("gate_fin",)
    procedure = case.latest(Procedure, TST)
    assert (procedure.version, procedure.status) == (3, ProcedureStatus.completed)
    values = snapshot.values
    [judgment] = [j for j in values["judgments"] if j.procedure_id == TST]
    assert (judgment.id, judgment.version, judgment.review_status) == (f"J-{TST}-1-S1", 2, ReviewStatus.confirmed)
    [task] = [t for t in values["review_tasks"] if t.procedure_id == TST]
    assert (task.version, task.status) == (2, TaskStatus.decided)
    assert "D-1" in [r.id for r in values["review_records"]]
    assert next(p for p in values["procedure_pack"] if p.id == TST) == procedure
    assert case.agent.calls == 1


# ── 验收 4: 两条任务, 提交顺序不影响结果 ──


async def two_decisions(tmp_path: Path, order: list[str]) -> Case:
    """S1 confirm (D-1), S2 override (D-2), 按 order 提交; 第一个之后仍等待, 载荷列出它."""
    case = await at_tst(tmp_path, TWO)
    made = {"D-1": confirm(await case.task("S1"), "D-1"), "D-2": override(await case.task("S2"), "D-2")}
    first, second = order
    await submit(case, made[first])
    assert await case.waits() == [tst_payload([first])]
    await submit(case, made[second])
    assert (await case.runner.app.aget_state(CONFIG)).next == ("gate_fin",)
    return case


async def test_two_tasks_wait_until_both_decided_in_any_order(tmp_path: Path) -> None:
    forward = await two_decisions(tmp_path / "a", ["D-1", "D-2"])
    backward = await two_decisions(tmp_path / "b", ["D-2", "D-1"])

    assert store_state(forward.output_store) == store_state(backward.output_store)
    assert await graph_state(forward) == await graph_state(backward)
    assert forward.latest(Procedure, TST).status is ProcedureStatus.completed
    assert forward.agent.calls == backward.agent.calls == 1


# ── 验收 5: 恢复补做 ──


async def submit_unread(
    case: Case, decision: ReviewRecord, monkeypatch: pytest.MonkeyPatch, lose_trail: bool = False
) -> None:
    """决定写入 Store (含留痕) 而节点没读回, 检查点停在原处: 后台不继续; 已有未读回的决定时照样放行, 模拟两次提交
    都写进 Store 而检查点未跟上. lose_trail 为真时决定留痕也没写成 (提交后, 写留痕前中断), 由 recover 补写."""

    async def no_resume(*args: Any) -> None:
        return None

    trail = case.runner.deps.trail
    real = trail.record

    async def drop_decision(entry: TrailEntry) -> str:
        return "TR-lost" if entry.kind is TrailKind.decision else await real(entry)

    with monkeypatch.context() as m:
        m.setattr(case.runner, "launch", lambda eid, graph_input: case.runner.release(eid))
        m.setattr(decisions, "resume_required", no_resume)
        if lose_trail:
            m.setattr(trail, "record", drop_decision)
        await case.submit.submit(EID, decision)


def delivered(case: Case, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """之后以 Command(resume=决定 ID) 交给图的决定, 按交出顺序."""
    seen: list[str] = []
    real = case.runner.app.ainvoke

    async def spy(graph_input: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(graph_input, Command):
            seen.append(graph_input.resume)
        return await real(graph_input, *args, **kwargs)

    monkeypatch.setattr(case.runner.app, "ainvoke", spy)
    return seen


async def test_two_unread_decisions_recovered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """D-1, D-2 都写了 Store, 检查点停在第一次等待, D-2 的留痕也没写成: recover 补写留痕, 按 (次序, 0, 任务 ID)
    交回, 结果与不中断相同."""
    online = await two_decisions(tmp_path / "online", ["D-1", "D-2"])
    case = await at_tst(tmp_path / "lost", TWO)
    await submit_unread(case, override(await case.task("S2"), "D-2"), monkeypatch, lose_trail=True)
    await submit_unread(case, confirm(await case.task("S1"), "D-1"), monkeypatch)
    assert await case.waits() == [tst_payload([])]
    assert "D-2" not in case.decision_keys()
    order = delivered(case, monkeypatch)

    await case.runner.recover()
    await wait_idle(case.runner, EID)

    assert case.runner.run_error(EID) is None
    assert order == ["D-1", "D-2"]  # 按任务 ID, 不按写入顺序
    assert sorted(k for k in case.decision_keys() if k in ("D-1", "D-2")) == ["D-1", "D-2"]  # 各一行, 不重复
    assert store_state(case.output_store) == store_state(online.output_store)
    assert await graph_state(case) == await graph_state(online)


async def test_normal_and_rerun_recovered_normal_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """一轮 S1 normal 与 S2 failed: 在线先 confirm S1 再 rerun S2, 节点重跑 S2. 两个决定都写了 Store 而检查点未跟上时
    recover 先交 D-n 后交 D-r, 结果与在线相同."""
    online = await at_tst(tmp_path / "online", MIXED)
    await submit(online, confirm(await online.task("S1"), "D-n"))
    assert await online.waits() == [tst_payload(["D-n"])]
    await submit(online, rerun(await online.task("S2"), "D-r"))
    assert (await online.runner.app.aget_state(CONFIG)).next == ("gate_fin",)
    assert online.seen == [(1, []), (2, ["S2"])]

    case = await at_tst(tmp_path / "lost", MIXED)
    await submit_unread(case, confirm(await case.task("S1"), "D-n"), monkeypatch)
    await submit_unread(case, rerun(await case.task("S2"), "D-r"), monkeypatch)
    assert await case.waits() == [tst_payload([])]
    order = delivered(case, monkeypatch)
    await case.runner.recover()
    await wait_idle(case.runner, EID)

    assert case.runner.run_error(EID) is None
    assert order == ["D-n", "D-r"]
    assert case.seen == [(1, []), (2, ["S2"])]
    assert store_state(case.output_store) == store_state(online.output_store)
    assert await graph_state(case) == await graph_state(online)


async def test_two_reruns_for_one_round_still_rejected(tmp_path: Path) -> None:
    """同一程序同一轮有两个 rerun 决定 (违反 Q17 不变式, 直接写进 Store): recover 报无法确定读回顺序."""
    case = await at_tst(tmp_path, {1: [("S1", TaskKind.failed), ("S2", TaskKind.failed)]})
    tasks = [await case.task("S1"), await case.task("S2")]
    records = [rerun(t, f"D-{i}") for i, t in enumerate(tasks, 1)]
    async with case.output_store.transaction(EID):
        await case.output_store.put(EID, records, actor="U-1")

    await case.runner.recover()
    await wait_idle(case.runner, EID)

    assert "无法确定读回顺序" in (case.runner.run_error(EID) or "")


async def two_misstatements(tmp_path: Path, order: list[str]) -> Case:
    """S1, S2 的判断各引用一条规则例外, 对两条任务都提交 misstatement (D-M1, D-M2), 按 order 提交."""
    case = await at_tst(tmp_path, TWO, cited=frozenset({"S1", "S2"}))
    made = {
        f"D-M{n}": record((await case.task(f"S{n}")).id, Decision.misstatement, id=f"D-M{n}", misstatement_draft=DRAFT)
        for n in (1, 2)
    }
    for decision_id in order:
        await submit(case, made[decision_id])
    assert (await case.runner.app.aget_state(CONFIG)).next == ("gate_fin",)
    return case


async def test_misstatements_read_back_with_attributed_exceptions(tmp_path: Path) -> None:
    """misstatement 决定后节点从 Store 读回归因后的例外 (第 2 版) 与新建的错报, 并入检查点; 两种提交顺序的结果
    (检查点与 Store) 逐字段相同, 错报按 ID 排列."""
    forward = await two_misstatements(tmp_path / "a", ["D-M1", "D-M2"])
    backward = await two_misstatements(tmp_path / "b", ["D-M2", "D-M1"])

    values = (await forward.runner.app.aget_state(CONFIG)).values
    exceptions = [x for x in values["exceptions"] if x.procedure_id == TST]
    assert [(x.id, x.version) for x in exceptions] == [(f"X-{TST}-1-R1-S{n}", 2) for n in (1, 2)]
    assert all(x == forward.latest(AuditException, x.id) for x in exceptions)
    found = [m for m in values["misstatements"] if m.id.startswith("M-D-M")]
    assert [(m.id, m.judgment_id) for m in found] == [(f"M-D-M{n}", f"J-{TST}-1-S{n}") for n in (1, 2)]
    assert all(m == forward.latest(Misstatement, m.id) for m in found)
    assert store_state(forward.output_store) == store_state(backward.output_store)
    assert await graph_state(forward) == await graph_state(backward)


async def test_exception_of_another_program_read_back(tmp_path: Path) -> None:
    """TST-J01 不声明 exceptions, 其判断解释前面 PLN-07 的例外: 重放不取例外 (只取声明的字段), confirm 后归因出的
    第 2 版由整体读回 (判断引用的例外) 并入检查点."""
    explained = "X-PLN-07-1-A01-1"
    case = await at_tst(tmp_path, ONE, explains=explained, outputs="outputs: [judgments, review_tasks]\n")

    await submit(case, confirm(await case.task("S1"), "D-1"))

    [found] = [x for x in (await case.runner.app.aget_state(CONFIG)).values["exceptions"] if x.id == explained]
    assert found.version == 2 and found == case.latest(AuditException, explained)


# ── 再次进入节点, 先 rerun 后 confirm (第 1 轮 R87-01, R87-02) ──


async def test_reentered_node_ignores_decisions_already_in_checkpoint(tmp_path: Path) -> None:
    """FIN-01 驳回回到完成节点, TST-J01 第 2 轮又出 normal 任务: 第 1 轮的 D-1 已在检查点里, 不算未读回; confirm 第 2 轮
    的任务后越过 TST-J01, FIN-04 再执行后停在 gate_fin."""
    case = await at_tst(tmp_path, {1: [("S1", TaskKind.normal)], 2: [("S1", TaskKind.normal)]})
    await submit(case, confirm(await case.task("S1"), "D-1"))
    view = await GraphStateQuery(case.runner.app, case.runner.deps, case.runner).gate(EID)
    assert view is not None
    reject = fin_decision(GateVerdict.reject, target_id=view.target_id, target_version=view.target_version)
    await case.submit.submit(EID, reject)
    await wait_idle(case.runner, EID)
    assert await case.waits() == [tst_payload([], run_no=2)]

    await submit(case, confirm(await case.task("S1", run_no=2), "D-5"))

    assert (await case.runner.app.aget_state(CONFIG)).next == ("gate_fin",)
    assert case.latest(Procedure, TST).status is ProcedureStatus.completed
    assert case.agent.calls == 2


async def rerun_then_confirm(tmp_path: Path, output_store: FakeOutputStore | None = None) -> Case:
    """一轮 S1 normal 与 S2 failed: 先 rerun S2 (D-r), 第 2 轮执行后 S1 仍待决 (待决任务含此前各轮留下的), 程序仍为
    awaiting_human, 节点再等. 之后由调用方 confirm S1."""
    case = await at_tst(tmp_path, MIXED, output_store=output_store)
    await submit(case, rerun(await case.task("S2"), "D-r"))
    assert await case.waits() == [tst_payload(["D-r"], run_no=2)]
    assert case.latest(Procedure, TST).status is ProcedureStatus.awaiting_human
    return case


async def test_rerun_then_confirm_online(tmp_path: Path) -> None:
    case = await rerun_then_confirm(tmp_path)

    await submit(case, confirm(await case.task("S1"), "D-n"))

    assert (await case.runner.app.aget_state(CONFIG)).next == ("gate_fin",)
    assert case.latest(Procedure, TST).status is ProcedureStatus.completed
    assert case.seen == [(1, []), (2, ["S2"])]


async def test_rerun_then_confirm_recovered_after_read_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """读回 D-n 时节点出错 (等待中的节点带错误): recover 复制检查点, 按 (次序, 0/1, ...) 交回 D-n, D-r, 与在线顺序不同.
    节点每次等待前按 Store 判断还要不要等, 等待次数与交回顺序无关: 一次 recover 走到 gate_fin, 结果与在线相同."""
    online = await rerun_then_confirm(tmp_path / "online")
    await submit(online, confirm(await online.task("S1"), "D-n"))
    case = await rerun_then_confirm(tmp_path / "broken")
    real = GraphNodes.read_review
    calls: list[str] = []

    async def flaky(self: GraphNodes, eid: str, procedure_id: str) -> StatePatch:
        calls.append(procedure_id)
        if len(calls) == 1:
            raise RuntimeError("读回时出错")
        return await real(self, eid, procedure_id)

    monkeypatch.setattr(GraphNodes, "read_review", flaky)
    await case.submit.submit(EID, confirm(await case.task("S1"), "D-n"))
    await wait_idle(case.runner, EID)
    assert "读回时出错" in (case.runner.run_error(EID) or "")
    order = delivered(case, monkeypatch)

    await case.runner.recover()
    await wait_idle(case.runner, EID)

    assert case.runner.run_error(EID) is None
    assert order == ["D-n", "D-r"]
    assert (await case.runner.app.aget_state(CONFIG)).next == ("gate_fin",)
    assert store_state(case.output_store) == store_state(online.output_store)
    assert await graph_state(case) == await graph_state(online)


async def test_rerun_then_confirm_recovered_after_fin04_crash(tmp_path: Path) -> None:
    """D-n 读回后 FIN-04 写 Store 后被杀 (检查点停在 TST-J01 第 2 轮的等待): recover 经补做交回 D-n, D-r, 节点重放两轮,
    结果与在线相同."""
    online = await rerun_then_confirm(tmp_path / "online")
    await submit(online, confirm(await online.task("S1"), "D-n"))
    store = CrashAfterCommit("RUN-FIN-04-1")
    case = await rerun_then_confirm(tmp_path / "crash", output_store=store)
    await case.submit.submit(EID, confirm(await case.task("S1"), "D-n"))
    await wait_idle(case.runner, EID)
    assert "被杀" in (case.runner.run_error(EID) or "")

    await case.runner.recover()
    await wait_idle(case.runner, EID)

    assert case.runner.run_error(EID) is None
    assert (await case.runner.app.aget_state(CONFIG)).next == ("gate_fin",)
    assert store_state(store) == store_state(online.output_store)
    assert await graph_state(case) == await graph_state(online)


# ── 闸门程序与 manual_result (第 1 轮 R87-03, R87-04) ──


async def test_gate_program_stays_planned_after_its_tasks_decided(tmp_path: Path) -> None:
    """闸门程序 (模板 gate 不为空; 这里给 TST-J01 加 gate 模拟) 出复核任务: 决定后没有待决任务时回到 planned, 等闸门
    approve 才 completed (Q5); 节点结束时程序包中同为 planned."""
    case = await at_tst(tmp_path, ONE, yaml="gate: FIN-01\n")

    await submit(case, confirm(await case.task("S1"), "D-1"))

    snapshot = await case.runner.app.aget_state(CONFIG)
    assert snapshot.next == ("gate_fin",)
    procedure = case.latest(Procedure, TST)
    assert procedure.status is ProcedureStatus.planned
    assert next(p for p in snapshot.values["procedure_pack"] if p.id == TST) == procedure


# ── 需求 R158: P2 以外的程序开 manual_result_allowed, 节点读回失败任务的 manual_result ──

TWO_FAILED: Plan = {1: [("S1", TaskKind.failed), ("S2", TaskKind.failed)], 2: [("S2", None)]}
A8_OUTPUTS = "outputs: [judgments, review_tasks]\n"  # A8 不能写例外
JUDGING_LLM = "llm:\n  prompt: REV-03.md.j2\n  output_model: JudgmentDraft\n"  # 判断程序的大模型配置
MANUAL_ALLOWED = "manual_result_allowed: true\n" + JUDGING_LLM  # 只有判断程序能开 (第 1 轮审核 R158-07)


@pytest.mark.parametrize(
    ("llm", "loads"),
    [
        (JUDGING_LLM, True),
        ("llm:\n  prompt: DOC-04.md.j2\n  output_model: ContractExtraction\n", False),
        ("", False),
    ],
    ids=["judgment", "extraction", "no_llm"],
)
def test_manual_result_only_for_judging_programs(tmp_path: Path, llm: str, loads: bool) -> None:
    """R158 (第 1 轮审核 R158-07): manual_result_allowed 只许判断程序开 (llm.output_model 为 JudgmentDraft 或其子类,
    不论阶段): 人工结果建人工判断取代该对象的判断. 抽取程序 (如 DOC-04 的合同) 或规则程序误开时, 对象该产出的领域对象
    没人补, 下游悄悄少一个对象, 所以程序库加载即报错, 消息含程序编号."""
    extra = "manual_result_allowed: true\n" + llm
    if loads:
        assert tst_library(tmp_path / "knowledge", extra).load(TST).manual_result_allowed
    else:
        with pytest.raises(ValueError, match=f"只有判断程序能开 manual_result_allowed .*{TST}"):
            tst_library(tmp_path / "knowledge", extra)


def manual_result(task: ReviewTask, decision_id: str) -> ReviewRecord:
    """对带对象的 failed 任务人工执行并提交结果."""
    result = ManualResult(conclusion="人工执行的结论")
    return record(task.id, Decision.manual_result, id=decision_id, manual_result=result)


async def at_manual_tst(tmp_path: Path) -> Case:
    """TST-J01 由 A8 所有 (同 FIN-03), 开 manual_result_allowed: 第 1 轮 S1, S2 都失败, 第 2 轮 S2 出判断 (自动关闭)."""
    return await at_tst(tmp_path, TWO_FAILED, yaml=MANUAL_ALLOWED, outputs=A8_OUTPUTS, owner=AgentId.A8)


async def test_manual_result_on_failed_task_read_back_in_node(tmp_path: Path) -> None:
    """R158: P2 以外的程序 (A8 所有, 同 FIN-03) 开 manual_result_allowed 时程序库照常加载, 带对象的 failed 任务允许
    {rerun, manual_result}. 对 S1 提交 manual_result: DecisionSubmit 建人工判断 -H1 (confirmed), 任务 decided; 节点
    整体读回后仍等 S2 (载荷 records 带它). 再对 S2 rerun: S1 已出组, 第 2 轮只重跑 S2, 程序 completed, 走到 FIN-01
    闸门."""
    case = await at_manual_tst(tmp_path)
    s1 = await case.task("S1")
    assert case.runner.deps.queue.allowed(s1) == {Decision.rerun, Decision.manual_result}

    await submit(case, manual_result(s1, "D-m"))
    assert await case.waits() == [tst_payload(["D-m"])]
    human = case.latest(Judgment, f"J-{TST}-1-S1-H1")
    assert (human.review_status, human.conclusion, human.run_id) == (
        ReviewStatus.confirmed,
        "人工执行的结论",
        f"RUN-{TST}-1",
    )
    assert case.latest(ReviewTask, s1.id).status is TaskStatus.decided
    snapshot = await case.runner.app.aget_state(CONFIG)
    assert human not in snapshot.values["judgments"]  # 节点还在等, 读回的对象在节点结束时并入状态

    await submit(case, rerun(await case.task("S2"), "D-r"))
    snapshot = await case.runner.app.aget_state(CONFIG)
    assert snapshot.next == ("gate_fin",)
    assert case.seen == [(1, []), (2, ["S2"])]
    assert case.latest(Procedure, TST).status is ProcedureStatus.completed
    assert human in snapshot.values["judgments"]
    assert [r.id for r in snapshot.values["review_records"] if r.id in ("D-m", "D-r")] == ["D-m", "D-r"]


async def test_manual_result_and_rerun_recovered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """R158: 两个决定 (S1 的 manual_result, S2 的 rerun) 都写了 Store 而检查点停在第一次等待: recover 按 rerun 以外的
    决定在前交回 D-m, D-r, 节点读回 D-m 时把 S1 移出本轮的组, rerun 照样只重跑 S2; 结果与在线相同."""
    online = await at_manual_tst(tmp_path / "online")
    await submit(online, manual_result(await online.task("S1"), "D-m"))
    await submit(online, rerun(await online.task("S2"), "D-r"))

    case = await at_manual_tst(tmp_path / "lost")
    await submit_unread(case, manual_result(await case.task("S1"), "D-m"), monkeypatch)
    await submit_unread(case, rerun(await case.task("S2"), "D-r"), monkeypatch)
    assert await case.waits() == [tst_payload([])]
    order = delivered(case, monkeypatch)
    await case.runner.recover()
    await wait_idle(case.runner, EID)

    assert case.runner.run_error(EID) is None
    assert order == ["D-m", "D-r"]
    assert case.seen == [(1, []), (2, ["S2"])]
    assert store_state(case.output_store) == store_state(online.output_store)
    assert await graph_state(case) == await graph_state(online)


# ── 需求 R347: unattributed 任务可以重跑, 与 failed 任务同属一轮的组 ──

U, F = TaskKind.unattributed, TaskKind.failed
Step = tuple[str, int, Any, str]  # 一个决定: (对象, 任务所属的轮次, 决定, 决定 ID)
# 场景: (每轮的脚本, 依次提交的决定, 第 2 轮起各轮重判的对象); 最后一个决定都是 rerun D-r
UNATTRIBUTED_ROUNDS: dict[str, tuple[Plan, list[Step], list[list[str]]]] = {
    "unattributed": ({1: [("S1", U)], 2: [("S1", None)]}, [("S1", 1, rerun, "D-r")], [["S1"]]),
    "with_failed": (
        {1: [("S1", F), ("S2", U)], 2: [("S1", None), ("S2", None)]},
        [("S1", 1, rerun, "D-r")],
        [["S1", "S2"]],
    ),
    "override_first": (
        {1: [("S1", U), ("S2", U)], 2: [("S2", None)]},
        [("S1", 1, override, "D-o"), ("S2", 1, rerun, "D-r")],
        [["S2"]],
    ),
    "second_round": (
        {1: [("S1", F), ("S2", U)], 2: [("S1", F), ("S2", None)], 3: [("S1", None)]},
        [("S1", 1, rerun, "D-r1"), ("S1", 2, rerun, "D-r")],
        [["S1", "S2"], ["S1"]],
    ),
}


@pytest.mark.parametrize("scenario", list(UNATTRIBUTED_ROUNDS))
async def test_unattributed_tasks_rerun_with_their_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    """R347 (第 1 轮审核 R347-01): P2 以外的节点内等待, 带规则例外的对象判断失败留下的 unattributed 任务允许 rerun,
    与同一轮的 failed 任务同属一组. unattributed: 对它 rerun, 第 2 轮重判它; with_failed: S1 failed 与 S2 unattributed
    同轮, 对 S1 rerun 即整组关闭, 第 2 轮一起重判; override_first: S1, S2 都 unattributed, 先推翻 S1 (节点读回后 S1
    出组), 再对 S2 rerun, 第 2 轮只重判 S2; second_round: 同 with_failed, 第 2 轮 S1 又失败, 再对它 rerun, 第 3 轮只
    重判 S1. 各场景程序 completed, 走到 FIN-01 闸门. 最后一轮的决定都写了 Store 而检查点停在那一轮的第一次等待 (rerun
    的留痕也没写成) 时, recover 按 rerun 以外的在前交回, 补写的 rerun 留痕 (含一并关闭的同组任务, 只取同一次执行的)
    与提交时相同, 结果与在线相同."""
    plan, steps, rejudged = UNATTRIBUTED_ROUNDS[scenario]
    last = steps[-1][1]  # 最后一轮的决定在恢复变体里只写 Store
    online = await at_tst(tmp_path / "online", plan)
    for subject, run_no, decide, decision_id in steps:
        task = await online.task(subject, run_no)
        assert Decision.rerun in online.runner.deps.queue.allowed(task), f"在线: {task.id}"
        await submit(online, decide(task, decision_id))
    assert (await online.runner.app.aget_state(CONFIG)).next == ("gate_fin",), "在线: 没走到 FIN-01"
    assert online.seen == [(1, []), *enumerate(rejudged, 2)], "在线: 各轮重判的对象"
    assert online.latest(Procedure, TST).status is ProcedureStatus.completed, "在线: 程序状态"
    tasks = await online.output_store.latest(EID, ReviewTask, procedure_id=TST)
    assert {t.kind for t in tasks} <= {U, F} and all(t.status is TaskStatus.decided for t in tasks), "在线: 任务"

    case = await at_tst(tmp_path / "lost", plan)
    for subject, run_no, decide, decision_id in steps:
        record = decide(await case.task(subject, run_no), decision_id)
        if run_no < last:
            await submit(case, record)
        else:  # rerun 的留痕也没写成, 由 recover 补写
            await submit_unread(case, record, monkeypatch, decide is rerun)
    assert await case.waits() == [tst_payload([r for _, n, _, r in steps if n < last], run_no=last)], "恢复前的等待"
    order = delivered(case, monkeypatch)
    await case.runner.recover()
    await wait_idle(case.runner, EID)

    assert case.runner.run_error(EID) is None, "恢复: 后台出错"
    assert order == [decision_id for _, run_no, _, decision_id in steps if run_no == last], "恢复: 交回顺序"
    assert case.seen == online.seen, "恢复: 各轮重判的对象与在线不同"
    assert store_state(case.output_store) == store_state(online.output_store), "恢复: Store 与在线不同"
    assert await graph_state(case) == await graph_state(online), "恢复: 检查点与在线不同"
    # 补写的同组任务与提交时相同
    assert case.decision_payload("D-r") == online.decision_payload("D-r"), "恢复: 补写的 rerun 留痕"


async def test_rerun_on_normal_task_delivered_to_node_is_a_node_error(tmp_path: Path) -> None:
    """绕过 DecisionSubmit 把对 normal 任务的 rerun 决定交给节点: 按决定种类走 rerun 的读回, 报不是本轮的失败任务."""
    case = await at_tst(tmp_path, ONE)
    await case.output_store.put(EID, [rerun(await case.task("S1"), "D-X")], actor="U-1")

    with pytest.raises(ValueError, match=f"不是 {TST} 待处理的失败任务"):
        await case.runner.app.ainvoke(Command(resume="D-X"), CONFIG)


# ── 验收 6, 7, 9: DecisionSubmit 按载荷拒绝 ──


def other_task(procedure_id: str) -> ReviewTask:
    """直接写进 Store 的别的程序的待决任务 (目标不必存在: 等待的校验在版本之前)."""
    return ReviewTask(
        id=f"T-OTHER-{procedure_id}-v1",
        kind=TaskKind.normal,
        procedure_id=procedure_id,
        target_type=TargetType.Judgment,
        target_id=f"J-{procedure_id}-X",
        target_version=1,
        run_id=f"RUN-{procedure_id}-1",
        created_at=NOW,
    )


async def test_decisions_outside_the_wait_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """等 TST-J01 时: 别的程序的任务 → NOT_AWAITING_REVIEW; 对 normal 任务 rerun → DECISION_NOT_ALLOWED;
    等待为 correction 时一律 NOT_AWAITING_REVIEW. Store 与检查点不变 (assert_rejected)."""
    case = await at_tst(tmp_path, ONE)
    await case.output_store.put(EID, [other_task("REV-03")], actor="U-1")

    await assert_rejected(case, record("T-OTHER-REV-03-v1", id="D-9"), "NOT_AWAITING_REVIEW")
    await assert_rejected(case, rerun(await case.task("S1"), "D-9"), "DECISION_NOT_ALLOWED")
    monkeypatch.setattr(decisions, "review_waits", lambda snapshot: [{"awaiting": WaitKind.correction.value}])
    await assert_rejected(case, confirm(await case.task("S1"), "D-9"), "NOT_AWAITING_REVIEW")


async def test_other_programs_decision_delivered_to_node_is_a_node_error(tmp_path: Path) -> None:
    """绕过 DecisionSubmit 把别的程序任务的决定交给等 TST-J01 的节点: 节点出错, 不并入."""
    case = await at_tst(tmp_path, ONE)
    task = other_task("REV-03")
    await case.output_store.put(EID, [task, record(task.id, id="D-X")], actor="U-1")

    with pytest.raises(ValueError, match="不属于正在等待的 TST-J01"):
        await case.runner.app.ainvoke(Command(resume="D-X"), CONFIG)


async def test_await_events_takes_only_p2_tasks(tmp_path: Path) -> None:
    """停在 await_events 时载荷带 phase; Store 中有 TST-J01 (非 P2) 的待决任务, 对它提交 → NOT_AWAITING_REVIEW."""
    seen: list[Any] = []
    case = await at_tst_await_events(tmp_path, seen)
    assert await case.waits() == [{"awaiting": "review", "phase": "P2_Executing"}]
    await case.output_store.put(EID, [other_task(TST)], actor="U-1")

    await assert_rejected(case, record(f"T-OTHER-{TST}-v1", id="D-9"), "NOT_AWAITING_REVIEW")


async def at_tst_await_events(tmp_path: Path, seen: list[Any]) -> Case:
    """同 at_tst, 但停在 P2 的第一次等复核 (不提交 P2 的复核决定)."""
    engagement_store = FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT])
    runner, store = make_runner(
        engagement_store=engagement_store,
        agents=numbat_all_p2_agents,
        library=tst_library(tmp_path / "knowledge"),
    )
    case = Case(runner, store, engagement_store)
    await runner.start(EID)
    await wait_idle(runner, EID)
    await case.submit.submit(EID, acc01_decision(GateVerdict.approve, id="D-ACC"))
    await wait_idle(runner, EID)
    await approve_materiality(case)
    await wait_idle(runner, EID)
    assert (await runner.app.aget_state(CONFIG)).next == ("await_events",)
    return case


def test_accepts_by_payload_kind() -> None:
    """review 收载荷 phase 阶段的程序的任务, 旧检查点不带 phase 按 P2; human 只收本程序的; correction, data 与认不出的
    不收. 种类只在 waits.py 登记 (四种, 字符串与已有检查点一致), kind_of 认出载荷的种类."""
    pack = [make_procedure("REV-03", Phase.P2_Executing), make_procedure(TST, Phase.P3_Completion)]
    p2, tst = other_task("REV-03"), other_task(TST)
    assert review_payload() == {"awaiting": "review", "phase": "P2_Executing"}
    assert accepts(review_payload(), p2, pack) and not accepts(review_payload(), tst, pack)
    assert accepts({"awaiting": "review"}, p2, pack) and not accepts({"awaiting": "review"}, tst, pack)
    assert accepts(review_payload(Phase.P3_Completion), tst, pack)
    assert accepts(human_payload(TST, f"RUN-{TST}-1", []), tst, pack)
    assert not accepts(human_payload(TST, f"RUN-{TST}-1", []), p2, pack)
    assert not accepts({"awaiting": "correction"}, tst, pack)
    assert data_payload() == {"awaiting": "data"} and not accepts(data_payload(), p2, pack)
    assert not accepts({"awaiting": "unknown"}, p2, pack) and not accepts("review", p2, pack)
    assert {k.value for k in WaitKind} == {"review", "human", "correction", "data", "inputs"}  # inputs 见 R272
    payloads = [review_payload(), human_payload(TST, f"RUN-{TST}-1", []), {"awaiting": "correction"}, data_payload()]
    assert [kind_of(p) for p in payloads] == [WaitKind.review, WaitKind.human, WaitKind.correction, WaitKind.data]
    assert [kind_of(p) for p in ({"awaiting": "unknown"}, {}, "data", None)] == [None, None, None, None]


# ── 验收 8: 重放 ──


async def test_node_replays_runs_and_decisions_from_store(tmp_path: Path) -> None:
    """读回 D-1, D-2 后 FIN-04 的执行写 Store 后被杀: 再继续时节点从头执行, TST-J01 第 1 次取 Store 不调 Agent,
    两次等待依次取回 D-1, D-2, 结果与不中断相同, Store 无新增."""
    online = await two_decisions(tmp_path / "online", ["D-1", "D-2"])
    store = CrashAfterCommit("RUN-FIN-04-1")
    case = await at_tst(tmp_path / "crash", TWO, output_store=store)
    await submit(case, confirm(await case.task("S1"), "D-1"))
    await case.submit.submit(EID, override(await case.task("S2"), "D-2"))
    await wait_idle(case.runner, EID)
    assert "被杀" in (case.runner.run_error(EID) or "")
    rows = list(store.rows)

    await case.runner.app.ainvoke(None, CONFIG)

    assert store.rows == rows
    assert case.agent.calls == 1
    snapshot = await case.runner.app.aget_state(CONFIG)
    assert snapshot.next == ("gate_fin",)
    assert store_state(store) == store_state(online.output_store)
    assert await graph_state(case) == await graph_state(online)
