"""R92 人工程序契约与提交校验 (接口文档第 2.5, 5, 7, 8.1 节).

测试程序 TST-M01 放在临时 knowledge 目录 (完成阶段, owner A10, engine manual, L1, outputs [], manual 段要取得签字的
管理层声明书, 结果模型 TstLetter 为测试登记, 至少 2 条证据, 不同于缺省的 1). 完成节点改为先执行它再执行 FIN-04;
它的脚本 A10 出执行记录与 manual 任务 (派发与材料由 R93 实现), 节点按通用等待 (R87) 停在节点内. 提交
manual_result 后只写决定, 任务 decided, 程序 completed, 不建人工判断; 结果整理成领域对象由 R93 做.
"""

import re
import shutil
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from backend.agents.base import AgentDeps, RunContext
from backend.contracts.errors import DecisionRejected
from backend.contracts.manual_results import RESULT_MODELS
from backend.contracts.models import (
    AgentId,
    Decision,
    DocumentRef,
    DocumentType,
    EvidencePointer,
    EvidenceType,
    GateVerdict,
    Judgment,
    ManualRequest,
    ManualResult,
    Model,
    Phase,
    Procedure,
    ProcedureRun,
    ProcedureStatus,
    Reliability,
    ReviewRecord,
    ReviewTask,
    RunStatus,
    TargetType,
    TaskKind,
    TaskStatus,
    TrailKind,
    Versioned,
)
from backend.contracts.state import StatePatch, initial_state
from backend.core.config import get_settings
from backend.orchestration.decisions import check_manual_result
from backend.orchestration.merge import MergeRejected
from backend.orchestration.nodes import DISPATCH_FIELDS, NODE_PROCEDURES, check_dispatch, writer_of
from backend.orchestration.runner import GraphRunner, waiting_payloads
from backend.services.procedure_library import ProcedureLibrary, manual_program
from backend.services.review_queue import ReviewQueue
from tests.fakes import (
    ENGAGEMENT,
    NOW,
    NUMBAT_PROFILE,
    FakeEngagementStore,
    FakeRegistry,
    FakeTrail,
    ScriptA10,
    ScriptAgent,
    finished,
    make_deps,
    make_procedure,
    make_run,
    make_runner,
    numbat_all_p2_agents,
    wait_idle,
)
from tests.test_decisions import Started, acc01_decision, assert_rejected
from tests.test_flow import approve_materiality, review_all

EID = ENGAGEMENT.id
TST = "TST-M01"
RUN = f"RUN-{TST}-1"
TASK = f"T-{RUN}-v1"
INSTRUCTIONS = "Obtain the signed management representation letter"
TST_YAML = (
    f"id: {TST}\nname: 自测人工程序\nphase: P3_Completion\ncycle: Completion\nowner: A10\nengine: manual\n"
    "autonomy_level: L1\noutputs: []\n"
)
MANUAL = f"manual:\n  instructions: {INSTRUCTIONS}\n  result_model: TstLetter\n  min_evidence: 2\n"
LLM = "llm:\n  prompt: DOC-04.md.j2\n  output_model: ContractExtraction\n"  # 借 DOC-04 的, 只为能加载
MATERIAL = {"period_end": "2026-06-30"}  # 派发时给人的材料 (R93 由 owner 的 prepare 生成)
LETTER = DocumentRef(  # 审计师已上传的声明书 (2 页, 公司出具的原件)
    id="DOC-0a1b2c3d4e5f",
    type=DocumentType.representation_letter,
    file_name="representation_letter.pdf",
    sha256="0a1b2c3d4e5f" + "0" * 52,
    page_count=2,
    reliability=Reliability.internal_original,
    status="classified",
    uploaded_at=NOW,
)
DECIDED_AT = datetime(2026, 10, 23, 5, tzinfo=UTC)  # 悉尼 10-23 16:00


class TstLetter(Model):
    """测试登记的结果模型: 声明书的签署日与签署人."""

    signed_on: date
    signatory: str


