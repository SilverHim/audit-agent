"""科目表加标准分类列 (accounts.category), 未映射为空."""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 按当前模型建表, 新库已有此列; 旧库补上
    op.execute("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS category TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE accounts DROP COLUMN IF EXISTS category")
