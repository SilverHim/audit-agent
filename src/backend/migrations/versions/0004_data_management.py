"""数据管理 (需求 R10-R16) 的表结构: 新表 data_imports, data_requests, data_checks, account_category_history,
extraction_versions; dataset_loads 加就绪确认, accounts 加分类依据, journal_entries 加审批时间且录入人与录入时间可空,
documents 加媒体类型, 来源渠道, 上传人与失败步骤, contracts 加版本号."""

from alembic import op

from backend.persistence.models import (
    account_category_history,
    data_checks,
    data_imports,
    data_requests,
    extraction_versions,
)

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

NEW_TABLES = (data_imports, data_requests, data_checks, account_category_history, extraction_versions)


def upgrade() -> None:
    # 0001 按当前模型 create_all, 新库已有这些表与列 (并已开行级安全); 旧库补上. 各语句都可重复执行
    bind = op.get_bind()
    for table in NEW_TABLES:
        table.create(bind, checkfirst=True)
        op.execute(f"ALTER TABLE {table.name} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table.name} FORCE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS engagement_isolation ON {table.name}")
        op.execute(
            f"CREATE POLICY engagement_isolation ON {table.name} "
            "USING (engagement_id = current_setting('app.engagement_id', true)) "
            "WITH CHECK (engagement_id = current_setting('app.engagement_id', true))"
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table.name} TO audit_app")
    op.execute("ALTER TABLE dataset_loads ADD COLUMN IF NOT EXISTS ready_at TIMESTAMPTZ")
    op.execute("ALTER TABLE dataset_loads ADD COLUMN IF NOT EXISTS ready_by TEXT")
    # 已载入的项目都是数据集整套载入, 视同已确认数据已齐 (同 TableLoader), 升级后 P1 不会停下等数据
    op.execute("UPDATE dataset_loads SET ready_at = loaded_at, ready_by = 'dataset' WHERE ready_at IS NULL")
    op.execute("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS category_basis TEXT")
    op.execute("ALTER TABLE journal_entries ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ")
    op.execute("ALTER TABLE journal_entries ALTER COLUMN created_at DROP NOT NULL")
    op.execute("ALTER TABLE journal_entries ALTER COLUMN created_by DROP NOT NULL")
    op.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS media_type TEXT NOT NULL DEFAULT 'application/pdf'")
    op.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS channel TEXT NOT NULL DEFAULT 'auditor_upload'")
    op.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS uploaded_by TEXT")
    op.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS failed_step TEXT")
    # 已有的回函由函证平台直接回给审计师 (CSH-04 登记)
    op.execute("UPDATE documents SET channel = 'bank_direct' WHERE type = 'confirmation'")
    op.execute("ALTER TABLE contracts ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1")


def downgrade() -> None:
    for table in reversed(NEW_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table.name}")
    op.execute("ALTER TABLE contracts DROP COLUMN IF EXISTS version")
    for column in ("failed_step", "uploaded_by", "channel", "media_type"):
        op.execute(f"ALTER TABLE documents DROP COLUMN IF EXISTS {column}")
    op.execute("ALTER TABLE journal_entries DROP COLUMN IF EXISTS approved_at")
    op.execute("ALTER TABLE accounts DROP COLUMN IF EXISTS category_basis")
    op.execute("ALTER TABLE dataset_loads DROP COLUMN IF EXISTS ready_by")
    op.execute("ALTER TABLE dataset_loads DROP COLUMN IF EXISTS ready_at")
