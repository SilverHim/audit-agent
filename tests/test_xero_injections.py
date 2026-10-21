"""注入测试的离线断言 (设计稿 v2.1 第 8 节): 在 xero_case 的副本上经测试台 (tests/xero_harness) 跑现有程序.
- T0 基线副本 (需求 R121): 不打补丁的副本与原目录等价, 现有程序在副本上的结果即各场景的对照值. 七张表逐行相同已由
  tests/test_xero_case (R74 验收 3) 断言, 不另写.
- T1 收入截止 (需求 R122, 设计稿第 8.3 节), T2 重复开票未冲销 (需求 R252, 第 8.4 节), T4 银行回函余额差异 (需求
  R123, 第 8.6 节), T5 折旧错记与反向对照 T5' (需求 R253, 第 8.7 节): 规则引擎预置该场景的例外与候选行, 大模型输出
  叠在 tests/fixtures/llm_injected/<场景>. T2, T5 在演示配置的功能集 (含分录测试) 上跑, 其余在基线上跑.
- 真实大模型成对断言 (需求 R124) 的辅助 (paired_side, assert_t1_pair, assert_t4_pair) 写在这里, 连库 live 用例与
  这里不联网的自测共用.
离线跑法只检编排与校验链路: 判断结论是预写的 fixture, 不证明系统能识别错报 (第 8.1 节). 副本要本地快照 (没有时跳过),
标 external. 连库的见 tests/test_xero_injections_db."""

import re
from collections.abc import Awaitable, Callable, Iterable
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from backend.contracts.data import TableKind
from backend.contracts.models import (
    Assertion,
    AuditException,
    Confirmation,
    ConfirmationResult,
    CriticVerdict,
    Direction,
    EvidencePointer,
    ExceptionDisposition,
    Judgment,
    MisstatementDraft,
    ProcedureRun,
    ProposedAction,
    StepStatus,
    TrailKind,
)
from backend.contracts.ports import AuditOutputStore, LLMClient
from backend.contracts.services import RuleResult
from backend.contracts.state import EngagementState
from backend.services.features import BASELINE
from tests.fakes import csh05_population, engine_result
from tests.test_e2e_xero17 import Xero17, run_xero17
from tests.tools.live_budget import Budget
from tests.xero_fakes import (
    DEMO_FEATURES,
    ORC1026,
    REPO,
    XERO_PERIOD,
    XERO_RULES,
    XERO_XJ499_DRAFT,
    xero_contracts,
    xero_csh02_rows,
    xero_documents,
    xero_gen05_result,
    xero_rev08_exception,
    xero_tables,
)
from tests.xero_harness import (
    LayeredMockLLMClient,
    Ran,
    RoutedLLMClient,
    TableDiff,
    XeroHarness,
    imported_tables,
    offline_deps,
    request_text,
    run_programs,
    table_diff,
)
from tests.xero_injection import INJECTIONS, SNAPSHOT, SNAPSHOTS, XeroCase, contact_count, scan_copies

pytestmark = pytest.mark.external
shared_loop = pytest.mark.asyncio(loop_scope="module")  # 与模块共用的 R119 流程 (flow17) 同一个事件循环
T0_PROGRAMS = ["REV-02", "CSH-02"]  # 依赖闭包为 REV-08, REV-03 (与 DOC-04), REV-02, CSH-04, CSH-05, DOC-06, CSH-02
# 两次执行之间必然不同的字段: 判断的写入时刻与所记留痕的序号 (各自的留痕从头编号)
RUN_FIELDS = {"created_at", "trail_ref"}
LLM_INJECTED = REPO / "tests" / "fixtures" / "llm_injected"  # 注入场景的大模型输出: <场景>/<程序>/<对象>.json
CON_X01 = "DOC-7795e9312e9f"  # 补造的 contracts/CON-X01.pdf (manifest 的文件 ID)


@pytest.fixture
def t0(xero_case: Any) -> XeroCase:
    built: XeroCase = xero_case("T0")
    return built


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def flow17() -> Xero17:
    """R119 的离线全流程 (FV-52), 跑一次, 作逐字段对照."""
    return await run_xero17()


def test_t0_copy_rows_and_contacts(t0: XeroCase) -> None:
    """验收 1: 副本导入后的表行数 (总账 1017, 销售发票 42, 银行流水 146, 科目 62, 试算表本期 27 含余额为 0 的 801
    与上期 6, 服务完成记录 24, 银行账户 2); 副本 contacts.json 82 个联系人 (去掉了登录账号联系人), Bayside Club 发票的
    customer_abn 与原件相同. 只取计数与 ABN, 不在测试里留联系人列表."""
    tables = imported_tables(t0.loader)
    counts = {kind: len(tables[kind]) for kind in ("journal_entries", "sales_invoices", "bank_transactions")}
    counts |= {kind: len(tables[kind]) for kind in ("accounts", "delivery_notes", "bank_accounts")}
    assert counts == {
        "journal_entries": 1017,
        "sales_invoices": 42,
        "bank_transactions": 146,
        "accounts": 62,
        "delivery_notes": 24,
        "bank_accounts": 2,
    }
    balances = tables[TableKind.trial_balance]
    assert [len([r for r in balances if r.period == period]) for period in ("current", "prior")] == [27, 6]
    assert [r.balance_cents for r in balances if (r.period, r.account_code) == ("current", "801")] == [0]
    assert contact_count(t0.snapshot / "contacts.json") == 82

    def bayside(source: dict[str, list[Any]]) -> list[tuple[str, str | None]]:
        return sorted((r.id, r.customer_abn) for r in source["sales_invoices"] if r.customer == "Bayside Club")

    copied = bayside(tables)
    assert len(copied) == 2 and all(abn for _, abn in copied) and copied == bayside(imported_tables())


