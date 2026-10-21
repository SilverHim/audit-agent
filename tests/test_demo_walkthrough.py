"""需求 R287: 演示公司 (Xero Demo Company (AU)) 在应用里走通. 只经 HTTP 接口, 照页面的做法一步步走 (walk):

客户下拉取第一行 (演示公司) 的默认数据集与期间建项目 → 每个停点按数据管理页概览的 demo.suggested 导入该阶段的演示
资料 (页面按钮的阶段同此): ACC-01 闸门处 acceptance, 等数据就绪时 fieldwork (财务报表以账套为准, 不再导入报表,
Issue #46) → 闸门决定取 GET /gate 的材料 → 总体审计计划先退回一次 (要求写明银行函证的时间) 再批准 (需求 R317) →
复核按任务列表逐条提交 (决定同 R119: BA-091 认定错报, 其余 confirm; GEN-02 的 7 条同 R146: XJ-499 认定错报, XJ-575
推翻为接受, 其余 confirm) → FIN-01 无保留意见 → RPT-01 先退回再批准, 到归档.

大模型只读运行副本 data/xero_demo/20261017-q1/llm (应用里按项目数据集选的目录, 生成脚本从 llm_xero 复制, 正本里没有
fixture 文件的程序由 llm_xero_only17 补位), 不用 R119 的分层客户端; 登记 mock 按项目数据集定位 (DatasetRegistrySource).
功能集为演示配置 (.env.example 的 FEATURES=critic,collab,journals,planning, R155, R146, R317; R119 仍按基线回归).
断言: 例外, 判断, 复核任务, 错报与汇总表为 R119 加 R146 的增量 (复用其断言函数), 报告与归档同 R119 (意见与报告不在
R146, FIN-01 仍按 R119 的决定); 总体审计计划的增量只在本模块断言 (R317, 只在 PLN-15 上: 决定, 留痕, 大模型两次与
阶段推进点); 两个阶段都已导入, 补交请求只剩审批日志 open, 走完没有过期程序. R189 把全接入
的 RPT-03 两稿放进正本后, 运行副本随之换成全接入两稿, 本模块的报告断言 (只开现有 17 条的两稿) 在同一提交里随之改.
不与 R119 共用一次运行: 走 HTTP, 经演示资料导入上传文件, 终态 (文件 21 份) 不同.
读快照与补造目录, 标 external; 连库变体标 slow. 离线的走通按项目的工作语言 en, zh 各走一次 (需求 R343): mock 不另备
中文固定输出, 两次的结果相同, 执行记录都记 en.
"""

from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import pytest_asyncio

from backend.adapters.llm import DatasetFixtureLLMClient, SwitchableLLMClient, make_llm_client
from backend.agents.client_intel import check_directors
from backend.contracts.errors import DecisionRejected
from backend.contracts.models import (
    AcceptanceScorecard,
    AgentId,
    AuditException,
    AuditPlan,
    CorrectionNotice,
    DocumentType,
    GateDecision,
    GateId,
    GateVerdict,
    PlanStatus,
    ProcedureRun,
    ReviewTask,
    RunStatus,
    TaskKind,
    TrailEntry,
    TrailKind,
    WorkingLanguage,
)
from backend.contracts.views import GateView
from backend.core.config import get_settings
from backend.orchestration.decisions import GraphDecisionSubmit
from backend.orchestration.runner import GraphRunner
from backend.persistence.stores import RoutingDatasetLoader
from backend.services.abn import abn_problem
from backend.services.data_service import DataService
from tests.external import supplements_dir
from tests.fakes import (
    PLAN_REJECT_REASON,
    FakeTrail,
    api_client,
    wait_idle,
)
from tests.fixture_sets import XERO
from tests.test_decisions import Started
from tests.test_demo_default import demo_runner  # 演示默认数据集的离线装配 (R79), 按客户排序的用例用
from tests.test_e2e_mock import assert_pointers_resolve, real_storage  # 连库装配 (R56 起多个连库用例共用)
from tests.test_e2e_xero17 import (  # R119 的计数与断言
    AGENTS,
    Xero17,
    assert_documents,
    assert_materiality,
    assert_report,
    assert_results,
)
from tests.xero_fakes import (
    DEMO_FEATURES,
    FIXTURE_READS,
    PLAN_DECISIONS,
    SNAPSHOT_NAME,
    XERO17_ANNOTATION,
    XERO17_BA091_DRAFT,
    XERO17_MATERIALITY,
    XERO_DATASET,
    XERO_PERIOD,
    XERO_PLAN_ROWS,
    xero17_gates,
    xero_journal_reviews,
)
from tests.xero_flow import API, READY_REASON, demo_offline, drain, entered_p2_by, recorded_agents, xero_link
from tests.xero_harness import LayeredMockLLMClient
from tests.xero_injection import changed, originals

