"""需求 R161: FIN-08 错报沟通, 人工任务与管理层回复登记 (人工程序, 需求 R92, R93).

Xero 演示上把主图放在 P2 结清处: 检查点以 fan_in 身份写入 misstatement_fakes.fin03_state 的两条错报的已分类版本 (v2,
同 R159 确认后) 或给定的错报, 程序包为 FIN-03 与 FIN-08. 分诊节点执行 FIN-03 (没有未分类的错报, 空执行), 之后进
communicate: A10 派发 FIN-08, 材料由 A8 的 prepare 生成, 节点内等审计师提交 manual_result. 主图缺省在
await_correction 与 verify_correction 之前停下 (本文件只看沟通; 等待更正见 test_correction_notice).
登记取补造的中性格式 manual/fin08_round1.json, fin08_round2.json, 由 xero_fakes.registration 映射成 ManagementResponses.
收集阶段不读快照与补造 (参数化只放描述, 数据在用例内取).
"""

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any

import pytest

from backend.agents.workpaper import CORRECTION_REQUEST, after_response, to_communicate
from backend.contracts.errors import VersionConflict
from backend.contracts.models import (
    AgentId,
    Decision,
    Judgment,
    ManagementResponse,
    ManualResult,
    Misstatement,
    MisstatementStatus,
    Procedure,
    ProcedureRun,
    ProcedureStatus,
    RerunRequest,
    ReviewRecord,
    ReviewTask,
    TaskKind,
    TrailKind,
    WorkingLanguage,
)
from backend.orchestration.runner import GraphRunner, waiting_payloads
from backend.services.features import BASELINE, MISSTATEMENT
from backend.services.procedure_library import first_misstatement_communicated_at
from tests.fakes import (
    ENGAGEMENT,
    FakeTrail,
    entries,
    fin08_registered,
    finished,
    latest,
    make_ctx,
    make_run,
    record_writers,
    start_at_fan_in,
    submit_and_wait,
    wait_idle,
)
from tests.live_walk import CJK
from tests.misstatement_fakes import (
    CSH02,
    ROUND2,
    XJ499,
    communicated_fields,
    fin03_context,
    fin03_deps,
    fin03_state,
    round1_communicated,
    round1_misstatements,
)
from tests.test_decisions import Started, assert_rejected
from tests.test_rerun import CrashAfterCommit
from tests.test_review_decisions import record
from tests.xero_fakes import registration, round_file, xero_engagement

pytestmark = pytest.mark.external  # Xero 快照与补造

EID = ENGAGEMENT.id  # 同 xero_engagement().id; 收集阶段不读快照
CONFIG = GraphRunner.config(EID)
FEATURES = BASELINE | {MISSTATEMENT}
FIN08 = "FIN-08"
STOPS = ("await_correction", "verify_correction")  # 等待更正 (R162) 之前停下
DECIDED_1 = datetime(2026, 10, 20, tzinfo=UTC)  # 第 1 轮登记的决定时刻 (悉尼 10-20 11:00)
DECIDED_2 = datetime(2026, 10, 22, tzinfo=UTC)  # 第 2 轮 (悉尼 10-22 11:00)


def registered(
    task_id: str, rows: list[dict[str, Any]], performed_on: date, decided_at: datetime, oid: str = "D-M1"
) -> ReviewRecord:
    """对 FIN-08 的 manual 任务提交登记 oid (manual_result.details.responses)."""
    result = ManualResult(conclusion="已与管理层沟通并登记回复", performed_on=performed_on, details={"responses": rows})
    return record(task_id, Decision.manual_result, id=oid, manual_result=result, decided_at=decided_at)


