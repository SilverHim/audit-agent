"""需求 R161: FIN-08 错报沟通, 人工任务与管理层回复登记 (人工程序, 需求 R92, R93).

Xero 演示上把主图放在 P2 结清处: 检查点以 fan_in 身份写入 test_workpaper.fin03_state 的两条错报的已分类版本 (v2,
同 R159 确认后) 或给定的错报, 程序包为 FIN-03 与 FIN-08. 分诊节点执行 FIN-03 (没有未分类的错报, 空执行), 之后进
communicate: A10 派发 FIN-08, 材料由 A8 的 prepare 生成, 节点内等审计师提交 manual_result. 主图在 await_correction 与
verify_correction 之前停下 (等待更正由 R162 实现).
登记取补造的中性格式 manual/fin08_round1.json, fin08_round2.json, 由 registration 映射成 ManagementResponses.
"""

import json
from datetime import UTC, date, datetime
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from backend.agents.base import AgentDeps
from backend.agents.workpaper import CORRECTION_REQUEST, after_response, to_communicate
from backend.contracts.errors import VersionConflict
from backend.contracts.models import (
    AgentId,
    Assertion,
    Decision,
    Direction,
    Judgment,
    ManagementResponse,
    ManualResult,
    Misstatement,
    MisstatementStatus,
    MisstatementType,
    Procedure,
    ProcedureRun,
    ProcedureStatus,
    RerunRequest,
    ReviewRecord,
    ReviewTask,
    TaskKind,
    TrailKind,
)
from backend.orchestration.checkpoint import checkpoint_serde
from backend.orchestration.gates import gate_nodes
from backend.orchestration.graph import build_graph
from backend.orchestration.nodes import GraphNodes
from backend.orchestration.runner import GraphRunner, waiting_payloads
from backend.services.features import BASELINE, MISSTATEMENT
from backend.services.procedure_library import first_misstatement_communicated_at
from tests.external import supplements_dir
from tests.fakes import FakeTrail, finished, make_ctx, make_run, wait_idle
from tests.test_decisions import Started, assert_rejected
from tests.test_rerun import CrashAfterCommit
from tests.test_review_decisions import record
from tests.test_workpaper import FACTUAL, classified, fin03_deps, fin03_state
from tests.xero_fakes import xero_engagement

pytestmark = pytest.mark.external  # Xero 快照与补造

EID = xero_engagement().id
CONFIG = GraphRunner.config(EID)
FEATURES = BASELINE | {MISSTATEMENT}
FIN08 = "FIN-08"
XJ499, CSH02 = "M-D-XJ499", "M-D-CSH02-091"
STOPS = ("await_correction", "verify_correction")  # 等待更正 (R162) 之前停下
DECIDED_1 = datetime(2026, 10, 20, tzinfo=UTC)  # 第 1 轮登记的决定时刻 (悉尼 10-20 11:00)
DECIDED_2 = datetime(2026, 10, 22, tzinfo=UTC)  # 第 2 轮 (悉尼 10-22 11:00)


def round_file(n: int) -> dict[str, Any]:
    """补造的第 n 轮沟通记录 (中性格式)."""
    path = supplements_dir("20261017-q1") / "manual" / f"fin08_round{n}.json"
    return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def registration(n: int, misstatements: list[Misstatement], agrees: dict[str, bool] | None = None) -> list[dict]:
    """第 n 轮记录映射成 ManagementResponses 的行 (JSON 形式, 同前端提交): misstatement_sources 为错报的来源判断,
    每条一行, 依据的版本为当前版本; 沟通对象取 recipient; agrees 缺省都同意 (补造的两轮都同意)."""
    data = round_file(n)
    by_source = {m.judgment_id: m for m in misstatements}
    return [
        {
            "misstatement_id": by_source[source].id,
            "base_version": by_source[source].version,
            "communicated_at": data["communicated_at"],
            "communicated_to": data["recipient"],
            "responded_at": data["responded_at"],
            "response": data["response"],
            "agrees_to_correct": (agrees or {}).get(by_source[source].id, True),
        }
        for source in data["misstatement_sources"]
    ]


def registered(task_id: str, rows: list[dict], performed_on: date, decided_at: datetime, **fields: Any) -> ReviewRecord:
    """对 FIN-08 的 manual 任务提交登记 (manual_result.details.responses)."""
    result = ManualResult(conclusion="已与管理层沟通并登记回复", performed_on=performed_on, details={"responses": rows})
    return record(task_id, Decision.manual_result, id="D-M1", manual_result=result, decided_at=decided_at, **fields)