def test_t0_imported_state_equals_the_original(t0: XeroCase) -> None:
    """验收 2 前半: 副本经补造清单得到的导入后状态与原目录逐对象相同: 各表 (table_diff 为空), fieldwork 为止 21 份
    上传文件的登记 (含补登记的类型, 可靠性, 文件日期与关联对象), 按 DOC-04 fixture 抽取的 3 份合同."""
    assert table_diff(imported_tables(), imported_tables(t0.loader)) == {}
    documents = xero_documents("fieldwork", t0.loader)
    assert len(documents) == 21 and documents == xero_documents("fieldwork")
    contracts = xero_contracts(t0.loader)
    assert [c.id for c in contracts] == ["CON-X01", "CON-X02", "CON-X03"] and contracts == xero_contracts()


async def same_as_flow(state: EngagementState, store: AuditOutputStore, eid: str) -> None:
    """state 中的每条例外, 判断与函证, 与 R119 流程存储中同 ID 同版本的对象逐字段相同 (判断除 RUN_FIELDS)."""
    for model, items in ((AuditException, state["exceptions"]), (Judgment, state["judgments"])):
        for item in items:
            other = await store.get(eid, model, item.id, item.version)
            assert other is not None, (item.id, item.version)
            assert item.model_dump(exclude=RUN_FIELDS) == other.model_dump(exclude=RUN_FIELDS), item.id
    for confirmation in state["confirmations"]:
        other = await store.get(eid, Confirmation, confirmation.id, confirmation.version)
        assert confirmation == other, confirmation.id


@shared_loop
async def test_t0_programs_match_the_full_flow(t0: XeroCase, flow17: Xero17) -> None:
    """验收 2 后半: 副本上 (离线测试台) 跑 REV-08, REV-03, REV-02, CSH-04, DOC-06, CSH-05, CSH-02: REV-08 只有
    R03-1 (330000, 没有 R01, R02); ORC1026 的判断 accept; CSH-05 没有 B01; C-BA-090 agreed; D04-1 (BA-091, 4853);
    BA-091 的判断 misstatement. 这些与其余例外, 判断, 函证都与 R119 流程中的对应对象 (同 ID 同版本) 逐字段相同, 判断
    只除去写入时刻与留痕序号."""
    state = (await run_programs(await offline_deps(t0), T0_PROGRAMS)).state
    exceptions = {x.id: x for x in state["exceptions"]}
    assert sorted(i for i in exceptions if not i.startswith("X-PLN-07-")) == ["X-CSH-02-1-D04-1", "X-REV-08-1-R03-1"]
    assert (exceptions["X-REV-08-1-R03-1"].amount_cents, exceptions["X-REV-08-1-R03-1"].rule_id) == (330000, "R03")
    d04 = exceptions["X-CSH-02-1-D04-1"]
    assert (d04.source_ids, d04.amount_cents, d04.disposition) == (["BA-091"], 4853, ExceptionDisposition.open)
    judgments = {j.id: j for j in state["judgments"]}
    assert judgments[f"J-REV-02-1-{ORC1026}"].proposed_action is ProposedAction.accept
    assert judgments["J-CSH-02-1-BA-091"].proposed_action is ProposedAction.misstatement
    confirmations = {c.id: c for c in state["confirmations"]}
    assert (confirmations["C-BA-090"].result, confirmations["C-BA-090"].difference_cents) == (
        ConfirmationResult.agreed,
        0,
    )
    await same_as_flow(state, flow17.runner.deps.output_store, flow17.eid)


def test_t0_patch_holds_no_login_contact(t0: XeroCase) -> None:
    """验收 4: 补丁文件 T0.json 中检索不到登录账号联系人的全名与邮箱 (只在内存比对, 失败只报文件名). 副本由 xero_case
    建副本时查过 (scan_copies, 有则抛错); 原目录哈希由会话级夹具 xero_originals_unchanged 在会话结束时核对."""
    assert scan_copies(SNAPSHOTS / SNAPSHOT, [INJECTIONS / "T0.json"]) == []


def llm_calls(ran: Ran) -> list[tuple[str, str, int, str | None]]:
    """大模型调用的留痕: (程序, fixture_key, attempt, 失败级别), 按调用次序."""
    entries = [e for e in ran.deps.trail.entries if e.kind is TrailKind.llm_call]  # type: ignore[attr-defined]
    return [
        (e.procedure_id, e.payload["fixture_key"], e.payload["attempt"], e.payload["level_failed"]) for e in entries
    ]


def same_judgments(left: EngagementState, right: EngagementState, ids: set[str], exclude: Iterable[str] = ()) -> None:
    """两次执行中 ids 这几条判断逐字段相同 (除 RUN_FIELDS 与 exclude)."""
    fields = RUN_FIELDS | set(exclude)
    a = {j.id: j.model_dump(exclude=fields) for j in left["judgments"] if j.id in ids}
    b = {j.id: j.model_dump(exclude=fields) for j in right["judgments"] if j.id in ids}
    assert sorted(a) == sorted(ids) and a == b


# ── T1 收入截止 (需求 R122) ──

T1_ORC1026 = f"J-REV-02-1-{ORC1026}"
T1_DRAFT = MisstatementDraft(
    type="factual", account="Sales (200)", assertion="Cutoff", amount_cents=650000, direction="overstated"
)


def t1_r01(run: ProcedureRun) -> AuditException:
    """T1 的 REV-08 R01: ORC1026 于 09-17 开票入账, 服务完成记录 10-02 才签 (期后); 字段同连库 SqlRuleEngine 的结果
    (连库用例断言两者相等)."""
    return AuditException(
        id=f"X-REV-08-{run.run_no}-R01-1",
        procedure_id="REV-08",
        run_id=run.id,
        rule_id="R01",
        severity="high",
        source_ids=[ORC1026],
        amount_cents=715000,
        detail={"posting_date": "2026-09-17", "signed_date": "2026-10-02"},
        evidence=[
            EvidencePointer(
                type="invoice",
                source_type="sales_invoice",
                source_id=ORC1026,
                locator="amount_cents, posting_date, signed_date",
                reliability="internal_original",
            )
        ],
    )


# T1 的规则预置: REV-08 多出 R01, R03 不变; 其余程序同 T0 (XERO_RULES)
T1_RULES: dict[str, Callable[[ProcedureRun], RuleResult]] = {
    **XERO_RULES,
    "REV-08": lambda run: RuleResult(exceptions=[t1_r01(run), xero_rev08_exception(run)], rows={}),
}