@pytest.fixture(autouse=True)
def registered(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """登记 TstLetter; 完成节点先执行 TST-M01 再执行 FIN-04 (recovery 与 nodes 共用这张表)."""
    monkeypatch.setitem(RESULT_MODELS, "TstLetter", TstLetter)
    monkeypatch.setitem(NODE_PROCEDURES, "completion", (TST, "FIN-04"))
    yield


def manual_library(root: Path, yaml: str = TST_YAML + MANUAL) -> ProcedureLibrary:
    """knowledge 目录的副本另加 TST-M01 (YAML 为 yaml)."""
    shutil.copytree(get_settings().KNOWLEDGE_ROOT, root)
    (root / "procedures" / f"{TST}.yaml").write_text(yaml, encoding="utf-8")
    return ProcedureLibrary(root)


def pointer(
    source_type: str = "document", source_id: str = LETTER.id, page: int | None = 2, locator: str = "signature"
) -> EvidencePointer:
    """人填的证据指针: 种类填成 external, 不填可靠性 (写入时按文件登记改写为 voucher, internal_original)."""
    return EvidencePointer(
        type=EvidenceType.external, source_type=source_type, source_id=source_id, locator=locator, page=page
    )


BODY = pointer(page=1, locator="representations")  # 声明书正文页


def letter_result(**changes: Any) -> ManualResult:
    """取得声明书的结果: 结论, 10-23 执行, 证据为声明书第 1 页 (正文) 与第 2 页 (签字页), 结构化结果合 TstLetter."""
    base = ManualResult(
        conclusion="Signed letter obtained, dated 23 October 2026",
        performed_on=date(2026, 10, 23),
        evidence=[BODY, pointer()],
        details={"signed_on": "2026-10-23", "signatory": "Alex Morgan"},
    )
    return base.model_copy(update=changes)


def as_registered(result: ManualResult) -> ManualResult:
    """写入的结果: 证据指针按 LETTER 的登记改写种类与可靠性 (同判断的文件指针), 其余原样."""
    evidence = [
        p.model_copy(update={"type": EvidenceType.voucher, "reliability": Reliability.internal_original})
        for p in result.evidence
    ]
    return result.model_copy(update={"evidence": evidence})


def submitted(decision: Decision = Decision.manual_result, **changes: Any) -> ReviewRecord:
    """对 TST-M01 的 manual 任务的决定: 绑定执行记录第 1 版, 带理由与 letter_result; changes 覆盖任意字段."""
    base = ReviewRecord(
        id="D-M01",
        task_id=TASK,
        target_version=1,
        decision=decision,
        reason="Letter received from the CFO",
        manual_result=letter_result() if decision is Decision.manual_result else None,
        reviewed_by="U-1",
        decided_at=DECIDED_AT,
    )
    return base.model_copy(update=changes)


# ── 验收 1: 加载校验 ──


@pytest.mark.parametrize(
    ("yaml", "problem"),
    [
        (TST_YAML, f"{TST}: engine manual 且不是闸门程序, 须有 manual 段"),
        (TST_YAML + MANUAL.replace("TstLetter", "TstUnknown"), f"{TST}: 结果模型 TstUnknown 没有登记"),
        (TST_YAML.replace("P3_Completion", "P2_Executing") + MANUAL, f"{TST}: 本级不做 P2 的人工程序"),
        (
            TST_YAML.replace("engine: manual", "engine: llm") + LLM + MANUAL,
            f"{TST}: 不是人工程序 (engine manual 且不是闸门), 不能写 manual 段",
        ),
        (
            TST_YAML + MANUAL.replace("min_evidence: 2", "min_evidence: -1"),
            "manual.min_evidence\n  Input should be greater than or equal to 0",
        ),
    ],
    ids=["no_manual_section", "unregistered_model", "in_p2", "manual_on_llm_program", "negative_min_evidence"],
)
def test_library_rejects_malformed_manual_programs(tmp_path: Path, yaml: str, problem: str) -> None:
    """人工程序没写 manual 段, 结果模型没登记, 或放在 P2; 别的程序写了 manual 段; 证据下限为负: 程序库构造即报错,
    消息含程序编号或字段 (防派出没有说明或无从校验结果的任务, 提交时才发现)."""
    with pytest.raises(ValueError, match=re.escape(problem)):
        manual_library(tmp_path / "knowledge", yaml)


def test_gate_programs_are_not_manual_programs() -> None:
    """现有五个闸门程序都是 engine manual, 不算人工程序, 没有 manual 段照常加载."""
    library = ProcedureLibrary(get_settings().KNOWLEDGE_ROOT)
    gates = [t for t in library.all(include_disabled=True) if t.engine == "manual"]
    assert sorted(t.id for t in gates) == ["ACC-01", "FIN-01", "PLN-01", "PLN-03", "RPT-01"]
    assert not any(manual_program(t) or t.manual is not None for t in gates)


# ── 验收 2: manual 任务 ──


def test_manual_task_carries_the_request(tmp_path: Path) -> None:
    """make_task(manual): 目标为本次执行记录第 1 版, ID T-RUN-TST-M01-1-v1, subject_id 取模板的 RESULT, 请求取模板
    manual 段与给定材料; 只允许 manual_result. 没有 manual 段的程序不能建 manual 任务."""
    queue = ReviewQueue(manual_library(tmp_path / "knowledge"))
    run = finished(make_run(TST))
    task = queue.make_task(TaskKind.manual, run, TST, run.id, NOW, material=MATERIAL)
    assert (task.id, task.target_type, task.target_id, task.target_version, task.subject_id) == (
        TASK,
        TargetType.ProcedureRun,
        RUN,
        1,
        "RESULT",
    )
    assert task.manual_request == ManualRequest(
        instructions=INSTRUCTIONS, result_model="TstLetter", min_evidence=2, material=MATERIAL
    )
    assert queue.allowed(task) == {Decision.manual_result}
    with pytest.raises(ValueError, match="FIN-04 没有 manual 段"):
        queue.make_task(TaskKind.manual, finished(make_run("FIN-04")), "FIN-04", "RUN-FIN-04-1", NOW)


# ── 验收 3 至 5: 节点内等待, 拒绝与通过 ──


async def at_manual(tmp_path: Path) -> tuple[Started, ScriptA10]:
    """Numbat 走完 P2 与全部复核后进入完成阶段, 停在 TST-M01 的节点内等待; 返回项目与 TST-M01 的脚本 A10.
    之后登记审计师上传的声明书 LETTER."""
    holder: dict[str, ScriptA10] = {}

    def agents(deps: AgentDeps) -> dict[str, Any]:
        def script(ctx: RunContext) -> StatePatch:
            run = finished(ctx.run)
            task = deps.queue.make_task(TaskKind.manual, run, TST, run.id, NOW, material=MATERIAL)
            return {"procedure_runs": [run], "review_tasks": [task]}

        holder["agent"] = ScriptA10(deps, script)
        return {**numbat_all_p2_agents(deps), TST: holder["agent"]}

    engagement_store = FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT])
    runner, store = make_runner(
        engagement_store=engagement_store, agents=agents, library=manual_library(tmp_path / "knowledge")
    )
    s = Started(runner, store, engagement_store)
    await runner.start(EID)
    await wait_idle(runner, EID)
    await s.submit.submit(EID, acc01_decision(GateVerdict.approve, id="D-ACC"))
    await wait_idle(runner, EID)
    await approve_materiality(s)
    await wait_idle(runner, EID)
    await review_all(s)
    runner.deps.financial_store.document_refs.append(LETTER)  # type: ignore[attr-defined]
    return s, holder["agent"]


