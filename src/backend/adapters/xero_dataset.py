"""Xero 快照映射成系统财务表: 科目, 试算表, 总账分录, 销售发票, 应付账单, 银行账户, 银行流水; 另给客户画像.

科目: 每个科目映射到标准分类 (records.AccountCategory), 依次按 Xero 科目类型, 报告代码 (ReportingCode),
科目名称关键词 (services.account_mapping), 都认不出的为 None.

总账分录: 快照里有 journals.json (付费的 Journals 接口, 或其模拟) 时按它; 没有时从源单据按复式规则重建,
再按本期试算表补一笔调整分录 (plug_entry) 补齐重建不出的来源. 重建时每张单据一笔分录:
- 销售发票 (ACCREC): 借应收 (系统科目 DEBTORS) 价税合计, 贷各行科目净额, 贷 GST 税额; 采购发票 (ACCPAY) 方向相反.
- 贷项通知单 (ACCRECCREDIT, ACCPAYCREDIT): 与对应发票方向相反.
- 收付款: 收款借银行贷应收, 付款借应付贷银行.
- 银行收支 (RECEIVE, SPEND): 银行对各行科目与 GST; 转账类 (*-TRANSFER) 跳过, 由银行转账 (BankTransfers) 记.
- 手工分录: 已过账 (POSTED) 的按行记, LineAmount 正为借负为贷.
只取已生效的单据 (发票与贷项通知单 AUTHORISED, PAID; 其余 AUTHORISED); 作废与删除的不记 (Xero 中其分录已冲回).
各行净额按 LineAmountTypes: Inclusive 时 LineAmount 含税, 净额 = LineAmount - TaxAmount;
Exclusive 与 NoTax 时即 LineAmount.
单据合计与各行之和的分位差记入系统科目 ROUNDING.

API 不提供录入人与审批人: created_by 记 "xero", approved_by 为空; created_at 取 UpdatedDateUTC (接口没有创建时间).
费用报销, 期初转换余额, 分位调整不在免费接口之内, 由这些来源产生的分录缺失, 由补齐分录按科目补上差额.
银行流水: 取快照的 bank_statements.json (付费的 Bank Statements Plus, 或其模拟); 没有则为空. 发运单: Xero 没有, 为空.
应付账单 (需求 R178): 截至期末仍有未付余额的 ACCPAY 账单, 由账单, 付款与各类分配算出, 见 map_supplier_bills.
"""

import json
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from backend.contracts.models import ClientProfile
from backend.contracts.records import (
    Account,
    AccountCategory,
    BankAccount,
    BankTransaction,
    CategoryBasis,
    JournalEntry,
    SalesInvoice,
    SupplierBill,
    TrialBalanceRow,
)
from backend.services.account_mapping import classify_by_name
from backend.services.ledger import opening_balances

