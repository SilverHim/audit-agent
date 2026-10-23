"""可选客户清单 (R5): GET /datasets 与各载入器的 datasets().

用仓库里的 data 目录 (demo 即 Numbat) 与 Xero Demo Company 快照 (经 tests.external 定位: 没有时跳过 Xero 那几条,
仓库跟踪了却找不到时失败).
"""

import json
import shutil
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest

from backend.api.routes import datasets as datasets_route
from backend.contracts.ports import DatasetLoader
from backend.core.config import Settings, get_settings
from backend.persistence.stores import CsvDatasetLoader, RoutingDatasetLoader, XeroDatasetLoader
from tests.external import snapshot_dir, supplements_dir
from tests.fakes import (
    NUMBAT_AUDITOR,
    NUMBAT_PROFILE,
    XERO_SNAPSHOT,
    api_client,
    make_runner,
    xero_snapshots,
)

URL = "/api/v1/datasets"
NO_DB: Any = object()  # datasets 与 client_profile 只读文件, 不连库


def loaders(data: Path, xero: Path, supplements: Path | None = None) -> RoutingDatasetLoader:
    """与 main.build_deps 相同的组合: 默认 CSV, "xero/" 前缀走 Xero 快照 (给了 supplements 时带补造目录)."""
    csv = CsvDatasetLoader(data, engine=NO_DB, evidence_store=NO_DB)
    xero_loader = XeroDatasetLoader(xero, xero, NO_DB, NO_DB, supplements=supplements)
    return RoutingDatasetLoader(csv, {XeroDatasetLoader.PREFIX: xero_loader})


async def get_datasets(datasets: DatasetLoader) -> httpx.Response:
    runner, _ = make_runner()
    async with api_client(runner, deps=replace(runner.deps, datasets=datasets)) as client:
        return await client.get(URL)


async def test_lists_demo_from_data_root(tmp_path: Path) -> None:
    response = await get_datasets(loaders(get_settings().DATASET_ROOT, tmp_path / "no-xero"))
    demo = next(d for d in response.json() if d["dataset"] == "demo")
    assert demo == {
        "dataset": "demo",
        "legal_name": NUMBAT_PROFILE.legal_name,
        "abn": "68900042015",
        "period_start": None,  # CSV 数据集不自带期间 (R79)
        "period_end": None,
        "default": False,
    }
    assert all(d["dataset"] != "external" for d in response.json())  # 没有客户画像的目录不算


@pytest.mark.external
async def test_lists_loadable_xero_snapshot() -> None:
    snapshots = xero_snapshots()
    response = await get_datasets(loaders(get_settings().DATASET_ROOT, snapshots))
    rows = {d["dataset"]: d for d in response.json()}
    assert rows[f"xero/{XERO_SNAPSHOT}"] == {
        "dataset": f"xero/{XERO_SNAPSHOT}",
        "legal_name": "Demo Company (AU)",
        "abn": "11111111138",
        "period_start": "2026-07-01",  # 快照 dataset.json 的期间 (R79)
        "period_end": "2026-11-30",
        "default": False,
    }
    names = [d["dataset"] for d in response.json()]
    assert names == sorted(names)
    # 没有期间 dataset.json 的快照建不了项目, 不列出 (如早期免费接口的 20261017)
    unloadable = [p.name for p in snapshots.iterdir() if p.is_dir() and not (p / "dataset.json").exists()]
    assert not {f"xero/{name}" for name in unloadable} & set(rows)


async def test_skips_hidden_unprofiled_and_broken(tmp_path: Path) -> None:
    data, xero = tmp_path / "data", tmp_path / "xero"
    (data / "acme").mkdir(parents=True)
    (data / "acme" / "client_profile.json").write_text(
        NUMBAT_PROFILE.model_copy(update={"legal_name": "Acme"}).model_dump_json()
    )
    (data / "broken").mkdir()
    (data / "broken" / "client_profile.json").write_text("{}")  # 画像不合格: 列出名字但读不了, 接口跳过
    (data / "external").mkdir()  # 没有画像
    (xero / ".snap.partial").mkdir(parents=True)  # 拉取中的临时目录
    for name in ("organisation.json", "dataset.json"):
        (xero / ".snap.partial" / name).write_text("[]")
    datasets = loaders(data, xero)
    assert datasets.datasets() == ["acme", "broken"]

    response = await get_datasets(datasets)
    assert [d["dataset"] for d in response.json()] == ["acme"]
    assert loaders(tmp_path / "missing", tmp_path / "missing").datasets() == []