pytestmark = pytest.mark.external
shared_loop = pytest.mark.asyncio(loop_scope="module")  # 用模块共用的走通 (walked) 的用例与它同一个事件循环
BA091 = "J-CSH-02-1-BA-091"
REVIEW_ROUNDS = 5  # 复核循环的轮数上限 (本路径分三层出任务, 3 轮即完)
DEMO_ABN = "25900043021"
# 每个停点: (总览的闸门, 是否等数据就绪, 是否等复核, 概览建议导入的阶段); 复核的停点只记第一次
STOPS = [
    ("ACC-01", False, False, "acceptance"),
    (None, True, False, "fieldwork"),
    ("PLN-01/03", False, False, None),
    ("PLN-15", False, False, None),  # 计划第 1 修订, 退回 (需求 R317)
    ("PLN-15", False, False, None),  # 第 2 修订, 批准
    (None, False, True, None),
    ("FIN-01", False, False, None),
    ("RPT-01", False, False, None),
    ("RPT-01", False, False, None),
    (None, False, False, None),
]


@dataclass
class Walk:
    """走完的一次: R119 断言函数用的 Xero17 (x.client 记下了大模型读取), 依次停的位置 (STOPS 的写法), RPT-01 退回决定
    的 ID (接口生成), 最后一次总览, 数据管理页概览与客户列表; 两次停在计划审批时的 GET /gate 与 GET /plan, 归档后的
    GET /plan (需求 R317)."""

    x: Xero17
    stops: list[tuple[str | None, bool, bool, str | None]]
    reject_id: str
    overview: dict[str, Any]
    summary: dict[str, Any]
    clients: list[dict[str, Any]]
    plan_gates: list[dict[str, Any]]
    plan_pages: list[dict[str, Any]]
    final_plan: dict[str, Any]
    progress: list[dict[str, Any]]  # 每个停点总览的 progress (需求 R325), 同 stops 的顺序
    language: WorkingLanguage = "en"  # 建项目时的工作语言


async def walk(
    client: httpx.AsyncClient, runner: GraphRunner, service: DataService, llm: Any, language: WorkingLanguage = "en"
) -> Walk:
    """照页面的做法从新建项目走到归档 (见模块说明), 项目的工作语言为 language; 每步之后等后台作业与图都停下, 后台
    始终没有错误. 另记下每次执行的 Agent 与前后两次原目录 (快照与补造) 的逐文件哈希 (需求 R260)."""
    before = originals()
    with recorded_agents() as agents:
        w = await walk_steps(client, runner, service, llm, language)
    w.x = replace(w.x, agents=agents, moved=changed(before, originals()))
    return w


