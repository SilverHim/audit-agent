"""数据集载入器: 已载入时不读源数据, 按前缀分发, Xero 快照路径安全与缺失报错, 期初余额口径. 不连数据库."""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from backend.persistence import stores
from backend.services.ledger import opening_balances, retained_earnings_code


@asynccontextmanager
async def no_transaction(*args: Any, **kwargs: Any) -> Any:
    yield None


async def test_already_loaded_csv_does_not_read_source_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """已载入的项目再次 load 直接返回, 源 CSV 被移走也不报错 (Codex 审核 X01 的探针)."""
    loader = stores.CsvDatasetLoader(tmp_path, engine=object(), evidence_store=object())  # type: ignore[arg-type]

    async def writable(conn: Any, eid: str) -> None:
        return None

    async def rows(eid: str, statement: Any) -> list[dict[str, Any]]:
        return [{"dataset": "demo", "data_version": 1}]

    monkeypatch.setattr(stores, "transaction", no_transaction)
    monkeypatch.setattr(stores, "writable", writable)
    monkeypatch.setattr(loader, "rows", rows)
    await loader.load("E", "demo")  # tmp_path/demo 不存在


def xero_loader(root: Path) -> stores.XeroDatasetLoader:
    return stores.XeroDatasetLoader(root, root, engine=object(), evidence_store=object())  # type: ignore[arg-type]


def test_xero_snapshot_path_rejects_escape(tmp_path: Path) -> None:
    root = tmp_path / "snapshots"
    root.mkdir()
    (tmp_path / "outside").mkdir()
    (root / "escape").symlink_to(tmp_path / "outside")
    loader = xero_loader(root)
    for dataset in ("xero/../outside", "xero//tmp", "xero/..", "xero/escape", "xero/", "xero/a/b", "demo"):
        with pytest.raises(ValueError):
            loader.snapshot(dataset)


def test_missing_xero_snapshot_or_organisation_is_not_found(tmp_path: Path) -> None:
    """快照不存在或缺组织信息时报 FileNotFoundError, 建项目接口据此返回 NOT_FOUND, 不建空画像的项目."""
    loader = xero_loader(tmp_path)
    with pytest.raises(FileNotFoundError):
        loader.client_profile("xero/nope")
    (tmp_path / "empty").mkdir()
    with pytest.raises(FileNotFoundError):
        loader.client_profile("xero/empty")


def test_routing_picks_loader_by_prefix() -> None:
    csv_loader, xero = object(), object()
    routing = stores.RoutingDatasetLoader(csv_loader, {"xero/": xero})  # type: ignore[arg-type]
    assert routing.pick("xero/20261017") is xero and routing.pick("demo") is csv_loader


def test_opening_balances_roll_profit_into_unique_retained_earnings() -> None:
    kinds = {"090": "asset", "200": "revenue", "400": "expense", "960": "equity"}
    categories = {"090": "cash", "200": "revenue", "400": "operating_expenses", "960": "retained_earnings"}
    prior = {"090": 300, "200": -500, "400": 200, "960": -100}
    assert opening_balances(prior, kinds, categories) == {"090": 300, "960": -400}
    assert opening_balances({"090": 1}, kinds, {}) == {"090": 1}  # 没有损益时不需要留存收益科目
    with pytest.raises(ValueError, match="没有标准分类为留存收益"):
        opening_balances(prior, kinds, {})
    with pytest.raises(ValueError, match="不唯一"):
        retained_earnings_code({"960": "retained_earnings", "961": "retained_earnings"})


async def test_already_loaded_xero_survives_removed_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """已载入的 Xero 项目再次 load 直接返回, 快照被移走也不报错 (Codex 审核 X01 第 2 轮的探针)."""
    loader = xero_loader(tmp_path)

    async def writable(conn: Any, eid: str) -> None:
        return None

    async def rows(eid: str, statement: Any) -> list[dict[str, Any]]:
        return [{"dataset": "xero/removed", "data_version": 1}]

    monkeypatch.setattr(stores, "transaction", no_transaction)
    monkeypatch.setattr(stores, "writable", writable)
    monkeypatch.setattr(loader, "rows", rows)
    await loader.load("E", "xero/removed")


def test_xero_without_supplements_tables_unchanged(tmp_path: Path) -> None:
    """R72: 没有补造目录的 Xero 快照 (20261017-mock) 载入的表与纯快照相同, 不登记合同; 服务完成记录为空,
    合同关联全空, 开户行为 Xero 科目名."""
    from backend.adapters.xero_dataset import Snapshot, build_tables

    api = Path(__file__).resolve().parents[1] / "data" / "external" / "xero" / "api"
    if not (api / "20261017-mock").is_dir():
        pytest.skip("没有本地 Xero 快照 20261017-mock")
    loader = stores.XeroDatasetLoader(
        api, api, engine=object(), evidence_store=object(), supplements=api.parents[2] / "xero_demo"
    )  # type: ignore[arg-type]
    tables, contracts = loader.tables("xero/20261017-mock")
    assert (tables, list(contracts)) == (build_tables(Snapshot(api / "20261017-mock")), [])
    assert tables["delivery_notes"] == [] and all(i.contract_id is None for i in tables["sales_invoices"])
    assert [a.bank_name for a in tables["bank_accounts"]] == ["Business Bank Account", "Business Savings Account"]
