"""需求 R178: 应付账单的期末未付明细 supplier_bills, 由 Xero 快照的 ACCPAY 账单连同付款, 贷项, 预付与多付的分配算出.

两份快照各建一次表 (模块级夹具): 20261017-q1 (期末 09-30, 演示用) 与 20261017-mock (期末 11-30, 有期内部分付款与
作废的付款). 每份都核对未付合计等于试算表应付 (800, 系统科目 CREDITORS) 的贷方余额. 边界情形在内存快照上改一处
(Snapshot 的 files, 不写盘), 不动原始快照.
"""

import builtins
import copy
import io
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from backend.adapters.xero_dataset import (
    REVERSAL_REFERENCE,
    Snapshot,
    build_tables,
    journal_entries_from_api,
    map_supplier_bills,
    trial_balance_cents,
)
from backend.contracts.knowledge import ProcedureTemplate
from backend.contracts.models import EvidenceType, ProcedureRun, Reliability
from backend.contracts.records import SupplierBill
from backend.services.rule_engine import SqlRuleEngine
from tests.external import snapshot_dir
from tests.xero_fakes import xero_traits

pytestmark = pytest.mark.external

# 连库用例按载入的顺序读回应付账单, 只取 SupplierBill 的字段 (不含 engagement_id)
SELECT_BILLS = f"SELECT {', '.join(SupplierBill.model_fields)} FROM supplier_bills ORDER BY bill_date, id"

# 20261017-q1 期末 (2026-09-30) 仍未付的 6 张账单 (需求 R178 验收 2): 连库用例与演示数据导入的连库用例也用它
Q1_BILLS = [
    SupplierBill(
        id="PB-69fc971e-9b37-41c5-9c87-174330f22343",
        bill_no="945-ORC",
        supplier="Central Copiers",
        bill_date=date(2026, 8, 17),
        due_date=date(2026, 9, 15),
        amount_cents=106356,
        outstanding_cents=106356,  # 10-14 付了 900.00, 期末仍是全额
        journal_entry_id="XJ-502-3",
    ),
    SupplierBill(
        id="PB-76bcb361-f93b-4513-b312-5a4af306d276",
        bill_no="SM0195-70135",
        supplier="SMART Agency",
        bill_date=date(2026, 9, 5),
        due_date=date(2026, 9, 19),
        amount_cents=200000,
        outstanding_cents=200000,
        journal_entry_id="XJ-548-3",  # XJ-547 与冲回它的 XJ-549 是作废对
    ),
    SupplierBill(
        id="PB-4ad1ec01-f4a3-41d7-bbb4-d2ab2fec8e65",
        bill_no="4AD1EC01",  # 账单没有编号, 取短 ID
        supplier="MCO Cleaning Services",
        bill_date=date(2026, 9, 16),
        due_date=date(2026, 10, 1),
        amount_cents=17050,
        outstanding_cents=17050,
        journal_entry_id="XJ-571-1",
    ),
    SupplierBill(
        id="PB-c011f4e5-d533-4744-97fe-c3282f928825",
        bill_no="M000442",
        supplier="MCO Cleaning Services",
        bill_date=date(2026, 9, 20),
        due_date=date(2026, 9, 27),
        amount_cents=22000,
        outstanding_cents=22000,  # 10-06 付清, 期末仍未付
        journal_entry_id="XJ-591-1",
    ),
    SupplierBill(
        id="PB-84f25a8b-63a1-4029-934e-071eb21cb633",
        bill_no="M000456",
        supplier="MCO Cleaning Services",
        bill_date=date(2026, 9, 27),
        due_date=date(2026, 10, 4),
        amount_cents=22000,
        outstanding_cents=22000,
        journal_entry_id="XJ-607-1",
    ),
    SupplierBill(
        id="PB-dba2f021-f149-4191-a126-5351d587ab0e",
        bill_no="3395",
        supplier="PowerDirect",
        bill_date=date(2026, 9, 30),
        due_date=date(2026, 10, 16),
        amount_cents=33110,
        outstanding_cents=33110,
        journal_entry_id="XJ-611-1",
    ),
]
SMART = "PB-76bcb361-f93b-4513-b312-5a4af306d276"
SMART_ID = SMART.removeprefix("PB-")
CREDITORS = "800"  # Demo Company 的应付科目 (系统科目 CREDITORS)
# 快照里有的资源中, 建应付账单读的那几个 (只读它们; 联系人 contacts 与网页导出 raw/ 都不读)
BILL_RESOURCES = ("accounts", "invoices", "payments", "credit_notes", "prepayments", "overpayments")


SNAPSHOTS = ("20261017-q1", "20261017-mock")


@pytest.fixture(scope="module")
def built() -> dict[str, tuple[list[SupplierBill], int]]:
    """两份快照各建一次全部表: 快照名 → (应付账单, 本期试算表应付科目的余额 (借正))."""
    found = {}
    for name in SNAPSHOTS:
        snapshot = Snapshot(snapshot_dir(name))
        meta = snapshot.load("dataset")
        balances = trial_balance_cents(snapshot.trial_balance(date.fromisoformat(meta["period_end"])))
        found[name] = (build_tables(snapshot)["supplier_bills"], balances[CREDITORS])
    return found


