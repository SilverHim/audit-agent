"""新建客户 (R288): ABN 校验位 (ABR 公布的算法), 客户列表 GET /clients, 新建客户 POST /clients, 按新客户建项目,
新客户的项目不读任何数据集的 mock (ACC-04 转人工确认).

不连库: 假项目存储能建项目 (同 test_demo_default), 载入器读仓库里的数据集文件. 新客户的 ABN 是合成的 (9 位编号取
9000431xx 段, 校验位按算法补), 不是真实公司.
"""

import json
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from backend.adapters.confirmation_mock import DatasetConfirmationChannel
from backend.adapters.llm import DatasetFixtureLLMClient
from backend.adapters.registry import CompositeRegistrySource, UploadedExtracts
from backend.adapters.registry_mock import DatasetRegistrySource
from backend.agents.base import RunContext
from backend.agents.risk_assessor import RiskAssessorAgent
from backend.contracts.models import (
    AcceptanceScorecard,
    AuditException,
    EvidenceType,
    Procedure,
    ProcedureStatus,
    ProfileSource,
    Reliability,
    TrailKind,
)
from backend.contracts.ports import ConfirmationRequest
from backend.orchestration.runner import GraphRunner
from backend.persistence.stores import RoutingDatasetLoader, read_planning
from backend.services.abn import abn_problem
from backend.services.data_quality import source_label
from backend.services.data_service import DataService
from tests.external import optional, snapshot_dir
from tests.fakes import (
    FAKE_GUID,
    NUMBAT_AUDITOR,
    NUMBAT_PROFILE,
    CreatingEngagementStore,
    FakeAbr,
    FakeDataStore,
    FakeRegistry,
    FakeTrail,
    api_client,
    make_ctx,
    make_deps,
    wait_idle,
)
from tests.test_demo_default import SETTINGS, demo_runner, needs_snapshot

CLIENTS = "/api/v1/clients"
ENGAGEMENTS = "/api/v1/engagements"
WOMBAT_ABN = "44 900 043 101"  # 合成: 编号 900043101, 校验位 44
PERIOD = {"period_start": "2026-07-01", "period_end": "2027-06-30"}


def wombat(**changes: Any) -> dict[str, Any]:
    """新建客户的请求体 (合成的客户)."""
    body = {
        "legal_name": "Wombat Analytics Pty Ltd",
        "abn": WOMBAT_ABN,
        "entity_type": "small_proprietary",
        "industry_anzsic": "6920",
        "listed": False,
        "directors": ["Jane Citizen"],
    }
    return {**body, **changes}


# ── ABN 校验位 ───────────────


def test_abn_check_digits_follow_abr() -> None:
    """ABR 官方页的例子 51 824 753 556 (加权和 534 = 89 × 6) 有效; 仓库里各数据集与补造文件用的 ABN 都有效."""
    assert abn_problem("51 824 753 556") is None
    for abn in ("68900042015", "25900043021", "27900043030", "17900043049", "11111111138", "44900043101"):
        assert abn_problem(abn) is None, abn


@pytest.mark.parametrize(
    ("abn", "message"),
    [
        ("51 824 753 557", "校验位不对"),
        ("44900043102", "校验位不对"),
        ("4490004310", "11 位数字"),
        ("449000431011", "11 位数字"),
        ("44 900 043 1O1", "11 位数字"),  # 字母 O
        ("٤٤٩٠٠٠٤٣١٠١", "11 位数字"),  # 阿拉伯-印度数字不算
        ("", "11 位数字"),
    ],
)
def test_bad_abns_are_rejected(abn: str, message: str) -> None:
    problem = abn_problem(abn)
    assert problem is not None and message in problem


# ── 客户列表与新建客户 ───────────────


async def post(client: httpx.AsyncClient, body: dict[str, Any]) -> httpx.Response:
    return await client.post(CLIENTS, json=body)