def round1_misstatements() -> list[Misstatement]:
    """第 1 轮前: 两条错报已分类 (v2, 同 R159 确认后), identified."""
    return [classified(m, **FACTUAL) for m in fin03_state()["misstatements"]]


def runner_for(deps: AgentDeps) -> GraphRunner:
    nodes = GraphNodes(deps, 2)
    app = build_graph(nodes, deps.features).compile(
        checkpointer=InMemorySaver(serde=checkpoint_serde()), interrupt_before=[*gate_nodes(deps.features), *STOPS]
    )
    return GraphRunner(app, deps)


async def at_communicate(
    misstatements: list[Misstatement] | None = None, runs: list[ProcedureRun] | None = None, store: Any = None
) -> Started:
    """见模块 docstring: 推进到 communicate 停下 (等 FIN-08 的登记) 或之后的停点. misstatements 缺省为第 1 轮的两条;
    runs 为之前各轮的执行记录 (定下本轮的 run_no). Store 另有错报的各个版本."""
    deps = fin03_deps(FakeTrail(), features=FEATURES, output_store=store)
    misstatements = round1_misstatements() if misstatements is None else misstatements
    pack = [make_ctx(deps, pid, engagement=xero_engagement()).procedure for pid in ("FIN-03", FIN08)]
    output_store = deps.output_store
    versions = [m.model_copy(update={"version": v}) for m in misstatements for v in range(1, m.version)]
    async with output_store.transaction(EID):
        await output_store.put(EID, [*pack, *versions, *misstatements, *(runs or [])], actor=AgentId.A1)
    runner = runner_for(deps)
    state = fin03_state(misstatements=misstatements, procedure_runs=runs or [])
    await runner.app.aupdate_state(CONFIG, state | {"procedure_pack": pack}, as_node="fan_in")
    runner.acquire(EID)
    runner.launch(EID, None)
    await wait_idle(runner, EID)
    assert runner.run_error(EID) is None
    return Started(runner, output_store, deps.engagement_store)  # type: ignore[arg-type]


def latest(s: Started, model: type[Any], oid: str) -> Any:
    return max(
        (r for r in s.output_store.rows_of(EID) if isinstance(r, model) and r.id == oid), key=lambda r: r.version
    )


def task_of(run_no: int) -> str:
    return f"T-RUN-FIN-08-{run_no}-v1"


def payload(run_no: int, records: list[str]) -> dict[str, Any]:
    return {"awaiting": "human", "procedure_id": FIN08, "run_id": f"RUN-FIN-08-{run_no}", "records": records}


async def submit(s: Started, decision: ReviewRecord) -> None:
    await s.submit.submit(EID, decision)
    await wait_idle(s.runner, EID)
    assert s.runner.run_error(EID) is None


def listed(m: Misstatement, conclusion: str | None) -> dict[str, Any]:
    """材料中的一条错报 (验收 2 列的各项, JSON 形式)."""
    return {
        "id": m.id,
        "version": m.version,
        "account": m.account,
        "account_code": m.account_code,
        "assertion": m.assertion.value,
        "amount_cents": m.amount_cents,
        "direction": m.direction.value,
        "type": m.type.value,
        "fraud_indicative": m.fraud_indicative,
        "source_conclusion": conclusion,
    }


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


