"""数据变动记录 (需求 R274): 新表 data_changes 记 "哪个数据版本变了哪类数据" (DataInput), 程序过期按它判断.

已有项目按 data_imports 补记: 某数据版本有已提交的账表版本时记这些表, 套用 table_changes, 与在线写入同口径
(导入审批日志另记 journal_entries); 查不到种类的版本 (数据集载入, 改合同, 回函或科目分类, 更正分录载入) 记为全部
各类都变了, 与之前 "任何变动都让程序过期" 的口径一致. 补记在 all_projects 块内做 (需求 R283: 块内暂时关掉 FORCE
行级安全, 属主看得到各项目的行, 退出时恢复), 不设 app.engagement_id.
"""

from alembic import op
from sqlalchemy import text

from backend.contracts.data import DataInput, TableKind, table_changes
from backend.persistence.migration_tools import all_projects
from backend.persistence.models import data_changes

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def backfill_rows(eid: str, current: int, tables: dict[int, list[str]]) -> list[dict[str, object]]:
    """一个项目要补记的数据变动行: 版本 1 至 current 逐个, 有已提交账表版本的 (tables: 版本 → 表种类) 记
    table_changes(这些表), 其余记全部各类. 纯函数, 测试直接调."""
    every = [i.value for i in DataInput]
    return [
        {"eid": eid, "version": version, "input": value}
        for version in range(1, current + 1)
        for value in (
            [i.value for i in table_changes(TableKind(k) for k in tables[version])] if version in tables else every
        )
    ]


def upgrade() -> None:
    # 0001 按当前模型 create_all, 新库已有这张表 (并已开行级安全); 旧库补上. 各语句都可重复执行
    bind = op.get_bind()
    data_changes.create(bind, checkfirst=True)
    name = data_changes.name
    op.execute(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {name} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS engagement_isolation ON {name}")
    op.execute(
        f"CREATE POLICY engagement_isolation ON {name} "
        "USING (engagement_id = current_setting('app.engagement_id', true)) "
        "WITH CHECK (engagement_id = current_setting('app.engagement_id', true))"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {name} TO audit_app")
    with all_projects(name, "dataset_loads", "data_imports"):
        loads = bind.execute(
            text("SELECT engagement_id, data_version FROM dataset_loads ORDER BY engagement_id")
        ).all()
        tables: dict[str, dict[int, list[str]]] = {}
        for eid, version, kind in bind.execute(
            text(
                "SELECT engagement_id, data_version, kind FROM data_imports WHERE status = 'committed' "
                "AND data_version IS NOT NULL AND kind IS NOT NULL"
            )
        ).all():
            tables.setdefault(str(eid), {}).setdefault(int(version), []).append(str(kind))
        rows = [row for eid, current in loads if current for row in backfill_rows(eid, int(current), tables.get(eid, {}))]
        if rows:
            bind.execute(
                text(
                    f"INSERT INTO {name} (engagement_id, data_version, input) VALUES (:eid, :version, :input) "
                    "ON CONFLICT DO NOTHING"
                ),
                rows,
            )


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS {data_changes.name}")
