"""数据集载入器: 已载入时不读源数据, 按前缀分发, Xero 快照路径安全与缺失报错, 期初余额口径. 不连数据库."""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from backend.persistence import stores
from backend.services.ledger import opening_balances, retained_earnings_code
from tests.external import optional, snapshot_dir, snapshot_location, supplements_dir, supplements_path


@asynccontextmanager
async def no_transaction(*args: Any, **kwargs: Any) -> Any:
    yield None


async def test_already_loaded_csv_does_not_read_source_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """已载入的项目再次 load 直接返回, 源 CSV 被移走也不报错 (Codex 审核 X01 的探针)."""
    loader = stores.CsvDatasetLoader(tmp_path, engine=object(), evidence_store=object())  # type: ignore[arg-type]

    async def writable(conn: Any, eid: str) -> None:
        return None

    async def load_state(conn: Any, eid: str) -> tuple[dict[str, Any], bool]:
        return {"dataset": "demo", "data_version": 1}, True

    monkeypatch.setattr(stores, "transaction", no_transaction)
    monkeypatch.setattr(stores, "writable", writable)
    monkeypatch.setattr(loader, "load_state", load_state)
    await loader.load("E", "demo")  # tmp_path/demo 不存在


async def test_version_row_without_tables_still_loads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """R268 验收 5: 有 dataset_loads 行 (导入, 确认就绪或补登记建的版本行) 而还没有财务表行时照常载入, 不静默跳过:
    这里源 CSV 不存在, 读源数据即报错, 说明没有跳过 (写库部分见连库测试)."""
    loader = stores.CsvDatasetLoader(tmp_path, engine=object(), evidence_store=object())  # type: ignore[arg-type]

    async def writable(conn: Any, eid: str) -> None:
        return None

    async def load_state(conn: Any, eid: str) -> tuple[dict[str, Any], bool]:
        return {"dataset": "demo", "data_version": 0, "ready_at": None}, False

    monkeypatch.setattr(stores, "transaction", no_transaction)
    monkeypatch.setattr(stores, "writable", writable)
    monkeypatch.setattr(loader, "load_state", load_state)
    with pytest.raises(FileNotFoundError, match=r"accounts\.csv"):
        await loader.load("E", "demo")


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

    async def load_state(conn: Any, eid: str) -> tuple[dict[str, Any], bool]:
        return {"dataset": "xero/removed", "data_version": 1}, True

    monkeypatch.setattr(stores, "transaction", no_transaction)
    monkeypatch.setattr(stores, "writable", writable)
    monkeypatch.setattr(loader, "load_state", load_state)
    await loader.load("E", "xero/removed")


@pytest.mark.external
def test_xero_without_supplements_tables_unchanged(tmp_path: Path) -> None:
    """R72: 没有补造目录的 Xero 快照 (20261017-mock) 载入的表与纯快照相同, 不登记合同; 服务完成记录为空,
    合同关联全空, 开户行为 Xero 科目名."""
    from backend.adapters.xero_dataset import Snapshot, build_tables

    api = snapshot_dir("20261017-mock").parent
    loader = stores.XeroDatasetLoader(
        api, api, engine=object(), evidence_store=object(), supplements=supplements_path("20261017-q1").parent
    )  # type: ignore[arg-type]
    tables = loader.tables("xero/20261017-mock")
    assert tables == build_tables(Snapshot(api / "20261017-mock"))
    assert loader.supplement("xero/20261017-mock") is None and loader.supplement_tables("xero/20261017-mock") == []
    assert tables["delivery_notes"] == [] and all(i.contract_id is None for i in tables["sales_invoices"])
    assert [a.bank_name for a in tables["bank_accounts"]] == ["Business Bank Account", "Business Savings Account"]


REPO = Path(__file__).resolve().parents[1]
XERO_API = snapshot_location("20261017-q1").parent  # 快照的上级 (只给路径; 用例经 snapshot_dir 核对)


def routing() -> stores.RoutingDatasetLoader:
    supplements = supplements_path("20261017-q1").parent
    xero = stores.XeroDatasetLoader(
        XERO_API, XERO_API, engine=object(), evidence_store=object(), supplements=supplements
    )  # type: ignore[arg-type]
    csv = stores.CsvDatasetLoader(REPO / "data", engine=object(), evidence_store=object())  # type: ignore[arg-type]
    return stores.RoutingDatasetLoader(csv, {"xero/": xero})


