"""Idempotently create the configured demo auditor; never reset an existing password."""

import asyncio

from backend.core.config import get_settings
from backend.core.security import hash_password
from backend.persistence.db import default_engine
from backend.persistence.stores import PostgresUserStore

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


if __name__ == "__main__":
    asyncio.run(seed())