@pytest.mark.external
async def test_clients_are_listed_per_client() -> None:
    """数据集客户按画像 id 各一行 (不按数据集), 演示公司 (数据集带演示资料, R287) 在前; 还没进库的没有 latest_version.
    数据管理服务的演示来源同组装根 (项目的数据集载入器)."""
    runner = demo_runner()
    async with api_client(runner) as client:
        client._transport.app.state.data_service = DataService(  # type: ignore[attr-defined]
            runner.deps, runner, demo=cast(RoutingDatasetLoader, runner.deps.datasets)
        )
        rows = (await client.get(CLIENTS)).json()
    by_id = {row["client_id"]: row for row in rows}
    numbat = by_id["CLIENT-001"]
    assert numbat == {
        "client_id": "CLIENT-001",
        "legal_name": NUMBAT_PROFILE.legal_name,
        "abn": "68900042015",
        "origin": "dataset",
        "latest_version": None,
        "datasets": [{"dataset": "demo", "period_start": None, "period_end": None, "default": False, "demo": False}],
    }
    assert len(rows) == len(by_id)  # 每个客户一行
    if optional(snapshot_dir, "20261017-q1") is not None:  # 有 Xero 快照时另核对它的一行
        q1 = by_id["CLIENT-XERO-3C1332BB-Q1"]
        assert rows[0] == q1 and q1["legal_name"] == "Demo Company (AU) Pty Ltd" and q1["abn"] == "25900043021"
        assert q1["datasets"] == [
            {
                "dataset": "xero/20261017-q1",
                "period_start": "2026-07-01",
                "period_end": "2026-09-30",
                "default": True,
                "demo": True,
            }
        ]


async def test_create_client_checks_name_abn_and_duplicates() -> None:
    runner = demo_runner()
    store = runner.deps.engagement_store
    assert isinstance(store, CreatingEngagementStore)
    async with api_client(runner) as client:
        blank = await post(client, wombat(legal_name="  "))
        assert (blank.status_code, blank.json()["detail"]["code"]) == (422, "MISSING_FIELD")
        for abn, message in (("4490004310", "11 位数字"), ("44900043102", "校验位不对")):
            bad = await post(client, wombat(abn=abn))
            assert (bad.status_code, bad.json()["detail"]["code"]) == (422, "INVALID_VALUE")
            assert message in bad.json()["detail"]["message"]
        year_end = await post(client, wombat(financial_year_end="13-01"))
        assert (year_end.status_code, year_end.json()["detail"]["code"]) == (422, "INVALID_VALUE")

        taken = await post(client, wombat(abn="68 900 042 015"))  # Numbat 的 ABN (数据集客户, 还没进库)
        assert taken.status_code == 409
        detail = taken.json()["detail"]
        assert (detail["code"], detail["client_id"], detail["legal_name"]) == (
            "ABN_TAKEN",
            "CLIENT-001",
            NUMBAT_PROFILE.legal_name,
        )
        assert store.profiles == []  # 被拒的都没写

        created = await post(client, wombat(legal_name=" Wombat Analytics Pty Ltd ", approval_limit_cents=250000))
        assert created.status_code == 201, created.text
        row = created.json()
        assert re.fullmatch(r"CLIENT-M-[0-9a-f]{12}", row["client_id"])
        assert (row["legal_name"], row["abn"], row["origin"], row["latest_version"], row["datasets"]) == (
            "Wombat Analytics Pty Ltd",
            "44900043101",
            "manual",
            1,
            [],
        )
        again = await post(client, wombat(legal_name="Wombat Two Pty Ltd"))  # 同一 ABN 再建
        assert (again.status_code, again.json()["detail"]["client_id"]) == (409, row["client_id"])
        listed = {r["client_id"]: r for r in (await client.get(CLIENTS)).json()}
    assert listed[row["client_id"]] == row
    [record] = await store.profile_history(row["client_id"])
    assert (record.profile.version, record.source, record.updated_by) == (1, ProfileSource.manual, NUMBAT_AUDITOR.id)
    assert record.profile.approval_limit_cents == 250000 and record.profile.notes == ""


async def test_create_client_does_not_overwrite_an_existing_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """编号撞号 (极少见) 时 409 VERSION_MISMATCH, 已有客户的画像不动."""
    from backend.api.routes import clients as clients_route

    class FixedUuid:
        hex = "0123456789abcdef0123456789abcdef"

    monkeypatch.setattr(clients_route, "uuid4", FixedUuid)
    runner = demo_runner()
    async with api_client(runner) as client:
        first = await post(client, wombat())
        assert first.json()["client_id"] == "CLIENT-M-0123456789ab"
        clash = await post(client, wombat(legal_name="Other Pty Ltd", abn="25 900 043 102"))
    assert (clash.status_code, clash.json()["detail"]["code"]) == (409, "VERSION_MISMATCH")
    store = runner.deps.engagement_store
    assert [r.profile.legal_name for r in await store.profile_history("CLIENT-M-0123456789ab")] == [
        "Wombat Analytics Pty Ltd"
    ]