async def at_communicate(
    misstatements: list[Misstatement] | None = None,
    runs: list[ProcedureRun] | None = None,
    store: Any = None,
    history: dict[str, list[Any]] | None = None,
    stops: Sequence[str] = STOPS,
) -> Started:
    """见模块 docstring: 推进到 communicate 停下 (等 FIN-08 的登记) 或之后的停点 (stops 之前). misstatements 缺省为
    第 1 轮的两条; runs 为之前各轮的执行记录 (定下本轮的 run_no), history 为之前各轮的登记 (fakes.fin08_registered,
    只进状态). Store 另有错报的各个版本."""
    deps = fin03_deps(FakeTrail(), features=FEATURES, output_store=store)
    misstatements = round1_misstatements() if misstatements is None else misstatements
    pack = [make_ctx(deps, pid, engagement=xero_engagement()).procedure for pid in ("FIN-03", FIN08)]
    versions = [m.model_copy(update={"version": v}) for m in misstatements for v in range(1, m.version)]
    state = fin03_state(misstatements=misstatements, procedure_runs=runs or [], **(history or {}))
    runner = await start_at_fan_in(deps, state, pack, stops, [*versions, *misstatements, *(runs or [])])
    return Started(runner, deps.output_store, deps.engagement_store)  # type: ignore[arg-type]


def task_of(run_no: int) -> str:
    return f"T-RUN-FIN-08-{run_no}-v1"


def payload(run_no: int, records: list[str]) -> dict[str, Any]:
    return {"awaiting": "human", "procedure_id": FIN08, "run_id": f"RUN-FIN-08-{run_no}", "records": records}


def listed(m: Misstatement, source: Judgment) -> dict[str, Any]:
    """材料中的一条错报 (验收 2 列的各项, JSON 形式; 来源判断的结论与执行, 需求 R340)."""
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
        "source_conclusion": source.conclusion,
        "source_run_id": source.run_id,
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
    sources = {j.id: j for j in fin03_state()["judgments"]}

    assert latest(s, ProcedureRun, "RUN-FIN-08-1").status == "succeeded"
    task = latest(s, ReviewTask, task_of(1))
    assert (task.kind, task.subject_id, task.target_id) == (TaskKind.manual, "RESULT", "RUN-FIN-08-1")
    assert task.manual_request.material == {
        "request": CORRECTION_REQUEST["en"],  # mock 的执行记 en (需求 R343)
        "misstatements": [listed(m, sources[m.judgment_id]) for m in (xj499, csh02)],
    }
    assert [(row["amount_cents"], row["direction"]) for row in task.manual_request.material["misstatements"]] == [
        (75000, "overstated"),
        (4853, "understated"),
    ]
    fin08 = latest(s, Procedure, FIN08)
    assert (fin08.version, fin08.status) == (2, ProcedureStatus.awaiting_human)
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
    writers = record_writers(monkeypatch, s.output_store)
    await submit_and_wait(s, registered(task_of(1), registration(1, [xj499, csh02]), date(2026, 10, 19), DECIDED_1))

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
    assert [(e.idempotency_key, e.procedure_id) for e in entries(s, TrailKind.decision)] == [("D-M1", FIN08)]
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


@pytest.mark.parametrize("language", ["en", "zh"])
def test_request_in_run_language(language: WorkingLanguage) -> None:
    """需求 R357: 材料里的更正请求取本次执行的生成语言 (run.language, 即 AgentDeps.run_language); en 的没有中文."""
    deps = fin03_deps(FakeTrail(), features=FEATURES)
    ctx = fin03_context(deps, fin03_state(misstatements=round1_misstatements()), FIN08)
    material = deps.manual_owners[FIN08].prepare(replace(ctx, run=ctx.run.model_copy(update={"language": language})))
    assert material is not None
    request = material["request"]
    assert (request, CJK.search(request) is not None) == (CORRECTION_REQUEST[language], language == "zh")


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
    await submit_and_wait(s, registered(task_of(1), rows, date(2026, 10, 19), DECIDED_1))

    data = round_file(1)
    for mid in (XJ499, CSH02):
        written = latest(s, Misstatement, mid)
        expected = (
            (MisstatementStatus.uncorrected, "refused") if mid in refused else (MisstatementStatus.communicated, None)
        )
        assert (written.version, written.status, written.uncorrected_basis) == (3, *expected)
        assert (written.management_response, written.needs_recommunication) == (data["response"], False)
    assert (await s.runner.app.aget_state(CONFIG)).next == (after,)