async def walk_steps(
    client: httpx.AsyncClient, runner: GraphRunner, service: DataService, llm: Any, language: WorkingLanguage
) -> Walk:
    """walk 的各步 (见模块说明)."""
    owner = (await client.get(f"{API}/users/me")).json()["id"]
    clients = (await client.get(f"{API}/clients")).json()
    [dataset] = [d for d in clients[0]["datasets"] if d["default"]]  # 客户下拉默认选中第一行, 期间随数据集带出
    body = {k: dataset[k] for k in ("dataset", "period_start", "period_end")}
    created = await client.post(f"{API}/engagements", json={**body, "working_language": language})
    assert created.status_code == 201, created.text
    eid = created.json()["engagement"]["id"]
    base = f"{API}/engagements/{eid}"
    stops: list[tuple[str | None, bool, bool, str | None]] = []
    progress: list[dict[str, Any]] = []

    async def settle() -> dict[str, Any]:
        await drain(service)
        await wait_idle(runner, eid)
        overview = (await client.get(base)).json()
        assert overview["run_error"] is None, overview["run_error"]
        return overview

    async def stop() -> dict[str, Any]:
        """记下停的位置; 概览建议了阶段就像点页面按钮一样导入它."""
        overview = await settle()
        suggested = (await client.get(f"{base}/data/summary")).json()["demo"]["suggested"]
        stops.append((overview["current_gate"], overview["awaiting_data"], overview["awaiting_review"], suggested))
        progress.append(overview["progress"])
        if suggested is not None:
            started = await client.post(f"{base}/data/demo-import", json={"stage": suggested})
            assert (started.status_code, started.json()["status"]) == (202, "started"), started.text
            overview = await settle()
        return overview

    async def decide(decision: str, **fields: Any) -> dict[str, Any]:
        """对当前闸门的材料 (GET /gate 给的对象与版本) 提交决定, 返回 GET /gate 的视图. 不在闸门 (如某条程序失败,
        流程停下等人) 时 GET /gate 为 null, 报出来."""
        got = await client.get(f"{base}/gate")
        assert got.status_code == 200, got.text
        view = got.json()
        assert view is not None, "不在闸门 (GET /gate 为 null)"
        body = {k: view[k] for k in ("gate", "target_id", "target_version")}
        posted = await client.post(f"{base}/gate-decisions", json={**body, "decision": decision, **fields})
        assert posted.status_code == 202, posted.text
        return cast(dict[str, Any], view)

    await stop()
    await decide("approve")
    await stop()
    ready = await client.post(f"{base}/data/ready", json={"reason": READY_REASON})
    assert ready.status_code == 202, ready.text
    await stop()
    pln = await decide("approve", materiality=XERO17_MATERIALITY.model_dump())

    async def plan_page() -> dict[str, Any]:
        got = await client.get(f"{base}/plan")
        assert got.status_code == 200, got.text
        return cast(dict[str, Any], got.json())

    plan_gates, plan_pages = [], []
    for plan_decision in PLAN_DECISIONS:  # 先退回, 再批准
        await stop()
        plan_pages.append(await plan_page())
        plan_gates.append(await decide(**plan_decision))
    overview = await stop()
    journal_reviews = {r.task_id: r for r in xero_journal_reviews(owner)}
    for _ in range(REVIEW_ROUNDS):
        if not overview["awaiting_review"]:
            break
        pending = (await client.get(f"{base}/review-tasks", params={"status": "pending"})).json()
        assert pending, "等复核却没有待复核任务"
        for view in pending:
            task = view["task"]
            body: dict[str, Any] = {"task_id": task["id"], "target_version": task["target_version"]}
            if task["id"] in journal_reviews:  # GEN-02 的决定同 R146
                fields = {"decision", "reason", "override_conclusion", "misstatement_draft"}
                body |= journal_reviews[task["id"]].model_dump(mode="json", include=fields, exclude_none=True)
            elif task["target_id"] == BA091:
                draft = XERO17_BA091_DRAFT.model_dump(exclude_none=True)
                reason = "Accrued interest of AUD 48.53 confirmed by the bank was not recorded."
                body |= {"decision": "misstatement", "reason": reason, "misstatement_draft": draft}
            else:
                body |= {"decision": "confirm"}
            posted = await client.post(f"{base}/review-records", json=body)
            assert posted.status_code == 202, posted.text
            overview = await settle()
    assert not overview["awaiting_review"], f"{REVIEW_ROUNDS} 轮之后仍在等复核"
    await stop()
    fin = xero17_gates(owner)["fin"]
    await decide("approve", disposition=fin.disposition, reason=fin.reason)
    await stop()
    reject = xero17_gates(owner)["rpt_reject"]
    await decide("reject", annotations=[XERO17_ANNOTATION.model_dump()], reason=reject.reason)
    await stop()
    await decide("approve")
    overview = await stop()
    gates = await runner.deps.output_store.latest(eid, GateDecision)
    [reject_id] = [d.id for d in gates if d.gate is GateId.RPT_01 and d.decision is GateVerdict.reject]
    x = Xero17(runner, eid, owner, llm, False, GateView.model_validate(pln), [], [])
    summary = (await client.get(f"{base}/data/summary")).json()
    final_plan = await plan_page()
    return Walk(x, stops, reject_id, overview, summary, clients, plan_gates, plan_pages, final_plan, progress, language)


def app_llm(store: Any) -> DatasetFixtureLLMClient:
    """组装根的 mock 大模型 (make_llm_client): 补造根取 tests.external 核对过的位置."""
    root = supplements_dir(SNAPSHOT_NAME).parents[1]  # data/xero_demo/<快照> 的上两级, 即 DATASET_ROOT
    client = make_llm_client(get_settings().model_copy(update={"DATASET_ROOT": root}), store)
    assert isinstance(client, DatasetFixtureLLMClient)
    return client


@pytest_asyncio.fixture(scope="module", loop_scope="module", params=["en", "zh"])
async def walked(request: pytest.FixtureRequest) -> Walk:
    """离线 (tests.xero_flow.demo_offline: 空的 Xero 假存储, 能建项目的假项目存储, 规则引擎 FakeRuleEngine(XERO_RULES),
    登记 mock 按数据集定位); 大模型为只读运行副本的 LayeredMockLLMClient (记下读取; 与组装根对这个项目选的目录相同,
    见验收 4 的用例). 项目的工作语言按参数为 en 或 zh (需求 R343), 本模块的用例对两次走通都成立."""
    XERO.ready()
    llm = LayeredMockLLMClient([XERO.runtime])
    async with demo_offline(llm) as (runner, service, client):
        return await walk(client, runner, service, llm, request.param)