# ── 按新客户建项目 ───────────────


def spy_runner(abr: FakeAbr, guid: str) -> tuple[GraphRunner, list[str]]:
    """demo_runner 的登记换成组合实现 (同组装根): ABN 实查接假 ABR; 数据集一侧记下目录查找 (新客户的项目不应查任何
    数据集的目录)."""
    looked: list[str] = []

    def registry(store: Any, directory: Callable[[str], Path | None], deps: Any) -> CompositeRegistrySource:
        def spy(dataset: str) -> Path | None:
            looked.append(dataset)
            return directory(dataset)

        uploads = UploadedExtracts(deps.financial_store, deps.data, deps.evidence_store)
        return CompositeRegistrySource(store, DatasetRegistrySource(store, spy), abr.client(guid), uploads)

    return demo_runner(registry), looked


# 假 ABR 回放的有效登记 (tests/fixtures/abr/active.xml, 合成)
WOMBAT_RECORD = {
    "abn": "44900043101",
    "acn": "900043101",
    "entity_name": "WOMBAT ANALYTICS PTY LTD",
    "status": "Active",
    "entity_type": "Australian Private Company",
}
NOT_QUERIED = (EvidenceType.voucher, Reliability.internal_original)  # "没有查询" 的留痕只作定位 (审核 R288-04)
QUERIED = (EvidenceType.external, Reliability.external_original)


async def new_client_engagement(guid: str) -> tuple[GraphRunner, list[str], str, dict[str, Any], dict[str, Any]]:
    """经接口新建客户 Wombat 与它的项目 (没有数据集), 等后台停下: 返回 runner, 数据集目录的查找, 项目 ID, 总览与
    客户资料页. ABN 经假 ABR (Wombat 为有效的公司登记, 有 ACN)."""
    runner, looked = spy_runner(FakeAbr({"44900043101": "active"}), guid)
    async with api_client(runner) as client:
        client_id = (await post(client, wombat())).json()["client_id"]
        created = await client.post(ENGAGEMENTS, json={**PERIOD, "client_id": client_id})
        assert created.status_code == 201, created.text
        overview = created.json()
        eid = overview["engagement"]["id"]
        assert (overview["engagement"]["dataset"], overview["client_name"]) == (None, "Wombat Analytics Pty Ltd")
        await wait_idle(runner, eid)
        state = (await client.get(f"{ENGAGEMENTS}/{eid}")).json()
        view = (await client.get(f"{ENGAGEMENTS}/{eid}/client-profile")).json()
    assert runner.run_error(eid) is None and looked == []  # 不读任何数据集的登记 mock
    assert [(v["version"], v["source"], v["updated_by"], v["updated_by_name"]) for v in view["versions"]] == [
        (1, "manual", NUMBAT_AUDITOR.id, NUMBAT_AUDITOR.full_name)
    ]
    return runner, looked, eid, state, view


def external_calls(runner: GraphRunner) -> list[Any]:
    trail = runner.deps.trail
    assert isinstance(trail, FakeTrail)
    return [e for e in trail.entries if e.kind is TrailKind.external_call]


async def test_new_client_engagement_runs_to_acc01_with_manual_checks(caplog: pytest.LogCaptureFixture) -> None:
    """新客户的项目没有数据集, 没配 ABN Lookup: ACC-04 之前判断要不要等 ASIC 摘录时查不了 ABN, 不等 (需求 R290);
    ACC-04 四项待人工确认, 不出例外. 两次查询各记一条 "没有查询" 的外部调用留痕, 不含 GUID; 停在 ACC-01."""
    runner, _, eid, state, _ = await new_client_engagement("")
    assert state["current_gate"] == "ACC-01"
    [card] = await runner.deps.output_store.latest(eid, AcceptanceScorecard)
    assert [c.check for c in card.registry_checks] == [
        "abn_active",
        "name_match",
        "company_registered",
        "directors_match",
    ]
    assert [c.passed for c in card.registry_checks] == [None] * 4
    assert card.exception_ids == [] and await runner.deps.output_store.latest(eid, AuditException) == []
    queries = external_calls(runner)
    payload = {"abn": "44900043101", "available": False, "reason": "not_configured", "result": None}
    assert [q.payload for q in queries] == [payload, payload]  # p0_inputs 一次, ACC-04 一次
    assert {c.evidence.source_id for c in card.registry_checks} == {queries[-1].id}  # 都指向 ACC-04 的那条
    assert [(c.evidence.type, c.evidence.reliability) for c in card.registry_checks] == [NOT_QUERIED] * 4
    assert FAKE_GUID not in json.dumps([e.model_dump(mode="json") for e in runner.deps.trail.entries]) + caplog.text


