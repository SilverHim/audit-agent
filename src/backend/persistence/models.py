"""PostgreSQL tables shared by migrations and the stores."""

from typing import Any

from sqlalchemy import BigInteger, Boolean, Column, Date, DateTime, ForeignKey, Integer, Table, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import SQLModel

metadata = SQLModel.metadata


def table(name: str, fields: str, keys: tuple[str, ...], *, scoped: bool = True) -> Table:
    types: dict[str, Any] = {
        "s": Text,
        "i": Integer,
        "b": BigInteger,
        "d": Date,
        "t": DateTime(timezone=True),
        "j": JSONB,
        "z": Boolean,
    }
    columns = (
        [Column("engagement_id", Text, ForeignKey("engagements.id"), primary_key=name != "audit_trail", nullable=False)]
        if scoped
        else []
    )
    for field in fields.split():
        name_, typ = field.split(":")
        optional = {
            "accounts": {"category"},
            "journal_entries": {"approved_by", "description", "source_doc"},
            "sales_invoices": {"customer_abn", "contract_id", "journal_entry_id"},
            "delivery_notes": {"shipped_date", "signed_date", "signed_by"},
            "bank_transactions": {"description", "reference"},
            "contracts": {"counterparty_abn", "amount_cents", "sign_date"},
            "audit_trail": {
                "procedure_id",
                "run_id",
                "model",
                "prompt_hash",
                "input_tokens",
                "output_tokens",
                "idempotency_key",
            },
        }
        columns.append(
            Column(name_, types[typ], primary_key=name_ in keys, nullable=name_ in optional.get(name, set()))
        )
    return Table(name, metadata, *columns)


users = table("users", "id:s email:s full_name:s password_hash:s created_at:t", ("id",), scoped=False)
users.append_constraint(UniqueConstraint("email"))
engagements = table(
    "engagements",
    "id:s client_id:s period_start:d period_end:d dataset:s owner_id:s phase:s locked:z "
    "version_snapshot:j client_profile_version:i created_at:t updated_at:t",
    ("id",),
    scoped=False,
)
engagements.c.owner_id.append_foreign_key(ForeignKey("users.id"))
client_profiles = table("client_profiles", "id:s version:i data:j updated_at:t", ("id", "version"), scoped=False)
FINANCIAL = {
    "accounts": table("accounts", "code:s name:s type:s category:s", ("code",)),
    "trial_balance": table("trial_balance", "period:s account_code:s balance_cents:b", ("period", "account_code")),
    "journal_entries": table(
        "journal_entries",
        "id:s entry_no:s posting_date:d created_at:t created_by:s approved_by:s account_code:s "
        "debit_cents:b credit_cents:b description:s source_doc:s is_adjustment:z",
        ("id",),
    ),
    "sales_invoices": table(
        "sales_invoices",
        "id:s invoice_no:s customer:s customer_abn:s contract_id:s invoice_date:d posting_date:d "
        "amount_cents:b journal_entry_id:s",
        ("id",),
    ),
    "delivery_notes": table(
        "delivery_notes", "id:s delivery_no:s invoice_id:s shipped_date:d signed_date:d signed_by:s", ("id",)
    ),
    "bank_accounts": table("bank_accounts", "id:s bank_name:s bsb:s account_no:s gl_account_code:s", ("id",)),
    "bank_transactions": table(
        "bank_transactions",
        "id:s bank_account_id:s txn_date:d amount_cents:b balance_after_cents:b description:s reference:s",
        ("id",),
    ),
}
documents = table(
    "documents",
    "id:s type:s file_name:s file_path:s sha256:s page_count:i reliability:s status:s uploaded_at:t",
    ("id",),
)
documents.append_constraint(UniqueConstraint("engagement_id", "sha256"))
contracts = table(
    "contracts",
    "id:s document_id:s counterparty:s counterparty_abn:s type:s amount_cents:b sign_date:d "
    "evidence:j extracted_by_run:s",
    ("id",),
)
contract_terms = table("contract_terms", "id:s contract_id:s term_type:s text:s page:i", ("id",))
dataset_loads = table("dataset_loads", "dataset:s data_version:i loaded_at:t", ())
OUTPUT_KINDS = {
    "audit_objectives": ["AuditObjective"],
    "risk_register": ["RiskItem"],
    "procedures": ["Procedure", "ProcedureRun"],
    "exceptions": ["AuditException"],
    "judgments": ["Judgment"],
    "review_tasks": ["ReviewTask"],
    "decisions": ["ReviewRecord", "GateDecision"],
    "confirmations": ["Confirmation"],
    "misstatements": ["Misstatement"],
    "report_versions": ["AuditReportVersion"],
    "gate_materials": ["AcceptanceScorecard", "AnalyticsResult", "MaterialityCandidates", "UncorrectedSchedule"],
}
OUTPUTS = {}
for name, kinds in OUTPUT_KINDS.items():
    output = table(name, "id:s version:i kind:s data:j created_by:s created_at:t", ("id", "version"))
    for kind in kinds:
        OUTPUTS[kind] = output

audit_trail = table(
    "audit_trail",
    "id:s kind:s actor:s procedure_id:s run_id:s summary:s model:s prompt_hash:s "
    "input_tokens:i output_tokens:i idempotency_key:s payload:j created_at:t",
    ("id",),
)
SCOPED = [t for t in metadata.tables.values() if "engagement_id" in t.c]
