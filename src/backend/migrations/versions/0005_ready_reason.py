"""确认数据已齐时的理由与当时最近一次体检不通过的项 (需求 R273): dataset_loads 加 ready_reason, ready_failed_checks.
已有的确认行没有理由, 未通过项为空."""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 按当前模型 create_all, 新库已有这两列; 旧库补上. 各语句都可重复执行
    op.execute("ALTER TABLE dataset_loads ADD COLUMN IF NOT EXISTS ready_reason TEXT")
    op.execute("ALTER TABLE dataset_loads ADD COLUMN IF NOT EXISTS ready_failed_checks JSONB NOT NULL DEFAULT '[]'")


def downgrade() -> None:
    op.execute("ALTER TABLE dataset_loads DROP COLUMN IF EXISTS ready_failed_checks")
    op.execute("ALTER TABLE dataset_loads DROP COLUMN IF EXISTS ready_reason")