async def test_requires_login() -> None:
    runner, _ = make_runner()
    async with api_client(runner) as client:
        response = await client.get(URL, headers={"Authorization": ""})
    assert (response.status_code, response.json()["detail"]["code"]) == (401, "BAD_CREDENTIALS")


@pytest.mark.external
async def test_mismatched_supplements_are_skipped_and_rejected(tmp_path: Path) -> None:
    """补造文件与快照不匹配 (R70): GET /datasets 跳过该数据集, POST /engagements 返回 422 INVALID_VALUE, 不建项目."""
    snapshots = snapshot_dir("20261017-q1").parent
    demo = tmp_path / "xero_demo"
    shutil.copytree(supplements_dir("20261017-q1"), demo / "20261017-q1")
    xero = XeroDatasetLoader(snapshots, snapshots, NO_DB, NO_DB, supplements=demo)
    datasets = RoutingDatasetLoader(CsvDatasetLoader(get_settings().DATASET_ROOT, NO_DB, NO_DB), {"xero/": xero})
    rows = {d["dataset"]: d for d in (await get_datasets(datasets)).json()}
    assert rows["xero/20261017-q1"]["legal_name"] == "Demo Company (AU) Pty Ltd"  # 补造画像
    profile = demo / "20261017-q1" / "client_profile.json"
    profile.write_bytes(profile.read_bytes() + b" ")
    assert "xero/20261017-q1" not in {d["dataset"] for d in (await get_datasets(datasets)).json()}
    runner, _ = make_runner()
    deps = replace(runner.deps, datasets=datasets)
    before = await deps.engagement_store.list(NUMBAT_AUDITOR.id)
    async with api_client(runner, deps=deps) as client:
        body = {"period_start": "2026-07-01", "period_end": "2026-09-30", "dataset": "xero/20261017-q1"}
        response = await client.post("/api/v1/engagements", json=body)
    assert (response.status_code, response.json()["detail"]["code"]) == (422, "INVALID_VALUE")
    assert "补造文件与快照不匹配: client_profile.json" in response.json()["detail"]["message"]
    assert await deps.engagement_store.list(NUMBAT_AUDITOR.id) == before


# ── R79: 默认数据集与期间 ──

Q1 = "xero/20261017-q1"


def with_supplements() -> RoutingDatasetLoader:
    """快照 20261017-q1 与它的补造目录 (经 tests.external 定位) 加 data 目录的载入器."""
    return loaders(
        get_settings().DATASET_ROOT, snapshot_dir("20261017-q1").parent, supplements_dir("20261017-q1").parent
    )


@pytest.mark.external
async def test_default_row_has_period_and_supplement_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """验收 2, 3: xero/20261017-q1 取补造画像与快照期间, default 为真; 其余行为假. DEMO_DATASET 不在清单时全部为假."""
    datasets = with_supplements()
    assert datasets.period(Q1) == (date(2026, 7, 1), date(2026, 9, 30))
    assert datasets.period("xero/20261017-mock") == (date(2026, 7, 1), date(2026, 11, 30))
    assert datasets.period("demo") is None
    rows = {d["dataset"]: d for d in (await get_datasets(datasets)).json()}
    assert rows[Q1] == {
        "dataset": Q1,
        "legal_name": "Demo Company (AU) Pty Ltd",
        "abn": "25900043021",
        "period_start": "2026-07-01",
        "period_end": "2026-09-30",
        "default": True,
    }
    assert [name for name, row in rows.items() if row["default"]] == [Q1]
    monkeypatch.setattr(datasets_route, "get_settings", lambda: Settings(DEMO_DATASET="xero/none"))
    assert not any(d["default"] for d in (await get_datasets(datasets)).json())


