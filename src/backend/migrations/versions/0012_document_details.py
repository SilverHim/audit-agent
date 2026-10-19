"""文件的补登记 (需求 R90): documents 加 document_date (文件日期, 可空) 与 related_ids (关联对象 ID, 缺省 []).

只加列, 不改已有数据: 已登记的文件读回文件日期为空, 关联对象为 []. 0001 按当前模型 create_all, 新库已有这两列;
旧库在此补上. 各语句都可重复执行.
"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS document_date DATE")
    op.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS related_ids JSONB NOT NULL DEFAULT '[]'")


def downgrade() -> None:
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS related_ids")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS document_date")