async def test_round2_lists_only_new_misstatements() -> None:
    """验收 5: 第 1 轮的两条已沟通 (没有重新沟通的标记, 状态里有第 1 轮的登记), 第 2 轮三条已分类: RUN-FIN-08-2,
    T-RUN-FIN-08-2-v1, 材料只列三条新的; 登记第 2 轮 (10-21 15:00 沟通, 10-22 09:00 回复) 后三条 v3 communicated,
    沟通时间为第 2 轮的; 首次沟通时间仍为第 1 轮的 (取自登记)."""
    first = fin08_registered(1, registration(1, round1_misstatements()), DECIDED_1)
    s = await at_communicate([*round1_communicated(), *ROUND2], runs=[finished(make_run(FIN08))], history=first)

    task = latest(s, ReviewTask, task_of(2))
    assert [row["id"] for row in task.manual_request.material["misstatements"]] == [m.id for m in ROUND2]
    await submit_and_wait(s, registered(task_of(2), registration(2, ROUND2), date(2026, 10, 21), DECIDED_2, "D-M2"))

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
    fields = communicated_fields(2)
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
    await submit_and_wait(s, registered(task_of(3), [row], date(2026, 10, 22), datetime(2026, 10, 22, 5, tzinfo=UTC)))

    written = latest(s, Misstatement, f03.id)
    status = MisstatementStatus.communicated if agrees else MisstatementStatus.uncorrected
    assert (written.version, written.status, written.needs_recommunication) == (6, status, False)
    assert (written.communicated_at, written.responded_at, written.management_response) == (
        at(RECOMMUNICATED_AT),
        at(REPLIED_AT),
        row["response"],
    )


