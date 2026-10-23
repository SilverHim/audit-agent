"""演示默认数据集 (R79): 配置项 DEMO_DATASET 缺省为 demo-compact; 原 Xero 数据集继续回归.
用它建项目 (不连库: 假存储, 真实载入器读文件,
按项目数据集定位的登记 mock) 跑到 ACC-01; 种子审计师名为 Jordan Ellis. 需要本地 Xero 快照的用例在没有时跳过."""

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from backend.adapters.registry_mock import DatasetRegistrySource
from backend.contracts.models import AcceptanceScorecard, ProcedureRun, TrailKind, WorkingLanguage
from backend.core.config import Settings, get_settings
from backend.main import build_deps
from backend.orchestration.checkpoint import checkpoint_serde
from backend.orchestration.graph import compile_graph
from backend.orchestration.runner import GraphRunner
from backend.persistence import seed as seed_module
from backend.persistence.stores import CsvDatasetLoader, RoutingDatasetLoader, XeroDatasetLoader
from tests.external import needs, snapshot_location, supplements_path
from tests.fakes import (
    ENGAGEMENT,
    NUMBAT_AUDITOR,
    CreatingEngagementStore,
    FakeOutputStore,
    FakeRegistry,
    FakeTrail,
    api_client,
    make_deps,
    make_graph_nodes,
    wait_idle,
)

SETTINGS = get_settings()
DEFAULT = "xero/20261017-q1"
SNAPSHOTS = snapshot_location("20261017-q1").parent  # 快照目录的上级 (只给路径; 要快照的用例经 needs_snapshot 核对)
needs_snapshot = needs("q1_snapshot", "q1_supplements")  # 标 external, 没有快照或补造时跳过, 跟踪了却找不到时失败
NO_DB: Any = object()  # 建项目与 ACC-04 不读写财务表


def demo_runner(
    registry: Callable[[Any, Callable[[str], Path | None], Any], Any] | None = None,
    fixed_language: WorkingLanguage | None = "en",
) -> GraphRunner:
    """与组装根相同的数据集载入器 (CSV 加带补造目录的 Xero 快照) 与按项目数据集定位的登记 mock; 其余为假实现.
    registry 给出时以 (项目存储, 数据集目录的定位函数, 依赖) 建登记来源 (如组合实现, 其上传摘录一侧读依赖里的存储),
    替代登记 mock. fixed_language 见 tests.fakes.make_deps (缺省同组装根的 mock 模式)."""
    datasets = RoutingDatasetLoader(
        CsvDatasetLoader(SETTINGS.DATASET_ROOT, NO_DB, NO_DB),
        {
            XeroDatasetLoader.PREFIX: XeroDatasetLoader(
                SNAPSHOTS,
                SETTINGS.KNOWLEDGE_ROOT / "planning",
                NO_DB,
                NO_DB,
                supplements=supplements_path("20261017-q1").parent,
            )
        },
    )
    engagements = CreatingEngagementStore()
    deps = make_deps(FakeRegistry(), FakeTrail(), engagements, FakeOutputStore(), fixed_language=fixed_language)
    source = (
        registry(engagements, datasets.supplement_dir, deps)
        if registry is not None
        else DatasetRegistrySource(engagements, datasets.supplement_dir)
    )
    deps = replace(deps, datasets=datasets, registry=source)
    app = compile_graph(make_graph_nodes(deps), InMemorySaver(serde=checkpoint_serde()))
    return GraphRunner(app, deps)


def test_demo_dataset_defaults_to_compact() -> None:
    """精简演示成为缺省, 原数据集仍可选."""
    assert Settings.model_fields["DEMO_DATASET"].default == "demo-compact"


DEMO_BODY = {"period_start": "2024-01-01", "period_end": "2024-12-31", "dataset": "demo"}  # 与任何数据集都无关的期间


async def test_demo_created_with_any_period() -> None:
    """验收 4, 8: demo (Numbat) 不自带期间, 不比较: 用与任何数据集都无关的期间建项目 201, 项目期间为请求值,
    跑到 ACC-01. 不带 working_language 时项目与各次执行都是 en (需求 R339 验收 1, 3), 总览没有要标注的执行."""
    runner = demo_runner()
    async with api_client(runner) as client:
        created = await client.post("/api/v1/engagements", json=DEMO_BODY)
        assert created.status_code == 201, created.text
        eid = created.json()["engagement"]["id"]
        await wait_idle(runner, eid)
        overview = (await client.get(f"/api/v1/engagements/{eid}")).json()
    assert runner.run_error(eid) is None
    engagement = overview["engagement"]
    assert (engagement["period_start"], engagement["period_end"]) == ("2024-01-01", "2024-12-31")
    assert (engagement["dataset"], overview["current_gate"]) == ("demo", "ACC-01")
    runs = await runner.deps.output_store.latest(eid, ProcedureRun)
    assert engagement["working_language"] == "en" and runs and {run.language for run in runs} == {"en"}
    assert overview["run_languages"] == {}


