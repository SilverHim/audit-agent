"""登记与函证 mock 按项目数据集定位 (R71): Numbat 读 data/demo, Xero 读补造目录, 没有补造目录时查不到与未回函;
回执幂等且按项目隔离; 项目不存在报 LookupError. 不连库; Xero 的用例在本机没有快照时跳过."""

import shutil
from datetime import date
from pathlib import Path

import pytest

from backend.adapters.confirmation_mock import DatasetConfirmationChannel
from backend.adapters.registry_mock import DatasetRegistrySource
from backend.adapters.xero_supplements import SupplementMismatch
from backend.contracts.ports import ConfirmationRequest
from backend.persistence.stores import CsvDatasetLoader, RoutingDatasetLoader, XeroDatasetLoader
from tests.fakes import ENGAGEMENT, FakeEngagementStore

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / "data"
API = DATA / "external" / "xero" / "api"
NO_DB: object = object()
NUMBAT = ENGAGEMENT
Q1 = ENGAGEMENT.model_copy(update={"id": "ENG-2026-101", "dataset": "xero/20261017-q1"})
MOCK = ENGAGEMENT.model_copy(update={"id": "ENG-2026-102", "dataset": "xero/20261017-mock"})
needs_q1 = pytest.mark.skipif(not (API / "20261017-q1").is_dir(), reason="没有本地 Xero 快照 20261017-q1")
needs_mock = pytest.mark.skipif(not (API / "20261017-mock").is_dir(), reason="没有本地 Xero 快照 20261017-mock")


def datasets(api: Path = API, supplements: Path = DATA / "xero_demo") -> RoutingDatasetLoader:
    """与 main.build_deps 相同的组合 (不连库)."""
    xero = XeroDatasetLoader(api, api, NO_DB, NO_DB, supplements=supplements)  # type: ignore[arg-type]
    return RoutingDatasetLoader(CsvDatasetLoader(DATA, NO_DB, NO_DB), {"xero/": xero})  # type: ignore[arg-type]


def mocks(
    tmp_path: Path, loader: RoutingDatasetLoader | None = None
) -> tuple[DatasetRegistrySource, DatasetConfirmationChannel]:
    store = FakeEngagementStore(engagements=[NUMBAT, Q1, MOCK])
    routing = loader or datasets()
    return (
        DatasetRegistrySource(store, routing.supplement_dir),
        DatasetConfirmationChannel(store, routing.supplement_dir, tmp_path / "receipts"),
    )


def request(account_no: str, bank_account_id: str = "BA-01") -> ConfirmationRequest:
    return ConfirmationRequest(
        bank_account_id=bank_account_id, bank_name="Bank", bsb="000-000", account_no=account_no, as_of=date(2026, 6, 30)
    )


async def test_numbat_reads_data_demo(tmp_path: Path) -> None:
    registry, channel = mocks(tmp_path)
    abn = await registry.abn(NUMBAT.id, "68 900 042 015")
    assert abn is not None and abn.status == "Active"
    company = await registry.asic_company(NUMBAT.id, "900042015")
    assert company is not None and company.name == "Numbat Electronic Components Pty Ltd"
    for account in ("12345601", "23456702"):
        receipt = await channel.submit(NUMBAT.id, request(account), f"key-{account}")
        assert receipt.reply_bytes == (DATA / "demo" / "confirmations" / f"{account}.pdf").read_bytes()
        assert (receipt.reply_file_name, receipt.receipt_channel) == (f"{account}.pdf", "direct_email")


@needs_q1
async def test_xero_q1_reads_supplements(tmp_path: Path) -> None:
    registry, channel = mocks(tmp_path)
    demo = await registry.abn(Q1.id, "25900043021")
    assert demo is not None and (demo.status, demo.entity_name, demo.acn) == (
        "Active",
        "Demo Company (AU) Pty Ltd",
        "900043021",
    )
    company = await registry.asic_company(Q1.id, "900043021")
    assert company is not None and (company.status, company.registered_date) == ("Registered", date(2018, 9, 3))
    vpa = await registry.abn(Q1.id, "27900043030")
    assert vpa is not None and vpa.entity_name == "Victoria Property Agency Pty Ltd"
    replies = DATA / "xero_demo" / "20261017-q1" / "confirmations"
    for account, bank in (("12345678", "BA-090"), ("12345679", "BA-091")):
        receipt = await channel.submit(Q1.id, request(account, bank), f"key-{bank}")
        assert receipt.reply_bytes == (replies / f"{account}.pdf").read_bytes()
        assert receipt.receipt_channel == "direct_email"


