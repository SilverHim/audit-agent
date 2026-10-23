"""独立精简合成演示的导入清单. 文件均经 manifest 哈希验证, 不改原数据集."""

import csv
import hashlib
import json
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel

from backend.adapters.xero_supplements import DemoPlan, DemoTable, DemoUpload
from backend.contracts.data import ApprovalLogRow, TableKind
from backend.contracts.records import (
    Account,
    BankAccount,
    BankTransaction,
    DeliveryNote,
    JournalEntry,
    SalesInvoice,
    SupplierBill,
    TrialBalanceRow,
)

ROWS: dict[TableKind, type[BaseModel]] = {
    TableKind.accounts: Account,
    TableKind.trial_balance: TrialBalanceRow,
    TableKind.journal_entries: JournalEntry,
    TableKind.sales_invoices: SalesInvoice,
    TableKind.supplier_bills: SupplierBill,
    TableKind.bank_accounts: BankAccount,
    TableKind.bank_transactions: BankTransaction,
    TableKind.delivery_notes: DeliveryNote,
    TableKind.approval_log: ApprovalLogRow,
}


def manifest(root: Path) -> dict[str, Any]:
    """仅读固定目录下普通文件, 拒绝逃逸路径、链接及清单外内容."""
    source = root / "compact.json"
    if any(p.is_symlink() for p in [source, *source.parents]) or not source.is_file():
        raise ValueError("精简演示清单路径不合法")
    data: dict[str, Any] = json.loads(source.read_text())
    files = data["files"]
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()} - {"compact.json"}
    if actual != files.keys():
        raise ValueError("精简演示文件清单不匹配")
    for name, digest in files.items():
        relative = PurePosixPath(name)
        path = root / name
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or any(p.is_symlink() for p in [path, *path.parents] if p != root.parent)
            or not path.resolve().is_relative_to(root.resolve())
        ):
            raise ValueError("精简演示文件路径不合法")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError(f"精简演示文件哈希不符: {name}")
    return data


def read_plan(root: Path, dataset: str) -> DemoPlan:
    data = manifest(root)
    tables = []
    for kind, model in ROWS.items():
        with (root / f"{kind}.csv").open(newline="") as stream:
            rows = [
                model.model_validate({k: v if v != "" else None for k, v in row.items()})
                for row in csv.DictReader(stream)
            ]
        tables.append(DemoTable(kind, f"{kind}.csv", rows))
    return DemoPlan(
        dataset=dataset,
        tables=tables,
        uploads=[DemoUpload.model_validate(d) for d in data["documents"]],
        root=root,
        snapshot={},
    )


def period(root: Path) -> tuple[date, date]:
    data = manifest(root)
    return date.fromisoformat(data["period_start"]), date.fromisoformat(data["period_end"])