def test_t1_patch_changes_only_the_completion_date(xero_case: Any) -> None:
    """验收 1: 补丁只把 SR-ORC1026 的 signed_date 由 2026-09-17 改为 2026-10-02, 试算表不调; 导入后的各表与原目录相比
    只有这一行这一列不同."""
    case: XeroCase = xero_case("T1")
    assert table_diff(imported_tables(), imported_tables(case.loader)) == {
        TableKind.delivery_notes: TableDiff(
            added=[], deleted=[], changed={"SR-ORC1026": {"signed_date": (date(2026, 9, 17), date(2026, 10, 2))}}
        )
    }


async def test_t1_offline_cutoff_misstatement(xero_case: Any) -> None:
    """验收 2: T1 副本上跑 REV-02 的依赖闭包, 规则预置 R01 与 R03, ORC1026 的大模型输出取 llm_injected/T1: 判断建议
    misstatement, 草稿 {factual, Sales (200), Cutoff, 650000, overstated}, 解释 R01 与 R03 两条例外, 证据含 XJ-580-1,
    SR-ORC1026 与 CON-X01 第 2 页的 4.1 条; 一次通过四级校验 (金额校验: 650000 即副本中 XJ-580-1 贷方的金额); 其余 5 条
    REV-02 判断与 REV-03 三条与 T0 副本上的逐字段相同 (除 RUN_FIELDS)."""
    t1, t0 = xero_case("T1", base="t1"), xero_case("T0", base="t0")
    client = LayeredMockLLMClient([LLM_INJECTED / "T1", t1.supplements / "llm"])
    ran = await run_programs(await offline_deps(t1, llm=client, rules=T1_RULES), ["REV-02"])
    baseline = (await run_programs(await offline_deps(t0), ["REV-02"])).state
    judgments = {j.id: j for j in ran.state["judgments"]}
    cutoff = judgments[T1_ORC1026]
    assert (cutoff.proposed_action, cutoff.misstatement_draft) == (ProposedAction.misstatement, T1_DRAFT)
    assert cutoff.exception_ids == ["X-REV-08-1-R01-1", "X-REV-08-1-R03-1"]
    cited = {(p.source_type, p.source_id, p.locator, p.page) for p in cutoff.evidence}
    assert {
        ("journal_entry", "XJ-580-1", "credit_cents", None),
        ("delivery_note", "SR-ORC1026", "signed_date", None),
        ("document", CON_X01, "CON-X01-T1", 2),
    } <= cited
    [line] = [r for r in imported_tables(t1.loader)[TableKind.journal_entries] if r.id == "XJ-580-1"]
    assert (line.account_code, line.credit_cents, line.debit_cents) == ("200", T1_DRAFT.amount_cents, 0)
    assert {(attempt, failed) for _, _, attempt, failed in llm_calls(ran)} == {(1, None)}
    assert [(r.fixture_key, r.root) for r in client.reads if r.root == LLM_INJECTED / "T1"] == [
        (ORC1026, LLM_INJECTED / "T1")
    ]
    same_judgments(ran.state, baseline, {j.id for j in baseline["judgments"]} - {T1_ORC1026})


# ── T2 重复开票未冲销 (需求 R252; 设计稿第 8.4 节) ──

JOURNAL_FEATURES = DEMO_FEATURES  # 分录测试 (GEN-05, GEN-02) 在演示配置里; T2, T5 两侧都用它
T2_XJ405 = "J-GEN-02-1-XJ-405"
T2_DRAFT = MisstatementDraft(
    type="factual",
    account="Sales (200)",
    assertion="Occurrence",
    amount_cents=50000,
    direction="overstated",
    account_code="200",
)
T2_VOUCHERS = ["XJ-397", "XJ-405", "XJ-449", "XJ-499", "XJ-575", "XJ-601"]  # T2 上 GEN-02 的判断对象 (T0 另有 XJ-430)
T2_UNAFFECTED = {f"J-GEN-02-1-{no}" for no in ("XJ-397", "XJ-449", "XJ-499", "XJ-575", "XJ-601")}
T2_RENUMBERED = "J-GEN-02-1-XJ-449"  # 它解释的 R10 例外随 XJ-430 删去由 R10-2 改编为 R10-1
ORC1008 = "SI-1d1ba340-afa2-4f4c-8ff7-a147bda9a47b"  # 与 ORC1003 同日同额的 Hamilton Smith 发票, 有 7 月服务记录
CON_X02 = "DOC-02c5c90d4ffb"  # 补造的 contracts/CON-X02.pdf (manifest 的文件 ID)


def t2_gen05(run: ProcedureRun) -> RuleResult:
    """T2 的 GEN-05: 贷项凭证 XJ-430 删去后 R10 只剩 XJ-449-2 (编号变为 R10-1), R12 (XJ-405-2) 的 full_credits 为空;
    其余 (R09, R11, R12 的 paired_invoices, 不执行的规则与理由) 同 T0. 字段同连库 SqlRuleEngine 的结果 (连库用例断言
    两者相等)."""
    base = xero_gen05_result(run)
    kept = []
    for x in base.exceptions:
        if x.source_ids == ["XJ-430-2"]:
            continue
        if x.rule_id == "R10":
            x = x.model_copy(update={"id": f"X-GEN-05-{run.run_no}-R10-1"})
        elif x.rule_id == "R12":
            x = x.model_copy(update={"detail": {**x.detail, "full_credits": []}})
        kept.append(x)
    return engine_result("GEN-05", base.model_copy(update={"exceptions": kept}))


# T2 的规则预置: GEN-05 按上; PLN-07, PLN-01 在副本上随试算表变 (只在连库断言), 离线沿用 T0 的 (不影响分录一串)
T2_RULES: dict[str, Callable[[ProcedureRun], RuleResult]] = {**XERO_RULES, "GEN-05": t2_gen05}


def gen05_findings(state: EngagementState, rule_id: str) -> list[AuditException]:
    return [x for x in state["exceptions"] if x.procedure_id == "GEN-05" and x.rule_id == rule_id]