def latest(s: Started, model: type[Any], oid: str) -> Any:
    return max((r for r in s.output_store.rows if isinstance(r, model) and r.id == oid), key=lambda r: r.version)


def transitions(s: Started) -> list[tuple[str, str]]:
    trail = s.runner.deps.trail
    assert isinstance(trail, FakeTrail)
    return [
        (e.payload["from"], e.payload["to"])
        for e in trail.entries
        if e.kind is TrailKind.transition and e.payload["id"] == TST
    ]


REJECTED = [  # (说明, 决定, 错误码, 消息含)
    ("缺结果", submitted(manual_result=None), "MISSING_FIELD", "manual_result"),
    ("缺理由", submitted(reason=None), "MISSING_FIELD", "reason"),
    ("缺执行日期", submitted(manual_result=letter_result(performed_on=None)), "MISSING_FIELD", "performed_on"),
    ("confirm", submitted(Decision.confirm, reason=None), "DECISION_NOT_ALLOWED", "confirm"),
    ("rerun", submitted(Decision.rerun), "DECISION_NOT_ALLOWED", "rerun"),
    ("没有证据", submitted(manual_result=letter_result(evidence=[])), "INVALID_VALUE", "至少 2 条, 只有 0 条"),
    ("证据不够", submitted(manual_result=letter_result(evidence=[pointer()])), "INVALID_VALUE", "至少 2 条, 只有 1 条"),
    (  # ID 是已登记的声明书, 来源却写成留痕: 按来源类型拒, 不只按 ID 查登记
        "证据不是文件",
        submitted(manual_result=letter_result(evidence=[BODY, pointer("trail", LETTER.id)])),
        "INVALID_VALUE",
        f"证据须为本项目已登记的文件, trail {LETTER.id} 不是",
    ),
    (
        "文件不在本项目",
        submitted(manual_result=letter_result(evidence=[BODY, pointer(source_id="DOC-ffffffffffff")])),
        "INVALID_VALUE",
        "DOC-ffffffffffff",
    ),
    (
        "页码超出",
        submitted(manual_result=letter_result(evidence=[BODY, pointer(page=3)])),
        "INVALID_VALUE",
        "页码 3 不在 1-2 之间",
    ),
    (
        "执行日期晚于决定当天",
        submitted(manual_result=letter_result(performed_on=date(2026, 10, 24))),
        "INVALID_VALUE",
        "晚于决定当天 2026-10-23",
    ),
    (
        "结构化结果缺字段",
        submitted(manual_result=letter_result(details={"signed_on": "2026-10-23"})),
        "INVALID_VALUE",
        "TstLetter: signatory",
    ),
    ("版本不符", submitted(target_version=2), "VERSION_MISMATCH", RUN),
]


