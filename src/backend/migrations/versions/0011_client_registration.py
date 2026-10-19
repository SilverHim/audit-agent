"""新建客户 (需求 R288): 新建的客户没有数据集, engagements.dataset 与 dataset_loads.dataset 改为可空;
client_profiles 加 source (该版的来源: manual, dataset, update) 与 updated_by (写入的用户), 老版本两列为空 (未记录).

0001 按当前模型 create_all, 新库已是这样; 旧库在此补上. 各语句都可重复执行. 降级时没有数据集的项目 dataset 记为
空字符串后恢复非空约束.
"""

from alembic import op

from backend.persistence.migration_tools import all_projects

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE engagements ALTER COLUMN dataset DROP NOT NULL")
    op.execute("ALTER TABLE dataset_loads ALTER COLUMN dataset DROP NOT NULL")
    op.execute("ALTER TABLE client_profiles ADD COLUMN IF NOT EXISTS source TEXT")
    op.execute("ALTER TABLE client_profiles ADD COLUMN IF NOT EXISTS updated_by TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE client_profiles DROP COLUMN IF EXISTS updated_by")
    op.execute("ALTER TABLE client_profiles DROP COLUMN IF EXISTS source")
    with all_projects("dataset_loads"):  # 行级安全下属主也只看得到当前项目的行, 块内暂时放开
        op.execute("UPDATE dataset_loads SET dataset = '' WHERE dataset IS NULL")
    op.execute("ALTER TABLE dataset_loads ALTER COLUMN dataset SET NOT NULL")
    op.execute("UPDATE engagements SET dataset = '' WHERE dataset IS NULL")
    op.execute("ALTER TABLE engagements ALTER COLUMN dataset SET NOT NULL")