def assert_t2_rules(base: EngagementState, injected: EngagementState) -> None:
    """R252 验收 2, 4 的规则层: R10 由两条 (XJ-430-2, XJ-449-2, 都是 55000) 变为只有 XJ-449-2 (R10-1); R12 两边都是
    XJ-405-2 (55000, ORC1003), paired_invoices 都是 ORC1008 / XJ-408-2, full_credits 由 [XJ-430] 变为 []; R09
    (XJ-397-3, XJ-499-2) 与 R11 (XJ-575-1, XJ-601-1) 两边逐字段相同."""

    def r10(state: EngagementState) -> list[tuple[str, list[str], int | None]]:
        return [(x.id, x.source_ids, x.amount_cents) for x in gen05_findings(state, "R10")]

    assert r10(base) == [("X-GEN-05-1-R10-1", ["XJ-430-2"], 55000), ("X-GEN-05-1-R10-2", ["XJ-449-2"], 55000)]
    assert r10(injected) == [("X-GEN-05-1-R10-1", ["XJ-449-2"], 55000)]
    paired = [{"invoice_id": ORC1008, "invoice_no": "ORC1008", "journal_entry_id": "XJ-408-2"}]
    for state, credits in ((base, ["XJ-430"]), (injected, [])):
        [r12] = gen05_findings(state, "R12")
        assert (r12.source_ids, r12.amount_cents, r12.detail["invoice_no"]) == (["XJ-405-2"], 55000, "ORC1003")
        assert (r12.detail["paired_invoices"], r12.detail["full_credits"]) == (paired, credits)
    for rule_id, sources in (("R09", ["XJ-397-3", "XJ-499-2"]), ("R11", ["XJ-575-1", "XJ-601-1"])):
        assert [x.source_ids[0] for x in gen05_findings(injected, rule_id)] == sources
        assert gen05_findings(injected, rule_id) == gen05_findings(base, rule_id), rule_id


def t2_request(client: LayeredMockLLMClient) -> str:
    """GEN-02 对 XJ-405 的请求全文 (恰好一次)."""
    [request] = [r for r in client.requests if (r.procedure_id, r.fixture_key) == ("GEN-02", "XJ-405")]
    return request_text(request)


def assert_t2_judgments(base: EngagementState, injected: EngagementState, project_fields: Iterable[str] = ()) -> None:
    """R252 验收 2, 3, 4 的判断层: T0 上 XJ-405 accept 且无草稿, 有 XJ-430 的判断; T2 上没有 XJ-430 的判断, XJ-405 为
    misstatement (T2_DRAFT), 只解释 R12; 其余 5 条与 T0 逐字段相同 (除 RUN_FIELDS 与随项目不同的 project_fields;
    XJ-449 另除解释的例外, 那条例外随 R10 改编号, 指向的仍是 XJ-449-2)."""
    judgments = {j.id: j for j in base["judgments"]}
    original = judgments[T2_XJ405]
    assert (original.proposed_action, original.misstatement_draft) == (ProposedAction.accept, None)
    assert "J-GEN-02-1-XJ-430" in judgments
    judgments = {j.id: j for j in injected["judgments"]}
    assert sorted(j.subject_id for j in judgments.values() if j.procedure_id == "GEN-02") == T2_VOUCHERS
    duplicate = judgments[T2_XJ405]
    assert (duplicate.proposed_action, duplicate.misstatement_draft) == (ProposedAction.misstatement, T2_DRAFT)
    assert duplicate.exception_ids == ["X-GEN-05-1-R12-1"]
    same_judgments(injected, base, T2_UNAFFECTED - {T2_RENUMBERED}, exclude=project_fields)
    same_judgments(injected, base, {T2_RENUMBERED}, exclude={"exception_ids", *project_fields})
    renumbered = [{j.id: j for j in s["judgments"]}[T2_RENUMBERED].exception_ids for s in (base, injected)]
    assert renumbered == [["X-GEN-05-1-R10-2"], ["X-GEN-05-1-R10-1"]]


def test_t2_patch_removes_the_credit_note(xero_case: Any) -> None:
    """R252 验收 1: 补丁删去 JournalNumber 430 (CR1003 的过账: 200 借 500.00, 610 贷 550.00, 820 借 50.00), CR1003 的
    Status 由 PAID 改为 VOIDED, ORC1003 的 Status 由 PAID 改为 AUTHORISED, AmountDue 由 0.0 改为 550.0 (xero_case
    已核对各项改前值), 试算表 200 由 -3243100 调为 -3293100, 610 由 1019950 调为 1074950, 820 由 -119197 调为 -124197
    (xero_case 已核对副本借贷平衡且等于分录推出的余额); 导入后的各表与原目录相比只少 XJ-430 的 3 行, 试算表变这 3 行
    (发票表不变: ORC1003 仍是已生效的发票). 补丁与大模型输出中检索不到登录账号联系人."""
    case: XeroCase = xero_case("T2")
    original = imported_tables()
    lines = {r.id: (r.account_code, r.debit_cents, r.credit_cents) for r in original[TableKind.journal_entries]}
    assert {i: v for i, v in lines.items() if i.startswith("XJ-430-")} == {
        "XJ-430-1": ("200", 50000, 0),
        "XJ-430-2": ("610", 0, 55000),
        "XJ-430-3": ("820", 5000, 0),
    }
    assert table_diff(original, imported_tables(case.loader)) == {
        TableKind.journal_entries: TableDiff(added=[], deleted=["XJ-430-1", "XJ-430-2", "XJ-430-3"], changed={}),
        TableKind.trial_balance: TableDiff(
            added=[],
            deleted=[],
            changed={
                "current:200": {"balance_cents": (-3243100, -3293100)},
                "current:610": {"balance_cents": (1019950, 1074950)},
                "current:820": {"balance_cents": (-119197, -124197)},
            },
        ),
    }
    assert scan_copies(SNAPSHOTS / SNAPSHOT, [INJECTIONS / "T2.json", LLM_INJECTED / "T2"]) == []


