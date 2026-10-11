"""Idempotently create the configured demo auditor; never reset an existing password."""

import asyncio
from uuid import uuid4

from sqlalchemy.dialects.postgresql import insert

from backend.core.config import get_settings
from backend.core.security import hash_password
from backend.persistence.db import default_engine
from backend.persistence.models import users
from backend.persistence.stores import now


async def seed() -> None:
    settings = get_settings()
    engine = default_engine()
    try:
        async with engine.begin() as conn:
            await conn.execute(
                insert(users)
                .values(
                    id=f"USR-{uuid4()}",
                    email=settings.SEED_USER_EMAIL,
                    full_name="Demo Auditor",
                    password_hash=hash_password(settings.SEED_USER_PASSWORD),
                    created_at=now(),
                )
                .on_conflict_do_nothing(index_elements=["email"])
            )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
