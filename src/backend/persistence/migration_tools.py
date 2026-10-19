"""迁移共用工具 (需求 R283)."""

from collections.abc import Iterator
from contextlib import contextmanager

from alembic import op

from backend.persistence.models import SCOPED


@contextmanager
def all_projects(*tables: str) -> Iterator[None]:
    """在迁移里改各项目已有的行. 带 engagement_id 的表开了 FORCE 行级安全 (接口文档第 9 节), 属主角色 (不是超级用户,
    没有 BYPASSRLS) 不设 app.engagement_id 时一行都看不到, 改数据的语句安静地改到 0 行, 不报错. 块内对给定的表 (含
    语句里子查询读的表) 暂时 NO FORCE ROW LEVEL SECURITY, 属主不受策略约束; 退出时恢复 FORCE.
    不写 try/finally: 迁移在一个事务里, 块内出错时 NO FORCE 随事务一起回滚; 在已中止的事务里再执行 FORCE 反而会盖住
    原来的错误. 只收按项目隔离的表 (persistence.models.SCOPED), 写错表名抛 ValueError."""
    scoped = {table.name for table in SCOPED}
    unknown = [name for name in tables if name not in scoped]
    if unknown:
        raise ValueError(f"不是按项目隔离的表: {', '.join(unknown)}")
    for name in tables:
        op.execute(f"ALTER TABLE {name} NO FORCE ROW LEVEL SECURITY")
    yield
    for name in tables:
        op.execute(f"ALTER TABLE {name} FORCE ROW LEVEL SECURITY")
