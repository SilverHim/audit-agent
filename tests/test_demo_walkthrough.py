"""需求 R287: 演示公司 (Xero Demo Company (AU)) 在应用里走通. 只经 HTTP 接口, 照页面的做法一步步走 (walk):

客户下拉取第一行 (演示公司) 的默认数据集与期间建项目 → 每个停点按数据管理页概览的 demo.suggested 导入该阶段的演示
资料 (页面按钮的阶段同此): ACC-01 闸门处 acceptance, 等数据就绪时 fieldwork (财务报表以账套为准, 不再导入报表,
Issue #46) → 闸门决定取 GET /gate 的材料 → 复核按任务
列表逐条提交 (决定同 R119: BA-091 认定错报, 其余 confirm; GEN-02 的 7 条同 R146: XJ-499 认定错报, XJ-575 推翻为接受,
其余 confirm) → FIN-01 无保留意见 → RPT-01 先退回再批准, 到归档.

大模型只读运行副本 data/xero_demo/20261017-q1/llm (应用里按项目数据集选的目录, 生成脚本从 llm_xero 复制, 正本里没有
fixture 文件的程序由 llm_xero_only17 补位), 不用 R119 的分层客户端; 登记 mock 按项目数据集定位 (DatasetRegistrySource).
功能集为演示配置 (.env.example 的 FEATURES=critic,collab,journals, R155, R146; R119 仍按基线回归). 断言: 例外, 判断,
复核任务, 错报与汇总表为 R119 加 R146 的增量 (复用其断言函数), 报告与归档同 R119 (意见与报告不在 R146, FIN-01 仍按
R119 的决定); 两个阶段都已导入, 补交请求只剩审批日志 open, 走完没有过期程序. R189 把全接入
的 RPT-03 两稿放进正本后, 运行副本随之换成全接入两稿, 本模块的报告断言 (只开现有 17 条的两稿) 在同一提交里随之改.
不与 R119 共用一次运行: 走 HTTP, 经演示资料导入上传文件, 终态 (文件 21 份) 不同.
读快照与补造目录, 标 external; 连库变体标 slow.
"""

from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import pytest_asyncio

from backend.adapters.llm import DatasetFixtureLLMClient, SwitchableLLMClient, make_llm_client
from backend.adapters.registry_mock import DatasetRegistrySource
from backend.agents.base import AgentDeps
from backend.agents.client_intel import check_directors
from backend.contracts.models import (
    AcceptanceScorecard,
    AuditException,
    DocumentType,
    GateDecision,
    GateVerdict,
    TrailEntry,
    TrailKind,
)
from backend.contracts.views import GateView
from backend.core.config import get_settings
from backend.orchestration.runner import GraphRunner
from backend.persistence.stores import RoutingDatasetLoader
from backend.services.abn import abn_problem
from backend.services.data_service import DataService
from backend.services.features import parse_features
from backend.services.xero_link import XeroLink
from tests.external import supplements_dir
from tests.fakes import NUMBAT_AUDITOR, FakeRuleEngine, FakeTrail, FakeXeroConnectionStore, api_client, wait_idle
from tests.fixture_sets import XERO
from tests.test_demo_default import CreatingEngagementStore, demo_runner
from tests.test_e2e_mock import real_storage
from tests.test_e2e_xero17 import XERO_PERIOD, Xero17, assert_materiality, assert_report, assert_results
from tests.xero_fakes import (
    REPO,
    SNAPSHOT_NAME,
    XERO17_ANNOTATION,
    XERO17_BA091_DRAFT,
    XERO17_MATERIALITY,
    XERO_DATASET,
    XERO_RULES,
    new_xero_deps,
    xero17_gates,
    xero_journal_reviews,
)
from tests.xero_flow import READY_REASON, demo_source, drain, offline_runner
from tests.xero_harness import LayeredMockLLMClient

