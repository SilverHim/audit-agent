"""应付账单的期末未付明细 (需求 R178): 新表 supplier_bills (账表种类 TableKind.supplier_bills), 行级安全同 0004 的新表.
新表, 不回填数据 (已载入 Xero 数据的项目再同步一次或重新导入即有)."""

from alembic import op

from backend.persistence.models import FINANCIAL

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 按当前模型 create_all, 新库已有这张表 (并已开行级安全); 旧库补上. 各语句都可重复执行
    FINANCIAL["supplier_bills"].create(op.get_bind(), checkfirst=True)
    op.execute("ALTER TABLE supplier_bills ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE supplier_bills FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS engagement_isolation ON supplier_bills")
    op.execute(
        "CREATE POLICY engagement_isolation ON supplier_bills "
        "USING (engagement_id = current_setting('app.engagement_id', true)) "
        "WITH CHECK (engagement_id = current_setting('app.engagement_id', true))"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON supplier_bills TO audit_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS supplier_bills")