async def test_new_company_client_waits_for_the_asic_extract(caplog: pytest.LogCaptureFixture) -> None:
    """配了 ABN Lookup (假接口回放有效的公司登记, 有 ACN): ACC-04 之前查一次 ABN (留痕, 不含 GUID), 还没有 ASIC 摘录,
    ACC-04 记等待数据, A3 提出补交请求 "ASIC 摘录" (需求 R290); 不执行 ACC-04, 不到 ACC-01."""
    runner, _, eid, state, _ = await new_client_engagement(FAKE_GUID)
    assert state["current_gate"] is None
    [acc04] = await runner.deps.output_store.latest(eid, Procedure, id="ACC-04")
    assert (acc04.status, acc04.data_request_ids) == (ProcedureStatus.awaiting_data, ["REQ-registry_extract"])
    assert await runner.deps.output_store.latest(eid, AcceptanceScorecard) == []
    request = next(r for r in await runner.deps.data.requests(eid) if r.id == "REQ-registry_extract")
    assert (request.title, request.used_for, request.status) == ("ASIC 摘录", ["ACC-04"], "open")
    assert [q.payload for q in external_calls(runner)] == [{"abn": "44900043101", "result": WOMBAT_RECORD}]
    assert FAKE_GUID not in json.dumps([e.model_dump(mode="json") for e in runner.deps.trail.entries]) + caplog.text


async def test_next_engagement_brings_the_latest_profile() -> None:
    """同一客户再建项目 (下一年度): 带出最新一版画像 (ACC-01 改过的第 2 版), 不另写版本."""
    runner = demo_runner()
    async with api_client(runner) as client:
        client_id = (await post(client, wombat())).json()["client_id"]
        first = (await client.post(ENGAGEMENTS, json={**PERIOD, "client_id": client_id})).json()["engagement"]["id"]
        await wait_idle(runner, first)
        update = {**wombat(directors=["Jane Citizen", "Sam Example"]), "version": 2, "abn": "44900043101"}
        assert (await client.put(f"{ENGAGEMENTS}/{first}/client-profile", json=update)).status_code == 202
        await wait_idle(runner, first)
        next_year = {"period_start": "2027-07-01", "period_end": "2028-06-30", "client_id": client_id}
        second = (await client.post(ENGAGEMENTS, json=next_year)).json()
        await wait_idle(runner, second["engagement"]["id"])
    assert second["engagement"]["client_profile_version"] == 2
    history = await runner.deps.engagement_store.profile_history(client_id)
    assert [(r.profile.version, r.source) for r in history] == [(1, ProfileSource.manual), (2, ProfileSource.update)]
    assert history[1].profile.directors == ["Jane Citizen", "Sam Example"]


async def test_engagement_needs_exactly_one_of_dataset_or_client() -> None:
    runner = demo_runner()
    async with api_client(runner) as client:
        client_id = (await post(client, wombat())).json()["client_id"]
        both = await client.post(ENGAGEMENTS, json={**PERIOD, "dataset": "demo", "client_id": client_id})
        neither = await client.post(ENGAGEMENTS, json=PERIOD)
        unknown = await client.post(ENGAGEMENTS, json={**PERIOD, "client_id": "CLIENT-M-000000000000"})
        dataset_client = await client.post(ENGAGEMENTS, json={**PERIOD, "client_id": "CLIENT-001"})
    assert [r.status_code for r in (both, neither, unknown, dataset_client)] == [422, 422, 404, 422]
    assert dataset_client.json()["detail"]["code"] == "INVALID_VALUE"


