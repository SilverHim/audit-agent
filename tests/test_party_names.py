"""需求 R132: 往来名称规范化 party_name_key. Python 实现 (services/party_names.py) 与迁移 0018 的 SQL 函数逐步相同,
同一组名称 (CASES) 两侧结果相同且等于预期, 随机名称 (含带重音与非拉丁字符的) 上两侧也逐个相同; 迁移在已有数据的
库上可执行, 可重复执行, 接在 0017 之后. 连库的用例标 slow, 没有 TEST_DATABASE_URL (迁移往返另要
TEST_DATABASE_OWNER_URL) 时跳过."""

import os
import random
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
    ("Payment to Odette", ["payment", "payment to"], None, "odette"),  # 前缀取最长的, 不取列表中第一个匹配的
    ("Foo Pty Ltd", None, ["ltd", "pty ltd"], "foo"),  # 后缀同样取最长的
    ("Café Ltd", PREFIXES, SUFFIXES, "cafe"),  # NFKD 分解后去掉附加符, 同 "Cafe Ltd"
    ("Société Générale", None, None, "societe generale"),  # 附加符在词中间也不拆词
    ("Straße Pty", None, SUFFIXES, "stra e"),  # 分解后仍不是 ASCII 的字母当分隔符
    # 没有可比的名称本身时为 None (SQL 为 NULL), 不与任何名称相等 (审核 R132-05)
    ("王小明有限公司", PREFIXES, SUFFIXES, None),  # 全是非拉丁字符
    ("王小明 Pty Ltd", PREFIXES, SUFFIXES, None),  # 只剩公司后缀
    ("Limited", None, SUFFIXES, None),
    ("payment:", PREFIXES, SUFFIXES, None),  # 只剩前缀
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


def random_names(count: int) -> list[str | None]:
    """固定种子的随机名称: 公司名用词, 前缀与后缀词, 标点与空白, 带重音与组合附加符的拉丁字母, 非拉丁字母 (中文,
    西里尔, 希腊), 全角字母与不分解的 ß."""
    rng = random.Random(132)
    words = ["Payment", "to", "from", "payment:", "Pty", "Ltd", "Limited", "PTY.", "ltd.", "Hamilton", "Smith", "Co"]
    letters = [*"abcXYZ019", "é", "É", "ñ", "Ş", "İ", "ø", "ß", "e\u0301", "中", "王", "Ж", "Ω", "Ａ", "ﬁ"]
    separators = [" ", "", "  ", "-", ": ", ".", "/", "&", "\t"]
    names: list[str | None] = [None]
    for _ in range(count):
        parts = []
        for _ in range(rng.randint(1, 6)):
            word = rng.choice(words) if rng.random() < 0.5 else "".join(rng.choices(letters, k=rng.randint(1, 5)))
            parts += [word, rng.choice(separators)]
        names.append("".join(parts))
    return names


@pytest.mark.slow
@pytest.mark.parametrize(
    ("prefixes", "suffixes"),
    [(PREFIXES, SUFFIXES), (["payment", "Payment to", "pay:"], ["ltd", "Pty Ltd", "co"]), (None, [])],
)
async def test_sql_function_matches_python_on_random_names(prefixes: list[str] | None, suffixes: list[str]) -> None:
    """审核 R132-05: 400 个随机名称 (random_names) 上 SQL 函数与 Python 逐个相同; 前缀与后缀各一组短的排在前面,
    一组不去."""
    names = random_names(400)
    query = text(
        "SELECT party_name_key(n, CAST(:prefixes AS text[]), CAST(:suffixes AS text[])) "
        "FROM unnest(CAST(:names AS text[])) WITH ORDINALITY AS u(n, i) ORDER BY i"
    )
    engine = create_async_engine(database_url("TEST_DATABASE_URL"))
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(query, {"names": names, "prefixes": prefixes, "suffixes": suffixes})
            found = [row[0] for row in rows]
    finally:
        await engine.dispose()
    expected = [party_name_key(name, prefixes, suffixes) for name in names]
    assert [(n, f) for n, f, e in zip(names, found, expected, strict=True) if f != e] == []


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