async def test_manual_task_waits_rejects_and_accepts_the_result(tmp_path: Path) -> None:
    """验收 3 至 5, 一次运行:
    - 派发: 脚本 A10 只调 1 次, 同一事务写入 RUN-TST-M01-1 succeeded, manual 任务 (带请求) 与 TST-M01 awaiting_human;
      节点内等待, 载荷同其他人工等待;
    - 拒绝: 逐条提交 REJECTED, 错误码与消息对, Store, 留痕与检查点都不变;
    - 通过: 一个事务写入决定 (证据指针按文件登记改写种类与可靠性, 其余原样), 任务 v2 decided 与 TST-M01
      completed; 不建判断; 1 行 decision 留痕, 1 行 transition awaiting_human → completed; 节点读回后继续执行
      FIN-04, 停在 FIN-01 闸门."""
    s, agent = await at_manual(tmp_path)

    assert waiting_payloads(await s.runner.app.aget_state(GraphRunner.config(EID))) == [
        {"awaiting": "human", "procedure_id": TST, "run_id": RUN, "records": []}
    ]
    [commit] = [c for c in s.output_store.commits if any(getattr(o, "id", None) == RUN for o in c)]
    assert sorted((type(o).__name__, o.id, o.version) for o in commit if isinstance(o, Versioned)) == [
        ("Procedure", TST, 2),
        ("ProcedureRun", RUN, 1),
        ("ReviewTask", TASK, 1),
    ]
    task: ReviewTask = latest(s, ReviewTask, TASK)
    assert (task.kind, task.status, task.manual_request.material if task.manual_request else None) == (
        TaskKind.manual,
        TaskStatus.pending,
        MATERIAL,
    )
    assert (latest(s, ProcedureRun, RUN).status, latest(s, Procedure, TST).status) == (
        RunStatus.succeeded,
        ProcedureStatus.awaiting_human,
    )

    for name, decision, code, detail in REJECTED:
        try:
            await assert_rejected(s, decision, code, detail)
        except (AssertionError, DecisionRejected) as e:
            raise AssertionError(f"{name}: {e}") from e

    decision = submitted()
    await s.submit.submit(EID, decision)
    await wait_idle(s.runner, EID)
    assert s.runner.run_error(EID) is None
    [commit] = [c for c in s.output_store.commits if any(getattr(o, "id", None) == "D-M01" for o in c)]
    assert sorted((type(o).__name__, o.id, o.version) for o in commit if isinstance(o, Versioned)) == [
        ("Procedure", TST, 3),
        ("ReviewRecord", "D-M01", 1),
        ("ReviewTask", TASK, 2),
    ]
    stored = latest(s, ReviewRecord, "D-M01")
    assert stored == decision.model_copy(update={"manual_result": as_registered(letter_result())})
    assert [(p.type, p.reliability) for p in stored.manual_result.evidence] == [
        (EvidenceType.voucher, Reliability.internal_original)
    ] * 2  # 人填的 external 与空可靠性不留
    assert (latest(s, ReviewTask, TASK).status, latest(s, Procedure, TST).status) == (
        TaskStatus.decided,
        ProcedureStatus.completed,
    )
    assert [j.id for j in s.output_store.rows if isinstance(j, Judgment) and j.procedure_id == TST] == []
    trail = s.runner.deps.trail
    assert isinstance(trail, FakeTrail)
    assert [e.idempotency_key for e in trail.entries if e.kind is TrailKind.decision].count("D-M01") == 1
    assert transitions(s) == [("planned", "awaiting_human"), ("awaiting_human", "completed")]
    assert (await s.next_nodes()) == ("gate_fin",)
    assert agent.calls == 1