@pytest.mark.parametrize(("mode", "fixed"), [("mock", "en"), ("live", None)])
def test_assembly_fixes_the_language_only_in_mock_mode(mode: Literal["mock", "live"], fixed: str | None) -> None:
    """需求 R343: 组装根在 LLM_MODE=mock 时把 AgentDeps.fixed_language 填 en (固定输出都是英文, 执行照实记 en), live
    为空 (执行记项目的工作语言)."""
    deps = build_deps(Settings(LLM_MODE=mode))
    assert deps.fixed_language == fixed
    assert deps.run_language(ENGAGEMENT.model_copy(update={"working_language": "zh"})) == (fixed or "zh")


@pytest.mark.parametrize(("fixed", "generated"), [(None, "zh"), ("en", "en")], ids=["live", "mock"])
async def test_demo_created_in_chinese(fixed: Literal["en"] | None, generated: Literal["zh", "en"]) -> None:
    """需求 R339 验收 1, 3: 带 working_language zh 建项目, 项目为 zh, 跑到 ACC-01 的各次执行都是 zh; mock (需求 R343)
    时执行照实记 en. 总览的 run_languages 只给生成语言与当前工作语言不同的执行 (审核 R339-09): live 时都是 zh, 为空,
    改为 en 后列出这些执行; mock 时反过来, 改为 en 之前列出, 之后为空 (页面据此标 "以英文生成")."""
    runner = demo_runner(fixed_language=fixed)
    async with api_client(runner) as client:
        created = await client.post("/api/v1/engagements", json={**DEMO_BODY, "working_language": "zh"})
        assert created.status_code == 201, created.text
        eid = created.json()["engagement"]["id"]
        await wait_idle(runner, eid)
        before = (await client.get(f"/api/v1/engagements/{eid}")).json()
        changed = await client.put(f"/api/v1/engagements/{eid}/working-language", json={"language": "en"})
        after = (await client.get(f"/api/v1/engagements/{eid}")).json()
    runs = await runner.deps.output_store.latest(eid, ProcedureRun)
    assert before["engagement"]["working_language"] == "zh" and runs and {run.language for run in runs} == {generated}
    listed = {run.id: generated for run in runs}
    assert changed.status_code == 200
    assert (before["run_languages"], after["run_languages"]) == (({}, listed) if fixed is None else (listed, {}))


@needs_snapshot
async def test_xero_dataset_runs_to_acc01() -> None:
    """验收 5: GET /datasets 的默认行带出期间, 按它建项目: 总览客户名为 Demo Company (AU) Pty Ltd; ACC-04 的三项登记
    核对都通过, external_call 2 行; 停在 ACC-01 闸门."""
    runner = demo_runner()
    async with api_client(runner) as client:
        [default] = [d for d in (await client.get("/api/v1/datasets")).json() if d["dataset"] == DEFAULT]
        assert (default["dataset"], default["period_start"], default["period_end"]) == (
            DEFAULT,
            "2026-07-01",
            "2026-09-30",
        )
        body = {k: default[k] for k in ("dataset", "period_start", "period_end")}
        created = await client.post("/api/v1/engagements", json=body)
        assert created.status_code == 201, created.text
        eid = created.json()["engagement"]["id"]
        assert created.json()["client_name"] == "Demo Company (AU) Pty Ltd"
        await wait_idle(runner, eid)
        overview = (await client.get(f"/api/v1/engagements/{eid}")).json()
    assert runner.run_error(eid) is None
    assert (overview["client_name"], overview["current_gate"]) == ("Demo Company (AU) Pty Ltd", "ACC-01")
    [card] = await runner.deps.output_store.latest(eid, AcceptanceScorecard)
    assert [(c.check, c.passed) for c in card.registry_checks] == [
        ("abn_active", True),
        ("name_match", True),
        ("company_registered", True),
    ]
    trail = runner.deps.trail
    assert isinstance(trail, FakeTrail)
    assert sum(e.kind is TrailKind.external_call for e in trail.entries) == 2


async def test_seed_creates_jordan_ellis(monkeypatch: pytest.MonkeyPatch) -> None:
    """验收 6: seed 新建账号的姓名为 Jordan Ellis (报告签字块取项目负责人姓名); 登录名去空白转小写, 已有账号时
    create 返回 None, 什么都不改 (幂等由 UserStore.create 保证)."""
    created: list[tuple[str, str]] = []

    class Store:
        def __init__(self, engine: Any) -> None:
            pass

        async def create(self, email: str, full_name: str, password_hash: str) -> None:
            created.append((email, full_name))

    class Engine:
        async def dispose(self) -> None:
            pass

    monkeypatch.setattr(seed_module, "PostgresUserStore", Store)
    monkeypatch.setattr(seed_module, "default_engine", lambda: Engine())
    await seed_module.seed()
    assert created == [(SETTINGS.SEED_USER_EMAIL.strip().lower(), "Jordan Ellis")]
    assert NUMBAT_AUDITOR.full_name == seed_module.SEED_USER_NAME