CREATED_BY = "xero"  # API 不提供录入人
# Xero 科目类型 → 系统科目类别
ACCOUNT_CLASSES = {
    "asset": {"BANK", "CURRENT", "CURRASSET", "FIXED", "INVENTORY", "NONCURRENT", "PREPAYMENT"},
    "liability": {
        "CURRLIAB",
        "LIABILITY",
        "TERMLIAB",
        "PAYGLIABILITY",
        "SUPERANNUATIONLIABILITY",
        "WAGESPAYABLELIABILITY",
    },
    "equity": {"EQUITY"},
    "revenue": {"REVENUE", "SALES", "OTHERINCOME"},
    "expense": {"EXPENSE", "DIRECTCOSTS", "OVERHEADS", "DEPRECIATN", "SUPERANNUATIONEXPENSE", "WAGESEXPENSE"},
}
LIVE_DOCUMENTS = {"AUTHORISED", "PAID"}  # 已生效的发票与贷项通知单
# Xero 科目类型直接决定的标准分类
TYPE_CATEGORIES: dict[str, AccountCategory] = {
    "BANK": "cash",
    "INVENTORY": "inventory",
    "PREPAYMENT": "prepayments",
    "DIRECTCOSTS": "cost_of_sales",
    "DEPRECIATN": "depreciation_amortisation",
    "OTHERINCOME": "other_income",
    "WAGESEXPENSE": "employee_expenses",
    "SUPERANNUATIONEXPENSE": "employee_expenses",
    "PAYGLIABILITY": "tax_payable",
    "SUPERANNUATIONLIABILITY": "employee_provisions",
    "WAGESPAYABLELIABILITY": "employee_provisions",
}
# Xero 报告代码前缀 → 标准分类, 取最长的匹配前缀
REPORTING_CODES: dict[str, AccountCategory] = {
    "ASS.CUR.CAS": "cash",
    "ASS.CUR.REC.PRE": "prepayments",
    "ASS.CUR.REC": "receivables",
    "ASS.CUR.INY": "inventory",
    "ASS.CUR": "other_current_assets",
    "ASS.NCA.FIX": "property_plant_equipment",
    "ASS.NCA.INT": "intangible_assets",
    "ASS.NCA": "other_non_current_assets",
    "LIA.CUR.TAX": "tax_payable",
    "LIA.CUR.PAY.EMP": "employee_provisions",
    "LIA.CUR.PAY.PAY": "tax_payable",
    "LIA.CUR.PAY": "payables",
    "LIA.CUR.EMP": "employee_provisions",
    "LIA.CUR.LOA": "borrowings",
    "LIA.CUR": "other_current_liabilities",
    "LIA.NCL.LOA": "borrowings",
    "LIA.NCL": "other_non_current_liabilities",
    "EQU.SHA": "share_capital",
    "EQU.RET": "retained_earnings",
    "EQU": "other_equity",
    "REV.TRA": "revenue",
    "REV": "other_income",
    "EXP.COS": "cost_of_sales",
    "EXP.DEP": "depreciation_amortisation",
    "EXP.AMO": "depreciation_amortisation",
    "EXP.EMP": "employee_expenses",
    "EXP.INT": "finance_costs",
    "EXP.INC": "income_tax",
}
MS_DATE = re.compile(r"/Date\((-?\d+)([+-]\d{4})?\)/")  # Xero 的 /Date(毫秒+时区)/
# Xero 冲回分录的说明 (Journals 的 Reference) 里点名被冲回的原分录号, 格式固定 (如 "Reversed: SMART Agency -
# Reversal of ID 547"); 冲回分录可能换了来源类型, 不带原单据的 SourceID, 号码也可能比原分录小. Xero 数据集声明的
# reversal_reference_pattern (规则 SQL 认作废对用) 与它相同 (测试核对)
REVERSAL_REFERENCE = r"Reversal of ID (\d+)"


@dataclass(frozen=True)
class Snapshot:
    """pull 写出的快照目录: 每个资源一个 JSON 文件. files 不为空时是只在内存中的快照 (网页同步, 需求 R279 Q3:
    原始响应不落盘), 资源按名称取自 files, root 只作名称; files 不进 repr 与比较 (含联系人等原始数据)."""

    root: Path
    files: Mapping[str, Any] | None = field(default=None, repr=False, compare=False)

    def load(self, name: str) -> Any:
        if self.files is not None:
            return self.files.get(name, [])
        path = self.root / f"{name}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []

    def trial_balance(self, day: date) -> dict[str, Any]:
        data: dict[str, Any] = self.load(f"trial_balance_{day.isoformat()}")
        if not data:
            raise FileNotFoundError(f"快照里没有 {day} 的试算表, pull 时用 --date {day} 拉取")
        return data


