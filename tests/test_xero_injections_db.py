"""注入测试的连库断言 (设计稿 v2.1 第 8 节): 副本 (xero_case) 经补造清单写入测试库, 用真实 SQL 与存储跑现有程序.
本条 (需求 R121) 写 T0 基线副本. 设 TEST_DATABASE_URL 为已迁移的独立测试库 (没配时跳过); 要本地快照."""

from pathlib import Path
from typing import Any

import pytest

from tests.test_xero_rules_db import (
    build_xero_project,
    check_acc04,
    check_cash,
    check_csh02_fieldwork,
    check_planning,
    check_revenue,
    check_versions,
    database_url,
    run_rule_programs,
)
from tests.xero_fakes import manifest_documents, xero_uploads
from tests.xero_harness import db_deps

pytestmark = [pytest.mark.slow, pytest.mark.external]


async def test_t0_rules_on_postgres(xero_case: Any, tmp_path: Path) -> None:
    """验收 3 前半: R112 的断言函数以 T0 副本为数据根重跑 (账表按副本的补造清单写入测试库, 回函与登记读副本), 结果
    与原目录上的相同: 版本行, 计划阶段 (重要性与 PLN-07 A01, PLN-01 M01), 收入 (REV-08 R03, REV-02 S01), 现金
    (CSH-05, CSH-02 D02 至 D04), CSH-02 外勤 (只判断 BA-091), ACC-04."""
    case = xero_case("T0")
    url = database_url()
    project = await build_xero_project(url, case.snapshot, case.supplements, tmp_path / "rules")
    results = await run_rule_programs(project)
    await check_versions(project)
    await check_planning(project, results)
    await check_revenue(project, results)
    await check_cash(project, results)
    await check_csh02_fieldwork(project, case.supplements)
    await check_acc04(project)


async def test_t0_demo_import_on_postgres(xero_case: Any) -> None:
    """验收 3 后半: R118 的 db_deps 在 T0 副本上做完整的演示数据导入 (acceptance, fieldwork): 登记的 21 份文件与原目录
    清单相同 (文件 ID, 类型, 可靠性, 文件日期, 关联对象, 哈希与页数), 上传人为新建的负责人."""
    h = await db_deps(xero_case("T0"))
    entries = manifest_documents()
    planned = {u.document_id: u for u in xero_uploads() if u.stage in ("acceptance", "fieldwork")}
    documents = {d.id: d for d in await h.deps.financial_store.documents(h.engagement.id)}
    assert len(documents) == 21 and sorted(documents) == sorted(planned)
    for i, d in documents.items():
        u, entry = planned[i], entries[i]
        assert (d.type, d.reliability, d.document_date, d.related_ids, d.sha256, d.page_count, d.uploaded_by) == (
            u.document_type,
            u.reliability,
            u.document_date,
            u.related_ids,
            entry["sha256"],
            entry["page_count"],
            h.actor,
        ), d.file_name
