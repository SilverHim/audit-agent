"""检查点: 序列化白名单与连接. setup() 只在迁移阶段以属主角色执行 (开发接口文档.md 第 9 节)."""

import inspect
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

import backend.contracts.models
import backend.contracts.services
from backend.core.config import Settings

_STATE_MODULES = (backend.contracts.models, backend.contracts.services)


def checkpoint_serde() -> JsonPlusSerializer:
    """状态里的 Pydantic 对象须登记, 否则反序列化会被拒绝."""
    allowed = [
        obj
        for module in _STATE_MODULES
        for _, obj in inspect.getmembers(module, inspect.isclass)
        if issubclass(obj, BaseModel) and obj.__module__ == module.__name__
    ]
    return JsonPlusSerializer(allowed_msgpack_modules=allowed)


@asynccontextmanager
async def open_checkpointer(settings: Settings) -> AsyncIterator[BaseCheckpointSaver[str]]:
    if settings.CHECKPOINT_BACKEND == "memory":
        yield InMemorySaver(serde=checkpoint_serde())
        return
    async with AsyncConnectionPool(
        settings.DATABASE_URL,
        kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": 0},
        open=False,
    ) as pool:
        yield AsyncPostgresSaver(pool, serde=checkpoint_serde())  # type: ignore[arg-type]


async def setup_checkpoint_tables(owner_url: str) -> None:
    """迁移阶段调用: 以属主角色建检查点表."""
    async with AsyncPostgresSaver.from_conn_string(owner_url) as saver:
        await saver.setup()


if __name__ == "__main__":
    import asyncio

    from backend.core.config import get_settings

    if sys.argv[1:] == ["setup"]:
        asyncio.run(setup_checkpoint_tables(get_settings().DATABASE_OWNER_URL))
