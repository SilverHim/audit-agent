"""项目的工作语言 (需求 R339, Issue #51): engagements 加 working_language, 之前的项目为 en (那时的输出都是英文).

新库 0001 按当前模型建表时已带这一列 (缺省 en), 这里跳过; 旧库补上. 执行记录 (ProcedureRun.language) 与报告版本
(AuditReportVersion.language) 存在审计产出表的 JSON 里, 不用改表: 旧行没有这个键, 读回时取缺省 en. 可重复执行;
降级删掉这一列.
"""

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE engagements ADD COLUMN IF NOT EXISTS working_language TEXT NOT NULL DEFAULT 'en'")


def downgrade() -> None:
    op.execute("ALTER TABLE engagements DROP COLUMN IF EXISTS working_language")
