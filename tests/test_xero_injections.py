"""注入测试的离线断言 (设计稿 v2.1 第 8 节): 在 xero_case 的副本上经测试台 (tests/xero_harness) 跑现有程序.
- T0 基线副本 (需求 R121): 不打补丁的副本与原目录等价, 现有程序在副本上的结果即各场景的对照值. 七张表逐行相同已由
  tests/test_xero_case (R74 验收 3) 断言, 不另写.
- T1 收入截止 (需求 R122, 设计稿第 8.3 节): 规则引擎预置该场景的例外, 大模型输出叠在 tests/fixtures/llm_injected/T1.
离线跑法只检编排与校验链路: 判断结论是预写的 fixture, 不证明系统能识别错报 (第 8.1 节). 副本要本地快照 (没有时跳过),
标 external. 连库的见 tests/test_xero_injections_db."""

from collections.abc import Callable
from datetime import date
from typing import Any

import pytest
import pytest_asyncio

from backend.contracts.data import TableKind
from backend.contracts.models import (
    AuditException,
    Confirmation,
    ConfirmationResult,
    EvidencePointer,
    ExceptionDisposition,
    Judgment,
    MisstatementDraft,
    ProcedureRun,
    ProposedAction,
    TrailKind,
)
from backend.contracts.ports import AuditOutputStore
from backend.contracts.services import RuleResult
from backend.contracts.state import EngagementState
from tests.test_e2e_xero17 import Xero17, run_xero17
from tests.xero_fakes import (
    ORC1026,
    REPO,
    XERO_RULES,
    xero_contracts,
    xero_documents,
    xero_rev08_exception,
)
from tests.xero_harness import (
    LayeredMockLLMClient,
    Ran,
    TableDiff,
    imported_tables,
    offline_deps,
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


def same_judgments(left: EngagementState, right: EngagementState, ids: set[str]) -> None:
    """两次执行中 ids 这几条判断逐字段相同 (除 RUN_FIELDS)."""
    a = {j.id: j.model_dump(exclude=RUN_FIELDS) for j in left["judgments"] if j.id in ids}
    b = {j.id: j.model_dump(exclude=RUN_FIELDS) for j in right["judgments"] if j.id in ids}
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