@pytest.mark.external
@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2025-07-01", "2026-06-30"),  # 两端都不同 (Numbat 的财年)
        ("2026-07-01", "2026-11-30"),  # 只有截止日不同 (mock 快照的期间)
        ("2026-06-01", "2026-09-30"),  # 只有起始日不同
    ],
)
async def test_period_must_match_dataset(start: str, end: str) -> None:
    """验收 4: 数据集自带期间而请求的期间不同 (任一端) 时 422 INVALID_VALUE, 消息含两组期间, 不建项目.
    demo 照常建项目见 test_demo_default.test_demo_created_with_any_period."""
    runner, _ = make_runner()
    deps = replace(runner.deps, datasets=with_supplements())
    before = await deps.engagement_store.list(NUMBAT_AUDITOR.id)
    async with api_client(runner, deps=deps) as client:
        body = {"period_start": start, "period_end": end, "dataset": Q1}
        response = await client.post("/api/v1/engagements", json=body)
    assert (response.status_code, response.json()["detail"]["code"]) == (422, "INVALID_VALUE")
    message = response.json()["detail"]["message"]
    assert "2026-07-01 至 2026-09-30" in message and f"{start} 至 {end}" in message
    assert await deps.engagement_store.list(NUMBAT_AUDITOR.id) == before


SYNTHETIC_ORG = [{"OrganisationID": "00000000-0000-0000-0000-00000000c0de", "Name": "Synthetic Test Co"}]


def synthetic_snapshot(root: Path, name: str, dataset_json: str) -> None:
    """只有组织信息与 dataset.json 的合成快照 (Synthetic – for demonstration only), dataset.json 按给定文本写."""
    directory = root / name
    directory.mkdir(parents=True)
    org = [{**SYNTHETIC_ORG[0], "RegistrationNumber": "11111111138"}]
    (directory / "organisation.json").write_text(json.dumps(org), encoding="utf-8")
    (directory / "dataset.json").write_text(dataset_json, encoding="utf-8")


async def test_unreadable_period_row_is_skipped(tmp_path: Path) -> None:
    """dataset.json 缺键, 不是对象, 日期写错或不是 JSON 时期间读不了: GET /datasets 跳过该行, 其余照常; 用它建项目
    422 INVALID_VALUE, 不建项目."""
    good = '{"period_start": "2026-01-01", "period_end": "2026-03-31", "prior_end": "2025-12-31"}'
    bad = {
        "missing-key": '{"period_start": "2026-01-01", "period_end": "2026-03-31"}',
        "not-object": '"2026-01-01"',
        "bad-date": '{"period_start": "2026-13-01", "period_end": "2026-03-31", "prior_end": "2025-12-31"}',
        "not-json": "not json",
    }
    synthetic_snapshot(tmp_path, "good", good)
    for name, text in bad.items():
        synthetic_snapshot(tmp_path, name, text)
    datasets = loaders(get_settings().DATASET_ROOT, tmp_path)
    rows = {d["dataset"]: d for d in (await get_datasets(datasets)).json()}
    assert sorted(rows) == ["demo", "demo-compact", "xero/good"]
    assert (rows["xero/good"]["period_start"], rows["xero/good"]["period_end"]) == ("2026-01-01", "2026-03-31")
    runner, _ = make_runner()
    deps = replace(runner.deps, datasets=datasets)
    before = await deps.engagement_store.list(NUMBAT_AUDITOR.id)
    async with api_client(runner, deps=deps) as client:
        for name in bad:
            body = {"period_start": "2026-01-01", "period_end": "2026-03-31", "dataset": f"xero/{name}"}
            response = await client.post("/api/v1/engagements", json=body)
            assert (response.status_code, response.json()["detail"]["code"]) == (422, "INVALID_VALUE"), name
            message = response.json()["detail"]["message"]
            assert str(tmp_path) not in message and bad[name] not in message, name  # 不带服务器路径与文件内容
            if name != "not-json":  # 不是 JSON 时为解析器的消息 (只有行列号)
                expected = "须为含 period_start, period_end, prior_end (YYYY-MM-DD) 的对象"
                assert message == f"快照 {name} 的 dataset.json {expected}", name
    assert await deps.engagement_store.list(NUMBAT_AUDITOR.id) == before