def entries(w: Walk) -> list[TrailEntry]:
    trail = w.x.runner.deps.trail
    assert isinstance(trail, FakeTrail)
    return trail.entries


# ── 验收 1: ACC-04 用数据集的登记 mock ──


@shared_loop
async def test_acc04_passes_on_the_dataset_registry(walked: Walk) -> None:
    """ABN 25900043021 过校验位 (R288 的 abn_problem); ACC-04 三项登记核对都通过 (ABN 有效, 名称一致, ASIC 已注册),
    external_call 2 行, 不出例外; 登记摘录上的董事与画像一致 (新客户 ACC-04 的同一比对函数 check_directors; 数据集
    客户的 ACC-04 只做三项, 方案取舍)."""
    x = walked.x
    deps = x.runner.deps
    assert abn_problem(DEMO_ABN) is None
    [card] = await deps.output_store.latest(x.eid, AcceptanceScorecard)
    assert [(c.check, c.passed) for c in card.registry_checks] == [
        ("abn_active", True),
        ("name_match", True),
        ("company_registered", True),
    ]
    assert Counter(e.procedure_id for e in entries(walked) if e.kind is TrailKind.external_call)["ACC-04"] == 2
    exceptions = await deps.output_store.latest(x.eid, AuditException)
    assert [e.id for e in exceptions if e.id.startswith("X-ACC-04-")] == []
    engagement = await deps.engagement_store.get(x.eid)
    assert engagement is not None
    profile = await deps.engagement_store.client_profile(engagement.client_id)
    assert profile is not None and profile.abn == DEMO_ABN and profile.directors
    record = await deps.registry.abn(x.eid, profile.abn)
    assert record is not None and record.acn is not None
    extract = await deps.registry.asic_extract(x.eid, record.acn)
    assert extract is not None and extract.evidence == []
    # 数据集的登记摘录不经上传, 没有字段指针 (check_directors 要取 officeholders 的); 借 ACC-04 那次 ASIC 查询的
    # 指针, 比对只看姓名
    asic = card.registry_checks[2].evidence
    pointed = extract.model_copy(update={"evidence": [asic.model_copy(update={"locator": "officeholders"})]})
    check = check_directors(pointed, profile.directors)
    assert check.passed is True, check.detail


# ── 验收 2: 客户下拉与期间 ──


@shared_loop
async def test_demo_client_first_with_its_period(walked: Walk) -> None:
    """客户列表第一行为演示公司 (数据集带演示资料, 标 demo), 默认数据集带出期间 07-01 至 09-30; 按它建的项目期间同此
    (完整的客户清单与排序见 tests/test_clients)."""
    first = walked.clients[0]
    assert (first["legal_name"], first["abn"]) == ("Demo Company (AU) Pty Ltd", DEMO_ABN)
    assert [(d["dataset"], d["default"], d["demo"]) for d in first["datasets"]] == [(XERO_DATASET, True, True)]
    engagement = walked.overview["engagement"]
    assert (engagement["dataset"], engagement["period_start"], engagement["period_end"]) == (
        XERO_DATASET,
        *(d.isoformat() for d in XERO_PERIOD),
    )


async def test_demo_client_first_even_when_not_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """演示公司排在第一看的是 demo (数据集带演示资料), 不是 default: 把 DEMO_DATASET 设成 Numbat 时演示公司仍排第一,
    Numbat 排第二 (带默认数据集); 只有带补造目录的 xero/20261017-q1 标 demo, 没有补造目录的 20261017-mock 与 Numbat
    不标. 数据管理服务的演示来源同组装根 (项目的数据集载入器)."""
    settings = get_settings().model_copy(update={"DEMO_DATASET": "demo"})
    monkeypatch.setattr("backend.api.routes.clients.get_settings", lambda: settings)
    runner = demo_runner()
    service = DataService(runner.deps, runner, demo=cast(RoutingDatasetLoader, runner.deps.datasets))
    async with api_client(runner, xero=xero_link(runner.deps, service)) as client:
        rows = (await client.get(f"{API}/clients")).json()
    flags = [(r["legal_name"], [(d["dataset"], d["default"], d["demo"]) for d in r["datasets"]]) for r in rows]
    assert flags[:2] == [
        ("Demo Company (AU) Pty Ltd", [(XERO_DATASET, False, True)]),
        ("Numbat Electronic Components Pty Ltd", [("demo", True, False)]),
    ]
    assert ("Demo Company (AU)", [("xero/20261017-mock", False, False)]) in flags


