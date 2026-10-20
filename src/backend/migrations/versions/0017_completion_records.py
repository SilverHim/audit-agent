"""签字前提的四类审计产出 (需求 R215): 新表 completion_records, 存声明书, 合规检查, 治理层沟通与独立性声明
(persistence.models.OUTPUT_KINDS), 行级安全同 0016 的新表. 新表, 不回填数据."""

from alembic import op

from backend.persistence.models import OUTPUTS

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 按当前模型 create_all, 新库已有这张表 (并已开行级安全); 旧库补上. 各语句都可重复执行
    OUTPUTS["ComplianceCheck"].create(op.get_bind(), checkfirst=True)
    op.execute("ALTER TABLE completion_records ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE completion_records FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS engagement_isolation ON completion_records")
    op.execute(
        "CREATE POLICY engagement_isolation ON completion_records "
        "USING (engagement_id = current_setting('app.engagement_id', true)) "
        "WITH CHECK (engagement_id = current_setting('app.engagement_id', true))"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON completion_records TO audit_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS completion_records")