async def test_t2_offline_duplicate_invoice(xero_case: Any) -> None:
    """R252 验收 2, 3: 演示配置的功能集下, T0 与 T2 副本各跑 GEN-02 的依赖闭包 (GEN-05, GEN-02). T0 规则预置
    XERO_RULES, 大模型读运行副本: XJ-405 的请求含 XJ-430 的 3 行, 判断 accept. T2 规则预置 T2_RULES, XJ-405 的大模型
    输出取 llm_injected/T2: 规则层见 assert_t2_rules; XJ-405 的请求不含 XJ-430; 判断为 misstatement {factual, Sales
    (200), Occurrence, 50000, overstated, 科目代码 200}, 证据含 XJ-405-1, XJ-408-1, SR-ORC1008 与 CON-X02 第 2 页的
    5.1 条 (每月一张月费发票), 结论写明应收多记 550.00, GST 多记 50.00; 一次通过四级校验, 推理链每步都受支持 (金额
    校验: 50000 即副本中 XJ-405-1 贷方的金额); 没有 XJ-430 的判断, 其余判断同 T0 (assert_t2_judgments)."""
    t2, t0 = xero_case("T2", base="t2"), xero_case("T0", base="t0")
    base_client = LayeredMockLLMClient([t0.supplements / "llm"])
    baseline = (
        await run_programs(await offline_deps(t0, llm=base_client, features=JOURNAL_FEATURES), ["GEN-02"])
    ).state
    client = LayeredMockLLMClient([LLM_INJECTED / "T2", t2.supplements / "llm"])
    deps = await offline_deps(t2, llm=client, rules=T2_RULES, features=JOURNAL_FEATURES)
    ran = await run_programs(deps, ["GEN-02"])
    assert_t2_rules(baseline, ran.state)
    assert all(f'"id": "XJ-430-{n}"' in t2_request(base_client) for n in (1, 2, 3))
    assert "XJ-430" not in t2_request(client)
    assert_t2_judgments(baseline, ran.state)
    duplicate = {j.id: j for j in ran.state["judgments"]}[T2_XJ405]
    cited = {(p.source_type, p.source_id, p.locator, p.page) for p in duplicate.evidence}
    assert {
        ("journal_entry", "XJ-405-1", "credit_cents", None),
        ("journal_entry", "XJ-408-1", "credit_cents", None),
        ("delivery_note", "SR-ORC1008", "signed_date", None),
        ("document", CON_X02, "CON-X02-T2", 2),
    } <= cited
    assert {s.status for s in duplicate.reasoning_chain} == {StepStatus.supported}
    assert "Accounts Receivable (610) overstated by AUD 550.00" in duplicate.conclusion
    assert "GST (820) overstated by AUD 50.00" in duplicate.conclusion
    [line] = [r for r in imported_tables(t2.loader)[TableKind.journal_entries] if r.id == "XJ-405-1"]
    assert (line.account_code, line.credit_cents, line.debit_cents) == ("200", T2_DRAFT.amount_cents, 0)
    calls = llm_calls(ran)
    assert [key for pid, key, *_ in calls if pid == "GEN-02"] == T2_VOUCHERS
    assert {(attempt, failed) for _, _, attempt, failed in calls} == {(1, None)}
    assert [(r.procedure_id, r.fixture_key) for r in client.reads if r.root == LLM_INJECTED / "T2"] == [
        ("GEN-02", "XJ-405")
    ]


# ── T5 折旧错记与反向对照 T5' (需求 R253; 设计稿第 8.7 节): T5 不注入 (T0 副本已有 #499), T5' 删去 #499 ──

T5_XJ499 = "J-GEN-02-1-XJ-499"
T5_DRAFT = XERO_XJ499_DRAFT.model_copy(update={"account_code": "710"})  # 运行副本的 fixture 带科目代码 (需求 R156)
T5P_VOUCHERS = ["XJ-397", "XJ-405", "XJ-430", "XJ-449", "XJ-575", "XJ-601"]  # T5' 上 GEN-02 的判断对象 (少 XJ-499)
T5P_UNAFFECTED = {f"J-GEN-02-1-{no}" for no in T5P_VOUCHERS}


def t5p_gen05(run: ProcedureRun) -> RuleResult:
    """T5' 的 GEN-05: #499 删去后 R09 只剩 XJ-397-3 (编号不变), 其余同 T0. 字段同连库 SqlRuleEngine 的结果 (连库用例
    断言两者相等)."""
    base = xero_gen05_result(run)
    kept = [x for x in base.exceptions if x.source_ids != ["XJ-499-2"]]
    return engine_result("GEN-05", base.model_copy(update={"exceptions": kept}))


# T5' 的规则预置: GEN-05 按上; PLN-07 在副本上 A01 少了 710, 711 两条, 只在连库断言, 离线沿用 T0 的 (同 T4)
T5P_RULES: dict[str, Callable[[ProcedureRun], RuleResult]] = {**XERO_RULES, "GEN-05": t5p_gen05}


def assert_t5(base: EngagementState, removed: EngagementState, project_fields: Iterable[str] = ()) -> None:
    """R253 验收 1, 3: T5 (T0 副本) 上 R09 两条: XJ-397-3 (1921221), XJ-499-2 (82500); XJ-499 的判断为 misstatement
    (T5_DRAFT), 只解释 R09-2, 有复核任务. T5' 上 R09 只剩 XJ-397-3, 与 T0 的那条逐字段相同, R10 至 R12 也都相同; 没有
    XJ-499 的判断与复核任务; 其余 6 条判断与 T0 逐字段相同 (除 RUN_FIELDS 与随项目不同的 project_fields)."""
    r09 = [(x.id, x.source_ids, x.amount_cents) for x in gen05_findings(base, "R09")]
    assert r09 == [("X-GEN-05-1-R09-1", ["XJ-397-3"], 1921221), ("X-GEN-05-1-R09-2", ["XJ-499-2"], 82500)]
    assert gen05_findings(removed, "R09") == gen05_findings(base, "R09")[:1]
    for rule_id in ("R10", "R11", "R12"):
        assert gen05_findings(removed, rule_id) == gen05_findings(base, rule_id), rule_id
    judgments = {j.id: j for j in base["judgments"]}
    depreciation = judgments[T5_XJ499]
    assert (depreciation.proposed_action, depreciation.misstatement_draft) == (ProposedAction.misstatement, T5_DRAFT)
    assert depreciation.exception_ids == ["X-GEN-05-1-R09-2"]
    assert f"T-{T5_XJ499}-v1" in {t.id for t in base["review_tasks"]}
    assert sorted(j.subject_id for j in removed["judgments"] if j.procedure_id == "GEN-02") == T5P_VOUCHERS
    assert not [t.id for t in removed["review_tasks"] if T5_XJ499 in t.id]
    same_judgments(removed, base, T5P_UNAFFECTED, exclude=project_fields)