@pytest.mark.external
def test_adjustment_entries_and_numbers() -> None:
    """R73: 更正分录的凭证摘要与凭证号; 没有更正分录文件的数据集为空, 不抛错."""
    from datetime import date

    loader = routing()
    assert loader.adjustment_entries("demo") == [] and loader.adjustment_entry_nos("demo") == []
    if optional(snapshot_dir, "20261017-mock") is not None:
        assert loader.adjustment_entry_nos("xero/20261017-mock") == []
    snapshot_dir("20261017-q1")
    supplements_dir("20261017-q1")
    summaries = loader.adjustment_entries("xero/20261017-q1")
    assert [s.entry_no for s in summaries] == ["AJE-X01", "AJE-X02", "AJE-X03", "AJE-X04", "AJE-X05"]
    assert loader.adjustment_entry_nos("xero/20261017-q1") == [s.entry_no for s in summaries]
    assert [s.line_count for s in summaries] == [3, 2, 2, 2, 2] and sum(s.line_count for s in summaries) == 11
    assert all(s.posting_date == date(2026, 9, 30) for s in summaries)
    assert summaries[0].description is not None and summaries[0].description.startswith("Audit adjustment AJE-X01")


@pytest.mark.external
async def test_load_adjustments_rejects_before_writing(tmp_path: Path) -> None:
    """R73: 凭证号不在文件中 (LookupError, 含编号), 数据集没有更正分录文件 (FileNotFoundError, 含路径), 某笔借贷不平
    (ValueError) 都在开事务之前拒绝 (本用例的载入器没有数据库, 走到写入就会出错)."""
    loader = routing()
    with pytest.raises(FileNotFoundError, match="adjustments"):
        await loader.load_adjustments("E", "demo", ["AJE-X01"])
    if optional(snapshot_dir, "20261017-mock") is not None:
        with pytest.raises(FileNotFoundError, match="20261017-mock"):
            await loader.load_adjustments("E", "xero/20261017-mock")
    source = supplements_dir("20261017-q1") / "adjustments" / "journal_entries.csv"
    if optional(snapshot_dir, "20261017-q1") is not None:
        with pytest.raises(LookupError, match="AJE-X09"):
            await loader.load_adjustments("E", "xero/20261017-q1", ["AJE-X01", "AJE-X09"])
    target = tmp_path / "acme" / "adjustments" / "journal_entries.csv"
    target.parent.mkdir(parents=True)
    target.write_text(source.read_text(encoding="utf-8").replace(",478,426120,0,", ",478,426121,0,"), encoding="utf-8")
    acme = stores.CsvDatasetLoader(tmp_path, engine=object(), evidence_store=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="AJE-X02"):
        await acme.load_adjustments("E", "acme", ["AJE-X02"])
    assert [s.entry_no for s in acme.adjustment_entries("acme")][:2] == ["AJE-X01", "AJE-X02"]


@pytest.mark.external
async def test_fake_loader_adjustments() -> None:
    from tests.fakes import FakeDatasetLoader, FakeFinancialStore

    source = supplements_dir("20261017-q1") / "adjustments" / "journal_entries.csv"
    fake = FakeDatasetLoader(FakeFinancialStore(), adjustments=stores.read_adjustments(source))
    rows = await fake.load_adjustments("E", "xero/20261017-q1", ["AJE-X05", "AJE-X01"])
    assert [r.id for r in rows] == ["AJE-X01-1", "AJE-X01-2", "AJE-X01-3", "AJE-X05-1", "AJE-X05-2"]
    assert fake.adjustment_loads == [("E", "xero/20261017-q1", ["AJE-X05", "AJE-X01"])]
    assert fake.adjustment_actors == ["A1"]  # 收到的写入人另记一列 (R162 断言提交更正通知的审计师)
    assert fake.adjustment_entry_nos("xero/20261017-q1") == ["AJE-X01", "AJE-X02", "AJE-X03", "AJE-X04", "AJE-X05"]
    with pytest.raises(LookupError):
        await fake.load_adjustments("E", "xero/20261017-q1", ["AJE-X09"])


@pytest.mark.parametrize("actor", [None, "U-7"])
async def test_adjustment_writer_is_passed_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, actor: str | None
) -> None:
    """需求 R271: 更正分录载入的写入人 (actor, 缺省 A1) 经路由与 CSV, Xero 两种载入器原样交给公共写入 (写版本行的
    created_by), 防路由转发时丢掉而一律记成 A1."""
    seen: list[tuple[str, str]] = []

    async def insert(self: Any, eid: str, dataset: str, path: Path, entry_nos: Any, writer: str) -> list[Any]:
        seen.append((dataset, writer))
        return []

    monkeypatch.setattr(stores.TableLoader, "insert_adjustments", insert)
    csv = stores.CsvDatasetLoader(tmp_path, engine=object(), evidence_store=object())  # type: ignore[arg-type]
    xero = xero_loader(tmp_path)
    monkeypatch.setattr(xero, "adjustments_path", lambda dataset: tmp_path / "adjustments.csv")
    routing = stores.RoutingDatasetLoader(csv, {"xero/": xero})
    kwargs = {} if actor is None else {"actor": actor}
    for dataset in ("demo", "xero/x"):
        await routing.load_adjustments("E", dataset, ["AJE-X01"], **kwargs)
    assert seen == [("demo", actor or "A1"), ("xero/x", actor or "A1")]