async def test_round1_dispatched_registered_and_collected(monkeypatch: pytest.MonkeyPatch) -> None:
    """验收 2, 3, 9, 一次运行:
    - 派发: 分诊之后进 communicate, RUN-FIN-08-1 succeeded, T-RUN-FIN-08-1-v1 (manual, 对象 RESULT), 材料逐条列两条
      已分类错报 (v2, 710 75000 高估, 270 4853 低估, 都是 factual 无舞弊迹象, 来源判断的结论) 与更正请求; FIN-08 v2
      awaiting_human; 停在 communicate, 等待载荷同其他人工等待.
    - 登记第 1 轮 (补造记录: 10-19 15:00 沟通, 10-20 09:00 回复, 都同意): 节点读回后调 A8 collect 一次, 以 A8 写出两条
      v3 communicated, 沟通与回复四项与登记相同; 不建判断; FIN-08 completed; 1 行 decision 留痕; 下一节点等待更正.
    - first_misstatement_communicated_at 解析为第 1 轮的沟通时间."""
    s = await at_communicate()
    xj499, csh02 = round1_misstatements()
    conclusions = {j.id: j.conclusion for j in fin03_state()["judgments"]}

    assert latest(s, ProcedureRun, "RUN-FIN-08-1").status == "succeeded"
    task = latest(s, ReviewTask, task_of(1))
    assert (task.kind, task.subject_id, task.target_id) == (TaskKind.manual, "RESULT", "RUN-FIN-08-1")
    assert task.manual_request.material == {
        "request": CORRECTION_REQUEST,
        "misstatements": [listed(m, conclusions[m.judgment_id]) for m in (xj499, csh02)],
    }
    assert [(row["amount_cents"], row["direction"]) for row in task.manual_request.material["misstatements"]] == [
        (75000, "overstated"),
        (4853, "understated"),
    ]
    assert latest(s, Procedure, FIN08).status is ProcedureStatus.awaiting_human
    snapshot = await s.runner.app.aget_state(CONFIG)
    assert snapshot.next == ("communicate",)
    assert waiting_payloads(snapshot) == [payload(1, [])]

    owner = s.runner.deps.manual_owners[FIN08]
    collected: list[str] = []
    collect = owner.collect

    def counting(ctx: Any, decided: ReviewRecord) -> Any:
        collected.append(decided.id)
        return collect(ctx, decided)

    monkeypatch.setattr(owner, "collect", counting)
    writers: dict[tuple[str, int], str] = {}
    put = s.output_store.put

    async def recording(eid: str, objs: Any, *, actor: str) -> Any:
        for o in objs:
            writers.setdefault((o.id, o.version), actor)
        return await put(eid, objs, actor=actor)

    monkeypatch.setattr(s.output_store, "put", recording)
    await submit(s, registered(task_of(1), registration(1, [xj499, csh02]), date(2026, 10, 19), DECIDED_1))

    data = round_file(1)
    for m in (XJ499, CSH02):
        written = latest(s, Misstatement, m)
        assert (written.version, written.status, writers[(m, 3)]) == (3, MisstatementStatus.communicated, AgentId.A8)
        assert (
            written.communicated_at,
            written.communicated_to,
            written.responded_at,
            written.management_response,
        ) == (
            at(data["communicated_at"]),
            data["recipient"],
            at(data["responded_at"]),
            data["response"],
        )
    assert collected == ["D-M1"]
    assert [j.id for j in s.output_store.rows_of(EID) if isinstance(j, Judgment) and j.procedure_id == FIN08] == []
    assert latest(s, Procedure, FIN08).status is ProcedureStatus.completed
    trail = s.runner.deps.trail
    assert isinstance(trail, FakeTrail)
    decisions = [e for e in trail.entries if e.kind is TrailKind.decision]
    assert [(e.idempotency_key, e.procedure_id) for e in decisions] == [("D-M1", FIN08)]
    snapshot = await s.runner.app.aget_state(CONFIG)
    assert snapshot.next == ("await_correction",)
    assert first_misstatement_communicated_at(snapshot.values) == at(data["communicated_at"])


def superseded_source() -> dict[str, Any]:
    """CSH-02 第 1 次执行被过期重跑取代 (其判断引出的错报不在当前结果中)."""
    request = RerunRequest(
        id="RR-1",
        procedure_ids=["CSH-02"],
        superseded_run_ids=["RUN-CSH-02-1"],
        data_version=2,
        requested_by="U-1",
        requested_at=DECIDED_1,
    )
    return {"rerun_requests": [request]}


@pytest.mark.parametrize(
    ("status", "changes", "listed"),
    [
        (MisstatementStatus.identified, {}, True),  # 已分类
        (MisstatementStatus.identified, {"classification_judgment_id": None}, False),  # 未分类, 先回分诊
        (MisstatementStatus.communicated, {"needs_recommunication": True}, True),  # 分录对不上, 重新沟通
        (MisstatementStatus.communicated, {}, False),  # 已沟通, 等更正
        (MisstatementStatus.uncorrected, {"uncorrected_basis": "refused"}, False),
    ],
    ids=["classified", "unclassified", "recommunicate", "communicated", "refused"],
)
def test_to_communicate(status: MisstatementStatus, changes: dict[str, Any], listed: bool) -> None:
    """沟通对象: 已分类的 identified 与带重新沟通标记的 communicated; 来源执行被过期重跑取代的不算 (当前结果)."""
    xj499, csh02 = round1_misstatements()
    state = fin03_state(misstatements=[xj499, csh02.model_copy(update={"status": status, **changes})])
    assert [x.id for x in to_communicate(state)] == [XJ499, *([CSH02] if listed else [])]
    assert [x.id for x in to_communicate(state | superseded_source())] == [XJ499]