# ── 按请求核对的边界 (纯函数) ──


@pytest.mark.parametrize(
    ("result_model", "decided_at", "performed_on", "details", "problem"),
    [
        (None, DECIDED_AT, date(2026, 10, 23), {}, None),  # 没有结果模型, details 为空
        (
            None,
            DECIDED_AT,
            date(2026, 10, 23),
            {"signatory": "A"},
            "TST-M01 没有结果模型, manual_result.details 须为空",
        ),
        # 10-23 14:00 UTC 是悉尼 10-24 01:00: 10-24 执行不算晚 (按 UTC 日期会误拒)
        (
            "TstLetter",
            datetime(2026, 10, 23, 14, tzinfo=UTC),
            date(2026, 10, 24),
            {"signed_on": "2026-10-24", "signatory": "A"},
            None,
        ),
        (
            "TstLetter",
            DECIDED_AT,
            date(2026, 10, 23),
            {"signed_on": "2026-10-23", "signatory": "A", "extra": 1},
            "TstLetter: extra",
        ),
        # 派发之后结果模型名从登记里拿掉了 (或模板改了名): 旧任务的请求取不到模型, 拒绝而不是 500
        (
            "TstGone",
            DECIDED_AT,
            date(2026, 10, 23),
            {"signed_on": "2026-10-23", "signatory": "A"},
            "结果模型 TstGone 未登记 (contracts/manual_results), 无从核对 manual_result.details",
        ),
    ],
    ids=["no_model_empty_details", "no_model_with_details", "sydney_date", "extra_field", "unregistered_model"],
)
def test_result_checked_against_the_request(
    result_model: str | None, decided_at: datetime, performed_on: date, details: dict[str, Any], problem: str | None
) -> None:
    """check_manual_result 按任务请求核对: 没有结果模型时 details 须为空; 执行日期按决定时刻的悉尼日期比; 结果模型
    多出的字段也报 (TstLetter 继承 Model, extra forbid); 请求里的模型没登记报 INVALID_VALUE. 通过时返回的结果证据指针
    按文件登记改写, 其余原样."""
    request = ManualRequest(instructions=INSTRUCTIONS, result_model=result_model, min_evidence=2, material={})
    task = ReviewTask(
        id=TASK,
        kind=TaskKind.manual,
        procedure_id=TST,
        target_type=TargetType.ProcedureRun,
        target_id=RUN,
        target_version=1,
        run_id=RUN,
        subject_id="RESULT",
        created_at=NOW,
        manual_request=request,
    )
    record = submitted(decided_at=decided_at, manual_result=letter_result(performed_on=performed_on, details=details))
    if problem is None:
        assert record.manual_result is not None
        assert check_manual_result(record, task, [LETTER]) == as_registered(record.manual_result)
        return
    with pytest.raises(DecisionRejected) as e:
        check_manual_result(record, task, [LETTER])
    assert e.value.code == "INVALID_VALUE" and problem in str(e.value)