async def test_dataset_client_path_is_unchanged() -> None:
    """选数据集客户照旧发 dataset: 项目的 dataset 与采用的画像同 R288 之前; 画像第 1 版记来源 dataset 与建项目的人."""
    runner = demo_runner()
    async with api_client(runner) as client:
        created = await client.post(ENGAGEMENTS, json={**PERIOD, "dataset": "demo"})
        engagement = created.json()["engagement"]
        await wait_idle(runner, engagement["id"])
    assert (engagement["dataset"], engagement["client_id"], engagement["client_profile_version"]) == (
        "demo",
        "CLIENT-001",
        1,
    )
    [record] = await runner.deps.engagement_store.profile_history("CLIENT-001")
    assert record.profile == runner.deps.datasets.client_profile("demo")  # 数据集画像原样
    assert (record.source, record.updated_by) == (ProfileSource.dataset, NUMBAT_AUDITOR.id)


@needs_snapshot
async def test_xero_dataset_client_is_listed_after_use() -> None:
    """用过的数据集客户 (库里已有画像) 仍是一行, origin dataset, latest_version 为库里的版本."""
    runner = demo_runner()
    async with api_client(runner) as client:
        body = {"dataset": "xero/20261017-q1", "period_start": "2026-07-01", "period_end": "2026-09-30"}
        eid = (await client.post(ENGAGEMENTS, json=body)).json()["engagement"]["id"]
        await wait_idle(runner, eid)
        rows = [r for r in (await client.get(CLIENTS)).json() if r["client_id"] == "CLIENT-XERO-3C1332BB-Q1"]
    assert [(r["origin"], r["latest_version"], [d["dataset"] for d in r["datasets"]]) for r in rows] == [
        ("dataset", 1, ["xero/20261017-q1"])
    ]


# ── 没有数据集时的各处取值 ───────────────


async def test_no_dataset_falls_back_everywhere(tmp_path: Path) -> None:
    """fixture 读默认目录, 函证出未回函回执, 计划取 knowledge/planning 的通用模板."""
    store = CreatingEngagementStore()
    engagement = await store.create(
        owner_id=NUMBAT_AUDITOR.id,
        period_start=date(2026, 7, 1),
        period_end=date(2027, 6, 30),
        dataset=None,
        profile=NUMBAT_PROFILE.model_copy(update={"id": "CLIENT-M-abcdefabcdef"}),
    )
    looked: list[str] = []

    def directory(dataset: str) -> Path | None:
        looked.append(dataset)
        return tmp_path

    llm = DatasetFixtureLLMClient(tmp_path / "fixtures", SETTINGS.DATASET_ROOT, store)
    assert await llm.directory(engagement.id) == tmp_path / "fixtures"
    channel = DatasetConfirmationChannel(store, directory, tmp_path / "receipts")
    request = ConfirmationRequest(
        bank_account_id="BA-1", bank_name="Bank", bsb="000000", account_no="12345678", as_of=date(2027, 6, 30)
    )
    receipt = await channel.submit(engagement.id, request, "K-1")
    assert receipt.reply_bytes is None and looked == []
    assert receipt.replied_at is None
    assert demo_runner().deps.library.planning_templates() == read_planning(SETTINGS.KNOWLEDGE_ROOT / "planning")


