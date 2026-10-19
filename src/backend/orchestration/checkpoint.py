"""检查点: 序列化白名单与连接. setup() 只在迁移阶段以属主角色执行 (开发接口文档.md 第 9 节)."""

import inspect
import sys
import types
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import Enum
from typing import Any, Union, get_args, get_origin, get_type_hints

from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

import backend.contracts.models
import backend.contracts.services
from backend.contracts.state import EngagementState
from backend.core.config import Settings

# 状态里出现的 Pydantic 类与枚举 (递归到字段类型) 只能定义在这些模块中; 新类型放在别的模块时须在此登记
# (tests/test_state_compat.py 守卫)
_STATE_MODULES = (backend.contracts.models, backend.contracts.services)


def allowed_types() -> list[type]:
    """检查点序列化白名单: _STATE_MODULES 中定义的 Pydantic 类与枚举 (不含从别处导入的)."""
    return [
        obj
        for module in _STATE_MODULES
        for _, obj in inspect.getmembers(module, inspect.isclass)
        if issubclass(obj, (BaseModel, Enum)) and obj.__module__ == module.__name__
    ]


def checkpoint_serde() -> JsonPlusSerializer:
    """状态里的 Pydantic 对象与枚举须登记, 否则反序列化会被拒绝.

    未登记的枚举不报错, 而是退化为普通字符串: 状态顶层的 phase 读回后 `is Phase.X` 恒为假.
    """
    return JsonPlusSerializer(allowed_msgpack_modules=allowed_types())


def top_level_enums(state: Any = EngagementState) -> dict[str, type[Enum]]:
    """状态顶层的枚举字段 (类型为某个枚举, 或枚举 | None) → 枚举类, 由注解推出; 现为 phase 与 opinion_route.
    含枚举的其他联合 (两个枚举, 枚举 | str) 读回时分不清该转成哪个, 抛 TypeError, 免得读回后静默留作字符串."""
    found: dict[str, type[Enum]] = {}
    for key, hint in get_type_hints(state).items():
        if get_origin(hint) in (Union, types.UnionType):
            members = [arg for arg in get_args(hint) if arg is not type(None)]
            if len(members) > 1 and any(isinstance(m, type) and issubclass(m, Enum) for m in members):
                raise TypeError(f"状态字段 {key} 的类型 {hint} 含枚举, 顶层枚举字段只支持 枚举 或 枚举 | None")
            hint = members[0] if len(members) == 1 else None
        if isinstance(hint, type) and issubclass(hint, Enum):
            found[key] = hint
    return found


TOP_LEVEL_ENUMS = top_level_enums()


def restore_enums(channels: dict[str, Any], enums: dict[str, type[Enum]] = TOP_LEVEL_ENUMS) -> None:
    """把读回为普通字符串的顶层枚举字段转回枚举 (原地修改); 缺键与 None 不动."""
    for key, kind in enums.items():
        if channels.get(key) is not None:
            channels[key] = kind(channels[key])


class AuditPostgresSaver(AsyncPostgresSaver):
    """Postgres 检查点把值为 str, int 等基本类型的通道直接存进 JSON, 不经 msgpack 与白名单, 枚举读回成了字符串.

    在读取检查点的统一入口 (最新检查点与历史都经过这里) 按 TOP_LEVEL_ENUMS 转回枚举.
    """

    async def _load_checkpoint_tuple(self, value: DictRow) -> CheckpointTuple:
        checkpoint = await super()._load_checkpoint_tuple(value)
        restore_enums(checkpoint.checkpoint["channel_values"])
        return checkpoint


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
        yield AuditPostgresSaver(pool, serde=checkpoint_serde())  # type: ignore[arg-type]


async def setup_checkpoint_tables(owner_url: str) -> None:
    """迁移阶段调用: 以属主角色建检查点表."""
    async with AsyncPostgresSaver.from_conn_string(owner_url) as saver:
        await saver.setup()


if __name__ == "__main__":
    import asyncio

    from backend.core.config import get_settings

    if sys.argv[1:] == ["setup"]:
        asyncio.run(setup_checkpoint_tables(get_settings().DATABASE_OWNER_URL))
