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
    Index,
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
            "supplier_bills": {"journal_entry_id"},
            "delivery_notes": {"shipped_date", "signed_date", "signed_by"},
            "bank_transactions": {"description", "reference"},
            "employees": {"employment_type"},
            "contracts": {"counterparty_abn", "amount_cents", "sign_date"},
            "documents": {"uploaded_by", "failed_step", "document_date"},
            "engagements": {"dataset"},  # 新建的客户没有数据集 (R288)
            "client_profiles": {"source", "updated_by"},  # R288 之前写入的版本没有出处
            "dataset_loads": {"dataset", "ready_at", "ready_by", "ready_reason"},
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
            "data_requests": {
                "table_kind",
                "document_type",
                "format_hint",
                "resolved_at",
                "resolved_by",
                "unavailable_reason",
            },
            "data_checks": {"request_id"},
            "account_category_history": {"category", "basis", "reason"},
            "extraction_versions": {"field", "reason"},
            "data_changes": {"document_types"},
            "financial_statements": {"directors_declaration_date", "document_id"},
            "financial_statement_lines": {"prior_amount_cents"},
            "xero_connections": {"tenant_id", "tenant_name", "last_synced_at"},
            "import_batches": {"request_id", "committed_at", "committed_by", "data_version", "error"},
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
# 客户画像: 每个客户 (id) 一组版本, 跨项目; source 为该版的来源 (ProfileSource), updated_by 为写入的用户 (迁移 0011)
client_profiles = table(
    "client_profiles", "id:s version:i data:j source:s updated_by:s updated_at:t", ("id", "version"), scoped=False
)
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
    # 应付账单的期末未付明细 (需求 R178, 迁移 0014): 随 Xero 快照写入 (同步, 演示数据导入, 数据集载入); Numbat 为空
    "supplier_bills": table(
        "supplier_bills",
        "id:s bill_no:s supplier:s bill_date:d due_date:d amount_cents:b outstanding_cents:b journal_entry_id:s",
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
    # 工资册 (需求 R96, 迁移 0013): 不随数据集载入, 经 DataStore.write_tables 写入
    "employees": table(
        "employees",
        "employee_id:s name:s start_date:d employment_type:s pay_period_days:i gross_per_period_cents:b",
        ("employee_id",),
    ),
}
documents = table(
    "documents",
    "id:s type:s file_name:s file_path:s sha256:s page_count:i reliability:s status:s uploaded_at:t "
    "media_type:s channel:s uploaded_by:s failed_step:s document_date:d related_ids:j",
    ("id",),
)
documents.c.media_type.server_default = DefaultClause("application/pdf")  # 迁移 0004 之前的文件都是 PDF
documents.c.channel.server_default = DefaultClause("auditor_upload")
documents.c.related_ids.server_default = DefaultClause("[]")  # 迁移 0012 之前登记的文件没有关联对象
documents.append_constraint(UniqueConstraint("engagement_id", "sha256"))
contracts = table(
    "contracts",
    "id:s version:i document_id:s counterparty:s counterparty_abn:s type:s amount_cents:b sign_date:d "
    "evidence:j extracted_by_run:s",
    ("id",),
)
contract_terms = table("contract_terms", "id:s contract_id:s term_type:s text:s page:i", ("id",))
contracts.c.version.server_default = DefaultClause("1")
# 项目的数据版本与就绪确认: 每次写入账表 data_version +1; ready_at 为审计师确认数据已齐 (数据集载入视同确认),
# ready_reason 与 ready_failed_checks 为确认时的理由与最近一次体检不通过的项 (迁移 0005)
dataset_loads = table(
    "dataset_loads",
    "dataset:s data_version:i loaded_at:t ready_at:t ready_by:s ready_reason:s ready_failed_checks:j",
    (),
)
dataset_loads.c.ready_failed_checks.server_default = DefaultClause("[]")
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
    "status:s raised_by:s created_at:t resolved_at:t resolved_by:s unavailable_reason:s",
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
# 待审财报 (需求 R217, 迁移 0019): 每版一行, 各行另表 (按 line_no 为列示顺序); A3 从上传的财务报表抽取与审计师修改
# 各写一版, 旧版本不删. 当前版本为每个项目 version 最高的一版 (FinancialDataStore.financial_statements)
financial_statements = table(
    "financial_statements",
    "id:s version:i prepared_at:d directors_declaration_date:d document_id:s comparative_restatement_entry_nos:j",
    ("id", "version"),
)
financial_statement_lines = table(
    "financial_statement_lines",
    "fs_id:s fs_version:i line_no:i code:s statement:s name:s amount_cents:b sign:i account_codes:j subtotal:z "
    "prior_amount_cents:b",
    ("fs_id", "fs_version", "code"),
)
# 抽取结果 (合同, 回函) 的各版本: 第 1 版为抽取所得, 之后每次审计师修改一版; data 为该版完整内容
extraction_versions = table(
    "extraction_versions",
    "target_type:s target_id:s version:i data:j field:s reason:s changed_by:s changed_at:t",
    ("target_type", "target_id", "version"),
)
# 数据变动: 哪个数据版本变了哪类数据 (DataInput); 过期判断只读这里 (需求 R274, 迁移 0008). document_types 为 documents
# 变动时登记变了的文件的类型 (JSON 数组, 需求 R304, 迁移 0020); 其余为空, 之前记的与数据集载入的 documents 也为空
data_changes = table("data_changes", "data_version:i input:s document_types:j", ("data_version", "input"))
# 项目的 Xero 网页连接 (需求 R279, 迁移 0009): 每个项目最多一行, 断开即删; 令牌只存 Fernet 密文 (tokens_encrypted,
# 主密钥 SETTINGS_SECRET_KEY), 明文不落库; tenants 为授权给到的组织 [{id, name}], synced_tables 为最近一次同步写的表
xero_connections = table(
    "xero_connections",
    "tenants:j tenant_id:s tenant_name:s scope:s status:s connected_by:s connected_at:t refreshed_at:t "
    "last_synced_at:t synced_tables:j tokens_encrypted:s",
    (),
)
# 智能导入的批次 (需求 R277, 迁移 0010): 上传的文件, A3 识别出的确认单 (items) 与写入结果; 整批按 ID 覆盖
import_batches = table(
    "import_batches",
    "id:s status:s request_id:s created_by:s created_at:t files:j items:j committed_at:t committed_by:s data_version:i "
    "error:s",
    ("id",),
)
OUTPUT_KINDS = {
    "audit_objectives": ["AuditObjective"],
    "risk_register": ["RiskItem"],
    "procedures": ["Procedure", "ProcedureRun"],
    "exceptions": ["AuditException"],
    "judgments": ["Judgment"],
    "review_tasks": ["ReviewTask"],
    "decisions": ["ReviewRecord", "GateDecision", "RerunRequest"],
    "confirmations": ["Confirmation"],
    "misstatements": ["Misstatement"],
    "report_versions": ["AuditReportVersion"],
    "gate_materials": [
        "AcceptanceScorecard",
        "AnalyticsResult",
        "MaterialityCandidates",
        "UncorrectedSchedule",
        "EngagementTerms",  # 需求 R198; kind 为字符串列, 不需迁移
    ],
    "ownership_graphs": ["OwnershipGraph"],  # 需求 R136, 迁移 0016
    # 签字前提的四类产出 (需求 R215, 迁移 0017)
    "completion_records": [
        "WrittenRepresentationLetter",
        "ComplianceCheck",
        "GovernanceCommunication",
        "IndependenceDeclaration",
    ],
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
# 按项目读留痕走索引 (需求 R298, 迁移 0015). 项目条件来自行级安全 (engagement_id = 当前项目), 排序键同
# AuditTrail.list 的 (created_at, id):
# - TRAIL_BY_TIME: 时间线的最近一页 (newest) 与游标往前翻 (before), 带或不带一组 kind;
# - TRAIL_BY_KIND: 只取一类时 (trail_pages.all_of_kind 按写入顺序逐页读完, last_of 取最近一条), 不必扫过别的类;
# - TRAIL_BY_KEY: find_success 按防重复键取 (每写一条带键的留痕前查一次), 部分索引, 只含带键的行.
TRAIL_BY_TIME = Index(
    "ix_audit_trail_engagement_time", audit_trail.c.engagement_id, audit_trail.c.created_at, audit_trail.c.id
)
TRAIL_BY_KIND = Index(
    "ix_audit_trail_engagement_kind_time",
    audit_trail.c.engagement_id,
    audit_trail.c.kind,
    audit_trail.c.created_at,
    audit_trail.c.id,
)
TRAIL_BY_KEY = Index(
    "ix_audit_trail_engagement_key",
    audit_trail.c.engagement_id,
    audit_trail.c.idempotency_key,
    postgresql_where=audit_trail.c.idempotency_key.is_not(None),
)
SCOPED = [t for t in metadata.tables.values() if "engagement_id" in t.c]
