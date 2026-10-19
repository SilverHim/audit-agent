"""智能导入的批次与确认单 (需求 R277): 新表 import_batches, 行级安全同 0004 的新表. 新表, 不回填数据."""

from alembic import op

from backend.persistence.models import import_batches

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 按当前模型 create_all, 新库已有这张表 (并已开行级安全); 旧库补上. 各语句都可重复执行
    import_batches.create(op.get_bind(), checkfirst=True)
    op.execute("ALTER TABLE import_batches ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE import_batches FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS engagement_isolation ON import_batches")
    op.execute(
        "CREATE POLICY engagement_isolation ON import_batches "
        "USING (engagement_id = current_setting('app.engagement_id', true)) "
        "WITH CHECK (engagement_id = current_setting('app.engagement_id', true))"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON import_batches TO audit_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS import_batches")