@pytest.mark.parametrize("name", SNAPSHOTS)
def test_outstanding_bills_tie_to_the_payables_balance(
    built: dict[str, tuple[list[SupplierBill], int]], name: str
) -> None:
    """验收 2: 未付合计等于期末试算表应付的贷方余额 (q1: 6 张 400516; mock: 11 张 850778); q1 逐字段等于 Q1_BILLS,
    到期日早于期末的 3 张合计 328356, 其余 72160."""
    bills, payables = built[name]
    assert sum(b.outstanding_cents for b in bills) == -payables
    if name == "20261017-q1":
        assert bills == Q1_BILLS and -payables == 400516
        overdue = [b.outstanding_cents for b in bills if b.due_date < date(2026, 9, 30)]
        assert (sum(overdue), -payables - sum(overdue)) == (328356, 72160)
    else:
        assert (len(bills), -payables) == (11, 850778)


@pytest.mark.parametrize(
    ("snapshot", "bill", "expected"),
    [
        # 期后付款不扣 (验收 3): 945-ORC 10-14 付了 900.00, M000442 10-06 付清
        ("20261017-q1", "PB-69fc971e-9b37-41c5-9c87-174330f22343", 106356),
        ("20261017-q1", "PB-c011f4e5-d533-4744-97fe-c3282f928825", 22000),
        # 9 月前由付款 250.00 与贷项 200.00 结清的 Party Hire, 期后 10-11 的 PC Complete 账单: 都不出现
        ("20261017-q1", "PB-7dae876a-b424-436b-a4e6-17b3fdeec80c", None),
        ("20261017-q1", "PB-c12aff7e-12bf-4185-8702-460929f19674", None),
        # 期内的部分付款: 到 11-30 时 945-ORC 只剩 163.56
        ("20261017-mock", "PB-69fc971e-9b37-41c5-9c87-174330f22343", 16356),
        # 作废的付款不扣: O721-003 只有一笔 DELETED 的付款
        ("20261017-mock", "PB-86102312-aa3f-438c-9938-6840f4d8dda6", 5413),
    ],
    ids=["paid_after_period", "settled_after_period", "settled_by_credit", "after_period", "partial", "deleted"],
)
def test_outstanding_follows_the_settlements(
    built: dict[str, tuple[list[SupplierBill], int]], snapshot: str, bill: str, expected: int | None
) -> None:
    found = {b.id: b.outstanding_cents for b in built[snapshot][0]}
    assert found.get(bill) == expected


def test_journal_line_skips_reversed_postings(built: dict[str, tuple[list[SupplierBill], int]]) -> None:
    """journal_entry_id 取最后过账且未被冲回的分录里贷记应付的那一行 (销售发票同一取法). mock 快照里
    SM0210-70209 最后一笔 XJ-643 被 XJ-639 冲回 (冲回号码更小, 来源类型也换了), 取 XJ-641; GB1-White 的 XJ-719 被
    XJ-707 冲回, 取 XJ-709. 冲回的格式与 Xero 数据集声明的 reversal_reference_pattern 相同."""
    lines = {b.bill_no: b.journal_entry_id for b in built["20261017-mock"][0]}
    assert (lines["SM0210-70209"], lines["GB1-White"]) == ("XJ-641-3", "XJ-709-3")
    assert xero_traits().reversal_reference_pattern == REVERSAL_REFERENCE


def test_bills_are_built_without_contacts_or_raw_exports(monkeypatch: pytest.MonkeyPatch) -> None:
    """验收 5: 建应付账单期间只打开 BILL_RESOURCES 的文件, 不打开 contacts.json, 也不碰 raw/ (打开文件的钩子记下)."""
    snapshot = Snapshot(snapshot_dir("20261017-q1"))
    entries = journal_entries_from_api(snapshot)
    opened: list[str] = []
    real_open, real_io_open = builtins.open, io.open

    def spy(original: Callable[..., Any]) -> Callable[..., Any]:
        def hooked(file: Any, *args: Any, **kwargs: Any) -> Any:
            opened.append(str(file))
            return original(file, *args, **kwargs)

        return hooked

    monkeypatch.setattr(builtins, "open", spy(real_open))
    monkeypatch.setattr(io, "open", spy(real_io_open))
    bills = map_supplier_bills(snapshot, entries, date(2026, 9, 30))
    monkeypatch.undo()
    assert len(bills) == 6
    assert sorted({Path(p).stem for p in opened}) == sorted(
        r for r in BILL_RESOURCES if (snapshot.root / f"{r}.json").exists()
    )
    assert not [p for p in opened if Path(p).name == "contacts.json" or "raw" in Path(p).parts]


# ── 边界: 在内存快照上改一处 ──


def payment(day: str, status: str = "AUTHORISED", amount: float = 500.0) -> dict[str, Any]:
    return {
        "PaymentID": f"P-{day}-{status}",
        "DateString": f"{day}T00:00:00",
        "Amount": amount,
        "Status": status,
        "PaymentType": "ACCPAYPAYMENT",
        "Invoice": {"InvoiceID": SMART_ID},
    }