def test_manual_request_goes_with_manual_tasks_only() -> None:
    """ReviewTask 的模型校验: manual 任务须带请求, 其余类型不能带 (不靠核对时的 assert)."""
    request = ManualRequest(instructions=INSTRUCTIONS, result_model=None, min_evidence=0, material={})
    fields: dict[str, Any] = {
        "id": TASK,
        "procedure_id": TST,
        "target_type": TargetType.ProcedureRun,
        "target_id": RUN,
        "target_version": 1,
        "run_id": RUN,
        "created_at": NOW,
    }
    with pytest.raises(ValidationError, match="manual 任务须带 manual_request"):
        ReviewTask(kind=TaskKind.manual, **fields)
    with pytest.raises(ValidationError, match="其余类型不能带"):
        ReviewTask(kind=TaskKind.failed, manual_request=request, **fields)
    assert ReviewTask(kind=TaskKind.manual, manual_request=request, **fields).manual_request == request
    with pytest.raises(ValidationError, match="min_evidence"):
        ManualRequest(instructions=INSTRUCTIONS, result_model=None, min_evidence=-1, material={})


# ── 派发片段 (纯函数) ──


def dispatch(*tasks: ReviewTask, **fields: Any) -> tuple[StatePatch, dict[str, Any]]:
    """TST-M01 的派发片段 (本次执行 succeeded 与给定任务, 另加 fields) 与 check_dispatch 的其余参数."""
    run = finished(make_run(TST))
    patch: StatePatch = {"procedure_runs": [run], "review_tasks": list(tasks), **fields}
    state = initial_state(ENGAGEMENT, [make_procedure(TST, Phase.P3_Completion)], 1)
    return patch, {"procedure_id": TST, "run_id": run.id, "state": state}


def manual_task(kind: TaskKind = TaskKind.manual) -> ReviewTask:
    request = ManualRequest(instructions=INSTRUCTIONS, result_model=None, min_evidence=0, material={})
    return ReviewTask(
        id=TASK,
        kind=kind,
        procedure_id=TST,
        target_type=TargetType.ProcedureRun,
        target_id=RUN,
        target_version=1,
        run_id=RUN,
        subject_id="RESULT",
        created_at=NOW,
        manual_request=request if kind is TaskKind.manual else None,
    )


def test_dispatch_patch_holds_the_run_and_manual_tasks_only(tmp_path: Path) -> None:
    """派发片段 (check_dispatch) 由 A10 写, 只能含本次执行记录与 manual 任务: 另带 A10 写入范围内的 report_versions
    (模板 outputs 不列, 按 DISPATCH_FIELDS 查) 被拒, 建 failed 任务被拒. 写入者取 writer_of: 人工程序不论由哪个
    Agent 执行都是 A10; 按执行它的非 A10 Agent (脚本 Agent 为 A2, 不能写 review_tasks) 查则被拒."""
    patch, where = dispatch(manual_task())
    check_dispatch(patch, writer=AgentId.A10, **where)
    assert DISPATCH_FIELDS == ("review_tasks",)

    with pytest.raises(MergeRejected, match="未声明产出 report_versions"):
        check_dispatch(dispatch(manual_task(), report_versions=[])[0], writer=AgentId.A10, **where)
    with pytest.raises(MergeRejected, match=f"派发片段只能建 manual 任务, {TASK} 不是"):
        check_dispatch(dispatch(manual_task(TaskKind.failed))[0], writer=AgentId.A10, **where)

    template = manual_library(tmp_path / "knowledge").load(TST)
    agent = ScriptAgent(make_deps(FakeRegistry(), FakeTrail()), lambda ctx: {})
    assert (agent.agent_id, writer_of(template, agent)) == (AgentId.A2, AgentId.A10)
    with pytest.raises(MergeRejected, match="review_tasks"):
        check_dispatch(patch, writer=agent.agent_id, **where)