def test_t5p_patch_removes_the_depreciation_entry(xero_case: Any) -> None:
    """R253 验收 2: T5' 补丁删去 JournalNumber 499 (改前 3 行: 710 借 750.00, 711 贷 825.00, 820 借 75.00; xero_case
    已核对改前值), 试算表 710 由 75000 调为 0, 711 由 -82500 调为 0, 820 由 -119197 调为 -126697 (xero_case 已核对副本
    借贷平衡且等于分录推出的余额); 导入后的各表与原目录相比只少 XJ-499 的 3 行, 试算表变这 3 行 (710, 711 余额为 0
    的行照留). 补丁中检索不到登录账号联系人."""
    case: XeroCase = xero_case("T5p")
    original = imported_tables()
    lines = {r.id: (r.account_code, r.debit_cents, r.credit_cents) for r in original[TableKind.journal_entries]}
    assert {i: v for i, v in lines.items() if i.startswith("XJ-499-")} == {
        "XJ-499-1": ("710", 75000, 0),
        "XJ-499-2": ("711", 0, 82500),
        "XJ-499-3": ("820", 7500, 0),
    }
    assert table_diff(original, imported_tables(case.loader)) == {
        TableKind.journal_entries: TableDiff(added=[], deleted=["XJ-499-1", "XJ-499-2", "XJ-499-3"], changed={}),
        TableKind.trial_balance: TableDiff(
            added=[],
            deleted=[],
            changed={
                "current:710": {"balance_cents": (75000, 0)},
                "current:711": {"balance_cents": (-82500, 0)},
                "current:820": {"balance_cents": (-119197, -126697)},
            },
        ),
    }
    assert scan_copies(SNAPSHOTS / SNAPSHOT, [INJECTIONS / "T5p.json"]) == []


async def test_t5_offline_depreciation(xero_case: Any) -> None:
    """R253 验收 1, 3 (离线): 演示配置的功能集下, T0 (即 T5) 与 T5' 副本各跑 GEN-02 的依赖闭包, 规则预置 XERO_RULES 与
    T5P_RULES, 大模型都读运行副本: 两边的对比见 assert_t5; 75000 即 T0 副本中 XJ-499-1 (710) 借方的金额; T5' 的大模型
    调用少 XJ-499 一次, 都一次通过."""
    t0, t5p = xero_case("T0", base="t0"), xero_case("T5p", base="t5p")
    base = (await run_programs(await offline_deps(t0, features=JOURNAL_FEATURES), ["GEN-02"])).state
    ran = await run_programs(await offline_deps(t5p, rules=T5P_RULES, features=JOURNAL_FEATURES), ["GEN-02"])
    assert_t5(base, ran.state)
    [line] = [r for r in imported_tables(t0.loader)[TableKind.journal_entries] if r.id == "XJ-499-1"]
    assert (line.account_code, line.debit_cents, line.credit_cents) == ("710", T5_DRAFT.amount_cents, 0)
    calls = llm_calls(ran)
    assert [key for pid, key, *_ in calls if pid == "GEN-02"] == T5P_VOUCHERS
    assert {(attempt, failed) for _, _, attempt, failed in calls} == {(1, None)}


# ── T4 银行回函余额差异 (需求 R123) ──


def t4_b01(run: ProcedureRun) -> AuditException:
    """T4 的 CSH-05 B01: BA-090 对账单期末 -1090044 (BT-090-0072), 账面 (副本试算表 090) -1035044, 差 -55000; 字段同
    连库 SqlRuleEngine 的结果 (连库用例断言两者相等)."""
    return AuditException(
        id=f"X-CSH-05-{run.run_no}-B01-1",
        procedure_id="CSH-05",
        run_id=run.id,
        rule_id="B01",
        severity="medium",
        source_ids=["BA-090"],
        amount_cents=-55000,
        detail={
            "statement_balance_cents": -1090044,
            "book_balance_cents": -1035044,
            "last_bank_transaction_id": "BT-090-0072",
        },
        evidence=[
            EvidencePointer(
                type="bank",
                source_type="bank_account",
                source_id="BA-090",
                locator="amount_cents, statement_balance_cents, book_balance_cents, last_bank_transaction_id",
                reliability="external_copy",
            )
        ],
    )


def t4_rules(case: XeroCase) -> dict[str, Callable[[ProcedureRun], RuleResult]]:
    """T4 的规则预置: CSH-05 出 B01, 总体按副本的表派生 (090 的账面为调过的 -1035044, 需求 R327); CSH-02 的候选行
    (D02 至 D04) 按副本的表派生; 其余同 T0 (XERO_RULES). PLN-07 在副本上 A01 少了 090 一条, 只在连库断言, 离线沿用
    T0 的 (不影响现金一串)."""
    tables = xero_tables(case.loader)
    population = csh05_population(
        tables["bank_accounts"], tables["bank_transactions"], tables["trial_balance"], XERO_PERIOD[1]
    )
    return {
        **XERO_RULES,
        "CSH-05": lambda run: engine_result("CSH-05", RuleResult(exceptions=[t4_b01(run)], rows={}), [population]),
        "CSH-02": lambda run: RuleResult(exceptions=[], rows=xero_csh02_rows(case.loader)),
    }