@pytest.mark.external
async def test_adjustment_inputs_are_checked(tmp_path: Path) -> None:
    """created_at 没有时区时拒绝 (写库读回带时区, 重放会被误判为内容不同); 单个字符串当凭证号列表时拒绝."""
    source = supplements_dir("20261017-q1") / "adjustments" / "journal_entries.csv"
    target = tmp_path / "acme" / "adjustments" / "journal_entries.csv"
    target.parent.mkdir(parents=True)
    text = source.read_text(encoding="utf-8")
    target.write_text(text.replace("2026-10-20T03:00:00Z", "2026-10-20T03:00:00", 1), encoding="utf-8")
    acme = stores.CsvDatasetLoader(tmp_path, engine=object(), evidence_store=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="created_at 没有时区: AJE-X01-1"):
        await acme.load_adjustments("E", "acme", ["AJE-X01"])
    target.write_text(text, encoding="utf-8")
    with pytest.raises(TypeError, match="凭证号列表"):
        await acme.load_adjustments("E", "acme", "AJE-X01")  # type: ignore[arg-type]


async def test_fake_loader_matches_real_semantics() -> None:
    """假载入器: 凭证号自然序 (同真实实现); 没有预置更正分录时载入抛 FileNotFoundError, 列表为空."""
    from datetime import UTC, date, datetime

    from backend.contracts.records import JournalEntry
    from tests.fakes import FakeDatasetLoader, FakeFinancialStore

    def line(no: str) -> JournalEntry:
        return JournalEntry(
            id=f"{no}-1",
            entry_no=no,
            posting_date=date(2026, 9, 30),
            created_at=datetime(2026, 10, 20, tzinfo=UTC),
            created_by="xero",
            approved_by=None,
            account_code="620",
            debit_cents=1,
            description=None,
            source_doc=None,
        )

    fake = FakeDatasetLoader(FakeFinancialStore(), adjustments=[line("AJE-X10"), line("AJE-X2"), line("AJE-X1")])
    assert fake.adjustment_entry_nos("x") == ["AJE-X1", "AJE-X2", "AJE-X10"]
    assert stores.natural_key("AJE-X2") < stores.natural_key("AJE-X10")
    empty = FakeDatasetLoader(FakeFinancialStore())
    assert empty.adjustment_entries("demo") == []
    with pytest.raises(FileNotFoundError):
        await empty.load_adjustments("E", "demo")


def test_dataset_traits_default_for_csv_and_routing() -> None:
    """CSV (Numbat) 没有声明, 取缺省值; 路由按前缀转发."""
    from backend.contracts.records import DatasetTraits
    from tests.fakes import FakeDatasetLoader, FakeFinancialStore

    csv = routing().default
    assert csv.dataset_traits("demo") == DatasetTraits()
    declared = DatasetTraits(dataset="xero/x", prior_period_start=None, audit_adjustment_entry_prefix="ADJ-")
    xero = FakeDatasetLoader(FakeFinancialStore(), traits={"xero/x": declared})
    picked = stores.RoutingDatasetLoader(csv, {"xero/": xero})
    assert picked.dataset_traits("xero/x") is declared and picked.dataset_traits("demo") == DatasetTraits()
    assert xero.traits_calls == ["xero/x"]


@pytest.mark.external
def test_dataset_traits_read_from_supplements() -> None:
    """R96 验收 2: 没有补造目录的快照 (20261017-mock) 取缺省值; xero/20261017-q1 读已核对的补造目录的
    dataset_traits.json (各值同设计第 5.2 节, 见 test_knowledge.test_dataset_traits_from_design)."""
    from backend.contracts.records import DatasetTraits

    snapshot_dir("20261017-mock")
    snapshot_dir("20261017-q1")
    declared = supplements_dir("20261017-q1") / "dataset_traits.json"
    loaders = routing()
    assert loaders.dataset_traits("xero/20261017-mock") == DatasetTraits()
    q1 = loaders.dataset_traits("xero/20261017-q1")
    assert q1 == DatasetTraits.model_validate_json(declared.read_bytes()) != DatasetTraits()
