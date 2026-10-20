"""注入测试的连库断言 (设计稿 v2.1 第 8 节): 副本 (xero_case) 经补造清单写入测试库, 用真实 SQL 与存储跑现有程序,
检真实 SQL 与取数能否把注入的事实带到判断面前 (大模型仍读 fixture). T0 基线副本 (需求 R121), T1 收入截止 (需求
R122), T4 银行回函余额差异 (需求 R123). 例外按 (规则, 对象 ID) 比, 不按序号. 设 TEST_DATABASE_URL 为已迁移的
独立测试库 (没配时跳过); 要本地快照."""

from pathlib import Path
from typing import Any

import pytest

from backend.contracts.services import RuleResult
from tests.test_xero_injections import LLM_INJECTED, T1_RULES, t4_rules
from tests.test_xero_rules_db import (
    build_xero_project,
    check_acc04,
    check_cash,
    check_csh02_fieldwork,
    check_planning,
    check_revenue,
    check_versions,
    database_url,
    offline,
    run_rule_programs,
)
from tests.xero_fakes import ORC1026, manifest_documents, xero_uploads
from tests.xero_harness import LayeredMockLLMClient, db_deps, request_text, run_programs

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


def by_rule(result: RuleResult) -> tuple[dict[tuple[str | None, tuple[str, ...]], dict[str, Any]], dict[str, Any]]:
    """规则结果按 (规则, 对象 ID) 取例外 (不含例外 ID, 其序号随命中条数变) 与各规则的行."""
    found = {(x.rule_id, tuple(x.source_ids)): x.model_dump(exclude={"id"}) for x in result.exceptions}
    assert len(found) == len(result.exceptions)
    return found, result.rows


async def test_t1_rules_on_postgres(xero_case: Any, tmp_path: Path) -> None:
    """R122 验收 3 前半: T1 副本上真实 SQL 的 REV-08 恰有 R01 (ORC1026, 715000, posting_date 09-17, signed_date
    10-02) 与 R03 (330000) 各一条, 没有 R02, 与离线预置 T1_RULES 相同; PLN-07, PLN-01, CSH-05, CSH-02, REV-02 与 T0
    (XERO_RULES) 逐条相同."""
    case = xero_case("T1")
    project = await build_xero_project(database_url(), case.snapshot, case.supplements, tmp_path / "rules")
    results = await run_rule_programs(project)
    run, rev08 = results["REV-08"]
    assert by_rule(rev08) == by_rule(T1_RULES["REV-08"](run))
    assert sorted(by_rule(rev08)[0]) == [("R01", (ORC1026,)), ("R03", (ORC1026,))]
    for procedure_id in ("PLN-07", "PLN-01", "CSH-05", "CSH-02", "REV-02"):
        run, found = results[procedure_id]
        assert by_rule(found) == by_rule(offline(procedure_id, run)), procedure_id


async def test_t1_rev02_request_carries_the_completion_date(xero_case: Any) -> None:
    """R122 验收 3 后半: 连库测试台上 REV-02 对 ORC1026 的请求, T1 含服务完成日 2026-10-02 与分录行 XJ-580-1 (Sales
    贷方 650000, 即带符号的 -650000), T0 的同一请求含 2026-09-17 而不含 2026-10-02."""
    texts = {}
    for name, roots in (("T1", [LLM_INJECTED / "T1"]), ("T0", [])):
        case = xero_case(name, base=name.lower())
        client = LayeredMockLLMClient([*roots, case.supplements / "llm"])
        await run_programs(await db_deps(case, llm=client), ["REV-02"])
        [request] = [r for r in client.requests if (r.procedure_id, r.fixture_key) == ("REV-02", ORC1026)]
        texts[name] = request_text(request)
    assert all(
        text in texts["T1"] for text in ('"signed_date": "2026-10-02"', '"id": "XJ-580-1"', '"credit_cents": 650000')
    )
    assert "2026-09-17" in texts["T0"] and "2026-10-02" not in texts["T0"]


async def test_t4_rules_on_postgres(xero_case: Any, tmp_path: Path) -> None:
    """R123 验收 3: T4 副本上真实 SQL 的 CSH-05 恰有 B01 一条 (BA-090, -55000 = 对账单 -1090044 减账面 -1035044), 与
    离线预置相同; CSH-02 的候选行与按副本派生的相同, D02 含 BT-090-0081, D03 含 XJ-637-2; REV-08 (R03) 同 T0; PLN-07
    A01 为 8 条, 不再有 090 (变动 -164724 不到期初绝对值的 20%), 610 为 964950."""
    case = xero_case("T4")
    project = await build_xero_project(database_url(), case.snapshot, case.supplements, tmp_path / "rules")
    results = await run_rule_programs(project)
    rules = t4_rules(case)
    for procedure_id in ("CSH-05", "CSH-02"):
        run, found = results[procedure_id]
        assert by_rule(found) == by_rule(rules[procedure_id](run)), procedure_id
    _, csh05 = results["CSH-05"]
    assert [(x.rule_id, x.source_ids, x.amount_cents) for x in csh05.exceptions] == [("B01", ["BA-090"], -55000)]
    _, csh02 = results["CSH-02"]
    assert "BT-090-0081" in {r["id"] for r in csh02.rows["D02"]}
    assert "XJ-637-2" in {r["id"] for r in csh02.rows["D03"]}
    run, rev08 = results["REV-08"]
    assert by_rule(rev08) == by_rule(offline("REV-08", run))
    _, pln07 = results["PLN-07"]
    changes = {x.source_ids[0]: x.amount_cents for x in pln07.exceptions if x.rule_id == "A01"}
    assert len(changes) == 8 and "090" not in changes and changes["610"] == 964950
