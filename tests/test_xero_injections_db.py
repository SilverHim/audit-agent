"""注入测试的连库断言 (设计稿 v2.1 第 8 节): 副本 (xero_case) 经补造清单写入测试库, 用真实 SQL 与存储跑现有程序,
检真实 SQL 与取数能否把注入的事实带到判断面前 (大模型仍读 fixture). T0 基线副本 (需求 R121), T1 收入截止 (需求
R122), T2 重复开票未冲销 (需求 R252), T4 银行回函余额差异 (需求 R123), T5 与 T5' 折旧错记 (需求 R253); T2, T5 在演示
配置的功能集上跑. 例外按 (规则, 对象 ID) 比, 不按序号. 设 TEST_DATABASE_URL 为已迁移的独立测试库 (没配时跳过);
要本地快照.

live 组 (需求 R124, R255, 设计稿第 8.1 节): 识别能力只由真实模型证明, 同一判断对象在基线 (T0) 与注入副本上成对断言,
两侧都必须通过. 只有被测对象交真实模型, 其余读 fixture; 另标 live (须配 LLM_MODEL 与 LLM_API_KEY, 见
tests.external.live_llm_settings, 没配时跳过)."""

import json
from pathlib import Path
from typing import Any

import pytest

from backend.adapters.llm import make_llm_client
from backend.contracts.data import TableKind
from backend.contracts.models import TrailEntry, TrailKind
from backend.contracts.ports import LLMClient
from backend.contracts.services import RuleResult
from backend.contracts.state import EngagementState
from tests.external import live_llm_settings, snapshot_dir, supplements_dir
from tests.fakes import rev08_populations
from tests.test_fieldwork_csh02 import assert_d04_unattributed, flaky_ba091
from tests.test_xero_injections import (
    JOURNAL_FEATURES,
    LLM_INJECTED,
    T1_ORC1026,
    T1_RULES,
    T2_RULES,
    T2_XJ405,
    T4_TRANSIT,
    T5P_RULES,
    assert_t1_pair,
    assert_t2_judgments,
    assert_t2_pair,
    assert_t2_rules,
    assert_t4_pair,
    assert_t5,
    paired_side,
    t2_request,
    t4_rules,
)
from tests.test_xero_rules_db import (
    check_acc04,
    check_cash,
    check_csh02_fieldwork,
    check_planning,
    check_revenue,
    check_versions,
    offline,
    run_rule_programs,
)
from tests.tools.live_budget import Budget, report
from tests.xero_db import build_xero_project, database_url
from tests.xero_fakes import (
    ORC1026,
    SNAPSHOT_NAME,
    XERO_PERIOD,
    XERO_REV08_WINDOW,
    XERO_RULES,
    manifest_documents,
    xero_rev08_populations,
    xero_tables,
    xero_uploads,
)
from tests.xero_harness import (
    LayeredMockLLMClient,
    XeroHarness,
    db_deps,
    imported_tables,
    request_text,
    run_programs,
)
from tests.xero_injection import DeleteOp, Patch, SetOp, XeroCase

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


