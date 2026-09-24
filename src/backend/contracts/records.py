"""财务数据的行模型，与 开发接口文档.md 第 9 节财务表逐列对应（去掉 engagement_id）。"""

from datetime import date, datetime
from typing import Literal

from backend.contracts.models import Model


class Account(Model):
    code: str
    name: str
    type: Literal["asset", "liability", "equity", "revenue", "expense"]


class TrialBalanceRow(Model):
    period: Literal["current", "prior"]
    account_code: str
    balance_cents: int  # 借方为正


class JournalEntry(Model):
    id: str
    entry_no: str
    posting_date: date
    created_at: datetime
    created_by: str
    approved_by: str | None
    account_code: str
    debit_cents: int = 0
    credit_cents: int = 0
    description: str | None
    source_doc: str | None
    is_adjustment: bool = False


class SalesInvoice(Model):
    id: str
    invoice_no: str
    customer: str
    customer_abn: str | None
    contract_id: str | None
    invoice_date: date
    posting_date: date
    amount_cents: int
    journal_entry_id: str | None


class DeliveryNote(Model):
    id: str
    delivery_no: str
    invoice_id: str
    shipped_date: date | None
    signed_date: date | None
    signed_by: str | None


class BankAccount(Model):
    id: str
    bank_name: str
    bsb: str
    account_no: str
    gl_account_code: str


class BankTransaction(Model):
    id: str
    bank_account_id: str
    txn_date: date
    amount_cents: int  # 存入为正
    balance_after_cents: int
    description: str | None
    reference: str | None
