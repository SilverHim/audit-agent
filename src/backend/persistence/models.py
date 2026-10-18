"""PostgreSQL tables shared by migrations and the stores."""

from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    DefaultClause,
    ForeignKey,
    Integer,
    Table,
    Text,
    UniqueConstraint,
)
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
            "accounts": {"category", "category_basis"},
            "journal_entries": {"created_at", "created_by", "approved_by", "approved_at", "description", "source_doc"},
            "sales_invoices": {"customer_abn", "contract_id", "journal_entry_id"},
            "delivery_notes": {"shipped_date", "signed_date", "signed_by"},
            "bank_transactions": {"description", "reference"},
            "contracts": {"counterparty_abn", "amount_cents", "sign_date"},
            "documents": {"uploaded_by", "failed_step"},
            "dataset_loads": {"ready_at", "ready_by"},
            "data_imports": {
                "kind",
                "file_name",
                "file_path",
                "document_id",
                "sheet",
                "date_format",
                "mapping",
                "row_count",
                "records",
                "rows",
                "data_version",
                "request_id",
                "committed_at",
            },
            "data_requests": {"table_kind", "document_type", "format_hint", "resolved_at"},
            "data_checks": {"request_id"},
            "account_category_history": {"category", "basis", "reason"},
            "extraction_versions": {"field", "reason"},
            "llm_settings": {"api_key_encrypted", "key_last4"},
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
# 设置页的模型配置: 系统级, 不按项目隔离, 每套 (slot) 只存最新一行; 密钥只存密文 (迁移 0003)
llm_settings = table(
    "llm_settings",
    "slot:s provider:s base_url:s model:s api_key_encrypted:s key_last4:s updated_by:s updated_at:t",
    ("slot",),
    scoped=False,
)
FINANCIAL = {
    "accounts": table("accounts", "code:s name:s type:s category:s category_basis:s", ("code",)),
    "trial_balance": table("trial_balance", "period:s account_code:s balance_cents:b", ("period", "account_code")),
    "journal_entries": table(
        "journal_entries",
        "id:s entry_no:s posting_date:d created_at:t created_by:s approved_by:s approved_at:t account_code:s "
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
    "id:s type:s file_name:s file_path:s sha256:s page_count:i reliability:s status:s uploaded_at:t "
    "media_type:s channel:s uploaded_by:s failed_step:s",
    ("id",),
)
documents.c.media_type.server_default = DefaultClause("application/pdf")  # 迁移 0004 之前的文件都是 PDF
documents.c.channel.server_default = DefaultClause("auditor_upload")
documents.append_constraint(UniqueConstraint("engagement_id", "sha256"))
contracts = table(
    "contracts",
    "id:s version:i document_id:s counterparty:s counterparty_abn:s type:s amount_cents:b sign_date:d "
    "evidence:j extracted_by_run:s",
    ("id",),
)
contract_terms = table("contract_terms", "id:s contract_id:s term_type:s text:s page:i", ("id",))
contracts.c.version.server_default = DefaultClause("1")
# 项目的数据版本与就绪确认: 每次写入账表 data_version +1; ready_at 为审计师确认数据已齐 (数据集载入视同确认)
dataset_loads = table("dataset_loads", "dataset:s data_version:i loaded_at:t ready_at:t ready_by:s", ())
# 账表导入会话与各表的版本: 上传解析后 status parsed; 写入后 committed, 记数据版本与行快照 (rows), 旧版本不删
data_imports = table(
    "data_imports",
    "id:s kind:s source:s file_name:s file_path:s document_id:s sheet:s date_format:s mapping:j row_count:i "
    "records:i rows:j status:s data_version:i request_id:s created_by:s created_at:t committed_at:t",
    ("id",),
)
# 数据管理智能体 (A3) 提出的补交资料请求
data_requests = table(
    "data_requests",
    "id:s kind:s table_kind:s document_type:s title:s used_for:j reason_code:s reason_params:j format_hint:s "
    "status:s raised_by:s created_at:t resolved_at:t",
    ("id",),
)
# 数据质量体检: 每项只存最近一次的结果, 历次结果在 data 留痕里
data_checks = table("data_checks", "id:s status:s params:j request_id:s data_version:i checked_at:t", ("id",))
# 科目标准分类的历史: 第 1 版为系统归类, 之后每次审计师修改或重新导入改了归类各一版
account_category_history = table(
    "account_category_history",
    "account_code:s version:i category:s basis:s changed_by:s changed_at:t reason:s",
    ("account_code", "version"),
)
# 抽取结果 (合同, 回函) 的各版本: 第 1 版为抽取所得, 之后每次审计师修改一版; data 为该版完整内容
extraction_versions = table(
    "extraction_versions",
    "target_type:s target_id:s version:i data:j field:s reason:s changed_by:s changed_at:t",
    ("target_type", "target_id", "version"),
)
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
