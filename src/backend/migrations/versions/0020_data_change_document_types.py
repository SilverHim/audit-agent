"""数据变动记文件类型 (需求 R304): data_changes 加 document_types (JSONB, 可空), 记 documents 变动时登记变了的文件
的类型, 过期判断据此只让读该类文件登记的程序过期.

只加列, 不改已有数据: 之前记的 documents 变动为空, 即不知道是哪类文件, 照旧按 reads 含 documents 判过期. 0001 按
当前模型 create_all, 新库已有这一列; 旧库在此补上. 各语句都可重复执行.
"""

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE data_changes ADD COLUMN IF NOT EXISTS document_types JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE data_changes DROP COLUMN IF EXISTS document_types")