# ── 验收 3: 按停点一键导入 ──


@shared_loop
async def test_each_stop_suggests_its_stage(walked: Walk) -> None:
    """停的位置依次为 ACC-01, 等数据就绪, PLN-01/03, PLN-15 两次 (退回再批准, R317), 等复核, FIN-01, RPT-01 两次, 归档;
    前两处建议的阶段依次为 acceptance, fieldwork, 其余不建议 (不再要报表草稿, Issue #46). 两个阶段都已导入; 补交请求
    只剩审批日志 open (Xero 不给录入人)."""
    assert walked.stops == STOPS
    demo = walked.summary["demo"]
    assert (demo["stages_done"], demo["suggested"]) == (["acceptance", "fieldwork"], None)
    deps = walked.x.runner.deps
    assert {r.id: r.status for r in await deps.data.requests(walked.x.eid)} == {
        "REQ-accounts": "received",
        "REQ-trial_balance": "received",
        "REQ-journal_entries": "received",
        "REQ-contract": "received",
        "REQ-approval_log": "open",
    }


@shared_loop
async def test_tables_and_documents_from_the_demo(walked: Walk) -> None:
    """账表写成新数据版本, 来源沿用 R269: 快照七张为 xero (一版), 补造四张为 import (原文件名为补造来源文件); 上传 21
    份 (承接 2, 外勤 19; 不再上传报表, Issue #46), 都由项目负责人上传; 合同 3 份抽取."""
    deps = walked.x.runner.deps
    eid = walked.x.eid
    versions = await deps.data.versions(eid)
    assert Counter(v.source for v in versions) == {"xero": 7, "import": 4}
    assert sorted(v.file_name for v in versions if v.source == "import") == [
        "bank_accounts.csv",
        "contract_links.csv",
        "delivery_notes.csv",
        "payroll/employees.csv",
    ]
    uploaded = [d for d in await deps.financial_store.documents(eid) if d.channel == "auditor_upload"]
    assert len(uploaded) == 21 and {d.uploaded_by for d in uploaded} == {walked.x.owner}
    assert [d.status for d in uploaded if d.type is DocumentType.contract] == ["extracted"] * 3
    assert DocumentType.financial_statements not in {d.type for d in uploaded}


# ── 验收 4: 计数为 R119 加 R146 的增量, 走到报告; R260 (并入 R259) 的终态, 计数与留痕 ──

# 闸门决定 7 条, 按决定时间: (闸门, 决定)
DEMO_GATES = [
    ("ACC-01", "approve"),
    ("PLN-01/03", "approve"),
    ("PLN-15", "reject"),
    ("PLN-15", "approve"),
    ("FIN-01", "approve"),
    ("RPT-01", "reject"),
    ("RPT-01", "approve"),
]
# 执行过的程序 → 执行它的 Agent: R119 的 13 条加 PLN-15 (功能 planning) 与 GEN-05, GEN-02 (功能 journals)
DEMO_AGENTS = {**AGENTS, "PLN-15": "RiskAssessorAgent", "GEN-05": "FieldworkAgent", "GEN-02": "FieldworkAgent"}
DEMO_RUNS = {pid: 2 if pid in ("PLN-15", "RPT-03") else 1 for pid in DEMO_AGENTS}  # 计划与报告各两稿 (退回一次)
# llm_call 留痕: (程序, 是否上传处理 (执行 ID 以 UPLOAD- 开头)) → 行数. 编排内 23 行, 上传处理 24 行; GEN-09 没有
# (同 R119: REV-03, REV-02 为 L2 跳过, BA-091 的建议不触发主规则, 抽样未中; GEN-02 的 7 条为 L2 同样跳过)
DEMO_LLM_CALLS = {
    ("PLN-15", False): 2,
    ("DOC-06", False): 2,
    ("REV-03", False): 3,
    ("REV-02", False): 6,
    ("CSH-02", False): 1,
    ("GEN-02", False): 7,
    ("RPT-03", False): 2,
    ("DOC-CLASSIFY", True): 21,
    ("DOC-04", True): 3,
}