def test_response_on_stale_version_conflicts() -> None:
    """按回复写新版本时, 回复依据的不是错报当前版本抛 VersionConflict (collect 取的状态与登记对不上时为节点错误)."""
    misstatements = round1_misstatements()
    response = ManagementResponse.model_validate(registration(1, misstatements)[0] | {"base_version": 1})
    xj499 = misstatements[0]
    with pytest.raises(VersionConflict, match=XJ499):
        after_response(xj499, response)


@pytest.mark.parametrize(
    ("refused", "after"),
    [({CSH02}, "await_correction"), ({XJ499, CSH02}, "verify_correction")],
    ids=["one", "both"],
)
async def test_refusal_recorded_as_uncorrected(refused: set[str], after: str) -> None:
    """验收 4: 不同意更正的错报 v3 uncorrected (refused), 带沟通与回复四项, 不须重新沟通; 同意的 communicated. 还有
    已沟通的去等待更正, 都拒绝时去验证."""
    s = await at_communicate()
    rows = registration(1, round1_misstatements(), {m: False for m in refused})
    await submit(s, registered(task_of(1), rows, date(2026, 10, 19), DECIDED_1))

    data = round_file(1)
    for mid in (XJ499, CSH02):
        written = latest(s, Misstatement, mid)
        expected = (
            (MisstatementStatus.uncorrected, "refused") if mid in refused else (MisstatementStatus.communicated, None)
        )
        assert (written.version, written.status, written.uncorrected_basis) == (3, *expected)
        assert (written.management_response, written.needs_recommunication) == (data["response"], False)
    assert (await s.runner.app.aget_state(CONFIG)).next == (after,)


def round2_misstatement(code: str, suffix: str, account: str, amount_cents: int) -> Misstatement:
    """第 2 轮的错报 (FIN-15 第 1 次对科目 code 的判断认定, Xero演示数据集设计第 2.8 节), 已分类 (v2)."""
    m = Misstatement(
        id=f"M-D-{suffix}",
        judgment_id=f"J-FIN-15-1-{code}",
        exception_id=None,
        review_record_id=f"D-{suffix}",
        type=MisstatementType.factual,
        account=account,
        assertion=Assertion.Completeness,
        amount_cents=amount_cents,
        direction=Direction.understated,
        account_code=code,
    )
    return classified(m, f"J-FIN-03-2-{m.id}", **FACTUAL)


ROUND2 = [
    round2_misstatement("826", "F03", "Superannuation payable (826)", 426120),
    round2_misstatement("804", "F04", "Wages payable (804)", 201000),
    round2_misstatement("805", "F05", "Annual leave provision (805)", 268736),
]


def round1_communicated() -> list[Misstatement]:
    """第 1 轮登记之后的两条 (v3 communicated, 等更正); 不再列进沟通材料."""
    data = round_file(1)
    fields = {
        "status": MisstatementStatus.communicated,
        "communicated_at": at(data["communicated_at"]),
        "communicated_to": data["recipient"],
        "responded_at": at(data["responded_at"]),
        "management_response": data["response"],
    }
    return [m.model_copy(update={"version": 3, **fields}) for m in round1_misstatements()]


async def test_round2_lists_only_new_misstatements() -> None:
    """验收 5: 第 1 轮的两条已沟通 (没有重新沟通的标记), 第 2 轮三条已分类: RUN-FIN-08-2, T-RUN-FIN-08-2-v1, 材料只列
    三条新的; 登记第 2 轮 (10-21 15:00 沟通, 10-22 09:00 回复) 后三条 v3 communicated, 沟通时间为第 2 轮的; 首次沟通
    时间仍为第 1 轮的."""
    s = await at_communicate([*round1_communicated(), *ROUND2], runs=[finished(make_run(FIN08))])

    task = latest(s, ReviewTask, task_of(2))
    assert [row["id"] for row in task.manual_request.material["misstatements"]] == [m.id for m in ROUND2]
    await submit(s, registered(task_of(2), registration(2, ROUND2), date(2026, 10, 21), DECIDED_2))

    data = round_file(2)
    for m in ROUND2:
        written = latest(s, Misstatement, m.id)
        assert (written.version, written.status, written.communicated_at) == (
            3,
            MisstatementStatus.communicated,
            at(data["communicated_at"]),
        )
    snapshot = await s.runner.app.aget_state(CONFIG)
    assert first_misstatement_communicated_at(snapshot.values) == at(round_file(1)["communicated_at"])


