"""股权与任职关系图 (需求 R136): 新表 ownership_graphs (审计产出 OwnershipGraph, persistence.models.OUTPUT_KINDS),
行级安全同 0014 的新表. 新表, 不回填数据."""

from alembic import op

from backend.persistence.models import OUTPUTS

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 按当前模型 create_all, 新库已有这张表 (并已开行级安全); 旧库补上. 各语句都可重复执行
    OUTPUTS["OwnershipGraph"].create(op.get_bind(), checkfirst=True)
    op.execute("ALTER TABLE ownership_graphs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE ownership_graphs FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS engagement_isolation ON ownership_graphs")
    op.execute(
        "CREATE POLICY engagement_isolation ON ownership_graphs "
        "USING (engagement_id = current_setting('app.engagement_id', true)) "
        "WITH CHECK (engagement_id = current_setting('app.engagement_id', true))"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ownership_graphs TO audit_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ownership_graphs")