pytestmark = pytest.mark.external
shared_loop = pytest.mark.asyncio(loop_scope="module")  # 用模块共用的走通 (walked) 的用例与它同一个事件循环
API = "/api/v1"
BA091 = "J-CSH-02-1-BA-091"
# 演示配置的功能集: 取 .env.example 的 FEATURES (critic,collab,journals; R155 打开 collab, R146 打开 journals)
DEMO_FEATURES = parse_features(
    next(
        line
        for line in (REPO / ".env.example").read_text(encoding="utf-8").splitlines()
        if line.startswith("FEATURES=")
    ).split("=", 1)[1]
)
REVIEW_ROUNDS = 5  # 复核循环的轮数上限 (本路径分三层出任务, 3 轮即完)
DEMO_ABN = "25900043021"
# 每个停点: (总览的闸门, 是否等数据就绪, 是否等复核, 概览建议导入的阶段); 复核的停点只记第一次
STOPS = [
    ("ACC-01", False, False, "acceptance"),
    (None, True, False, "fieldwork"),
    ("PLN-01/03", False, False, None),
    (None, False, True, None),
    ("FIN-01", False, False, None),
    ("RPT-01", False, False, None),
    ("RPT-01", False, False, None),
    (None, False, False, None),
]


@dataclass
class Walk:
    """走完的一次: R119 断言函数用的 Xero17 (x.client 记下了大模型读取), 依次停的位置 (STOPS 的写法), RPT-01 退回决定
    的 ID (接口生成), 最后一次总览, 数据管理页概览与客户列表."""

    x: Xero17
    stops: list[tuple[str | None, bool, bool, str | None]]
    reject_id: str
    overview: dict[str, Any]
    summary: dict[str, Any]
    clients: list[dict[str, Any]]


async def walk(client: httpx.AsyncClient, runner: GraphRunner, service: DataService, llm: Any) -> Walk:
    """照页面的做法从新建项目走到归档 (见模块说明); 每步之后等后台作业与图都停下, 后台始终没有错误."""
    owner = (await client.get(f"{API}/users/me")).json()["id"]
    clients = (await client.get(f"{API}/clients")).json()
    [dataset] = [d for d in clients[0]["datasets"] if d["default"]]  # 客户下拉默认选中第一行, 期间随数据集带出
    created = await client.post(
        f"{API}/engagements", json={k: dataset[k] for k in ("dataset", "period_start", "period_end")}
    )
    assert created.status_code == 201, created.text
    eid = created.json()["engagement"]["id"]
    base = f"{API}/engagements/{eid}"
    stops: list[tuple[str | None, bool, bool, str | None]] = []

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
        if suggested is not None:
            started = await client.post(f"{base}/data/demo-import", json={"stage": suggested})
            assert (started.status_code, started.json()["status"]) == (202, "started"), started.text
            overview = await settle()
        return overview

    async def decide(verdict: str, **fields: Any) -> dict[str, Any]:
        """对当前闸门的材料 (GET /gate 给的对象与版本) 提交决定, 返回 GET /gate 的视图. 不在闸门 (如某条程序失败,
        流程停下等人) 时报出 GET /gate 的响应."""
        got = await client.get(f"{base}/gate")
        assert got.status_code == 200, got.text
        view = got.json()
        body = {k: view[k] for k in ("gate", "target_id", "target_version")}
        posted = await client.post(f"{base}/gate-decisions", json={**body, "decision": verdict, **fields})
        assert posted.status_code == 202, posted.text
        return cast(dict[str, Any], view)

    await stop()
    await decide("approve")
    await stop()
    ready = await client.post(f"{base}/data/ready", json={"reason": READY_REASON})
    assert ready.status_code == 202, ready.text
    await stop()
    pln = await decide("approve", materiality=XERO17_MATERIALITY.model_dump())
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
    [reject_id] = [d.id for d in gates if d.decision is GateVerdict.reject]
    x = Xero17(runner, eid, owner, llm, False, GateView.model_validate(pln), [], [])
    summary = (await client.get(f"{base}/data/summary")).json()
    return Walk(x, stops, reject_id, overview, summary, clients)


def app_llm(store: Any) -> DatasetFixtureLLMClient:
    """组装根的 mock 大模型 (make_llm_client): 补造根取 tests.external 核对过的位置."""
    root = supplements_dir(SNAPSHOT_NAME).parents[1]  # data/xero_demo/<快照> 的上两级, 即 DATASET_ROOT
    client = make_llm_client(get_settings().model_copy(update={"DATASET_ROOT": root}), store)
    assert isinstance(client, DatasetFixtureLLMClient)
    return client


