"""文件渠道加 registry (需求 R306): 程序向登记机构查询后存档的文件 (GEN-08 的摘录与人员检索结果) 记这一渠道.

documents.channel 是文本列, 不用改表; 只把已有的程序存档改过来: 类型 registry_extract, 渠道 auditor_upload 且没有上传人
的 (审计师上传的都有上传人; 数据集载入只存合同, 不存摘录) 改为 registry. 在 all_projects 块内改各项目的行 (需求 R283).
降级改回 auditor_upload. 可重复执行.
"""

from alembic import op

from backend.persistence.migration_tools import all_projects

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with all_projects("documents"):
        op.execute(
            "UPDATE documents SET channel = 'registry' "
            "WHERE type = 'registry_extract' AND channel = 'auditor_upload' AND uploaded_by IS NULL"
        )


def downgrade() -> None:
    with all_projects("documents"):
        op.execute("UPDATE documents SET channel = 'auditor_upload' WHERE channel = 'registry'")
