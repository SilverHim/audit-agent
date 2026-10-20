"""Xero 付费接口的模拟 (开发与测试用): 用网页导出的报表代替 Journals 与 Bank Statements Plus 的响应.

XeroClient 照常发请求, 由 MockPaidTransport 截下这两个接口按官方 OpenAPI 的结构作答, 其余请求照常发往 Xero.
- Journals (GET api.xro/2.0/Journals?offset=): 取自网页 Journal Report (加选 Narration, Description 两列导出).
  网页导出没有来源单据, 按日期, 金额与往来名称匹配快照里的单据补 SourceType, SourceID; 冲回分录沿用原分录的来源;
  匹配不上的按分录说明推断 (费用报销等), 再不行为空. 网页的录入人 (Posted By) 接口没有, 不放进响应.
- Bank Statements Plus (GET finance.xro/1.0/BankStatementsPlus/statements): 取自网页 Bank Statement 报表.
  与真实接口一样: 区间超过 12 个月或截止日在今天之后时返回 400.
网页导出与 API 快照须同一天取得 (Demo Company 每 28 天重置, 日期会平移).
"""

import datetime as dt
import re
import uuid
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from backend.adapters.xero_dataset import REVERSAL_REFERENCE, Snapshot, cents, xero_date

NAMESPACE = uuid.UUID("6f1c2a52-6a0e-4b8f-9d55-3a0c8b1f2e10")  # 模拟数据的确定性 ID
XERO_TZ = ZoneInfo("Australia/Sydney")  # 网页导出的时间按组织所在时区显示
JOURNAL_PAGE = 100  # 真实 Journals 接口每次最多返回的笔数
SOURCE_DOCS = (
    # (快照资源, 金额字段, 名称取法, 来源类型取法, ID 字段)
    ("invoices", "Total", "Contact", "Type", "InvoiceID"),
    ("credit_notes", "Total", "Contact", "Type", "CreditNoteID"),
    ("payments", "Amount", "Invoice", "PaymentType", "PaymentID"),
    ("bank_transactions", "Total", "Contact", "Type", "BankTransactionID"),
)
BANK_TYPES = {"SPEND": "CASHPAID", "RECEIVE": "CASHREC", "SPEND-TRANSFER": "TRANSFER", "RECEIVE-TRANSFER": "TRANSFER"}
REVERSAL = re.compile(REVERSAL_REFERENCE)  # 冲回分录说明的固定格式, 与映射共用一个常量
SYSTEM_PREFIXES = (
    "conversion balance",
    "reconciliation adjustment",
    "inventory opening balance",
)  # 系统分录, 没有来源单据


def ms_date(value: dt.datetime) -> str:
    """Xero JSON 的 /Date(毫秒+0000)/."""
    return f"/Date({int(value.timestamp() * 1000)}+0000)/"


def det_id(*parts: object) -> str:
    return str(uuid.uuid5(NAMESPACE, "/".join(str(p) for p in parts)))


def read_rows(path: Path, sheet: int = 0) -> list[tuple[Any, ...]]:
    import openpyxl  # 只在模拟时需要 (开发依赖)

    workbook = openpyxl.load_workbook(path, read_only=True)
    return list(workbook.worksheets[sheet].iter_rows(values_only=True))


def journals_from_export(path: Path, snapshot: Snapshot) -> list[dict[str, Any]]:
    """网页 Journal Report → Journals 接口结构的分录列表, 按 JournalNumber 升序."""
    rows = read_rows(path)
    header = next(r for r in rows if r and r[0] == "Date")
    col = {name: i for i, name in enumerate(header)}
    accounts = {a["Code"]: a for a in snapshot.load("accounts") if a.get("Code")}
    grouped: dict[int, list[tuple[Any, ...]]] = {}
    for row in rows:
        if isinstance(row[0], dt.datetime):
            grouped.setdefault(int(row[col["Journal ID"]]), []).append(row)
    journals = []
    for number in sorted(grouped):
        lines = grouped[number]
        first = lines[0]
        posted = first[col["Posted Date"]].replace(tzinfo=XERO_TZ)
        journal_date = dt.datetime.combine(first[0].date(), dt.time(), dt.UTC)
        journal_lines = []
        for n, row in enumerate(lines, 1):
            code = str(row[col["Account Code"]])
            account = accounts.get(code, {})
            net = (cents(row[col["Debit"]]) - cents(row[col["Credit"]])) / 100
            journal_lines.append(
                {
                    "JournalLineID": det_id("journal-line", number, n),
                    "AccountID": account.get("AccountID", det_id("account", code)),
                    "AccountCode": code,
                    "AccountType": account.get("Type", ""),
                    "AccountName": row[col["Account"]],
                    "Description": row[col["Description"]] or "",
                    "NetAmount": net,
                    "GrossAmount": net,
                    "TaxAmount": 0.0,
                }
            )
        journals.append(
            {
                "JournalID": det_id("journal", number),
                "JournalDate": ms_date(journal_date),
                "JournalNumber": number,
                "CreatedDateUTC": ms_date(posted.astimezone(dt.UTC)),
                "Reference": first[col["Narration"]] or "",
                "JournalLines": journal_lines,
            }
        )
    infer_sources(journals, snapshot)
    return journals


