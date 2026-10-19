"""补交请求标 "客户无此类资料" (需求 R280): data_requests 加 resolved_by, unavailable_reason. 已有的请求两列为空.

降级: 先把 status 为 unavailable 的请求改回 open (0005 的代码只认 open, received, withdrawn), 再删两列. 标记的理由与
标记人随之丢失 (留痕里仍有), 再升级时两列为空, 这些请求为 open, 下次体检照常对照."""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 按当前模型 create_all, 新库已有这两列; 旧库补上. 各语句都可重复执行
    op.execute("ALTER TABLE data_requests ADD COLUMN IF NOT EXISTS resolved_by TEXT")
    op.execute("ALTER TABLE data_requests ADD COLUMN IF NOT EXISTS unavailable_reason TEXT")


def downgrade() -> None:
    # 表开了 FORCE 行级安全, 属主不设 app.engagement_id 时看不到任何行: 改全部项目的行时暂时关掉, 改完再开
    op.execute("ALTER TABLE data_requests NO FORCE ROW LEVEL SECURITY")
    op.execute("UPDATE data_requests SET status = 'open', resolved_at = NULL WHERE status = 'unavailable'")
    op.execute("ALTER TABLE data_requests FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE data_requests DROP COLUMN IF EXISTS unavailable_reason")
    op.execute("ALTER TABLE data_requests DROP COLUMN IF EXISTS resolved_by")