async def test_seed_load_skips_an_engagement_without_dataset(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """seed load 对没有数据集的项目说明后跳过, 不调载入器."""
    from types import SimpleNamespace

    import backend.main
    from backend.persistence import seed

    store = CreatingEngagementStore()
    engagement = await store.create(
        owner_id=NUMBAT_AUDITOR.id,
        period_start=date(2026, 7, 1),
        period_end=date(2027, 6, 30),
        dataset=None,
        profile=NUMBAT_PROFILE.model_copy(update={"id": "CLIENT-M-abcdefabcdef"}),
    )

    class Engine:
        async def dispose(self) -> None:
            pass

    class Loader:
        async def load(self, eid: str, dataset: str) -> None:
            raise AssertionError("不应载入")

    deps = SimpleNamespace(financial_store=SimpleNamespace(engine=Engine()), engagement_store=store, datasets=Loader())
    monkeypatch.setattr(backend.main, "build_deps", lambda settings: deps)
    await seed.load(engagement.id)
    assert "没有数据集" in capsys.readouterr().err


# ── 没有数据集的两个分支: 计划预置与执行记录的数据集声明 (审核 R288-01) ───────────────


class SpyLoader(RoutingDatasetLoader):
    """与组装根相同的数据集载入器, 记下以数据集名调用的预置与声明 (新客户的项目不应调用)."""

    def __init__(self, inner: RoutingDatasetLoader) -> None:
        super().__init__(inner.default, inner.by_prefix)
        self.calls: list[tuple[str, str]] = []

    def planning_presets(self, dataset: str) -> Any:
        self.calls.append(("planning_presets", dataset))
        return super().planning_presets(dataset)

    def dataset_traits(self, dataset: str) -> Any:
        self.calls.append(("dataset_traits", dataset))
        return super().dataset_traits(dataset)


async def test_pln01_without_dataset_takes_the_generic_templates() -> None:
    """真实的 RiskAssessorAgent (PLN-01) 在项目没有数据集时取 knowledge/planning 的通用模板, 不以任何数据集名调用
    planning_presets (按默认数据集兜底即失败)."""
    deps = demo_runner().deps
    assert isinstance(deps.datasets, RoutingDatasetLoader)
    spy = SpyLoader(deps.datasets)
    deps = replace(make_deps(FakeRegistry(), FakeTrail()), datasets=spy)
    ctx = make_ctx(deps, "PLN-01")
    ctx = RunContext(
        ctx.engagement.model_copy(update={"dataset": None}), ctx.procedure, ctx.template, ctx.run, ctx.state
    )
    patch = await RiskAssessorAgent(deps).run(ctx)
    objectives, risks = read_planning(SETTINGS.KNOWLEDGE_ROOT / "planning")
    assert (patch["audit_objectives"], patch["risk_register"]) == (objectives, risks)
    assert spy.calls == []


@pytest.mark.parametrize("ready", [False, True])
async def test_new_client_engagement_never_reads_dataset_presets_or_traits(ready: bool) -> None:
    """新客户的项目从建项目到 ACC-01 通过之后 (数据未齐停在 p1_await_data; 已齐则跑完 P1 到 gate_p1_risk): 执行记录
    的数据集声明与计划预置都不以任何数据集名调用 (按默认数据集兜底即失败)."""
    base = demo_runner()
    assert isinstance(base.deps.datasets, RoutingDatasetLoader)
    spy = SpyLoader(base.deps.datasets)
    engagements = base.deps.engagement_store
    deps = replace(
        make_deps(FakeRegistry(), FakeTrail(), engagements, data=FakeDataStore(ready=ready)),
        datasets=spy,
        registry=base.deps.registry,
    )
    from langgraph.checkpoint.memory import InMemorySaver

    from backend.orchestration.checkpoint import checkpoint_serde
    from backend.orchestration.graph import compile_graph
    from tests.fakes import make_graph_nodes

    runner = GraphRunner(compile_graph(make_graph_nodes(deps), InMemorySaver(serde=checkpoint_serde())), deps)
    async with api_client(runner) as client:
        client_id = (await post(client, wombat())).json()["client_id"]
        eid = (await client.post(ENGAGEMENTS, json={**PERIOD, "client_id": client_id})).json()["engagement"]["id"]
        await wait_idle(runner, eid)
        gate = (await client.get(f"{ENGAGEMENTS}/{eid}/gate")).json()
        decision = {
            "gate": "ACC-01",
            "target_id": gate["target_id"],
            "target_version": gate["material"]["scorecard"]["version"],
            "decision": "approve",
        }
        assert (await client.post(f"{ENGAGEMENTS}/{eid}/gate-decisions", json=decision)).status_code == 202
        await wait_idle(runner, eid)
    assert runner.run_error(eid) is None, runner.run_error(eid)
    snapshot = await runner.app.aget_state(GraphRunner.config(eid))
    assert snapshot.next == (("gate_p1_risk",) if ready else ("p1_await_data",))
    assert spy.calls == []


def test_journal_source_label_without_dataset_is_empty() -> None:
    """新客户还没导入总账时体检的来源名为空 (前端显示尚未导入), 不借数据集的文件名 (审核 R288-07); 有数据集的照旧."""
    assert source_label(None, None) is None
    assert (source_label(None, "demo"), source_label(None, "xero/20261017-q1")) == ("journal_entries.csv", "Xero")
