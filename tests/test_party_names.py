"""需求 R132: 往来名称规范化 party_name_key. Python 实现 (services/party_names.py) 与迁移 0018 的 SQL 函数逐步相同,
同一组名称 (CASES) 两侧结果相同且等于预期; 迁移在已有数据的库上可执行, 可重复执行, 接在 0017 之后. 连库的用例标
slow, 没有 TEST_DATABASE_URL (迁移往返另要 TEST_DATABASE_OWNER_URL) 时跳过."""

import os
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.contracts.records import DatasetTraits
from backend.services.party_names import party_name_key

ROOT = Path(__file__).resolve().parents[1]
PREFIXES = DatasetTraits().counterparty_prefixes  # payment to, payment from, payment: (设计稿第 5.2 节)
SUFFIXES = ["pty ltd", "pty limited", "pty", "ltd", "limited"]  # GEN-05 的 entity_suffixes

CASES: list[tuple[str | None, list[str] | None, list[str] | None, str | None]] = [  # 名称, 前缀, 后缀, 预期
    ("Victoria Property Agency Pty Ltd", PREFIXES, SUFFIXES, "victoria property agency"),  # 验收 2 的三例
    ("Payment: Hamilton Smith Pty", PREFIXES, SUFFIXES, "hamilton smith"),
    ("Payment to Odette Garrison", PREFIXES, [], "odette garrison"),
    ("Payment Tools Pty. Limited", PREFIXES, SUFFIXES, "payment tools"),  # 前缀须整词; 后缀先规范化, 取最长的
    (" PAYMENT FROM  Yarra--Transport ", PREFIXES, None, "yarra transport"),  # 大写, 连续分隔符, 首尾空格; 后缀为空
    ("Limited", None, SUFFIXES, "limited"),  # 只剩后缀时不去
    ("Café Ltd", PREFIXES, SUFFIXES, "caf"),  # 只认 ASCII 字母数字, 两侧不随区域设置变化
    ("payment:", PREFIXES, SUFFIXES, ""),
    (None, PREFIXES, SUFFIXES, None),
]


@pytest.mark.parametrize(("name", "prefixes", "suffixes", "expected"), CASES)
def test_python_key(
    name: str | None, prefixes: list[str] | None, suffixes: list[str] | None, expected: str | None
) -> None:
    assert party_name_key(name, prefixes, suffixes) == expected


def test_migration_0018_follows_0017_on_the_single_head() -> None:
    """down_revision 为合入时的 head (0017), 迁移链仍只有一个 head 且经过 0018."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    [head] = script.get_heads()
    revision = script.get_revision("0018")
    assert revision is not None and revision.down_revision == "0017"
    assert "0018" in {r.revision for r in script.walk_revisions("base", head)}


def database_url(name: str) -> str:
    url = os.getenv(name)
    if not url:
        pytest.skip(f"需设 {name}: 已迁移的测试库")
    return url.replace("postgresql://", "postgresql+psycopg://")


KEY = text("SELECT party_name_key(:name, CAST(:prefixes AS text[]), CAST(:suffixes AS text[])) AS key")


@pytest.mark.slow
async def test_sql_function_matches_python() -> None:
    """应用角色 (规则 SQL 的执行者) 调 SQL 函数: 每个名称的结果与 Python 相同, 都等于预期."""
    engine = create_async_engine(database_url("TEST_DATABASE_URL"))
    try:
        async with engine.connect() as conn:
            found = [
                (await conn.execute(KEY, {"name": name, "prefixes": prefixes, "suffixes": suffixes})).scalar_one()
                for name, prefixes, suffixes, _ in CASES
            ]
    finally:
        await engine.dispose()
    assert found == [party_name_key(n, p, s) for n, p, s, _ in CASES] == [expected for *_, expected in CASES]


@pytest.mark.slow
async def test_migration_0018_round_trip() -> None:
    """以属主角色在一个事务里走 0018 降级再升级两次, 最后回滚 (不动测试库): 降级后函数不在 (同 0017 的已有库), 升级
    后可调, 再升级一次照常 (CREATE OR REPLACE)."""
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    from tests.test_data_store_postgres import migration

    engine = create_async_engine(database_url("TEST_DATABASE_OWNER_URL"))
    module = migration("0018_party_name_key")

    def run(step: str) -> Any:
        def call(sync_conn: Any) -> None:
            with Operations.context(MigrationContext.configure(sync_conn)):
                getattr(module, step)()

        return call

    exists = text("SELECT count(*) FROM pg_proc WHERE proname = 'party_name_key'")
    args = {"name": "Payment: Hamilton Smith Pty", "prefixes": PREFIXES, "suffixes": SUFFIXES}
    try:
        async with engine.connect() as conn:
            outer = await conn.begin()
            try:
                await conn.run_sync(run("downgrade"))
                assert (await conn.execute(exists)).scalar_one() == 0
                for _ in range(2):
                    await conn.run_sync(run("upgrade"))
                    assert (await conn.execute(KEY, args)).scalar_one() == "hamilton smith"
                assert (await conn.execute(exists)).scalar_one() == 1
            finally:
                await outer.rollback()
    finally:
        await engine.dispose()
