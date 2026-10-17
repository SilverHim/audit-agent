"""可选客户清单 (R5): GET /datasets 与各载入器的 datasets().

用仓库里的 data 目录 (demo 即 Numbat) 与本机的 Xero Demo Company 快照 (不入库, 没有时跳过 Xero 那条).
"""

from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx

from backend.contracts.ports import DatasetLoader
from backend.core.config import get_settings
from backend.persistence.stores import CsvDatasetLoader, RoutingDatasetLoader, XeroDatasetLoader
from tests.fakes import NUMBAT_PROFILE, XERO_SNAPSHOT, XERO_SNAPSHOTS, api_client, make_runner, xero_snapshot

URL = "/api/v1/datasets"
NO_DB: Any = object()  # datasets 与 client_profile 只读文件, 不连库


def loaders(data: Path, xero: Path) -> RoutingDatasetLoader:
    """与 main.build_deps 相同的组合: 默认 CSV, "xero/" 前缀走 Xero 快照."""
    csv = CsvDatasetLoader(data, engine=NO_DB, evidence_store=NO_DB)
    return RoutingDatasetLoader(csv, {XeroDatasetLoader.PREFIX: XeroDatasetLoader(xero, xero, NO_DB, NO_DB)})


async def get_datasets(datasets: DatasetLoader) -> httpx.Response:
    runner, _ = make_runner()
    async with api_client(runner, deps=replace(runner.deps, datasets=datasets)) as client:
        return await client.get(URL)


async def test_lists_demo_from_data_root() -> None:
    response = await get_datasets(loaders(get_settings().DATASET_ROOT, XERO_SNAPSHOTS))
    demo = next(d for d in response.json() if d["dataset"] == "demo")
    assert demo == {"dataset": "demo", "legal_name": NUMBAT_PROFILE.legal_name, "abn": "68900042015"}
    assert all(d["dataset"] != "external" for d in response.json())  # 没有客户画像的目录不算


async def test_lists_loadable_xero_snapshot() -> None:
    xero_snapshot()  # 本机没有快照时跳过
    response = await get_datasets(loaders(get_settings().DATASET_ROOT, XERO_SNAPSHOTS))
    rows = {d["dataset"]: d for d in response.json()}
    assert rows[f"xero/{XERO_SNAPSHOT}"] == {
        "dataset": f"xero/{XERO_SNAPSHOT}",
        "legal_name": "Demo Company (AU)",
        "abn": "11111111138",
    }
    names = [d["dataset"] for d in response.json()]
    assert names == sorted(names)
    # 没有期间 dataset.json 的快照建不了项目, 不列出 (如早期免费接口的 20261017)
    unloadable = [p.name for p in XERO_SNAPSHOTS.iterdir() if p.is_dir() and not (p / "dataset.json").exists()]
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
