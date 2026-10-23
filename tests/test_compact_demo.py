"""精简演示使用独立输入, 账务与证据相互一致, 清单校验不能绕过."""

import hashlib
import importlib.util
from pathlib import Path

import pytest

from backend.adapters.compact_demo import manifest, read_plan
from backend.contracts.data import TableKind
from backend.contracts.records import JournalEntry, SalesInvoice, TrialBalanceRow
from backend.persistence.stores import CsvDatasetLoader

DATA = Path(__file__).resolve().parents[1] / "data"


def test_compact_ledger_is_balanced_and_candidate_population_is_small() -> None:
    plan = read_plan(DATA / "demo-compact", "demo-compact")
    rows = {table.kind: table.rows for table in plan.tables}
    journals = [row for row in rows[TableKind.journal_entries] if isinstance(row, JournalEntry)]
    balances = [row for row in rows[TableKind.trial_balance] if isinstance(row, TrialBalanceRow)]
    invoices = [row for row in rows[TableKind.sales_invoices] if isinstance(row, SalesInvoice)]
    assert len(journals) == 10 and len(invoices) == 2
    for number in {j.entry_no for j in journals}:
        assert sum(j.debit_cents - j.credit_cents for j in journals if j.entry_no == number) == 0
    assert all(
        sum(row.balance_cents for row in balances if row.period == period) == 0 for period in ("prior", "current")
    )
    assert {i.contract_id for i in invoices} == {"CON-C01", "CON-C02"}
    assert len([d for d in plan.uploads if d.document_type == "contract"]) == 2
    approvals = rows[TableKind.approval_log]
    assert len(approvals) == 5  # 补齐审批信息, 不因缺字段增加资料请求.
    loader = CsvDatasetLoader(DATA)
    assert loader.has_supplement("demo-compact")
    assert loader.period("demo-compact")[1].isoformat() == "2026-06-30"
    assert loader.demo_plan("demo") is None


def test_generator_is_repeatable_and_preserves_original_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = DATA / "generate_compact_demo.py"
    spec = importlib.util.spec_from_file_location("compact_generator", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (DATA / "demo").rglob("*") if p.is_file()}
    monkeypatch.setattr(module, "OUT", tmp_path / "compact")
    module.generate()
    first = manifest(module.OUT)
    module.generate()
    assert manifest(module.OUT) == first
    assert all(hashlib.sha256(p.read_bytes()).hexdigest() == digest for p, digest in original.items())
    target = module.OUT / "accounts.csv"
    target.write_text(target.read_text() + "changed")
    with pytest.raises(ValueError, match="哈希不符"):
        read_plan(module.OUT, "demo-compact")


def test_manifest_rejects_linked_metadata_before_reading(tmp_path: Path) -> None:
    target = tmp_path / "outside.json"
    target.write_text("{}")
    root = tmp_path / "dataset"
    root.mkdir()
    (root / "compact.json").symlink_to(target)
    with pytest.raises(ValueError, match="清单路径不合法"):
        manifest(root)
    assert target.read_text() == "{}"


def test_adjustment_sources_match_the_audited_objects() -> None:
    import csv

    with (DATA / "demo-compact/adjustments/journal_entries.csv").open() as stream:
        entries = list(csv.DictReader(stream))
    assert {r["source_doc"] for r in entries if r["entry_no"] == "AJE-C01"} == {"SI-C02"}
    assert {r["source_doc"] for r in entries if r["entry_no"] == "AJE-C02"} == {"BA-C01"}