def source_documents(snapshot: Snapshot) -> list[dict[str, Any]]:
    """快照里可作分录来源的单据: 日期, 金额 (分), 往来名称, 来源类型, ID."""
    docs: list[dict[str, Any]] = []
    for name, amount, party, kind, key in SOURCE_DOCS:
        for item in snapshot.load(name):
            holder = item.get(party) or {}
            contact = holder.get("Contact", holder) if party == "Invoice" else holder
            source = item[kind]
            if name == "bank_transactions":
                source = BANK_TYPES.get(source, source)
            docs.append(
                {
                    "date": xero_date(item),
                    "cents": abs(cents(item.get(amount))),
                    "name": (contact.get("Name") or "").lower(),
                    "type": source,
                    "id": item[key],
                }
            )
    for transfer in snapshot.load("bank_transfers"):
        docs.append(
            {
                "date": xero_date(transfer),
                "cents": cents(transfer["Amount"]),
                "name": "",
                "type": "TRANSFER",
                "id": transfer["BankTransferID"],
            }
        )
    return docs


def payroll_codes(accounts: list[dict[str, Any]]) -> set[str]:
    """工资分录会用到的科目: 应付工资系统科目, 报告代码 EXP.EMP 开头的薪酬费用, 名称含 PAYG 或应付公积金的负债."""
    codes = set()
    for a in accounts:
        name = a["Name"].lower()
        if (
            a.get("SystemAccount") == "WAGEPAYABLES"
            or str(a.get("ReportingCode", "")).startswith("EXP.EMP")
            or "payg" in name
            or "superannuation payable" in name
        ):
            codes.add(a.get("Code", ""))
    return codes


def journal_type(lines: list[dict[str, Any]], codes: dict[str, str], banks: set[str]) -> str | None:
    """按科目与借贷方向推断来源单据类型 (Xero SourceType); 推不出为 None.

    应收借方 → 销售发票, 应收贷方 → 销售贷项通知单; 应付贷方 → 采购发票, 应付借方 → 采购贷项通知单;
    银行与应收, 应付同时出现 → 收付款 (方向决定是收款, 付款还是退款); 只有银行科目 → 转账;
    银行与其他科目 → 收支 (借银行为收, 贷银行为支); 用到未付报销科目 → 费用报销或其付款.
    """
    net: dict[str, int] = {}
    for x in lines:
        net[x["AccountCode"]] = net.get(x["AccountCode"], 0) + cents(x["NetAmount"])
    bank = sum(v for code, v in net.items() if code in banks)
    receivable, payable = net.get(codes.get("DEBTORS", ""), 0), net.get(codes.get("CREDITORS", ""), 0)
    if codes.get("UNPAIDEXPCLM") in net:
        return "EXPPAYMENT" if bank else "EXPCLAIM"
    if net.keys() <= banks and len(net) > 1:
        return "TRANSFER"
    if bank and receivable:
        return "ACCRECPAYMENT" if bank > 0 else "ARCREDITPAYMENT"
    if bank and payable:
        return "ACCPAYPAYMENT" if bank < 0 else "APCREDITPAYMENT"
    if receivable:
        return "ACCREC" if receivable > 0 else "ACCRECCREDIT"
    if payable:
        return "ACCPAY" if payable < 0 else "ACCPAYCREDIT"
    if bank:
        return "CASHREC" if bank > 0 else "CASHPAID"
    return None