def needs_recommunication() -> list[Misstatement]:
    """第 2 轮验证之后: M-D-F03 分录对不上, v5 communicated 且须重新沟通 (沟通字段为第 2 轮的); M-D-F04 已沟通, 没有
    标记 (等更正), 不再列."""
    data = round_file(2)
    fields = {
        "status": MisstatementStatus.communicated,
        "communicated_at": at(data["communicated_at"]),
        "communicated_to": data["recipient"],
        "responded_at": at(data["responded_at"]),
        "management_response": data["response"],
    }
    f03, f04, _ = ROUND2
    return [
        f03.model_copy(update={"version": 5, "needs_recommunication": True, "correction_notice_id": "D-CN2", **fields}),
        f04.model_copy(update={"version": 3, **fields}),
    ]


RECOMMUNICATED_AT = "2026-10-22T03:00:00Z"  # 再次沟通 (悉尼 10-22 14:00)
REPLIED_AT = "2026-10-22T04:00:00Z"


@pytest.mark.parametrize("agrees", [True, False], ids=["agrees", "refuses"])
async def test_recommunication_lists_flagged_only(agrees: bool) -> None:
    """验收 6: 须重新沟通的 M-D-F03 (communicated, 带标记) 进材料, 已沟通而没有标记的 M-D-F04 不进; 同意 → v6
    communicated, 标记清掉, 沟通字段为本次的; 拒绝 → v6 uncorrected (refused)."""
    f03, f04 = needs_recommunication()
    s = await at_communicate([f03, f04], runs=[finished(make_run(FIN08, n)) for n in (1, 2)])

    task = latest(s, ReviewTask, task_of(3))
    assert [(row["id"], row["version"]) for row in task.manual_request.material["misstatements"]] == [(f03.id, 5)]
    row = {
        "misstatement_id": f03.id,
        "base_version": 5,
        "communicated_at": RECOMMUNICATED_AT,
        "communicated_to": round_file(2)["recipient"],
        "responded_at": REPLIED_AT,
        "response": "Journal AJE-X02 will be reversed and re-posted to account 826.",
        "agrees_to_correct": agrees,
    }
    await submit(s, registered(task_of(3), [row], date(2026, 10, 22), datetime(2026, 10, 22, 5, tzinfo=UTC)))

    written = latest(s, Misstatement, f03.id)
    status = MisstatementStatus.communicated if agrees else MisstatementStatus.uncorrected
    assert (written.version, written.status, written.needs_recommunication) == (6, status, False)
    assert (written.communicated_at, written.responded_at, written.management_response) == (
        at(RECOMMUNICATED_AT),
        at(REPLIED_AT),
        row["response"],
    )


def rows_with(changes: dict[str, Any] | None = None, extra: list[dict] | None = None, drop: int = 0) -> list[dict]:
    """第 1 轮的登记, 第一行套上 changes, 去掉末尾 drop 行, 再加 extra 行."""
    rows = registration(1, round1_misstatements())
    rows[0] = rows[0] | (changes or {})
    return rows[: len(rows) - drop] + (extra or [])


ONE = registration(1, round1_misstatements())[0]  # M-D-XJ499 那一行
LATE = "2026-10-20T01:00:00Z"  # 晚于决定时刻 DECIDED_1


ON_19 = date(2026, 10, 19)


