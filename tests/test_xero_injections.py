"""注入测试的离线断言 (设计稿 v2.1 第 8 节): 在 xero_case 的副本上经测试台 (tests/xero_harness) 跑现有程序. 本条
(需求 R121) 写 T0 基线副本: 不打补丁的副本与原目录等价, 现有程序在副本上的结果即 T1, T4 等场景的对照值. 七张表
逐行相同已由 tests/test_xero_case (R74 验收 3) 断言, 不另写. 副本要本地快照 (没有时跳过), 标 external.
连库的 T0 见 tests/test_xero_injections_db."""

from typing import Any

import pytest
import pytest_asyncio

from backend.contracts.data import TableKind
from backend.contracts.models import (
    AuditException,
    Confirmation,
    ConfirmationResult,
    ExceptionDisposition,
    Judgment,
    ProposedAction,
)
from backend.contracts.ports import AuditOutputStore
from backend.contracts.state import EngagementState
from tests.test_e2e_xero17 import Xero17, run_xero17
from tests.xero_fakes import ORC1026, xero_contracts, xero_documents
from tests.xero_harness import imported_tables, offline_deps, run_programs, table_diff
from tests.xero_injection import INJECTIONS, SNAPSHOT, SNAPSHOTS, XeroCase, contact_count, scan_copies

pytestmark = pytest.mark.external
shared_loop = pytest.mark.asyncio(loop_scope="module")  # 与模块共用的 R119 流程 (flow17) 同一个事件循环
T0_PROGRAMS = ["REV-02", "CSH-02"]  # 依赖闭包为 REV-08, REV-03 (与 DOC-04), REV-02, CSH-04, CSH-05, DOC-06, CSH-02
# 两次执行之间必然不同的字段: 判断的写入时刻与所记留痕的序号 (各自的留痕从头编号)
RUN_FIELDS = {"created_at", "trail_ref"}


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