def match_document(
    docs: list[dict[str, Any]], kind: str, day: dt.date, total: int, lowered: str
) -> dict[str, Any] | None:
    """在同类单据中依次找: 往来名称出现在分录说明里且同日同金额, 未匹配过的优先 (同一单据改过会冲回再过账,
    一张单据可对应多笔分录); 没有往来名称的单据 (转账) 同日同金额且未匹配过; 名称相符, 同金额且只有一张.
    多张都可能或只有名称相符时不匹配."""
    same_kind = [d for d in docs if d["type"] == kind and d["cents"] == total]
    named = [d for d in same_kind if d["name"] and d["name"] in lowered]
    for candidates in (
        [d for d in named if d["date"] == day and not d.get("used")],
        [d for d in named if d["date"] == day],
        [d for d in same_kind if not d["name"] and d["date"] == day and not d.get("used")],
        named if len(named) == 1 else [],
    ):
        if candidates:
            candidates[0]["used"] = True
            return candidates[0]
    return None


def infer_sources(journals: list[dict[str, Any]], snapshot: Snapshot) -> None:
    """给每笔分录补 SourceType, SourceID (原地修改).

    网页导出的说明: 手工分录末尾带 " - Manual", 冲回分录带 "Reversal of ID n" (沿用原分录的来源).
    系统分录 (期初转换余额, 对账调整等) 没有来源. 其余先按科目与借贷方向推出单据类型 (journal_type), 工资分录
    (免费接口拿不到工资单) 记 PAYSLIP; 再在同类单据中匹配 SourceID, 匹配不上只记类型.
    """
    docs = source_documents(snapshot)
    accounts = snapshot.load("accounts")
    codes = {a["SystemAccount"]: a.get("Code", "") for a in accounts if a.get("SystemAccount")}
    banks = {a.get("Code", "") for a in accounts if a.get("Type") == "BANK"}
    manual = {m.get("Narration", "").lower(): m["ManualJournalID"] for m in snapshot.load("manual_journals")}
    payroll = payroll_codes(accounts)
    by_number: dict[int, dict[str, Any]] = {}
    for journal in journals:
        reference = journal["Reference"]
        lowered = reference.lower()
        source_type, source_id = None, None
        reversal = REVERSAL.search(reference)
        if reversal and int(reversal.group(1)) in by_number:
            original = by_number[int(reversal.group(1))]
            source_type, source_id = original.get("SourceType"), original.get("SourceID")
        elif lowered.endswith(" - manual") or lowered in manual:
            source_type, source_id = "MANJOURNAL", manual.get(lowered.removesuffix(" - manual"))
        elif not any(lowered.startswith(s) for s in SYSTEM_PREFIXES):
            source_type = journal_type(journal["JournalLines"], codes, banks)
            if source_type in (None, "CASHPAID", "CASHREC") and any(
                x["AccountCode"] in payroll for x in journal["JournalLines"]
            ):
                source_type = "PAYSLIP"  # 直接从银行发工资; 走应付的 (如付公积金账单) 仍按单据类型
            elif source_type:
                day = dt.datetime.fromtimestamp(int(journal["JournalDate"][6:-7]) / 1000, dt.UTC).date()
                total = sum(cents(x["NetAmount"]) for x in journal["JournalLines"] if x["NetAmount"] > 0)
                document = match_document(docs, source_type, day, total, lowered)
                source_id = document["id"] if document else None
        if source_type:
            journal["SourceType"] = source_type
        if source_id:
            journal["SourceID"] = source_id
        by_number[journal["JournalNumber"]] = journal


def statement_from_export(path: Path) -> tuple[str, list[dict[str, Any]], float]:
    """网页 Bank Statement 报表 → (账户名, 逐行流水, 期初余额). 取最后一张工作表 (报表包导出时它带明细)."""
    rows = read_rows(path, -1)
    name = str(rows[3][0])
    dated = [r for r in rows if isinstance(r[0], dt.datetime)]
    opening = float(dated[0][7] or 0)  # 首行是期初余额行
    lines = [
        {
            "date": r[0].date(),
            "payee": r[1] or "",
            "reference": (r[3] or "").strip(),
            "reconciled": r[4] == "Yes",
            "source": r[5] or "",
            "amount": float(r[6] or 0),
        }
        for r in dated[1:-1]  # 末行是期末余额行
    ]
    return name, lines, opening