def test_t4_patch_moves_the_receipt_into_the_period(xero_case: Any) -> None:
    """验收 1: 补丁把 JournalNumber 637 (Yarra Transport 付 ORC1021 的 550.00) 的日期由 10-06 改为 09-30, 试算表 090 由
    -1090044 调为 -1035044, 610 由 1019950 调为 964950 (xero_case 已核对副本借贷平衡且等于分录推出的余额); 导入后的各表
    与原目录相比只有 XJ-637-1, -2 的入账日与这两行试算表."""
    case: XeroCase = xero_case("T4")
    moved = {"posting_date": (date(2026, 10, 6), date(2026, 9, 30))}
    assert table_diff(imported_tables(), imported_tables(case.loader)) == {
        TableKind.journal_entries: TableDiff(added=[], deleted=[], changed={"XJ-637-1": moved, "XJ-637-2": moved}),
        TableKind.trial_balance: TableDiff(
            added=[],
            deleted=[],
            changed={
                "current:090": {"balance_cents": (-1090044, -1035044)},
                "current:610": {"balance_cents": (1019950, 964950)},
            },
        ),
    }


async def test_t4_offline_deposit_in_transit(xero_case: Any) -> None:
    """验收 2: T4 副本上跑 CSH-02 的依赖闭包, 规则预置 B01 与副本的候选行, BA-090 的判断与 A7 两轮取 llm_injected/T4:
    C-BA-090 账面 -1035044, 差额 -55000, 结果 difference, D01 例外 -55000; 判断 accept, 解释 CSH-05 B01 与 CSH-02 D01
    两条, 引 D03 的 XJ-637-2 与 D02 的 BT-090-0081 (两行都在请求的上下文里); A7 复核一致; 全部一次通过. BA-091 一侧
    (C-BA-091 agreed, D04 例外 4853, 判断 misstatement) 与 T0 副本上的逐字段相同 (判断除 RUN_FIELDS)."""
    t4, t0 = xero_case("T4", base="t4"), xero_case("T0", base="t0")
    client = LayeredMockLLMClient([LLM_INJECTED / "T4", t4.supplements / "llm"])
    ran = await run_programs(await offline_deps(t4, llm=client, rules=t4_rules(t4)), ["CSH-02"])
    baseline = (await run_programs(await offline_deps(t0), ["CSH-02"])).state
    confirmations = {c.id: c for c in ran.state["confirmations"]}
    c090 = confirmations["C-BA-090"]
    assert (c090.result, c090.confirmed_balance_cents, c090.book_balance_cents, c090.difference_cents) == (
        ConfirmationResult.difference,
        -1090044,
        -1035044,
        -55000,
    )
    exceptions = {x.id: x for x in ran.state["exceptions"]}
    assert (exceptions["X-CSH-02-1-D01-1"].source_ids, exceptions["X-CSH-02-1-D01-1"].amount_cents) == (
        ["BA-090"],
        -55000,
    )
    judgments = {j.id: j for j in ran.state["judgments"]}
    transit = judgments["J-CSH-02-1-BA-090"]
    assert (transit.proposed_action, transit.misstatement_draft) == (ProposedAction.accept, None)
    assert transit.exception_ids == ["X-CSH-05-1-B01-1", "X-CSH-02-1-D01-1"]
    assert {("journal_entry", "XJ-637-2"), ("bank_transaction", "BT-090-0081")} <= {
        (p.source_type, p.source_id) for p in transit.evidence
    }
    assert transit.critic_verdict is CriticVerdict.consistent
    [request] = [r for r in client.requests if (r.procedure_id, r.fixture_key) == ("CSH-02", "BA-090")]
    assert '"id": "XJ-637-2"' in request_text(request) and '"id": "BT-090-0081"' in request_text(request)
    calls = llm_calls(ran)
    assert {(attempt, failed) for _, _, attempt, failed in calls} == {(1, None)}
    assert [(pid, key) for pid, key, *_ in calls if pid in ("CSH-02", "GEN-09")] == [
        ("CSH-02", "BA-090"),  # A7 两轮紧跟在该判断之后
        ("GEN-09", "BA-090-r1"),
        ("GEN-09", "BA-090-r2"),
        ("CSH-02", "BA-091"),
    ]
    assert sorted((r.procedure_id, r.fixture_key) for r in client.reads if r.root == LLM_INJECTED / "T4") == [
        ("CSH-02", "BA-090"),
        ("GEN-09", "BA-090-r1"),
        ("GEN-09", "BA-090-r2"),
    ]
    before = {c.id: c for c in baseline["confirmations"]}
    assert confirmations["C-BA-091"] == before["C-BA-091"] and before["C-BA-091"].result is ConfirmationResult.agreed
    d04 = {x.id: x for x in baseline["exceptions"]}["X-CSH-02-1-D04-1"]
    assert exceptions["X-CSH-02-1-D04-1"] == d04 and d04.amount_cents == 4853
    same_judgments(ran.state, baseline, {"J-CSH-02-1-BA-091"})


# ── 真实大模型成对断言 (需求 R124) 的辅助与不联网的自测 ──

T4_TRANSIT = "J-CSH-02-1-BA-090"
Build = Callable[[XeroCase, LLMClient], Awaitable[XeroHarness]]  # 在副本上装配测试台 (离线或连库), 大模型为给定客户端


async def paired_side(
    build: Build,
    case: XeroCase,
    mock_roots: list[Path],
    live: LLMClient,
    key: tuple[str, str],
    budget: Budget,
    programs: list[str],
    judgment_id: str | None,
) -> tuple[Ran, list[XeroHarness]]:
    """成对断言的一侧: 只有被测对象 key (程序, fixture_key) 交 live (经 budget 计数, 超限即停发), 其余 (上游 DOC-04,
    REV-03, DOC-06, 同程序的其他对象, A7) 读 mock_roots; 测试台跑 programs 的依赖闭包. 被测判断 judgment_id 没出来
    (三次尝试都未通过校验) 时换新项目重做, 至多 1 次; 该侧本不该有判断 (None) 时不重做. 每次做完查一次预算 (已超限
    即失败). 返回最后一次的结果与各次的测试台 (取留痕用)."""
    built: list[XeroHarness] = []
    for _ in range(2):
        client = RoutedLLMClient(LayeredMockLLMClient(mock_roots), live, {key}, budget)
        h = await build(case, client)
        ran = await run_programs(h, programs)
        built.append(h)
        budget.check()
        if judgment_id is None or any(j.id == judgment_id for j in ran.state["judgments"]):
            break
    return ran, built


