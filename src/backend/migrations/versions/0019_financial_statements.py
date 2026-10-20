"""待审财报 (需求 R217): 新表 financial_statements (每版一行) 与 financial_statement_lines (各版的行), 行级安全同
0013 的新表. 新表, 不回填数据 (财报只由上传后的抽取写入)."""

from alembic import op

from backend.persistence.models import financial_statement_lines, financial_statements

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None
TABLES = (financial_statements, financial_statement_lines)


def upgrade() -> None:
    # 0001 按当前模型 create_all, 新库已有这两张表 (并已开行级安全); 旧库补上. 各语句都可重复执行
    for table in TABLES:
        table.create(op.get_bind(), checkfirst=True)
        op.execute(f"ALTER TABLE {table.name} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table.name} FORCE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS engagement_isolation ON {table.name}")
        op.execute(
            f"CREATE POLICY engagement_isolation ON {table.name} "
            "USING (engagement_id = current_setting('app.engagement_id', true)) "
            "WITH CHECK (engagement_id = current_setting('app.engagement_id', true))"
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table.name} TO audit_app")


def downgrade() -> None:
    for table in reversed(TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table.name}")
