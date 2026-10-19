"""种子脚本: 建演示审计师 (幂等, 不重置已有密码); 或给已建的项目载入其数据集.

    python -m backend.persistence.seed            # 建 SEED_USER_EMAIL 账号
    python -m backend.persistence.seed load <eid> # 载入项目的数据集 (如 demo), 视同已确认数据已齐

建项目不再自动载入财务数据 (需求 R10), 演示时可用第二条代替在数据管理页逐表导入. 项目正停在 P1 前等数据时,
载入后在数据管理页点 "确认数据已齐" (或重启应用由 recover 补做) 即继续.
"""

import asyncio
import sys
from typing import cast

from backend.core.config import get_settings
from backend.core.security import hash_password
from backend.persistence.db import default_engine
from backend.persistence.stores import PostgresFinancialDataStore, PostgresUserStore

SEED_USER_NAME = "Jordan Ellis"  # 种子审计师的姓名 (合成人名); RPT-03 报告签字块取项目负责人的姓名 (需求清单 Q15)


async def seed() -> None:
    """与注册同一写入口: 登录名去首尾空白并转小写, 已有同名账号 (不区分大小写) 时什么都不做."""
    settings = get_settings()
    engine = default_engine()
    try:
        await PostgresUserStore(engine).create(
            settings.SEED_USER_EMAIL.strip().lower(), SEED_USER_NAME, hash_password(settings.SEED_USER_PASSWORD)
        )
    finally:
        await engine.dispose()


async def load(eid: str) -> None:
    """按组装根的数据集载入器载入项目的数据集 (已载入则跳过); 项目不存在抛 LookupError; 项目没有数据集 (新建的
    客户) 时说明后跳过."""
    from backend.main import build_deps  # 在此导入: 组装根依赖本包, 放在开头会循环导入

    deps = build_deps(get_settings())
    engine = cast(PostgresFinancialDataStore, deps.financial_store).engine
    try:
        engagement = await deps.engagement_store.get(eid)
        if engagement is None:
            raise LookupError(f"Engagement {eid} not found")
        if engagement.dataset is None:
            print(f"项目 {eid} 的客户没有数据集, 不载入", file=sys.stderr)
            return
        await deps.datasets.load(eid, engagement.dataset)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    if sys.argv[1:2] == ["load"] and len(sys.argv) == 3:
        asyncio.run(load(sys.argv[2]))
    else:
        asyncio.run(seed())