async def test_t0_csh02_failure_with_own_d04_on_postgres(xero_case: Any) -> None:
    """需求 R322 验收 2: 同 test_fieldwork_csh02.test_xero_failure_with_own_d04, 写入 Postgres: BA-091 判断失败时
    本次的 D04 只写 v1 (unattributed), 不报 VersionConflict."""
    case = xero_case("T0")
    await assert_d04_unattributed(await run_programs(await db_deps(case, llm=flaky_ba091(case)), ["CSH-02"]))


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
    离线预置相同, 总体的 090 账面也是 -1035044 (需求 R327); CSH-02 的候选行与按副本派生的相同, D02 含 BT-090-0081,
    D03 含 XJ-637-2; REV-08 (R03) 同 T0; PLN-07 A01 为 8 条, 不再有 090 (变动 -164724 不到期初绝对值的 20%), 610 为
    964950."""
    case = xero_case("T4")
    project = await build_xero_project(database_url(), case.snapshot, case.supplements, tmp_path / "rules")
    results = await run_rule_programs(project)
    rules = t4_rules(case)
    for procedure_id in ("CSH-05", "CSH-02"):
        run, found = results[procedure_id]
        assert by_rule(found) == by_rule(rules[procedure_id](run)), procedure_id
    run, csh05 = results["CSH-05"]
    assert [(x.rule_id, x.source_ids, x.amount_cents) for x in csh05.exceptions] == [("B01", ["BA-090"], -55000)]
    [b01] = csh05.populations  # 需求 R327: 总体与副本派生的相同, 090 的账面为调过的数, 对账单余额不变
    assert csh05.populations == rules["CSH-05"](run).populations
    assert [(i.source_id, i.amount_cents, i.fields["statement_balance_cents"]) for i in b01.items] == [
        ("BA-090", -1035044, -1090044),
        ("BA-091", 1158123, 1158123),
    ]
    _, csh02 = results["CSH-02"]
    assert "BT-090-0081" in {r["id"] for r in csh02.rows["D02"]}
    assert "XJ-637-2" in {r["id"] for r in csh02.rows["D03"]}
    run, rev08 = results["REV-08"]
    assert by_rule(rev08) == by_rule(offline("REV-08", run))
    _, pln07 = results["PLN-07"]
    changes = {x.source_ids[0]: x.amount_cents for x in pln07.exceptions if x.rule_id == "A01"}
    assert len(changes) == 8 and "090" not in changes and changes["610"] == 964950


async def journals_side(case: XeroCase, roots: list[Path]) -> tuple[EngagementState, LayeredMockLLMClient]:
    """连库测试台 (演示配置的功能集) 上跑 GEN-02 的依赖闭包; 大模型先读 roots, 再读副本的运行副本."""
    client = LayeredMockLLMClient([*roots, case.supplements / "llm"])
    ran = await run_programs(await db_deps(case, llm=client, features=JOURNAL_FEATURES), ["GEN-02"])
    return ran.state, client


def a01_changes(state: EngagementState) -> dict[str, int | None]:
    """PLN-07 A01 的例外: 科目代码 → 变动额."""
    return {x.source_ids[0]: x.amount_cents for x in state["exceptions"] if x.rule_id == "A01"}


async def test_t2_on_postgres(xero_case: Any) -> None:
    """R252 验收 2, 4, 5: 演示配置的功能集下, T0 与 T2 副本各经连库测试台 (真实 SQL 与存储) 跑 GEN-02 的依赖闭包,
    T2 一侧 XJ-405 的大模型输出取 llm_injected/T2.
    - GEN-05: 真实 SQL 的例外与离线预置相同 (T0 为 XERO_RULES, T2 为 T2_RULES); 规则层的区分见 assert_t2_rules
      (R10 少 XJ-430-2, R12 的 full_credits 由 [XJ-430] 变为 [], paired_invoices 与 R09, R11 不变).
    - GEN-02 对 XJ-405 的请求: T0 含 XJ-430 的 3 行, T2 不含 "XJ-430"; 判断见 assert_t2_judgments (XJ-397, 449, 499,
      575, 601 同 T0).
    - 随之变化: 重要性由 32431 / 16216 / 1622 变为 32931 / 16466 / 1647 (收入 3243100 变为 3293100 的 1%, 50%, 5%);
      PLN-07 A01 仍为 9 条, 610 由 1019950 变为 1074950, 820 由 -198317 变为 -203317, 其余不变; R09 的 XJ-539 (9 分)
      与 R11 的 XJ-585 (1050 分) 低于新的明显微小 1647 (GEN-05 的参数快照), 两边都不出例外."""
    t0, t2 = xero_case("T0", base="t0"), xero_case("T2", base="t2")
    base, base_client = await journals_side(t0, [])
    injected, client = await journals_side(t2, [LLM_INJECTED / "T2"])
    states = (base, injected)
    for state, rules in ((base, XERO_RULES), (injected, T2_RULES)):
        [run] = [r for r in state["procedure_runs"] if r.procedure_id == "GEN-05"]
        assert [x for x in state["exceptions"] if x.procedure_id == "GEN-05"] == rules["GEN-05"](run).exceptions
    assert_t2_rules(base, injected)
    assert all(f'"id": "XJ-430-{n}"' in t2_request(base_client) for n in (1, 2, 3))
    assert "XJ-430" not in t2_request(client)
    assert_t2_judgments(base, injected, project_fields={"performed_by"})  # 两个项目各自新建的负责人
    amounts = [(s["materiality_cents"], s["performance_materiality_cents"], s["clearly_trivial_cents"]) for s in states]
    assert amounts == [(32431, 16216, 1622), (32931, 16466, 1647)]
    assert a01_changes(injected) == a01_changes(base) | {"610": 1074950, "820": -203317}
    assert (len(a01_changes(base)), a01_changes(base)["610"], a01_changes(base)["820"]) == (9, 1019950, -198317)
    [run] = [r for r in injected["procedure_runs"] if r.procedure_id == "GEN-05"]
    trivial = run.params_snapshot["clearly_trivial_cents"]
    lines = imported_tables(t2.loader)[TableKind.journal_entries]
    largest = {
        no: max(abs(r.debit_cents - r.credit_cents) for r in lines if r.entry_no == no) for no in ("XJ-539", "XJ-585")
    }
    assert (trivial, largest) == (1647, {"XJ-539": 9, "XJ-585": 1050})
    flagged = {str(x.detail["entry_no"]) for s in states for x in s["exceptions"] if x.procedure_id == "GEN-05"}
    assert not flagged & set(largest)


async def test_t5_on_postgres(xero_case: Any) -> None:
    """R253 验收 1, 3 (连库): 演示配置的功能集下, T0 (即 T5) 与 T5' 副本各经连库测试台跑 GEN-02 的依赖闭包, 大模型都读
    运行副本: 真实 SQL 的 GEN-05 与离线预置相同 (T0 为 XERO_RULES, T5' 为 T5P_RULES), 两边的对比见 assert_t5; 重要性
    两边都是 32431 / 16216 / 1622 (收入不变); PLN-07 A01 由 9 条变为 7 条, 少 710 与 711 (余额都为 0, 没有变动), 820
    由 -198317 变为 -205817 (本期 -126697 减期初 79120), 其余不变."""
    t0, t5p = xero_case("T0", base="t0"), xero_case("T5p", base="t5p")
    base, _ = await journals_side(t0, [])
    removed, _ = await journals_side(t5p, [])
    for state, rules in ((base, XERO_RULES), (removed, T5P_RULES)):
        [run] = [r for r in state["procedure_runs"] if r.procedure_id == "GEN-05"]
        assert [x for x in state["exceptions"] if x.procedure_id == "GEN-05"] == rules["GEN-05"](run).exceptions
        amounts = (state["materiality_cents"], state["performance_materiality_cents"], state["clearly_trivial_cents"])
        assert amounts == (32431, 16216, 1622)
    assert_t5(base, removed, project_fields={"performed_by"})  # 两个项目各自新建的负责人
    expected = {code: v for code, v in a01_changes(base).items() if code not in ("710", "711")} | {"820": -205817}
    assert (len(a01_changes(base)), a01_changes(removed)) == (9, expected)


# ── REV-08 被测总体随注入变 (需求 R324 审核 R324-02): 补丁只在副本上执行, 期望值由副本的表经 rev08_populations 算出 ──

ORC1024_RECORD = {  # 补造的 ORC1024 服务完成记录 (delivery_notes.csv 一行)
    "id": "SR-ORC1024",
    "delivery_no": "SR-ORC1024",
    "invoice_no": "ORC1024",
    "shipped_date": "2026-08-19",
    "signed_date": "2026-08-19",
    "signed_by": "Ruth Carmody",
}
REFUND_1020 = {  # 期末后退还 ORC1020 的 550.00: 090 账户的负数流水, 参考号含 1020, 记在对账单末尾 (原流水编号不变)
    "statementLineId": "00000000-0000-5000-8000-000000001020",
    "postedDate": "2026-10-17",
    "payee": "Hamilton Smith PTY",
    "reference": "Refund 1020",
    "notes": "",
    "chequeNo": "",
    "amount": -550.0,
    "transactionDate": "2026-10-17",
    "type": "DEBIT",
    "isReconciled": True,
    "isDuplicate": False,
    "isDeleted": False,
    "payments": [],
    "bankTransactions": [],
}
# 场景 → (各规则的 (对象数, 金额合计), 与基线总体相同的规则, R02 中 ORC1024 的 records)
REV08_INJECTIONS = {
    "no_record_orc1024": (
        [("R01", 5, 935000), ("R02", 6, 990000), ("R03", 5, 935000)],
        ["R03"],
        None,
    ),
    "refund_1020": (
        [("R01", 6, 990000), ("R02", 6, 990000), ("R03", 5, 935000)],
        ["R01", "R02", "R03"],
        [{"delivery_no": "SR-ORC1024", "signed_date": "2026-08-19", "signed_by": "Ruth Carmody"}],
    ),
}


def rev08_patch(scenario: str) -> Patch:
    """两个注入: 删去 ORC1024 的服务完成记录; 090 账户对账单末尾加 REFUND_1020 (before 取原快照的对账单行)."""
    if scenario == "no_record_orc1024":
        op: DeleteOp | SetOp = DeleteOp(
            op="delete", file="xero_demo/delivery_notes.csv", row="SR-ORC1024", before=ORC1024_RECORD
        )
    else:
        statements = json.loads((snapshot_dir(SNAPSHOT_NAME) / "bank_statements.json").read_text(encoding="utf-8"))
        lines = statements["090"]["statements"][0]["statementLines"]
        op = SetOp(
            op="set",
            file="api/bank_statements.json",
            key="090",
            field="statements.0.statementLines",
            before=lines,
            after=[*lines, REFUND_1020],
        )
    return Patch(scenario=scenario, title=scenario, ops=[op])


@pytest.mark.parametrize("scenario", REV08_INJECTIONS)
async def test_rev08_populations_follow_injections(xero_case: Any, tmp_path: Path, scenario: str) -> None:
    """R324-02: 注入副本上连库的 REV-08 总体等于由副本的表算出的 (rev08_populations). 删去 ORC1024 的记录: R01 为
    5 张 935000, R02 仍为 6 张 990000 而 ORC1024 的 records 为空, R03 同基线; 期末后参考号含 1020 的退款 (负数流水):
    R03 只认正数流水, 三条规则的总体都同基线."""
    counts, unchanged, orc1024 = REV08_INJECTIONS[scenario]
    case = xero_case(rev08_patch(scenario))
    project = await build_xero_project(database_url(), case.snapshot, case.supplements, tmp_path / "rules")
    _, rev08 = (await run_rule_programs(project))["REV-08"]
    tables = xero_tables(case.loader)
    assert rev08.populations == rev08_populations(
        tables["sales_invoices"],
        tables["delivery_notes"],
        tables["bank_transactions"],
        XERO_PERIOD[1],
        XERO_REV08_WINDOW,
    )
    assert [(p.rule_id, p.count, p.amount_cents) for p in rev08.populations] == counts
    base = {p.rule_id: p for p in xero_rev08_populations()}
    assert [p.rule_id for p in rev08.populations if p == base[p.rule_id]] == unchanged
    [r02] = [p for p in rev08.populations if p.rule_id == "R02"]
    [item] = [i for i in r02.items if i.fields["invoice_no"] == "ORC1024"]
    assert item.fields["records"] == orc1024


# ── live 组: T1, T4 (需求 R124) 与 T2 (需求 R255) 真实大模型成对断言; 三个用例的调用上限合计 25 次 ──


def live_client() -> LLMClient:
    """真实模型的客户端. 先核对数据 (快照与补造跟踪了却找不到时失败), 再看测试库与模型配置, 缺哪个都跳过."""
    snapshot_dir("20261017-q1")
    supplements_dir("20261017-q1")
    database_url()
    return make_llm_client(live_llm_settings())


async def db_build(case: XeroCase, llm: LLMClient) -> XeroHarness:
    return await db_deps(case, llm=llm)


async def usage(name: str, built: list[XeroHarness], budget: Budget) -> None:
    """EX-2 用量汇总 (调用与 token 取预算, 尝试次数与失败级别取各次项目的 llm_call 留痕), 写 var/eval 并打印."""
    entries: list[TrailEntry] = []
    for h in built:
        entries += await h.deps.trail.list(h.engagement.id, TrailKind.llm_call, limit=10_000)
    report(name, entries, budget)


@pytest.mark.live(max_calls=10)  # 两侧各至多 2 次执行 x 3 次尝试 = 12, 正常 2 次; 超限即停发并失败
async def test_live_t1_pair(xero_case: Any, live_budget: Budget) -> None:
    """R124 验收 1, 2, 4: 连库测试台, 只有 REV-02 对 ORC1026 交真实模型 (上游 DOC-04, REV-03 与其余 5 张发票读运行副本).
    T0 副本上为 accept 且无错报草稿, T1 副本上为 misstatement (650000, Cutoff, overstated, 科目含 200/Sales/Revenue),
    解释 R01 与 R03; 判断没出来的一侧重做至多 1 次; 结束时打印用量汇总."""
    live = live_client()
    t0, t1 = xero_case("T0", base="t0"), xero_case("T1", base="t1")
    key, built = ("REV-02", ORC1026), []
    try:
        base, runs = await paired_side(
            db_build, t0, [t0.supplements / "llm"], live, key, live_budget, ["REV-02"], T1_ORC1026
        )
        built += runs
        cutoff, runs = await paired_side(
            db_build, t1, [t1.supplements / "llm"], live, key, live_budget, ["REV-02"], T1_ORC1026
        )
        built += runs
    finally:
        await usage("test_live_t1_pair", built, live_budget)
    assert_t1_pair(base.state, cutoff.state)


@pytest.mark.live(max_calls=5)  # 注入侧至多 2 次执行 x 3 次尝试 = 6 会超, 正常 1 次; 基线侧不调真实模型
async def test_live_t4_pair(xero_case: Any, live_budget: Budget) -> None:
    """R124 验收 1, 3, 4: 连库测试台, 只有 CSH-02 对 BA-090 交真实模型 (DOC-06, BA-091 与 A7 读 fixture; A7 两轮取
    llm_injected/T4). T0 副本上没有 BA-090 的 D01 与判断 (不调真实模型); T4 副本上 D01 为 -55000, 判断 accept 或
    close 且无错报草稿; 判断没出来时重做至多 1 次; 结束时打印用量汇总."""
    live = live_client()
    t0, t4 = xero_case("T0", base="t0"), xero_case("T4", base="t4")
    key, built = ("CSH-02", "BA-090"), []
    try:
        base, runs = await paired_side(db_build, t0, [t0.supplements / "llm"], live, key, live_budget, ["CSH-02"], None)
        built += runs
        roots = [LLM_INJECTED / "T4", t4.supplements / "llm"]
        transit, runs = await paired_side(db_build, t4, roots, live, key, live_budget, ["CSH-02"], T4_TRANSIT)
        built += runs
    finally:
        await usage("test_live_t4_pair", built, live_budget)
    assert_t4_pair(base.state, transit.state)


async def journals_build(case: XeroCase, llm: LLMClient) -> XeroHarness:
    return await db_deps(case, llm=llm, features=JOURNAL_FEATURES)


@pytest.mark.live(max_calls=10)  # 两侧各至多 2 次执行 x 3 次尝试 = 12, 正常 2 次; 超限即停发并失败
async def test_live_t2_pair(xero_case: Any, live_budget: Budget) -> None:
    """R255 验收 1, 2, 4 (只做 T2): 连库测试台, 演示配置的功能集, 只有 GEN-02 对 XJ-405 交真实模型 (其余凭证读运行
    副本). T0 副本上为 accept 且无错报草稿, T2 副本上为 misstatement (50000, Occurrence, overstated, 科目含 200 或
    Sales); 判断没出来的一侧重做至多 1 次; 结束时打印用量汇总."""
    live = live_client()
    t0, t2 = xero_case("T0", base="t0"), xero_case("T2", base="t2")
    key, built = ("GEN-02", "XJ-405"), []
    try:
        base, runs = await paired_side(
            journals_build, t0, [t0.supplements / "llm"], live, key, live_budget, ["GEN-02"], T2_XJ405
        )
        built += runs
        duplicate, runs = await paired_side(
            journals_build, t2, [t2.supplements / "llm"], live, key, live_budget, ["GEN-02"], T2_XJ405
        )
        built += runs
    finally:
        await usage("test_live_t2_pair", built, live_budget)
    assert_t2_pair(base.state, duplicate.state)