def xero_link(deps: AgentDeps, service: DataService) -> XeroLink:
    """数据接口用的 Xero 连接服务 (假连接存储, 一律未连接; Xero 应用与主密钥置空, 同 tests.fakes.api_client)."""
    blank = get_settings().model_copy(
        update={"XERO_CLIENT_ID": "", "XERO_CLIENT_SECRET": "", "SETTINGS_SECRET_KEY": ""}
    )
    return XeroLink(deps, service, FakeXeroConnectionStore(), blank)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def walked() -> Walk:
    """离线: 空的 Xero 假存储, 能建项目的假项目存储, 规则引擎 FakeRuleEngine(XERO_RULES), 登记 mock 按数据集定位;
    大模型为只读运行副本的 LayeredMockLLMClient (记下读取; 与组装根对这个项目选的目录相同, 见验收 4 的用例)."""
    XERO.ready()
    store = CreatingEngagementStore()
    llm = LayeredMockLLMClient([XERO.runtime])
    datasets = demo_source()
    deps = new_xero_deps(
        trail=FakeTrail(),
        llm=llm,
        review_llm=llm,
        rules=FakeRuleEngine(dict(XERO_RULES)),
        features=DEMO_FEATURES,
        engagement_store=store,
    )
    deps = replace(deps, registry=DatasetRegistrySource(store, datasets.supplement_dir))
    runner = offline_runner(deps)
    service = DataService(deps, runner, demo=datasets)
    async with api_client(runner, NUMBAT_AUDITOR.id, xero=xero_link(deps, service)) as client:
        return await walk(client, runner, service, llm)


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
    """停的位置依次为 ACC-01, 等数据就绪, PLN-01/03, 等复核, FIN-01, RPT-01 两次, 归档; 前两处建议的阶段依次为
    acceptance, fieldwork, 其余不建议 (不再要报表草稿, Issue #46). 两个阶段都已导入; 补交请求只剩审批日志 open
    (Xero 不给录入人)."""
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


# ── 验收 4: 计数为 R119 加 R146 的增量, 走到报告 ──


@shared_loop
async def test_counts_and_report(walked: Walk) -> None:
    """R119 的计数 (例外 11 条, 判断 10 条 v2 confirmed, 复核任务 10 条 decided, 错报 1 条 BA-091 4853, 函证两户,
    重要性) 加 R146 的增量 (例外多 GEN-05 七条, 判断多 8 条, 任务多 7 条, 错报多 XJ-499 的 75000, 汇总表合计 79853,
    候选保留意见); REPORT-2 签字归档, 回应接口生成的 RPT-01 退回决定 (同 R119). 走完没有过期程序, 后台没有错误;
    项目冻结的功能集为演示配置 critic, collab, journals."""
    assert sorted(DEMO_FEATURES) == ["collab", "critic", "journals"]
    await assert_results(walked.x, journals=True)
    await assert_materiality(walked.x, journals=True)
    await assert_report(walked.x, walked.reject_id)
    assert (walked.overview["stale_procedure_ids"], walked.overview["run_error"]) == ([], None)
    assert sorted(walked.overview["features"]) == sorted(DEMO_FEATURES)


@shared_loop
async def test_llm_reads_only_the_runtime_copy(walked: Walk) -> None:
    """大模型 45 次都读运行副本里各自的 fixture (RPT-03 两稿 r1, r2 也在里面; GEN-02 七笔凭证各一次, 需求 R146;
    没有回落到 default.json 或找不到), 各文件一次, attempt 1 无失败级别; 组装根的 mock 客户端对这个项目选的正是运行副本
    (应用里按项目数据集选目录)."""
    llm = walked.x.client
    reads = [(r.procedure_id, r.fixture_key, r.root) for r in llm.reads]
    assert [r for r in llm.reads if r.file != f"{r.procedure_id}/{r.fixture_key}.json"] == []
    assert Counter(pid for pid, *_ in reads) == {
        "DOC-CLASSIFY": 21,
        "DOC-04": 3,
        "DOC-06": 2,
        "REV-03": 3,
        "REV-02": 6,
        "CSH-02": 1,
        "GEN-02": 7,
        "RPT-03": 2,
    }
    assert sorted(key for pid, key, _ in reads if pid == "RPT-03") == ["r1", "r2"]
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
