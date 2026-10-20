"""R92 人工程序契约与提交校验 (接口文档第 2.5, 5, 7, 8.1 节).

测试程序 TST-M01 放在临时 knowledge 目录 (完成阶段, owner A10, engine manual, L1, outputs [], manual 段要取得签字的
管理层声明书, 结果模型 TstLetter 为测试登记). 完成节点改为先执行它再执行 FIN-04; 它的脚本 A10 出执行记录与 manual
任务 (派发与材料由 R93 实现), 节点按通用等待 (R87) 停在节点内. 提交 manual_result 后只写决定, 任务 decided, 程序
completed, 不建人工判断; 结果整理成领域对象由 R93 做.
"""

import shutil
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from backend.agents.base import AgentDeps, RunContext
from backend.contracts.errors import DecisionRejected
from backend.contracts.manual_results import RESULT_MODELS
from backend.contracts.models import (
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
from backend.contracts.state import StatePatch
from backend.core.config import get_settings
from backend.orchestration.decisions import check_manual_result
from backend.orchestration.nodes import NODE_PROCEDURES
from backend.orchestration.runner import GraphRunner, waiting_payloads
from backend.services.procedure_library import ProcedureLibrary, manual_program
from backend.services.review_queue import ReviewQueue
from tests.fakes import (
    ENGAGEMENT,
    NOW,
    NUMBAT_PROFILE,
    FakeEngagementStore,
    FakeTrail,
    ScriptA10,
    finished,
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
MANUAL = f"manual:\n  instructions: {INSTRUCTIONS}\n  result_model: TstLetter\n  min_evidence: 1\n"
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


def pointer(source_type: str = "document", source_id: str = LETTER.id, page: int | None = 2) -> EvidencePointer:
    return EvidencePointer(
        type=EvidenceType.voucher, source_type=source_type, source_id=source_id, locator="signature", page=page
    )


def letter_result(**changes: Any) -> ManualResult:
    """取得声明书的结果: 结论, 10-23 执行, 证据为声明书第 2 页 (签字页), 结构化结果合 TstLetter."""
    base = ManualResult(
        conclusion="Signed letter obtained, dated 23 October 2026",
        performed_on=date(2026, 10, 23),
        evidence=[pointer()],
        details={"signed_on": "2026-10-23", "signatory": "Alex Morgan"},
    )
    return base.model_copy(update=changes)


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
    ],
)
def test_library_rejects_malformed_manual_programs(tmp_path: Path, yaml: str, problem: str) -> None:
    """人工程序没写 manual 段, 结果模型没登记, 或放在 P2, 程序库构造即报错, 消息含程序编号 (防派出没有说明或无从
    校验结果的任务, 提交时才发现)."""
    with pytest.raises(ValueError, match=problem):
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
        instructions=INSTRUCTIONS, result_model="TstLetter", min_evidence=1, material=MATERIAL
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
    ("没有证据", submitted(manual_result=letter_result(evidence=[])), "INVALID_VALUE", "至少 1 条"),
    (
        "证据不是文件",
        submitted(manual_result=letter_result(evidence=[pointer("sales_invoice", "SI-2026-0612", None)])),
        "INVALID_VALUE",
        "sales_invoice SI-2026-0612",
    ),
    (
        "文件不在本项目",
        submitted(manual_result=letter_result(evidence=[pointer(source_id="DOC-ffffffffffff")])),
        "INVALID_VALUE",
        "DOC-ffffffffffff",
    ),
    (
        "页码超出",
        submitted(manual_result=letter_result(evidence=[pointer(page=3)])),
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
    - 通过: 一个事务写入决定 (结果原样), 任务 v2 decided 与 TST-M01 completed; 不建判断; 1 行 decision 留痕, 1 行
      transition awaiting_human → completed; 节点读回后继续执行 FIN-04, 停在 FIN-01 闸门."""
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
    assert latest(s, ReviewRecord, "D-M01") == decision  # 结果连同 details 原样存下
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
    ],
)
def test_result_checked_against_the_request(
    result_model: str | None, decided_at: datetime, performed_on: date, details: dict[str, Any], problem: str | None
) -> None:
    """check_manual_result 按任务请求核对: 没有结果模型时 details 须为空; 执行日期按决定时刻的悉尼日期比; 结果模型
    多出的字段也报 (TstLetter 继承 Model, extra forbid)."""
    request = ManualRequest(instructions=INSTRUCTIONS, result_model=result_model, min_evidence=1, material={})
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
        check_manual_result(record, task, [LETTER])
        return
    with pytest.raises(DecisionRejected) as e:
        check_manual_result(record, task, [LETTER])
    assert e.value.code == "INVALID_VALUE" and problem in str(e.value)
