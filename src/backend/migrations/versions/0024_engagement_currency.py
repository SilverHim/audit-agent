"""项目的记账币种 (需求 R366, Issue #66): engagements 加 currency (ISO 4217 代码) 与 currency_source (default, xero,
auditor), 之前的项目为澳元, 来源 default (还没确认). 金额照旧按 "分" 存, 不改数据.

新库 0001 按当前模型建表时已带这两列 (缺省同上), 这里跳过; 旧库补上. 可重复执行; 降级删掉这两列.
"""

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE engagements ADD COLUMN IF NOT EXISTS currency TEXT NOT NULL DEFAULT 'AUD'")
    op.execute("ALTER TABLE engagements ADD COLUMN IF NOT EXISTS currency_source TEXT NOT NULL DEFAULT 'default'")


def downgrade() -> None:
    op.execute("ALTER TABLE engagements DROP COLUMN IF EXISTS currency_source")
    op.execute("ALTER TABLE engagements DROP COLUMN IF EXISTS currency")