@pytest.mark.parametrize(
    ("rows", "performed_on", "code", "detail"),
    [
        (rows_with(drop=1), ON_19, "INVALID_VALUE", f"漏了材料中的错报 {CSH02}"),
        (
            rows_with(extra=[ONE | {"misstatement_id": "M-D-OTHER"}]),
            ON_19,
            "INVALID_VALUE",
            "材料中没有的错报 M-D-OTHER",
        ),
        (rows_with(extra=[ONE]), ON_19, "INVALID_VALUE", f"{XJ499} 登记了 2 行"),
        (rows_with({"base_version": 1}), ON_19, "VERSION_MISMATCH", f"{XJ499} 的登记依据第 1 版, 材料中为第 2 版"),
        (rows_with({"response": ""}), ON_19, "MISSING_FIELD", "responses.0.response"),
        (rows_with({"communicated_to": "  "}), ON_19, "MISSING_FIELD", "responses.0.communicated_to"),
        (rows_with({"communicated_at": LATE, "responded_at": LATE}), ON_19, "INVALID_VALUE", "沟通时间"),
        (rows_with({"responded_at": LATE}), ON_19, "INVALID_VALUE", "回复时间"),
        (rows_with({"responded_at": "2026-10-18T00:00:00Z"}), ON_19, "INVALID_VALUE", "responses.0"),  # 回复早于沟通
        (rows_with(), date(2026, 10, 21), "INVALID_VALUE", "晚于决定当天"),  # 通用核对
    ],
    ids=[
        "missing",
        "extra",
        "twice",
        "base_version",
        "no_response",
        "no_recipient",
        "communicated_late",
        "replied_late",
        "replied_first",
        "date",
    ],
)
async def test_registration_checked_before_writing(
    rows: list[dict], performed_on: date, code: str, detail: str
) -> None:
    """验收 7: 提交时核对登记 (A8 的 check 在通用核对之后), 不过时 Store, 留痕与检查点都不变: 漏列, 多列材料中的
    错报, 同一错报两行 → INVALID_VALUE; 依据的不是材料中的版本 → VERSION_MISMATCH; 回复原文或沟通对象为空白 →
    MISSING_FIELD (结果模型); 沟通或回复晚于决定时刻 → INVALID_VALUE; 结果模型与通用核对照常 (回复早于沟通, 执行
    日期晚于决定当天)."""
    s = await at_communicate()
    await assert_rejected(s, registered(task_of(1), rows, performed_on, DECIDED_1), code, detail)


async def test_nothing_to_communicate_runs_empty() -> None:
    """验收 8: 没有可沟通的错报 (已分类的 identified 与带标记的 communicated 都没有; 这里只有已沟通而等更正的两条):
    RUN-FIN-08-1 空执行成功, 不建任务, 不等人, 程序 completed, 继续去等待更正."""
    s = await at_communicate(round1_communicated())

    assert latest(s, ProcedureRun, "RUN-FIN-08-1").status == "succeeded"
    assert [t.id for t in s.output_store.rows_of(EID) if isinstance(t, ReviewTask) and t.procedure_id == FIN08] == []
    assert latest(s, Procedure, FIN08).status is ProcedureStatus.completed
    assert (await s.runner.app.aget_state(CONFIG)).next == ("await_correction",)


ROUND1 = registered(task_of(1), registration(1, round1_misstatements()), date(2026, 10, 19), DECIDED_1)


async def test_registration_recovered_after_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    """验收 10 前半: 登记写进 Store 而后台没继续 (检查点未跟上): 再提交报 RESUME_REQUIRED; 新 runner 启动时 recover
    把登记交回 communicate, A8 整理出两条 v3 communicated, 之后去等待更正."""
    s = await at_communicate()
    with monkeypatch.context() as m:
        m.setattr(s.runner, "launch", lambda eid, graph_input: s.runner.release(eid))
        await s.submit.submit(EID, ROUND1)
    await assert_rejected(s, ROUND1.model_copy(update={"id": "D-M2"}), "RESUME_REQUIRED")

    restarted = GraphRunner(s.runner.app, s.runner.deps)
    await restarted.recover()
    await wait_idle(restarted, EID)

    assert restarted.run_error(EID) is None
    assert [latest(s, Misstatement, m).status for m in (XJ499, CSH02)] == [MisstatementStatus.communicated] * 2
    assert (await s.runner.app.aget_state(CONFIG)).next == ("await_correction",)


async def test_collect_replayed_after_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    """验收 10 后半: A8 整理出的错报写进 Store 后, 写检查点前被杀; 再继续时节点从头执行: 不再调 prepare (派发已是
    终态, 取 Store), collect 重算出同一版本, Store 不新增, 去等待更正."""
    store = CrashAfterCommit("")
    s = await at_communicate(store=store)
    store.crash_run = XJ499  # 装配时写过错报, 从这里起才在整理写入后出事
    owner = s.runner.deps.manual_owners[FIN08]
    prepared: list[str] = []
    prepare = owner.prepare

    def counting(ctx: Any) -> Any:
        prepared.append(ctx.run.id)
        return prepare(ctx)

    monkeypatch.setattr(owner, "prepare", counting)
    await s.submit.submit(EID, ROUND1)
    await wait_idle(s.runner, EID)
    assert "被杀" in (s.runner.run_error(EID) or "")
    rows = list(store.rows_of(EID))

    await s.runner.app.ainvoke(None, CONFIG)

    assert prepared == []
    assert store.rows_of(EID) == rows
    assert [latest(s, Misstatement, m).version for m in (XJ499, CSH02)] == [3, 3]
    assert (await s.runner.app.aget_state(CONFIG)).next == ("await_correction",)