async def assert_end_state(w: Walk) -> None:
    """需求 R260 (并入 R259, 按 Q32 改写为演示配置): 走通的终态, 计数与留痕, 离线与连库 (R261), 重启 (R262) 共用.
    - 停点同 STOPS; 后台没有错误, 走完没有过期程序, 项目冻结的功能集为演示配置.
    - 例外 18 条, 判断 18 条, 复核任务 17 条 decided, 函证两户, 错报 2 条 (未更正) 与汇总表, 重要性: R119 加 R146 的
      增量 (assert_results, assert_materiality, journals); 报告两稿与归档锁定同 R119 (assert_report).
    - 闸门决定 7 条 (DEMO_GATES); 人工队列, 更正通知与审计更正分录都没有 (错报闭环不在演示配置).
    - 文件 23 份, 数据版本 10 与补交请求同 R119 (assert_documents).
    - 每条程序由真实 Agent 执行, 次数 DEMO_RUNS, 执行都成功.
    - 留痕: llm_call 47 行 (DEMO_LLM_CALLS), decision 24 行 (闸门 7, 复核 17), external_call 4 行 (ACC-04 2, CSH-04 2).
    - 证据指针 (判断, 合同, 函证, 例外, 承接评分卡) 都能解析; 原目录哈希不变.
    - 签字后再提交决定报 ENGAGEMENT_LOCKED."""
    x = w.x
    deps = x.runner.deps
    assert w.stops == STOPS
    assert (w.overview["run_error"], w.overview["stale_procedure_ids"]) == (None, [])
    assert sorted(w.overview["features"]) == sorted(DEMO_FEATURES)
    await assert_results(x, journals=True)
    await assert_materiality(x, journals=True)
    await assert_report(x, w.reject_id)
    await assert_documents(x)
    gates = sorted(await deps.output_store.latest(x.eid, GateDecision), key=lambda d: d.decided_at)
    assert [(d.gate.value, d.decision.value) for d in gates] == DEMO_GATES
    assert await deps.output_store.latest(x.eid, CorrectionNotice) == []
    assert [t for t in await deps.output_store.latest(x.eid, ReviewTask) if t.kind is TaskKind.manual] == []
    assert {pid: name for pid, name, _ in x.agents} == DEMO_AGENTS
    assert [(pid, name) for pid, name, script in x.agents if script] == []
    assert Counter(pid for pid, _, _ in x.agents) == DEMO_RUNS
    runs = await deps.output_store.latest(x.eid, ProcedureRun)
    assert Counter(r.procedure_id for r in runs) == DEMO_RUNS and {r.status for r in runs} == {RunStatus.succeeded}
    trail = await deps.trail.list(x.eid, limit=10_000)
    calls = [e for e in trail if e.kind is TrailKind.llm_call]
    assert Counter((e.procedure_id, (e.run_id or "").startswith("UPLOAD-")) for e in calls) == DEMO_LLM_CALLS
    assert len([e for e in trail if e.kind is TrailKind.decision]) == len(DEMO_GATES) + 17
    assert Counter(e.procedure_id for e in trail if e.kind is TrailKind.external_call) == {"ACC-04": 2, "CSH-04": 2}
    started = Started(x.runner, deps.output_store, deps.engagement_store)  # type: ignore[arg-type]
    await assert_pointers_resolve(started, x.eid, everything=True)
    assert x.moved == []
    after = xero17_gates(x.owner)["rpt_approve"].model_copy(update={"id": "D-AFTER-SIGNING"})
    with pytest.raises(DecisionRejected) as rejected:
        await GraphDecisionSubmit(x.runner.app, deps, x.runner).submit(x.eid, after)
    assert rejected.value.code == "ENGAGEMENT_LOCKED"


@shared_loop
async def test_end_state(walked: Walk) -> None:
    """需求 R287 验收 4 与 R260: 见 assert_end_state."""
    assert sorted(DEMO_FEATURES) == ["collab", "critic", "journals", "planning"]
    await assert_end_state(walked)


@shared_loop
async def test_mock_runs_are_generated_in_english(walked: Walk) -> None:
    """需求 R343 验收 1: mock 不另备中文固定输出, 项目的工作语言为 zh 时也走到归档 (计数与报告同上), 各执行记录的
    language 都是 en (组装根填 fixed_language en); 中文项目的总览 run_languages 列出全部执行, 页面据此在判断, 计划与
    报告旁标 "以英文生成", 英文项目为空."""
    deps, eid = walked.x.runner.deps, walked.x.eid
    runs = await deps.output_store.latest(eid, ProcedureRun)
    assert walked.overview["engagement"]["working_language"] == walked.language
    assert runs and {r.language for r in runs} == {"en"}
    assert walked.overview["run_languages"] == ({r.id: "en" for r in runs} if walked.language == "zh" else {})


# ── R325: 执行进度留痕与进度 ──


