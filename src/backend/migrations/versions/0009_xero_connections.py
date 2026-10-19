"""Xero 网页连接表 xero_connections (需求 R279): 按项目存连接与加密令牌, 行级安全同其他项目表."""

from alembic import op

from backend.persistence.models import xero_connections

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 按当前模型 create_all, 新库已有这张表 (并已开行级安全); 旧库补上. 各语句都可重复执行
    xero_connections.create(op.get_bind(), checkfirst=True)
    name = xero_connections.name
    op.execute(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {name} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS engagement_isolation ON {name}")
    op.execute(
        f"CREATE POLICY engagement_isolation ON {name} "
        "USING (engagement_id = current_setting('app.engagement_id', true)) "
        "WITH CHECK (engagement_id = current_setting('app.engagement_id', true))"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {name} TO audit_app")


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS {xero_connections.name}")