def judgment_of(state: EngagementState, judgment_id: str) -> Judgment:
    found = [j for j in state["judgments"] if j.id == judgment_id]
    assert found, f"没有判断 {judgment_id}"
    return found[0]


def assert_t1_pair(base: EngagementState, injected: EngagementState) -> None:
    """R124 验收 2: ORC1026 的判断在 T0 副本上为 accept 且无错报草稿; 在 T1 副本上为 misstatement, 草稿金额 650000,
    Cutoff, overstated, 科目含 200, Sales 或 Revenue (不分大小写), 解释的例外含 R01 与 R03."""
    before, after = judgment_of(base, T1_ORC1026), judgment_of(injected, T1_ORC1026)
    assert (before.proposed_action, before.misstatement_draft) == (ProposedAction.accept, None)
    draft = after.misstatement_draft
    assert after.proposed_action is ProposedAction.misstatement and draft is not None
    assert (draft.amount_cents, draft.assertion, draft.direction) == (650000, Assertion.Cutoff, Direction.overstated)
    assert re.search(r"200|sales|revenue", draft.account, re.IGNORECASE), draft.account
    explained = {x.rule_id for x in injected["exceptions"] if x.id in after.exception_ids}
    assert {"R01", "R03"} <= explained, after.exception_ids


def assert_t4_pair(base: EngagementState, injected: EngagementState) -> None:
    """R124 验收 3: T0 副本上没有 BA-090 的 D01 例外, 也没有 BA-090 的判断; T4 副本上有 D01 (-55000), BA-090 的判断为
    accept 或 close 且无错报草稿."""

    def d01(state: EngagementState) -> list[AuditException]:
        return [x for x in state["exceptions"] if x.rule_id == "D01" and x.source_ids == ["BA-090"]]

    assert d01(base) == [] and T4_TRANSIT not in {j.id for j in base["judgments"]}
    assert [x.amount_cents for x in d01(injected)] == [-55000]
    transit = judgment_of(injected, T4_TRANSIT)
    assert transit.proposed_action in (ProposedAction.accept, ProposedAction.close), transit.proposed_action
    assert transit.misstatement_draft is None


def offline_build(rules: Any, features: frozenset[str] = BASELINE) -> Build:
    """自测用的装配: 离线测试台, 规则预置 rules, 功能集 features (缺省为基线)."""

    async def build(case: XeroCase, llm: LLMClient) -> XeroHarness:
        return await offline_deps(case, llm=llm, rules=rules, features=features)

    return build


async def test_paired_assertions_tell_right_from_wrong(xero_case: Any) -> None:
    """R124 自测 (不联网): live 换成读 fixture 的假客户端, 基线侧给运行副本的输出, 注入侧给 llm_injected 的: 两对断言都
    通过, 且各侧只有被测对象交给 live. 答反的都报错: T1 两侧都 accept, T4 基线侧也有 D01, T4 的判断建议了错报."""
    t0, t1, t4 = xero_case("T0", base="t0"), xero_case("T1", base="t1"), xero_case("T4", base="t4")
    budget = Budget(max_calls=10)
    base, _ = await paired_side(
        offline_build(XERO_RULES),
        t0,
        [t0.supplements / "llm"],
        LayeredMockLLMClient([t0.supplements / "llm"]),
        ("REV-02", ORC1026),
        budget,
        ["REV-02", "CSH-02"],
        T1_ORC1026,
    )
    cutoff, _ = await paired_side(
        offline_build(T1_RULES),
        t1,
        [t1.supplements / "llm"],
        LayeredMockLLMClient([LLM_INJECTED / "T1"]),
        ("REV-02", ORC1026),
        budget,
        ["REV-02"],
        T1_ORC1026,
    )
    transit, _ = await paired_side(
        offline_build(t4_rules(t4)),
        t4,
        [LLM_INJECTED / "T4", t4.supplements / "llm"],
        LayeredMockLLMClient([LLM_INJECTED / "T4"]),
        ("CSH-02", "BA-090"),
        budget,
        ["CSH-02"],
        T4_TRANSIT,
    )
    assert (budget.used.calls, budget.exceeded) == (3, False)  # 两侧的 ORC1026 与 T4 的 BA-090 各一次
    assert_t1_pair(base.state, cutoff.state)
    assert_t4_pair(base.state, transit.state)
    with pytest.raises(AssertionError):
        assert_t1_pair(base.state, base.state)
    with pytest.raises(AssertionError):
        assert_t4_pair(transit.state, transit.state)
    wrong = judgment_of(transit.state, T4_TRANSIT).model_copy(
        update={"proposed_action": ProposedAction.misstatement, "misstatement_draft": T1_DRAFT}
    )
    with pytest.raises(AssertionError):
        assert_t4_pair(base.state, {**transit.state, "judgments": [wrong]})  # type: ignore[typeddict-item]


async def test_paired_side_redoes_once_without_the_judgment(xero_case: Any, tmp_path: Path) -> None:
    """R124 自测: 被测对象的输出三次都不合格 (live 返回空对象) 时判断出不来, 换新项目重做一次后不再重做: 共装配两次,
    live 计 6 次调用 (每次三次尝试); 基线侧本不该有判断 (judgment_id 为 None) 时只做一次."""
    t0 = xero_case("T0")
    bad = tmp_path / "bad"
    (bad / "REV-02").mkdir(parents=True)
    (bad / "REV-02" / f"{ORC1026}.json").write_text("{}", encoding="utf-8")
    budget = Budget(max_calls=10)
    args = (offline_build(XERO_RULES), t0, [t0.supplements / "llm"], LayeredMockLLMClient([bad]), ("REV-02", ORC1026))
    ran, built = await paired_side(*args, budget, ["REV-02"], T1_ORC1026)
    assert len(built) == 2 and budget.used.calls == 6
    assert T1_ORC1026 not in {j.id for j in ran.state["judgments"]}
    _, once = await paired_side(*args, Budget(max_calls=10), ["REV-02"], None)
    assert len(once) == 1