def rows_with(
    changes: dict[str, Any] | None = None,
    extra: Sequence[dict[str, Any]] = (),
    drop: int = 0,
    without: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """第 1 轮的登记: 第一行套上 changes 并去掉 without 中的键, 去掉末尾 drop 行, 再加 extra 各项套在原第一行上的行."""
    rows = registration(1, round1_misstatements())
    first = rows[0]
    rows[0] = {k: v for k, v in (first | (changes or {})).items() if k not in without}
    return rows[: len(rows) - drop] + [first | e for e in extra]


LATE = "2026-10-20T01:00:00Z"  # 晚于决定时刻 DECIDED_1
NAIVE = "2026-10-19T15:00:00"  # 不带时区 (前端 datetime-local 的缺省写法)
ON_19 = date(2026, 10, 19)


@pytest.mark.parametrize(
    ("edit", "performed_on", "code", "detail"),
    [
        ({"drop": 1}, ON_19, "INVALID_VALUE", f"漏了材料中的错报 {CSH02}"),
        ({"extra": [{"misstatement_id": "M-D-OTHER"}]}, ON_19, "INVALID_VALUE", "材料中没有的错报 M-D-OTHER"),
        ({"extra": [{}]}, ON_19, "INVALID_VALUE", f"{XJ499} 登记了 2 行"),
        ({"changes": {"base_version": 1}}, ON_19, "VERSION_MISMATCH", f"{XJ499} 的登记依据第 1 版, 材料中为第 2 版"),
        ({"changes": {"response": ""}}, ON_19, "MISSING_FIELD", "responses.0.response"),
        ({"changes": {"communicated_to": "  "}}, ON_19, "MISSING_FIELD", "responses.0.communicated_to"),
        # 空白之外另有不合 (缺键): 不是只缺必填文字, 报 INVALID_VALUE
        ({"changes": {"response": ""}, "without": ["agrees_to_correct"]}, ON_19, "INVALID_VALUE", "agrees_to_correct"),
        ({"changes": {"communicated_at": LATE, "responded_at": LATE}}, ON_19, "INVALID_VALUE", "沟通时间"),
        ({"changes": {"responded_at": LATE}}, ON_19, "INVALID_VALUE", "回复时间"),
        ({"changes": {"responded_at": "2026-10-18T00:00:00Z"}}, ON_19, "INVALID_VALUE", "responses.0"),  # 回复早于沟通
        # 时间不带时区 (第 1 轮审核 R161-01): 结果模型报错, 不在与决定时刻比较时出 500
        ({"changes": {"communicated_at": NAIVE, "responded_at": NAIVE}}, ON_19, "INVALID_VALUE", "communicated_at"),
        ({"changes": {"responded_at": NAIVE}}, ON_19, "INVALID_VALUE", "responses.0.responded_at"),  # 只回复不带
        ({}, date(2026, 10, 21), "INVALID_VALUE", "晚于决定当天"),  # 通用核对
    ],
    ids=[
        "missing",
        "extra",
        "twice",
        "base_version",
        "no_response",
        "no_recipient",
        "blank_and_missing_key",
        "communicated_late",
        "replied_late",
        "replied_first",
        "naive_times",
        "naive_reply",
        "date",
    ],
)
async def test_registration_checked_before_writing(
    edit: dict[str, Any], performed_on: date, code: str, detail: str
) -> None:
    """验收 7: 提交时核对登记 (A8 的 check 在通用核对之后), 不过时 Store, 留痕与检查点都不变: 漏列, 多列材料中的
    错报, 同一错报两行 → INVALID_VALUE; 依据的不是材料中的版本 → VERSION_MISMATCH; 回复原文或沟通对象为空白 →
    MISSING_FIELD (结果模型; 另有缺键时 INVALID_VALUE); 沟通或回复晚于决定时刻 → INVALID_VALUE; 结果模型与通用核对
    照常 (回复早于沟通, 时间不带时区, 执行日期晚于决定当天)."""
    s = await at_communicate()
    await assert_rejected(s, registered(task_of(1), rows_with(**edit), performed_on, DECIDED_1), code, detail)


async def test_registration_at_decision_time_accepted() -> None:
    """沟通与回复恰在决定时刻 (不晚于) 时通过, 照常整理出 v3 communicated."""
    s = await at_communicate()
    now = DECIDED_1.isoformat()
    await submit_and_wait(
        s, registered(task_of(1), rows_with({"communicated_at": now, "responded_at": now}), ON_19, DECIDED_1)
    )
    written = latest(s, Misstatement, XJ499)
    assert (written.version, written.communicated_at, written.responded_at) == (3, DECIDED_1, DECIDED_1)


async def test_nothing_to_communicate_runs_empty() -> None:
    """验收 8: 没有可沟通的错报 (已分类的 identified 与带标记的 communicated 都没有; 这里只有已沟通而等更正的两条):
    RUN-FIN-08-1 空执行成功, 不建任务, 不等人, 程序 completed, 继续去等待更正."""
    s = await at_communicate(round1_communicated())

    assert latest(s, ProcedureRun, "RUN-FIN-08-1").status == "succeeded"
    assert [t.id for t in s.output_store.rows_of(EID) if isinstance(t, ReviewTask) and t.procedure_id == FIN08] == []
    assert latest(s, Procedure, FIN08).status is ProcedureStatus.completed
    assert (await s.runner.app.aget_state(CONFIG)).next == ("await_correction",)


def round1() -> ReviewRecord:
    """第 1 轮的登记 D-M1 (补造记录, 都同意)."""
    return registered(task_of(1), registration(1, round1_misstatements()), ON_19, DECIDED_1)


async def test_registration_recovered_after_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    """验收 10 前半: 登记写进 Store 而后台没继续 (检查点未跟上): 再提交报 RESUME_REQUIRED; 新 runner 启动时 recover
    把登记交回 communicate, A8 整理出两条 v3 communicated, 之后去等待更正."""
    s = await at_communicate()
    with monkeypatch.context() as m:
        m.setattr(s.runner, "launch", lambda eid, graph_input: s.runner.release(eid))
        await s.submit.submit(EID, round1())
    await assert_rejected(s, round1().model_copy(update={"id": "D-M2"}), "RESUME_REQUIRED", "D-M1")

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
    await s.submit.submit(EID, round1())
    await wait_idle(s.runner, EID)
    assert "被杀" in (s.runner.run_error(EID) or "")
    rows = list(store.workflow_rows_of(EID))

    await s.runner.app.ainvoke(None, CONFIG)

    assert prepared == []
    assert store.workflow_rows_of(EID) == rows
    assert [latest(s, Misstatement, m).version for m in (XJ499, CSH02)] == [3, 3]
    assert (await s.runner.app.aget_state(CONFIG)).next == ("await_correction",)