def allocation(day: str, amount: float = 300.0) -> dict[str, Any]:
    return {
        "AllocationID": f"A-{day}",
        "DateString": f"{day}T00:00:00",
        "Amount": amount,
        "Invoice": {"InvoiceID": SMART_ID},
    }


def allocated(resource: str, status: str, day: str) -> Callable[[dict[str, Any]], None]:
    def change(files: dict[str, Any]) -> None:
        files[resource] = [*files[resource], {"Status": status, "Allocations": [allocation(day)]}]

    return change


def paid(day: str, status: str = "AUTHORISED") -> Callable[[dict[str, Any]], None]:
    def change(files: dict[str, Any]) -> None:
        files["payments"] = [*files["payments"], payment(day, status)]

    return change


def in_usd(files: dict[str, Any]) -> None:
    for bill in files["invoices"]:
        if bill["InvoiceID"] == SMART_ID:
            bill["CurrencyCode"] = "USD"


@pytest.fixture(scope="module")
def q1_files() -> dict[str, Any]:
    """q1 快照里建应付账单读的资源 (内存中的副本)."""
    snapshot = Snapshot(snapshot_dir("20261017-q1"))
    return {name: snapshot.load(name) for name in BILL_RESOURCES}


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (paid("2026-09-30"), 150000),  # 期末当天的付款要扣 (含当天)
        (paid("2026-09-01"), 150000),  # 早于账单日 (09-05) 的付款也扣: 付款日不设下限
        (paid("2026-09-30", "DELETED"), 200000),  # 作废的付款不扣
        (allocated("credit_notes", "AUTHORISED", "2026-09-20"), 170000),  # 贷项分配
        (allocated("credit_notes", "AUTHORISED", "2026-10-01"), 200000),  # 期后的分配不扣
        (allocated("credit_notes", "VOIDED", "2026-09-20"), 200000),  # 作废的贷项通知单不扣
        (allocated("prepayments", "AUTHORISED", "2026-09-20"), 170000),  # 预付款的分配
        (allocated("overpayments", "PAID", "2026-09-20"), 170000),  # 多付款的分配
        (allocated("prepayments", "VOIDED", "2026-09-20"), 200000),
        (in_usd, 200000),  # 外币账单按原币记, 不换算 (同销售发票)
    ],
    ids=[
        "paid_on_period_end",
        "paid_before_bill_date",
        "deleted_payment",
        "credit_note",
        "credit_after_period",
        "voided_credit_note",
        "prepayment",
        "overpayment",
        "voided_prepayment",
        "foreign_currency",
    ],
)
def test_settlement_edges(q1_files: dict[str, Any], change: Callable[[dict[str, Any]], None], expected: int) -> None:
    """SM0195-70135 (2000.00, 账单日 09-05) 上加一笔: 期末日 (含) 及以前未作废的付款与已生效单据的分配才扣;
    amount_cents 仍是含税总额, 不随付款变."""
    files = copy.deepcopy(q1_files)
    change(files)
    bills = map_supplier_bills(Snapshot(Path("edge"), files=files), [], date(2026, 9, 30))
    bill = {b.id: b for b in bills}[SMART]
    assert (bill.amount_cents, bill.outstanding_cents) == (200000, expected)


def test_settled_bill_is_left_out(q1_files: dict[str, Any]) -> None:
    """付款与分配加起来正好结清的账单不出现 (未付为 0)."""
    files = copy.deepcopy(q1_files)
    files["payments"] = [*files["payments"], payment("2026-09-10", amount=1700.0)]
    allocated("credit_notes", "PAID", "2026-09-12")(files)
    bills = map_supplier_bills(Snapshot(Path("edge"), files=files), [], date(2026, 9, 30))
    assert SMART not in {b.id for b in bills} and len(bills) == 5


async def test_rule_rows_on_supplier_bills_point_to_invoices() -> None:
    """规则 SQL 出 supplier_bill 行时证据指针为 invoice, internal_original (同销售发票)."""
    template = ProcedureTemplate.model_validate(
        dict(
            id="GEN-04",
            name="Overdue payables",
            phase="P2_Executing",
            cycle="Pervasive",
            owner="A6",
            engine="rule",
            autonomy_level="L3",
            rules=[dict(id="R01", name="Overdue", severity="medium", sql="SELECT 1 ORDER BY 1")],
        )
    )
    run = ProcedureRun.model_validate(
        dict(
            id="RUN-GEN-04-1",
            procedure_id="GEN-04",
            run_no=1,
            trigger="initial",
            params_snapshot={},
            started_at="2026-10-19T00:00:00Z",
            status="running",
        )
    )
    financial = AsyncMock()
    financial.query.return_value = [{"source_type": "supplier_bill", "source_id": SMART, "amount_cents": 200000}]
    result = await SqlRuleEngine(financial).run("ENG", template, run, {})
    [pointer] = result.exceptions[0].evidence
    assert (pointer.type, pointer.reliability, pointer.source_id) == (
        EvidenceType.invoice,
        Reliability.internal_original,
        SMART,
    )