@needs_q1
async def test_mismatched_supplements_raise(tmp_path: Path) -> None:
    api, demo = tmp_path / "api", tmp_path / "xero_demo"
    shutil.copytree(API / "20261017-q1", api / "20261017-q1")
    shutil.copytree(DATA / "xero_demo" / "20261017-q1", demo / "20261017-q1")
    asic = demo / "20261017-q1" / "registry" / "asic.json"
    asic.write_bytes(asic.read_bytes() + b" ")
    registry, channel = mocks(tmp_path, datasets(api, demo))
    with pytest.raises(SupplementMismatch, match=r"registry/asic\.json"):
        await registry.asic_company(Q1.id, "900043021")
    with pytest.raises(SupplementMismatch):
        await channel.submit(Q1.id, request("12345678", "BA-090"), "key")


@needs_mock
async def test_xero_without_supplements_finds_nothing(tmp_path: Path) -> None:
    """20261017-mock 没有补造目录: 登记查不到, 回函为未回函, 回执照常持久化, 同键重复调用返回同一回执."""
    registry, channel = mocks(tmp_path)
    assert await registry.abn(MOCK.id, "11111111138") is None
    assert await registry.asic_company(MOCK.id, "900043021") is None
    first = await channel.submit(MOCK.id, request("12345678", "BA-090"), "key")
    assert (first.replied_at, first.reply_file_name, first.reply_bytes, first.receipt_channel) == (
        None,
        None,
        None,
        None,
    )
    assert list((tmp_path / "receipts" / MOCK.id).glob("*.json"))
    assert await channel.submit(MOCK.id, request("12345678", "BA-090"), "key") == first


async def test_receipts_are_idempotent_and_per_engagement(tmp_path: Path) -> None:
    """同一项目同一键: 新建实例后仍返回同一回执. 两个项目同账号同键互不影响."""
    _, channel = mocks(tmp_path)
    first = await channel.submit(NUMBAT.id, request("12345601"), "same")
    _, restarted = mocks(tmp_path)
    assert await restarted.submit(NUMBAT.id, request("12345601"), "same") == first
    other = ENGAGEMENT.model_copy(update={"id": "ENG-2026-103", "dataset": "missing-dataset"})
    store = FakeEngagementStore(engagements=[NUMBAT, other])
    separate = DatasetConfirmationChannel(store, datasets().supplement_dir, tmp_path / "receipts")
    receipt = await separate.submit(other.id, request("12345601"), "same")
    assert receipt.reply_bytes is None and first.reply_bytes is not None  # 数据集目录不存在: 未回函
    assert await separate.submit(NUMBAT.id, request("12345601"), "same") == first


async def test_unknown_engagement_raises_lookup_error(tmp_path: Path) -> None:
    registry, channel = mocks(tmp_path)
    with pytest.raises(LookupError):
        await registry.abn("ENG-missing", "68900042015")
    with pytest.raises(LookupError):
        await channel.submit("ENG-missing", request("12345601"), "key")


def test_main_has_no_demo_literal() -> None:
    text = (REPO / "src" / "backend" / "main.py").read_text(encoding="utf-8")
    assert '"demo"' not in text and "'demo'" not in text


@needs_q1
async def test_existing_receipt_is_returned_without_resolving(tmp_path: Path) -> None:
    """已发出的函证同键再取: 直接返回原回执, 不再核对补造目录 (之后补造被改坏也取得回); 新键照常核对."""
    api, demo = tmp_path / "api", tmp_path / "xero_demo"
    shutil.copytree(API / "20261017-q1", api / "20261017-q1")
    shutil.copytree(DATA / "xero_demo" / "20261017-q1", demo / "20261017-q1")
    _, channel = mocks(tmp_path, datasets(api, demo))
    first = await channel.submit(Q1.id, request("12345678", "BA-090"), "sent")
    asic = demo / "20261017-q1" / "registry" / "asic.json"
    asic.write_bytes(asic.read_bytes() + b" ")
    assert await channel.submit(Q1.id, request("12345678", "BA-090"), "sent") == first
    with pytest.raises(SupplementMismatch):
        await channel.submit(Q1.id, request("12345679", "BA-091"), "new")


async def test_fallback_branches(tmp_path: Path) -> None:
    """载入器没有 supplement_dir 时为 None; 目录没有 registry/ 时查不到; 目录没有该账号回函与 receipts.json 时未回函."""

    class Bare:
        def datasets(self) -> list[str]:
            return []

    assert RoutingDatasetLoader(Bare(), {}).supplement_dir("anything") is None  # type: ignore[arg-type]
    empty = tmp_path / "empty"
    empty.mkdir()
    store = FakeEngagementStore(engagements=[NUMBAT])
    registry = DatasetRegistrySource(store, lambda dataset: empty)
    assert await registry.abn(NUMBAT.id, "68900042015") is None
    assert await registry.asic_company(NUMBAT.id, "900042015") is None
    channel = DatasetConfirmationChannel(store, lambda dataset: empty, tmp_path / "receipts")
    receipt = await channel.submit(NUMBAT.id, request("12345601"), "key")
    assert (receipt.replied_at, receipt.reply_bytes, receipt.receipt_channel) == (None, None, None)