@shared_loop
async def test_run_entries_pair_every_run_and_progress(walked: Walk) -> None:
    """需求 R325 验收 1, 4, 6: 每次执行 (Store 中的 ProcedureRun) 恰有开始与结束各一行 run 留痕, 开始在前, 条数为执行数
    的两倍, 按写入顺序; REV-08 的范围按快照渲染, 结束的结果为去重 6 张 990000, 例外 1 条; REV-03 判断 3, 待复核 3;
    REV-02 判断 6. 停在执行阶段等复核时本阶段共 9 条程序, 不在执行, 没有正在运行的."""
    runs = await walked.x.runner.deps.output_store.latest(walked.x.eid, ProcedureRun)
    rows = [e for e in entries(walked) if e.kind is TrailKind.run]
    assert len(rows) == 2 * len(runs)
    for run in runs:
        actions = [(n, e.payload["action"]) for n, e in enumerate(rows) if e.run_id == run.id]
        assert [a for _, a in actions] == ["started", "finished"], run.id
    finished = {e.run_id: e.payload for e in rows if e.payload["action"] == "finished"}
    rev08 = finished["RUN-REV-08-1"]
    assert rev08["scope"]["zh"] == "期末前 20 天 (2026-09-10 至 2026-09-30) 入账的销售发票"
    assert rev08["status"] == "succeeded"
    assert {k: rev08["result"][k] for k in ("items", "amount_cents", "exceptions")} == {
        "items": 6,
        "amount_cents": 990000,
        "exceptions": 1,
    }
    assert [(p["rule_id"], p["count"]) for p in rev08["result"]["populations"]] == [("R01", 6), ("R02", 6), ("R03", 5)]
    rev03, rev02 = finished["RUN-REV-03-1"]["result"], finished["RUN-REV-02-1"]["result"]
    assert (rev03["judgments"], rev03["pending_reviews"], rev02["judgments"]) == (3, 3, 6)
    [review] = [p for stop, p in zip(walked.stops, walked.progress, strict=True) if stop[2]]  # 等复核的停点
    assert (review["phase"], review["total"], review["running"]) == ("P2_Executing", 9, [])


# ── R317: 计划批准在先, 退回一次再批准 ──

# 演示配置 (功能 journals) 下的程序表: R314 的 9 行, 加不针对单一认定的 GEN-02, GEN-05 (排在 DOC-06 之后)
DEMO_PLAN_ROWS = [*XERO_PLAN_ROWS, (None, "GEN-02"), (None, "GEN-05")]


@shared_loop
async def test_plan_approved_before_fieldwork(walked: Walk) -> None:
    """需求 R317 验收 2, 3: PLN-01/03 通过后停在计划审批, 呈报 PLAN-1 v1, 计划页程序表 11 行 (DEMO_PLAN_ROWS); 退回后
    再停, 呈报 PLAN-2 v1 (修订理由为退回理由, 此前的退回 1 条); 批准 (确认同 R205) 后进入执行阶段. 增量只在 PLN-15:
    两条决定与两行 decision 留痕, 两行 llm_call (A4, 执行 RUN-PLN-15-1, 2, 都是第 1 次通过), PLN-15 planned →
    completed 一行, 项目 P1_Planning → P2_Executing 仍是一行, 改记在 PLN-15 下 (计划批准推进阶段); PLAN-1 v2
    superseded, PLAN-2 v2 approved (项目负责人批准). 归档后计划页的当前计划为 PLAN-2, 程序表的程序在总览中都已完成."""
    first, second = walked.plan_gates
    assert [(g["gate"], g["target_id"], g["target_version"]) for g in walked.plan_gates] == [
        ("PLN-15", "PLAN-1", 1),
        ("PLN-15", "PLAN-2", 1),
    ]
    assert first["material"]["previous_decisions"] == []
    assert second["material"]["plan"]["revision_reason"] == PLAN_REJECT_REASON
    assert [d["reason"] for d in second["material"]["previous_decisions"]] == [PLAN_REJECT_REASON]
    for page, plan_id in zip(walked.plan_pages, ("PLAN-1", "PLAN-2"), strict=True):
        assert page["current_plan_id"] == plan_id
        assert [(r["objective_id"], r["procedure_id"]) for r in page["rows"]] == DEMO_PLAN_ROWS

    deps, eid = walked.x.runner.deps, walked.x.eid
    decisions = [d for d in await deps.output_store.latest(eid, GateDecision) if d.gate is GateId.PLN_15]
    assert [d.decision for d in sorted(decisions, key=lambda d: d.decided_at)] == [
        GateVerdict.reject,
        GateVerdict.approve,
    ]
    reject, approve = sorted(decisions, key=lambda d: d.decided_at)
    trail = entries(walked)
    decided = [e for e in trail if e.kind is TrailKind.decision and e.procedure_id == "PLN-15"]
    assert [(e.summary, e.payload["reason"], e.idempotency_key) for e in decided] == [
        ("PLN-15 reject", PLAN_REJECT_REASON, reject.id),
        ("PLN-15 approve", None, approve.id),
    ]
    calls = [e for e in trail if e.kind is TrailKind.llm_call and e.procedure_id == "PLN-15"]
    assert [(e.actor, e.run_id, e.payload["attempt"], e.payload["level_failed"]) for e in calls] == [
        (AgentId.A4, "RUN-PLN-15-1", 1, None),
        (AgentId.A4, "RUN-PLN-15-2", 1, None),
    ]
    moves = [e for e in trail if e.kind is TrailKind.transition]
    pln15 = [(e.payload["from"], e.payload["to"]) for e in moves if e.payload["id"] == "PLN-15"]
    assert pln15 == [("planned", "completed")]
    assert entered_p2_by(trail) == ["PLN-15"]
    plans = {(p.id, p.version): p for p in await deps.output_store.latest(eid, AuditPlan)}
    assert {k: p.status for k, p in plans.items()} == {
        ("PLAN-1", 2): PlanStatus.superseded,
        ("PLAN-2", 2): PlanStatus.approved,
    }
    assert plans[("PLAN-2", 2)].approved_by == walked.x.owner

    final = walked.final_plan
    assert final["current_plan_id"] == "PLAN-2"
    statuses = {p["id"]: p["status"] for p in walked.overview["procedures"]}
    assert {statuses[r["procedure_id"]] for r in final["rows"]} == {"completed"}


