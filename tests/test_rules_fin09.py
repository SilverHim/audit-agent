"""需求 R164 (slow): FIN-09 V01 的 SQL 连库验收. Xero q1 按补造清单写进测试库 (R112 的 build_xero_project, 数据集声明
随之载入), 更正分录照演示分两轮载入; 用真实的 PostgresFinancialDataStore 与 SqlRuleEngine 执行, 参数同 GraphNodes 用
build_run 解析. 没有 TEST_DATABASE_URL 时跳过."""

from datetime import UTC, datetime
from typing import Any

import pytest

from backend.orchestration.nodes import build_run
from tests.external import snapshot_dir, supplements_dir
from tests.test_xero_rules_db import XeroProject, build_xero_project, database_url, procedure_of
from tests.xero_fakes import SNAPSHOT_NAME

pytestmark = [pytest.mark.slow, pytest.mark.external]


async def v01(project: XeroProject, **overrides: object) -> list[tuple[Any, ...]]:
    """以参数快照 (叠加 overrides) 跑 FIN-09 的规则, V01 各行的 (行 ID, 科目, 科目类型, 科目分类)."""
    deps = project.deps
    template = deps.library.load("FIN-09")
    run = build_run(
        deps.library,
        template,
        procedure_of(deps, "FIN-09"),
        project.engagement,
        project.state,
        datetime.now(UTC),
        traits=project.traits,
        profile=project.profile,
    )
    assert run.params_snapshot == {"adjustment_prefix": "AJE-"}  # 取 Xero 数据集的声明
    result = await deps.rules.run(project.engagement.id, template, run, run.params_snapshot | overrides)
    assert (result.exceptions, list(result.rows)) == ([], ["V01"])
    return [(r["id"], r["account_code"], r["account_type"], r["account_category"]) for r in result.rows["V01"]]


async def test_v01_takes_audit_adjustment_lines_by_prefix(tmp_path_factory: pytest.TempPathFactory) -> None:
    """验收 3: 未载入更正分录时 V01 没有行 (XJ-397, XJ-499 等 is_adjustment 为真的手工分录不取); 载入第 1 轮 (AJE-X01,
    AJE-X05) 后为这两笔的 5 行, 科目类型与分类取科目表 (DA 的映射); 再载第 2 轮后共 11 行. 前缀按原文比较: 改为
    "AJE_" 或 "%" 时没有行 (用 LIKE 会把 _ 与 % 当通配符而取到全部更正行)."""
    url = database_url()
    project = await build_xero_project(
        url, snapshot_dir(SNAPSHOT_NAME), supplements_dir(SNAPSHOT_NAME), tmp_path_factory.mktemp("xero")
    )
    eid, dataset = project.engagement.id, project.engagement.dataset
    assert dataset is not None
    assert await v01(project) == []

    await project.datasets.load_adjustments(eid, dataset, ["AJE-X01", "AJE-X05"])
    assert await v01(project) == [
        ("AJE-X01-1", "711", "asset", "property_plant_equipment"),
        ("AJE-X01-2", "710", "asset", "property_plant_equipment"),
        ("AJE-X01-3", "820", "liability", "tax_payable"),
        ("AJE-X05-1", "620", "asset", "prepayments"),
        ("AJE-X05-2", "270", "revenue", "other_income"),
    ]

    await project.datasets.load_adjustments(eid, dataset, ["AJE-X02", "AJE-X03", "AJE-X04"])
    rows = await v01(project)
    assert len(rows) == 11 and [r[0] for r in rows] == sorted(r[0] for r in rows)
    assert {r[0].rsplit("-", 1)[0] for r in rows} == {f"AJE-X0{n}" for n in range(1, 6)}
    assert await v01(project, adjustment_prefix="AJE_") == await v01(project, adjustment_prefix="%") == []