def cents(value: Any) -> int:
    """Xero 的金额 (元, 小数) 转整数分, 四舍五入."""
    return int((Decimal(str(value or 0)) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def xero_date(item: dict[str, Any], key: str = "Date") -> date:
    """优先取 {key}String (ISO), 没有时解析 /Date(毫秒)/."""
    text = item.get(f"{key}String")
    if text:
        return date.fromisoformat(str(text)[:10])
    match = MS_DATE.fullmatch(str(item.get(key, "")))
    if not match:
        raise ValueError(f"无法解析日期 {key}: {item.get(key)!r}")
    return datetime.fromtimestamp(int(match.group(1)) / 1000, UTC).date()


def xero_datetime(item: dict[str, Any], key: str = "UpdatedDateUTC") -> datetime:
    match = MS_DATE.fullmatch(str(item.get(key, "")))
    if not match:
        raise ValueError(f"无法解析时间 {key}: {item.get(key)!r}")
    return datetime.fromtimestamp(int(match.group(1)) / 1000, UTC)


def account_class(xero_type: str) -> str:
    for name, types in ACCOUNT_CLASSES.items():
        if xero_type in types:
            return name
    raise ValueError(f"未知的 Xero 科目类型 {xero_type}")


def categorize(account: dict[str, Any]) -> tuple[AccountCategory | None, CategoryBasis | None]:
    """Xero 科目 → (标准分类, 依据): 先看科目类型, 再看报告代码的最长前缀, 最后按名称关键词; 都认不出为 (None, None)."""
    if account["Type"] in TYPE_CATEGORIES:
        return TYPE_CATEGORIES[account["Type"]], "xero_type"
    code = str(account.get("ReportingCode") or "")
    prefixes = [p for p in REPORTING_CODES if code == p or code.startswith(p + ".")]
    if prefixes:
        return REPORTING_CODES[max(prefixes, key=len)], "reporting_code"
    category = classify_by_name(account["Name"], account_class(account["Type"]))
    return category, "name" if category else None


def xero_category(account: dict[str, Any]) -> AccountCategory | None:
    """Xero 科目 → 标准分类, 规则见 categorize."""
    return categorize(account)[0]


def map_accounts(accounts: list[dict[str, Any]]) -> list[Account]:
    """有科目代码的科目 (含已归档的; 没有代码的是不能记账的占位科目, 跳过), 带标准分类与依据."""
    result = []
    for a in accounts:
        if not a.get("Code"):
            continue
        category, basis = categorize(a)
        result.append(
            Account(
                code=a["Code"], name=a["Name"], type=account_class(a["Type"]), category=category, category_basis=basis
            )
        )
    return result


def system_codes(accounts: list[dict[str, Any]]) -> dict[str, str]:
    """系统科目 (DEBTORS, CREDITORS, GST, ROUNDING 等) → 科目代码."""
    return {a["SystemAccount"]: a["Code"] for a in accounts if a.get("SystemAccount") and a.get("Code")}


@dataclass
class Line:
    """重建分录的一行, 金额为分, 借正贷负."""

    account_code: str
    amount: int
    description: str | None


def line_postings(lines: list[dict[str, Any]], amount_types: str, sign: int, gst: str) -> list[Line]:
    """单据明细行 → 各行科目的净额与 GST 合计 (sign=+1 记借方, -1 记贷方)."""
    postings: list[Line] = []
    tax_total = 0
    for item in lines:
        if not item.get("AccountCode"):
            continue
        tax = cents(item.get("TaxAmount"))
        net = cents(item.get("LineAmount")) - (tax if amount_types == "Inclusive" else 0)
        postings.append(Line(item["AccountCode"], sign * net, item.get("Description")))
        tax_total += tax
    if tax_total:
        postings.append(Line(gst, sign * tax_total, "GST"))
    return postings


def balance(postings: list[Line], counter: Line, rounding: str) -> list[Line]:
    """加上对方科目一行; 借贷不平的分位差记入 ROUNDING."""
    lines = [*postings, counter]
    diff = sum(p.amount for p in lines)
    if diff:
        lines.append(Line(rounding, -diff, "Rounding"))
    return lines


@dataclass
class Entry:
    """重建的一笔分录 (一张源单据)."""

    entry_no: str
    posting_date: date
    created_at: datetime
    lines: list[Line]
    description: str | None
    source_doc: str | None
    source_type: str  # 来源单据类型, 与 Xero 科目明细账的 Source 对应
    is_adjustment: bool = False
    source_id: str | None = None  # 来源单据的 Xero ID, 关联销售发票用
    sequence: int = 0  # 同日分录的过账顺序 (Journals 的 JournalNumber); 反推的分录为 0


def short(xero_id: str) -> str:
    return xero_id.replace("-", "")[:8].upper()


def document_entries(snapshot: Snapshot) -> list[Entry]:
    """从源单据重建全部分录, 按入账日期与分录号排序. 分录号取单据的 Xero ID (采购发票等的业务编号可重复),
    业务编号只作 source_doc."""
    accounts = snapshot.load("accounts")
    codes = system_codes(accounts)
    debtors, creditors, gst, rounding = codes["DEBTORS"], codes["CREDITORS"], codes["GST"], codes["ROUNDING"]
    entries: list[Entry] = []

    for doc in snapshot.load("invoices"):
        if doc["Status"] not in LIVE_DOCUMENTS:
            continue
        sales = doc["Type"] == "ACCREC"
        number = doc.get("InvoiceNumber") or short(doc["InvoiceID"])
        sign = -1 if sales else 1  # 销售发票明细记贷方, 采购发票记借方
        postings = line_postings(doc.get("LineItems", []), doc["LineAmountTypes"], sign, gst)
        counter = Line(debtors if sales else creditors, -sign * cents(doc["Total"]), doc["Contact"].get("Name"))
        entries.append(
            Entry(
                entry_no=f"XI-{doc['InvoiceID']}",
                posting_date=xero_date(doc),
                created_at=xero_datetime(doc),
                lines=balance(postings, counter, rounding),
                description=doc["Contact"].get("Name"),
                source_doc=number,
                source_type="Receivable Invoice" if sales else "Payable Invoice",
                source_id=doc["InvoiceID"],
            )
        )

    for doc in snapshot.load("credit_notes"):
        if doc["Status"] not in LIVE_DOCUMENTS:
            continue
        sales = doc["Type"] == "ACCRECCREDIT"
        number = doc.get("CreditNoteNumber") or short(doc["CreditNoteID"])
        sign = 1 if sales else -1  # 与发票相反
        postings = line_postings(doc.get("LineItems", []), doc["LineAmountTypes"], sign, gst)
        counter = Line(debtors if sales else creditors, -sign * cents(doc["Total"]), doc["Contact"].get("Name"))
        entries.append(
            Entry(
                entry_no=f"XC-{doc['CreditNoteID']}",
                posting_date=xero_date(doc),
                created_at=xero_datetime(doc),
                lines=balance(postings, counter, rounding),
                description=doc["Contact"].get("Name"),
                source_doc=number,
                source_type="Receivable Credit Note" if sales else "Payable Credit Note",
            )
        )

    for pay in snapshot.load("payments"):
        if pay["Status"] != "AUTHORISED":
            continue
        received = pay["PaymentType"] == "ACCRECPAYMENT"
        amount = cents(pay["Amount"])
        bank = pay["Account"]["Code"]
        invoice = pay.get("Invoice") or {}
        number = invoice.get("InvoiceNumber") or short(invoice.get("InvoiceID", pay["PaymentID"]))
        party = (invoice.get("Contact") or {}).get("Name")
        lines = (
            [Line(bank, amount, party), Line(debtors, -amount, party)]
            if received
            else [Line(creditors, amount, party), Line(bank, -amount, party)]
        )
        entries.append(
            Entry(
                entry_no=f"XP-{pay['PaymentID']}",
                posting_date=xero_date(pay),
                created_at=xero_datetime(pay),
                lines=lines,
                description=f"Payment: {party}" if party else "Payment",
                source_doc=pay.get("Reference") or number,
                source_type="Receivable Payment" if received else "Payable Payment",
            )
        )

    for txn in snapshot.load("bank_transactions"):
        if txn["Status"] != "AUTHORISED" or txn["Type"].endswith("-TRANSFER"):
            continue
        receive = txn["Type"].startswith("RECEIVE")
        sign = -1 if receive else 1  # 收款时明细记贷方
        postings = line_postings(txn.get("LineItems", []), txn["LineAmountTypes"], sign, gst)
        party = (txn.get("Contact") or {}).get("Name")
        counter = Line(txn["BankAccount"]["Code"], -sign * cents(txn["Total"]), party)
        entries.append(
            Entry(
                entry_no=f"XB-{txn['BankTransactionID']}",
                posting_date=xero_date(txn),
                created_at=xero_datetime(txn),
                lines=balance(postings, counter, rounding),
                description=party,
                source_doc=txn.get("Reference") or None,
                source_type="Receive Money" if receive else "Spend Money",
            )
        )

    for transfer in snapshot.load("bank_transfers"):
        amount = cents(transfer["Amount"])
        entries.append(
            Entry(
                entry_no=f"XT-{transfer['BankTransferID']}",
                posting_date=xero_date(transfer),
                created_at=xero_datetime(transfer, "CreatedDateUTC"),
                lines=[
                    Line(transfer["ToBankAccount"]["Code"], amount, "Bank Transfer"),
                    Line(transfer["FromBankAccount"]["Code"], -amount, "Bank Transfer"),
                ],
                description="Bank Transfer",
                source_doc=transfer.get("Reference") or None,
                source_type="Bank Transfer",
            )
        )

    for journal in snapshot.load("manual_journals"):
        if journal["Status"] != "POSTED":
            continue
        journal_lines: list[Line] = []
        for item in journal.get("JournalLines", []):
            if not item.get("AccountCode"):
                continue
            amount, tax = cents(item.get("LineAmount")), cents(item.get("TaxAmount"))
            sign = 1 if amount >= 0 else -1  # 税额随所在行的借贷方向
            net = amount - sign * tax if journal.get("LineAmountTypes") == "Inclusive" else amount
            journal_lines.append(Line(item["AccountCode"], net, item.get("Description")))
            if tax:
                journal_lines.append(Line(gst, sign * tax, "GST"))
        if diff := sum(x.amount for x in journal_lines):
            journal_lines.append(Line(rounding, -diff, "Rounding"))
        entries.append(
            Entry(
                entry_no=f"XM-{journal['ManualJournalID']}",
                posting_date=xero_date(journal),
                created_at=xero_datetime(journal),
                lines=journal_lines,
                description=journal.get("Narration"),
                source_doc=None,
                source_type="Manual Journal",
                is_adjustment=True,
            )
        )
    return sorted(entries, key=lambda e: (e.posting_date, e.entry_no))


def journal_rows(entries: Iterable[Entry]) -> Iterator[JournalEntry]:
    """重建的分录展开成 journal_entries 表的行; 零金额行不出."""
    for entry in entries:
        for n, line in enumerate((x for x in entry.lines if x.amount), 1):
            yield JournalEntry(
                id=f"{entry.entry_no}-{n}",
                entry_no=entry.entry_no,
                posting_date=entry.posting_date,
                created_at=entry.created_at,
                created_by=CREATED_BY,
                approved_by=None,
                account_code=line.account_code,
                debit_cents=max(line.amount, 0),
                credit_cents=max(-line.amount, 0),
                description=line.description or entry.description,
                source_doc=entry.source_doc,
                is_adjustment=entry.is_adjustment,
            )


def trial_balance_cents(report: dict[str, Any]) -> dict[str, int]:
    """Xero 试算表报表 → 科目代码: 余额 (分, 借正). 取 YTD Debit 与 YTD Credit 两列; 科目代码取名称末尾括号内."""
    header = next(r for r in report["Rows"] if r["RowType"] == "Header")
    names = [c["Value"] for c in header["Cells"]]
    debit, credit = names.index("YTD Debit"), names.index("YTD Credit")
    balances: dict[str, int] = {}
    for section in report["Rows"]:
        for row in section.get("Rows", []):
            if row["RowType"] != "Row":
                continue
            cells = [c["Value"] for c in row["Cells"]]
            match = re.search(r"\(([^()]+)\)$", cells[0])
            if match:
                balances[match.group(1)] = cents(cells[debit] or 0) - cents(cells[credit] or 0)
    return balances


# ---- 付费 Journals 接口, 补齐分录, 其余各表与客户画像 ----


SYSTEM_JOURNALS = ("conversion balance", "reconciliation adjustment", "opening balance")  # 系统生成的调整分录说明


def system_journal(reference: str | None) -> bool:
    """没有来源单据的系统分录 (期初转换余额, 对账调整, 存货期初) 及其冲回."""
    text = (reference or "").lower().removeprefix("reversed: ")
    return any(text.startswith(s) or s in text.split(" - ")[0] for s in SYSTEM_JOURNALS)


def journal_entries_from_api(snapshot: Snapshot) -> list[Entry]:
    """Journals 接口 (或其模拟) 的分录; 手工分录与系统分录 (期初余额, 对账调整等) 记为调整分录."""
    numbers = {i["InvoiceID"]: i.get("InvoiceNumber") for i in snapshot.load("invoices")}
    numbers |= {c["CreditNoteID"]: c.get("CreditNoteNumber") for c in snapshot.load("credit_notes")}
    entries = []
    for journal in snapshot.load("journals"):
        source_type = journal.get("SourceType") or ""
        reference = journal.get("Reference") or None
        lines = [
            Line(x["AccountCode"], cents(x["NetAmount"]), x.get("Description") or reference)
            for x in journal.get("JournalLines", [])
            if x.get("AccountCode")
        ]
        entries.append(
            Entry(
                entry_no=f"XJ-{journal['JournalNumber']}",
                posting_date=xero_date(journal, "JournalDate"),
                created_at=xero_datetime(journal, "CreatedDateUTC"),
                lines=lines,
                description=reference,
                source_doc=numbers.get(journal.get("SourceID", "")) or reference,
                source_type=source_type,
                is_adjustment=source_type == "MANJOURNAL" or (not source_type and system_journal(reference)),
                source_id=journal.get("SourceID"),
                sequence=int(journal["JournalNumber"]),
            )
        )
    return sorted(entries, key=lambda e: (e.posting_date, e.sequence))


def plug_entry(
    entries: list[Entry],
    accounts: list[dict[str, Any]],
    current: dict[str, int],
    prior: dict[str, int],
    start: date,
    end: date,
) -> Entry | None:
    """按本期试算表补齐, 口径与 gl_balance 相同: 期初余额 (services.ledger) 加本期 [start, end] 内的分录, 与本期
    试算表的差额. 本期以外的分录不计. 没有差额时为 None; 补齐分录记在 end, 标为调整分录.

    留存收益科目按标准分类识别; 上期有损益却认不出唯一的留存收益科目时抛 ValueError.
    """
    with_code = [a for a in accounts if a.get("Code")]
    kinds = {a["Code"]: account_class(a["Type"]) for a in with_code}
    categories: dict[str, str | None] = {a["Code"]: xero_category(a) for a in with_code}
    derived = opening_balances(prior, kinds, categories)
    for entry in entries:
        if start <= entry.posting_date <= end:
            for line in entry.lines:
                derived[line.account_code] = derived.get(line.account_code, 0) + line.amount
    lines = [
        Line(code, current.get(code, 0) - derived.get(code, 0), "按试算表补齐")
        for code in sorted(current.keys() | derived.keys())
    ]
    lines = [x for x in lines if x.amount]
    if not lines:
        return None
    return Entry(
        entry_no="XO-PLUG",
        posting_date=end,
        created_at=datetime.combine(end, datetime.min.time(), UTC),
        lines=lines,
        description="API 取不到的分录 (期初余额, 费用报销等) 按试算表补齐",
        source_doc=None,
        source_type="Plug",
        is_adjustment=True,
    )


def map_bank_accounts(accounts: list[dict[str, Any]]) -> list[BankAccount]:
    """银行科目 → 银行账户. Xero 澳洲的 BankAccountNumber 前 6 位是 BSB; 开户行名 API 不提供, 取科目名称."""
    rows = []
    for a in accounts:
        if a["Type"] != "BANK" or not a.get("Code"):
            continue
        number = re.sub(r"\D", "", str(a.get("BankAccountNumber") or ""))
        bsb, account_no = (number[:6], number[6:]) if len(number) > 6 else ("", number)
        rows.append(
            BankAccount(
                id=f"BA-{a['Code']}", bank_name=a["Name"], bsb=bsb, account_no=account_no, gl_account_code=a["Code"]
            )
        )
    return rows


def abn(value: Any) -> str | None:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits if len(digits) == 11 else None


def reversed_entries(entries: Iterable[Entry]) -> set[str]:
    """已被冲回的分录号: 某笔分录的说明按 REVERSAL_REFERENCE 点名了它 (XJ-{n}). 冲回分录本身不在其中."""
    pattern = re.compile(REVERSAL_REFERENCE)
    return {f"XJ-{m.group(1)}" for e in entries if (m := pattern.search(e.description or ""))}


def document_lines(entries: list[Entry], account: str | None, sign: int) -> dict[str, str]:
    """各单据 (Entry.source_id) 最后过账且未被冲回的分录中记该科目的行 ID ({分录号}-{k}, k 只数非零行, 同
    journal_rows): sign 为 1 取借方 (销售发票记应收), -1 取贷方 (应付账单记应付); 一笔分录有几行时取第一行.
    单据改过会冲回再过账, 所以取最后一笔; 过账顺序按分录号 (Journals 的 JournalNumber), 不按日期 (重新过账可能改了
    业务日期). 销售发票与应付账单共用 (需求 R178)."""
    cancelled = reversed_entries(entries)
    found: dict[str, str] = {}
    for entry in sorted(entries, key=lambda e: (e.sequence, e.posting_date, e.entry_no)):
        if not entry.source_id or entry.entry_no in cancelled:
            continue
        for n, line in enumerate((x for x in entry.lines if x.amount), 1):
            if line.account_code == account and line.amount * sign > 0:
                found[entry.source_id] = f"{entry.entry_no}-{n}"
                break
    return found


def map_sales_invoices(snapshot: Snapshot, entries: list[Entry]) -> list[SalesInvoice]:
    """已生效的销售发票; journal_entry_id 指向对应单据最后过账且未被冲回的分录里记应收的那一行 (document_lines)."""
    debtors = system_codes(snapshot.load("accounts")).get("DEBTORS")
    contacts = {c["ContactID"]: c for c in snapshot.load("contacts")}
    receivable_line = document_lines(entries, debtors, 1)
    rows = []
    for doc in snapshot.load("invoices"):
        if doc["Type"] != "ACCREC" or doc["Status"] not in LIVE_DOCUMENTS:
            continue
        number = doc.get("InvoiceNumber") or short(doc["InvoiceID"])
        contact = contacts.get(doc["Contact"].get("ContactID"), doc["Contact"])
        day = xero_date(doc)
        rows.append(
            SalesInvoice(
                id=f"SI-{doc['InvoiceID']}",
                invoice_no=number,
                customer=contact.get("Name", ""),
                customer_abn=abn(contact.get("TaxNumber")),
                contract_id=None,
                invoice_date=day,
                posting_date=day,
                amount_cents=cents(doc["Total"]),
                journal_entry_id=receivable_line.get(doc["InvoiceID"]),
            )
        )
    return sorted(rows, key=lambda r: (r.invoice_date, r.id))


def allocations(snapshot: Snapshot, resource: str) -> Iterator[tuple[str, date, int]]:
    """一类单据 (贷项通知单, 预付款, 多付款) 中已生效的那些分配到发票或账单的金额: (发票 ID, 分配日, 金额分)."""
    for doc in snapshot.load(resource):
        if doc.get("Status") not in LIVE_DOCUMENTS:
            continue
        for item in doc.get("Allocations") or []:
            invoice = (item.get("Invoice") or {}).get("InvoiceID")
            if invoice:
                yield invoice, xero_date(item), cents(item.get("Amount"))


def map_supplier_bills(snapshot: Snapshot, entries: list[Entry], period_end: date) -> list[SupplierBill]:
    """期末 (period_end) 仍有未付余额的应付账单 (需求 R178): ACCPAY, 已生效 (AUTHORISED, PAID), 账单日不晚于期末.
    期末未付 = 含税总额 - 期末日 (含) 及以前的付款 (payments, 只计 AUTHORISED, 作废的 DELETED 不计) - 期末日 (含)
    及以前的分配 (已生效的贷项通知单, 预付款与多付款的 Allocations); 付款日不设下限. 未付为 0 的不出.
    金额按账单币种, 不换算 (同销售发票). 供应商取账单上的 Contact.Name, 不读联系人 (contacts). journal_entry_id
    指向最后过账且未被冲回的分录里贷记应付的那一行 (document_lines). 按账单日与 ID 排序."""
    creditors = system_codes(snapshot.load("accounts")).get("CREDITORS")
    settled: dict[str, int] = {}
    for pay in snapshot.load("payments"):
        invoice = (pay.get("Invoice") or {}).get("InvoiceID")
        if invoice and pay.get("Status") == "AUTHORISED" and xero_date(pay) <= period_end:
            settled[invoice] = settled.get(invoice, 0) + cents(pay.get("Amount"))
    for resource in ("credit_notes", "prepayments", "overpayments"):
        for invoice, day, amount in allocations(snapshot, resource):
            if day <= period_end:
                settled[invoice] = settled.get(invoice, 0) + amount
    payable_line = document_lines(entries, creditors, -1)
    rows = []
    for doc in snapshot.load("invoices"):
        if doc["Type"] != "ACCPAY" or doc["Status"] not in LIVE_DOCUMENTS or xero_date(doc) > period_end:
            continue
        total = cents(doc["Total"])
        outstanding = total - settled.get(doc["InvoiceID"], 0)
        if outstanding == 0:
            continue
        rows.append(
            SupplierBill(
                id=f"PB-{doc['InvoiceID']}",
                bill_no=doc.get("InvoiceNumber") or short(doc["InvoiceID"]),
                supplier=doc["Contact"].get("Name", ""),
                bill_date=xero_date(doc),
                due_date=xero_date(doc, "DueDate"),
                amount_cents=total,
                outstanding_cents=outstanding,
                journal_entry_id=payable_line.get(doc["InvoiceID"]),
            )
        )
    return sorted(rows, key=lambda r: (r.bill_date, r.id))


def map_bank_transactions(snapshot: Snapshot) -> list[BankTransaction]:
    """对账单逐行流水 → 银行流水, 交易后余额从首张对账单的期初余额起算; 快照没有对账单时为空."""
    statements = snapshot.load("bank_statements") or {}
    rows: list[BankTransaction] = []
    for code, response in sorted(statements.items()):
        items = sorted(response.get("statements") or [], key=lambda s: s["startDate"])
        if not items:
            continue
        balance_cents = cents(items[0].get("startBalance"))
        lines = sorted(
            (line for s in items for line in s.get("statementLines") or [] if not line.get("isDeleted")),
            key=lambda x: x["postedDate"],
        )
        for n, line in enumerate(lines, 1):
            amount = cents(line["amount"])
            balance_cents += amount
            rows.append(
                BankTransaction(
                    id=f"BT-{code}-{n:04d}",
                    bank_account_id=f"BA-{code}",
                    txn_date=date.fromisoformat(line["postedDate"][:10]),
                    amount_cents=amount,
                    balance_after_cents=balance_cents,
                    description=line.get("payee") or None,
                    reference=line.get("reference") or None,
                )
            )
    return rows


@dataclass(frozen=True)
class Period:
    """快照对应的审计期间: 本期起止与上期截止 (试算表各取一份)."""

    start: date  # 本期起始日
    end: date  # 本期截止日
    prior_end: date  # 上期截止日


def snapshot_period(snapshot: Snapshot) -> Period:
    meta = snapshot.load("dataset")
    if not meta:
        raise FileNotFoundError(
            f"{snapshot.root} 没有 dataset.json, 用 pull --period-start/--period-end/--prior-end 重新拉取"
        )
    try:
        return Period(
            date.fromisoformat(meta["period_start"]),
            date.fromisoformat(meta["period_end"]),
            date.fromisoformat(meta["prior_end"]),
        )
    except (KeyError, TypeError, ValueError) as exc:  # 缺键, 不是对象, 日期不是字符串或写错
        # 消息只写快照名, 不带服务器路径与文件内容 (POST /engagements 原样返回给前端)
        raise ValueError(
            f"快照 {snapshot.root.name} 的 dataset.json 须为含 period_start, period_end, prior_end (YYYY-MM-DD) 的对象"
        ) from exc


def build_tables(snapshot: Snapshot) -> dict[str, list[Any]]:
    """快照 → 八张财务表的行 (含应付账单, 需求 R178). 总账分录有付费接口数据时按它, 否则单据反推加补齐分录."""
    period = snapshot_period(snapshot)
    accounts = snapshot.load("accounts")
    current = trial_balance_cents(snapshot.trial_balance(period.end))
    prior = trial_balance_cents(snapshot.trial_balance(period.prior_end))
    if snapshot.load("journals"):
        entries = journal_entries_from_api(snapshot)
    else:
        entries = document_entries(snapshot)
        plug = plug_entry(entries, accounts, current, prior, period.start, period.end)
        entries = [*entries, plug] if plug else entries
    return {
        "accounts": map_accounts(accounts),
        "trial_balance": [
            TrialBalanceRow(period=name, account_code=code, balance_cents=value)
            for name, balances in (("current", current), ("prior", prior))
            for code, value in sorted(balances.items())
        ],
        "journal_entries": list(journal_rows(entries)),
        "sales_invoices": map_sales_invoices(snapshot, entries),
        "supplier_bills": map_supplier_bills(snapshot, entries, period.end),
        "delivery_notes": [],
        "bank_accounts": map_bank_accounts(accounts),
        "bank_transactions": map_bank_transactions(snapshot),
    }


def client_profile(snapshot: Snapshot) -> ClientProfile:
    """组织信息 → 客户画像. ABN 取 RegistrationNumber (澳洲组织的 ABN); 行业代码与董事 API 不提供, 留空.

    快照目录, organisation.json 或组织 ID 缺失时抛 FileNotFoundError, 不拿空画像建项目.
    """
    if not snapshot.root.is_dir():
        raise FileNotFoundError(f"没有快照目录 {snapshot.root}")
    organisations = snapshot.load("organisation")
    org = organisations[0] if organisations else {}
    if not org.get("OrganisationID"):
        raise FileNotFoundError(f"快照 {snapshot.root} 缺组织信息 (organisation.json 或 OrganisationID)")
    return ClientProfile(
        id=f"CLIENT-XERO-{short(org['OrganisationID'])}",
        version=1,
        legal_name=org.get("LegalName") or org.get("Name", ""),
        abn=abn(org.get("RegistrationNumber")) or "",
        entity_type=str(org.get("OrganisationType", "")).lower(),
        industry_anzsic="",
        listed=False,
        directors=[],
        notes=f"Imported from Xero organisation {org.get('Name', '')} ({org.get('Class', '')}).",
    )