@shared_loop
async def test_llm_reads_only_the_runtime_copy(walked: Walk) -> None:
    """大模型 FIXED_CALLS 次都读运行副本里各自的 fixture (各程序的次数见 FIXTURE_READS: RPT-03 两稿 r1, r2 与 PLN-15
    两个修订 r1, r2 也在里面), 没有回落到 default.json 或找不到, 各文件一次, attempt 1 无失败级别; 组装根的 mock
    客户端对这个项目选的正是运行副本 (应用里按项目数据集选目录)."""
    llm = walked.x.client
    reads = [(r.procedure_id, r.fixture_key, r.root) for r in llm.reads]
    assert [r for r in llm.reads if r.file != f"{r.procedure_id}/{r.fixture_key}.json"] == []
    assert Counter(pid for pid, *_ in reads) == FIXTURE_READS
    assert sorted(key for pid, key, _ in reads if pid == "RPT-03") == ["r1", "r2"]
    assert sorted(key for pid, key, _ in reads if pid == "PLN-15") == ["r1", "r2"]
    assert len(set(reads)) == len(reads)
    calls = [e for e in entries(walked) if e.kind is TrailKind.llm_call]
    assert {(e.payload["attempt"], e.payload["level_failed"]) for e in calls} == {(1, None)}
    assert await app_llm(walked.x.runner.deps.engagement_store).directory(walked.x.eid) == XERO.runtime


# ── 连库变体 ──


@pytest.mark.slow
async def test_walkthrough_on_real_storage(tmp_path: Path) -> None:
    """同一走法, 换用组装根的依赖 (真实 Postgres 存储与检查点, 规则 SQL, 数据集载入, 登记与函证 mock, mock 大模型按
    项目数据集选运行副本), 经接口新建项目 (real_storage 自建的项目不用); 计数, 报告与停点同离线."""
    async with real_storage(
        tmp_path,
        dataset=XERO_DATASET,
        period=XERO_PERIOD,
        load_dataset=False,
        fresh_profile=False,
        FEATURES=",".join(sorted(DEMO_FEATURES)),
    ) as run:
        deps, runner = run.deps, run.s.runner
        assert isinstance(cast(SwitchableLLMClient, deps.llm).inner, DatasetFixtureLLMClient)
        service = run.data_service()
        async with api_client(runner, run.owner, deps=deps, xero=xero_link(deps, service)) as client:
            w = await walk(client, runner, service, None)
        assert w.stops == STOPS
        await assert_results(w.x, journals=True)
        await assert_materiality(w.x, journals=True)
        await assert_report(w.x, w.reject_id)
        assert (w.overview["stale_procedure_ids"], sorted(w.overview["features"])) == ([], sorted(DEMO_FEATURES))
        calls = [e for e in await deps.trail.list(w.x.eid, limit=10_000) if e.kind is TrailKind.llm_call]
        assert Counter(e.procedure_id for e in calls)["RPT-03"] == 2
        assert {(e.payload["attempt"], e.payload["level_failed"]) for e in calls} == {(1, None)}
