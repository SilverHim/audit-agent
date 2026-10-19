"""补回 0004 在 FORCE 行级安全下漏改的数据 (需求 R283). 0004 的两条 UPDATE 以属主角色执行, 不设 app.engagement_id 时
一行都看不到, 在已有数据的库上没生效; 0004 本身不改 (已执行过的迁移不改), 在这里按能区分的条件补:

- dataset_loads: ready_at 为空, 且本项目没有 data_version 1 的已提交 data_imports 行的, 补 ready_at = loaded_at,
  ready_by = 'dataset' (0004 之前的数据集载入, 视同已确认数据已齐). 0004 之后经导入或同步接入数据的项目, 第一次
  写入一定记一行 data_version 1 的已提交版本, 不动; 0004 之后的数据集载入自己写了 ready_at.
- documents: type 为 confirmation, channel 为 auditor_upload 且没有上传人的, 改为 bank_direct (0004 之前 CSH-04
  存档的银行直达回函). 审计师上传的文件一律有上传人; 之后存档的回函直接写 bank_direct.

新库上是空操作, 可重复执行. 降级不改数据: 补上的值与之后正常产生的分不开, 也不影响 0006 及以前的代码."""

from alembic import op

from backend.persistence.migration_tools import all_projects

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with all_projects("dataset_loads", "data_imports", "documents"):  # data_imports 在子查询里读, 一并关掉
        op.execute(
            "UPDATE dataset_loads AS l SET ready_at = l.loaded_at, ready_by = 'dataset' "
            "WHERE l.ready_at IS NULL AND NOT EXISTS ("
            "SELECT 1 FROM data_imports AS i WHERE i.engagement_id = l.engagement_id "
            "AND i.status = 'committed' AND i.data_version = 1)"
        )
        op.execute(
            "UPDATE documents SET channel = 'bank_direct' "
            "WHERE type = 'confirmation' AND channel = 'auditor_upload' AND uploaded_by IS NULL"
        )


def downgrade() -> None:
    pass
