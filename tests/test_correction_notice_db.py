"""需求 R162 (slow): 更正通知生效 (nodes.apply_notice) 在 Postgres 上: 更正分录载入 (R271: 总账出新版本, 数据版本
+1) 与错报新版本在同一事务内写入, 提交后随即写总账新版本的 data 留痕 (sync, 写入人为提交通知的审计师). Xero q1 按
补造清单写进测试库 (R112 的 build_xero_project), 错报 M-D-XJ499, M-D-CSH02-091 为第 1 轮登记之后的 v3 communicated.
没有 TEST_DATABASE_URL 时跳过."""

from pathlib import Path

import pytest

from backend.contracts.data import TableKind
from backend.contracts.errors import VersionConflict
from backend.contracts.models import AgentId, Misstatement, MisstatementStatus, TrailKind
from backend.orchestration.nodes import apply_notice
from backend.services.trail_pages import all_of_kind
from tests.external import snapshot_dir, supplements_dir
from tests.test_correction_notice import notice
from tests.test_misstatement_communication import CSH02, XJ499, round1_communicated
from tests.test_xero_rules_db import XeroProject, build_xero_project, database_url
from tests.xero_fakes import SNAPSHOT_NAME

pytestmark = [pytest.mark.slow, pytest.mark.external]
AJE_LINES = ["AJE-X01-1", "AJE-X01-2", "AJE-X01-3", "AJE-X05-1", "AJE-X05-2"]


async def communicated_project(tmp: Path) -> tuple[XeroProject, list[Misstatement]]:
    """测试库中的 Xero 项目, Store 已有两条错报的 v1 至 v3 (v3 communicated)."""
    project = await build_xero_project(database_url(), snapshot_dir(SNAPSHOT_NAME), supplements_dir(SNAPSHOT_NAME), tmp)
    misstatements = round1_communicated()
    versions = [m.model_copy(update={"version": v}) for m in misstatements for v in (1, 2)]
    store, eid = project.deps.output_store, project.engagement.id
    async with store.transaction(eid):
        await store.put(eid, [*versions, *misstatements], actor=AgentId.A1)
    return project, misstatements


async def data_version(project: XeroProject) -> int:
    return max(v.data_version for v in await project.deps.data.versions(project.engagement.id))


async def test_notice_loads_entries_with_misstatements_and_writes_data_trail(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """验收 2: 载入 AJE-X01, AJE-X05: 总账出新版本 (数据版本 +1, 来源 xero, 写入人为提交人), 库里有这两笔的 5 行;
    两条错报 v4 verifying 写入 Store; 提交后随即有这一版的 data 留痕 (sync, 写入人为提交人)."""
    project, misstatements = await communicated_project(tmp_path_factory.mktemp("xero"))
    eid, owner = project.engagement.id, project.owner
    before = await data_version(project)

    written = await apply_notice(project.deps, project.engagement, misstatements, notice(decided_by=owner))

    assert [(m.id, m.version, m.status) for m in written] == [
        (XJ499, 4, MisstatementStatus.verifying),
        (CSH02, 4, MisstatementStatus.verifying),
    ]
    assert [m.version for m in await project.deps.output_store.latest(eid, Misstatement)] == [4, 4]
    [version] = [v for v in await project.deps.data.versions(eid) if v.data_version > before]
    assert (version.data_version, version.kind, version.source, version.created_by) == (
        before + 1,
        TableKind.journal_entries,
        "xero",
        owner,
    )
    lines = await project.deps.financial_store.journal_entries(eid, entry_nos=["AJE-X01", "AJE-X05"])
    assert [x.id for x in lines] == AJE_LINES
    entries = await all_of_kind(project.deps.trail, eid, TrailKind.data)
    synced = [e for e in entries if e.idempotency_key == f"{eid}:data:v{before + 1}:journal_entries"]
    assert [(e.actor, e.payload["action"]) for e in synced] == [(owner, "sync")]


async def test_misstatement_clash_rolls_back_the_load(tmp_path_factory: pytest.TempPathFactory) -> None:
    """一次审计师动作一个事务 (补充裁定 7): Store 里已有内容不同的 M-D-XJ499 v4 时整体回滚, 更正分录也没载入 (数据版本
    不变, 库里没有 AJE 行), 不写 data 留痕."""
    project, misstatements = await communicated_project(tmp_path_factory.mktemp("xero"))
    eid, owner = project.engagement.id, project.owner
    other = misstatements[0].model_copy(update={"version": 4, "management_response": "另一版"})
    async with project.deps.output_store.transaction(eid):
        await project.deps.output_store.put(eid, [other], actor=AgentId.A1)
    before = await data_version(project)

    with pytest.raises(VersionConflict, match=XJ499):
        await apply_notice(project.deps, project.engagement, misstatements, notice(decided_by=owner))

    assert await data_version(project) == before
    assert await project.deps.financial_store.journal_entries(eid, entry_nos=["AJE-X01", "AJE-X05"]) == []
    assert (await project.deps.output_store.get(eid, Misstatement, CSH02)).version == 3  # type: ignore[union-attr]
