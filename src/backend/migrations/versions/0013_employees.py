"""工资册 (需求 R96): 新表 employees (账表种类 TableKind.employees), 行级安全同 0004 的新表. 新表, 不回填数据."""

from alembic import op

from backend.persistence.models import FINANCIAL

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 按当前模型 create_all, 新库已有这张表 (并已开行级安全); 旧库补上. 各语句都可重复执行
    FINANCIAL["employees"].create(op.get_bind(), checkfirst=True)
    op.execute("ALTER TABLE employees ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE employees FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS engagement_isolation ON employees")
    op.execute(
        "CREATE POLICY engagement_isolation ON employees "
        "USING (engagement_id = current_setting('app.engagement_id', true)) "
        "WITH CHECK (engagement_id = current_setting('app.engagement_id', true))"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON employees TO audit_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS employees")