def statements_response(
    account: dict[str, Any], lines: list[dict[str, Any]], opening: float, start: dt.date, end: dt.date
) -> dict[str, Any]:
    """Bank Statements Plus 的响应: 区间内的流水作一张对账单, 起止余额按期初余额与此前流水推算."""
    before = sum(cents(x["amount"]) for x in lines if x["date"] < start)
    inside = [x for x in lines if start <= x["date"] <= end]
    start_balance = (cents(opening) + before) / 100
    end_balance = (cents(opening) + before + sum(cents(x["amount"]) for x in inside)) / 100
    statement_lines = [
        {
            "statementLineId": det_id("line", account["AccountID"], n, x["date"], x["amount"]),
            "postedDate": x["date"].isoformat(),
            "payee": x["payee"],
            "reference": x["reference"],
            "notes": "",
            "chequeNo": "",
            "amount": x["amount"],
            "transactionDate": x["date"].isoformat(),
            "type": "CREDIT" if x["amount"] >= 0 else "DEBIT",
            "isReconciled": x["reconciled"],
            "isDuplicate": False,
            "isDeleted": False,
            "payments": [],
            "bankTransactions": [],
        }
        for n, x in enumerate(lines)
        if start <= x["date"] <= end
    ]
    return {
        "bankAccountId": account["AccountID"],
        "bankAccountName": account["Name"],
        "bankAccountCurrencyCode": account.get("CurrencyCode", "AUD"),
        "statements": [
            {
                "statementId": det_id("statement", account["AccountID"], start, end),
                "startDate": start.isoformat(),
                "endDate": end.isoformat(),
                "importSource": "STMTIMPORTSRC/MANUAL",
                "startBalance": start_balance,
                "endBalance": end_balance,
                "indicativeStartBalance": start_balance,
                "indicativeEndBalance": end_balance,
                "statementLines": statement_lines,
            }
        ],
    }


class MockPaidTransport(httpx.AsyncBaseTransport):
    """截下 Journals 与 Bank Statements Plus, 用网页导出作答; 其余请求经真实网络发出.

    exports: 网页导出目录, 含 journal_report_full.xlsx 与 bank_statement_{科目代码}.xlsx;
    snapshot: 本次 pull 的输出目录 (先写好科目与单据, 再取分录); today: 判断截止日是否在未来.
    """

    def __init__(self, exports: Path, snapshot: Path, today: dt.date) -> None:
        self.exports = exports
        self.snapshot = Snapshot(snapshot)
        self.today = today
        self._journals: list[dict[str, Any]] | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.lower()
        if path.endswith("/api.xro/2.0/journals"):
            return self.journals(int(request.url.params.get("offset", "0")))
        if path.endswith("/finance.xro/1.0/bankstatementsplus/statements"):
            return self.statements(request.url.params)
        async with httpx.AsyncHTTPTransport() as real:
            response = await real.handle_async_request(request)
            content = await response.aread()
        return httpx.Response(response.status_code, headers=response.headers, content=content, request=request)

    def journals(self, offset: int) -> httpx.Response:
        if self._journals is None:
            self._journals = journals_from_export(self.exports / "journal_report_full.xlsx", self.snapshot)
        batch = [j for j in self._journals if j["JournalNumber"] > offset][:JOURNAL_PAGE]
        return httpx.Response(200, json={"Journals": batch})

    def statements(self, params: httpx.QueryParams) -> httpx.Response:
        try:
            start = dt.date.fromisoformat(params["FromDate"])
            end = dt.date.fromisoformat(params["ToDate"])
            account_id = params["BankAccountID"]
        except (KeyError, ValueError):
            return bad_request("BankAccountID, FromDate, ToDate 必填, 日期格式 yyyy-MM-dd")
        if end > self.today:
            return bad_request("ToDate 不能在今天之后")
        if end < start or end >= add_months(start, 12):
            return bad_request("查询区间最多 12 个月")
        account = next((a for a in self.snapshot.load("accounts") if a.get("AccountID") == account_id), None)
        if account is None:
            return bad_request(f"没有银行账户 {account_id}")
        path = self.exports / f"bank_statement_{account['Code']}.xlsx"
        if not path.exists():
            return httpx.Response(404, json={"title": "NotFound", "detail": f"缺网页导出 {path.name}"})
        _, lines, opening = statement_from_export(path)
        return httpx.Response(200, json=statements_response(account, lines, opening, start, end))


def bad_request(detail: str) -> httpx.Response:
    return httpx.Response(400, json={"title": "BadRequest", "status": 400, "detail": detail})


def add_months(day: dt.date, months: int) -> dt.date:
    month = day.month - 1 + months
    year, month = day.year + month // 12, month % 12 + 1
    return day.replace(year=year, month=month, day=min(day.day, 28))
