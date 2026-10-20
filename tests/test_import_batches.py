"""需求 R277: 智能导入的批次 (上传一批, 查确认单, 调整单项, 确认导入). 存储用假实现, 大模型读上传的固定输出.

Xero 网页导出只读不改; 合同 PDF 用演示合同的文档 ID 与页数 (固定输出的证据指针指向它们), 内容是假的.
"""

import asyncio
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from backend.adapters.llm import DatasetFixtureLLMClient
from backend.agents.base import AgentDeps
from backend.agents.ingest import unfinished
from backend.agents.sheets import SheetIntake
from backend.contracts.data import (
    REGISTRY_TARGET,
    BatchIssueCode,
    BatchItemEdit,
    DataChange,
    DataInput,
    DataRequest,
    FieldSource,
    ImportBatch,
    SheetReading,
    TableKind,
    TableOrigin,
    TablePlan,
)
from backend.contracts.errors import DataRejected, DecisionRejected
from backend.contracts.models import DocumentRef, DocumentType, Engagement, Reliability, TrailEntry, TrailKind
from backend.core.config import get_settings
from backend.services import data_import, data_service
from backend.services.data_import import MASK, cell_text, raw_rows
from backend.services.data_quality import document_request
from backend.services.data_service import DataService
from backend.services.import_batches import BatchIntake, column_sample, period_issue, same_company, staged_of
from backend.services.judgment_engine import JudgmentEngine, OutputValidator
from tests.external import needs, raw_dir
from tests.fakes import (
    ENGAGEMENT,
    NOW,
    NUMBAT_CONTRACTS,
    NUMBAT_PROFILE,
    NUMBAT_REPLIES,
    UPLOAD_FIXTURES,
    FakeDataStore,
    FakeEngagementStore,
    FakeEvidenceStore,
    FakeFinancialStore,
    LayeredMockLLM,
    api_client,
    make_runner,
    wait_idle,
)
from tests.test_documents_upload import workbook_bytes
from tests.test_registry_extract import DOCUMENT as REGISTRY_DOCUMENT
from tests.test_registry_extract import REQUEST as REGISTRY_REQUEST
from tests.test_registry_extract import fixture_extraction as registry_fixture
from tests.test_sheet_intake import personal_values

EID = ENGAGEMENT.id
REPO = Path(__file__).resolve().parents[1]
DEMO = REPO / "data" / "demo"
MAIN_FIXTURES = UPLOAD_FIXTURES.parents[1] / "llm"
XERO_PROFILE = NUMBAT_PROFILE.model_copy(update={"legal_name": "Demo Company (AU) Pty Ltd"})  # 同 Xero 演示的画像名
BASE = f"/api/v1/engagements/{EID}/data/batches"


class Batch:
    """一套批次测试的环境: runner, 数据服务与几个假存储."""

    def __init__(self, data: FakeDataStore | None = None, profile: Any = NUMBAT_PROFILE) -> None:
        self.data = data if data is not None else FakeDataStore(ready=False)
        self.financial = FakeFinancialStore()
        self.runner, _ = make_runner(
            engagement_store=FakeEngagementStore([profile], [ENGAGEMENT]),
            data=self.data,
            financial_store=self.financial,
        )
        self.financial.document_refs.clear()  # 建图辅助按种子登记的 3 份合同不要, 这里都靠上传
        evidence = self.runner.deps.evidence_store
        assert isinstance(evidence, FakeEvidenceStore)
        self.evidence = evidence
        # 上传的合同取演示 PDF 的文档 ID 与页数 (固定输出的证据指针指向它们)
        evidence.ids.update({d.file_name: d.id for d in NUMBAT_CONTRACTS})
        evidence.page_counts.update({d.file_name: d.page_count for d in NUMBAT_CONTRACTS})
        self.service = DataService(self.deps(), self.runner)
        self.checkups = 0
        real = self.service.checkup

        async def counted(eid: str) -> None:
            self.checkups += 1
            await real(eid)

        self.service.checkup = counted  # type: ignore[method-assign]

    def deps(self) -> AgentDeps:
        """runner 的依赖换成先读上传固定输出的大模型 (分类, 账表识别), 存储仍是同一套."""
        deps = self.runner.deps
        llm = LayeredMockLLM(UPLOAD_FIXTURES, MAIN_FIXTURES)
        validator = OutputValidator(deps.financial_store, deps.library, deps.trail)
        return replace(deps, llm=llm, judgments=JudgmentEngine(llm, validator, deps.trail, deps.library))

    async def upload(self, files: list[tuple[str, bytes]], request_id: str | None = None) -> ImportBatch:
        batch = await self.service.start_batch(EID, files, actor="U-1", request_id=request_id)
        await asyncio.gather(*list(self.service.tasks))
        return await self.service.batch(EID, batch.id)

    def document(self, document_id: str) -> DocumentRef:
        return next(d for d in self.financial.document_refs if d.id == document_id)


def demo_csv(name: str) -> tuple[str, bytes]:
    return f"{name}.csv", (DEMO / f"{name}.csv").read_bytes()


def raw_file(name: str) -> tuple[str, bytes]:
    return name, (raw_dir() / name).read_bytes()  # Xero 网页导出, 只读


def contract_pdf(n: int) -> tuple[str, bytes]:
    return f"CON-00{n}.pdf", f"%PDF CON-00{n}".encode()


def codes(item: Any) -> list[BatchIssueCode]:
    return [i.code for i in item.issues]


def trail_entries(env: Batch, kind: TrailKind, action: str | None = None) -> list[TrailEntry]:
    return [
        e
        for e in env.runner.deps.trail.entries
        if e.kind is kind and (action is None or e.payload.get("action") == action)
    ]


class Crash(BaseException):
    """模拟进程在两步之间退出 (不是 Exception, 不被业务代码兜住)."""


# ── 样例 6: 混合一批 ──


async def test_mixed_batch_lists_six_items_and_writes_only_on_confirm() -> None:
    """Numbat 的三张 CSV 加三份合同: 确认单 6 项, 合同识别为合同并带抽取要素; 确认前财务表与合同不变, 文件暂存
    (staged, 类型 other) 不被程序使用; 确认后三张表一次写入 (数据版本 +1, 各表出处取各自的文件), 合同写入,
    文件按确认的类型登记, 体检只跑一次."""
    env = Batch()
    files = [demo_csv("accounts"), demo_csv("trial_balance"), demo_csv("journal_entries")]
    batch = await env.upload([*files, *(contract_pdf(n) for n in (1, 2, 3))])

    assert batch.status == "ready" and len(batch.items) == 6
    assert [i.id for i in batch.items] == [f"{batch.id}-{n}" for n in range(1, 7)]
    tables, documents = batch.items[:3], batch.items[3:]
    assert [(i.item_kind, i.plan.kind if i.plan else None, i.records) for i in tables] == [
        ("table", TableKind.accounts, 23),
        ("table", TableKind.trial_balance, 46),
        ("table", TableKind.journal_entries, 2686),
    ]
    assert all(i.selected and i.issues == [] and i.reading and i.reading.confidence == "high" for i in tables)
    assert [(i.item_kind, i.document_type, i.selected) for i in documents] == [
        ("document", DocumentType.contract, True)
    ] * 3
    contract = documents[0].contract
    assert contract is not None and contract.id == "CON-001" and contract.counterparty and contract.terms
    # 确认前: 财务表不变, 合同没写, 文件暂存, 启动时不补做, DOC-04 不取
    assert env.data.written == [] and env.financial.saved_contracts == {}
    staged = [env.document(f.document_id) for f in batch.files]
    assert {(d.status, d.type) for d in staged} == {("staged", DocumentType.other)}
    assert not any(unfinished(d) for d in staged)
    assert await env.financial.documents(EID, DocumentType.contract) == []  # DOC-04 只取合同类的文件
    assert env.checkups == 0

    done = await env.service.confirm_batch(EID, batch.id, "U-1")

    assert (done.status, done.committed_by, done.data_version) == ("committed", "U-1", 1)
    [(written, kwargs)] = env.data.written
    assert set(written) == {TableKind.accounts, TableKind.trial_balance, TableKind.journal_entries}
    assert {k: len(v) for k, v in written.items()} == {
        TableKind.accounts: 23,
        TableKind.trial_balance: 46,
        TableKind.journal_entries: 2686,
    }
    assert kwargs["origins"][TableKind.accounts] == TableOrigin(
        file_name="accounts.csv", document_id=tables[0].document_id, request_id=None
    )
    assert {v.file_name for v in env.data.table_versions} == {
        "accounts.csv",
        "trial_balance.csv",
        "journal_entries.csv",
    }
    # 各表的版本留痕带各自的原文件与证据文件
    assert {
        e.payload["table_kind"]: (e.payload["file_name"], e.payload["document_id"])
        for e in trail_entries(env, TrailKind.data, "import")
    } == {i.plan.kind.value: (i.file_name, i.document_id) for i in tables if i.plan and i.plan.kind}
    assert sorted(env.financial.saved_contracts) == ["CON-001", "CON-002", "CON-003"]
    registered = {d.file_name: (d.type, d.status) for d in env.financial.document_refs}
    assert registered["CON-002.pdf"] == (DocumentType.contract, "extracted")
    assert registered["accounts.csv"] == (DocumentType.other, "extracted")
    assert env.checkups == 1
    # 数据变动 (需求 R274): 三张表与合同记在同一个版本上
    changed = [DataInput.accounts, DataInput.trial_balance, DataInput.journal_entries, DataInput.contracts]
    assert env.data.data_changes == [DataChange(data_version=1, input=i) for i in changed]
    assert await env.service.confirm_batch(EID, batch.id, "U-1") == done  # 再确认返回原结果, 不再写
    assert len(env.data.written) == 1 and len(env.data.data_changes) == 4


async def test_confirming_only_contracts_bumps_the_data_version() -> None:
    """需求 R274: 确认写进的合同是程序执行之外的合同新版本 (同上传后的抽取), 记数据变动 contracts, 读合同的程序
    (REV-03, REV-02) 据此标过期; 批次里没有表时数据版本只为它 +1, 批次记这个版本."""
    env = Batch()
    batch = await env.upload([contract_pdf(1)])
    done = await env.service.confirm_batch(EID, batch.id, "U-1")
    assert (done.data_version, env.data.data_version, env.data.written) == (1, 1, [])
    assert env.data.data_changes == [DataChange(data_version=1, input=DataInput.contracts)]
    assert sorted(env.financial.saved_contracts) == ["CON-001"]


def test_migrations_have_a_single_head() -> None:
    """迁移只有一条链: 只有一个 head, import_batches (0010) 接在 xero_connections (0009) 之后."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(REPO / "alembic.ini")))
    assert len(script.get_heads()) == 1
    revision = script.get_revision("0010")
    assert revision is not None and revision.down_revision == "0009"


@pytest.mark.external
async def test_issue_samples_one_to_five_on_the_confirmation_sheet() -> None:
    """issue 的样例 1 至 5 经批次 (前端 "上传资料" 走的路径) 在 mock 下的确认单: 识别为哪张表, 将写入的行数, 把握,
    读到的公司与截至日, 问题与缺省勾选. 截至 2026-06-30 的项目, 画像名同 Xero 演示公司. 样例 3 的科目表有一处
    解析失败 (803 的 Type 为空, 第 44 行), 标红不勾."""
    env = Batch(profile=XERO_PROFILE)
    expected = {
        "trial_balance_2026-06-30.xlsx": (TableKind.trial_balance, 12, "Demo Company (AU)", date(2026, 6, 30), []),
        "journal_report.xlsx": (TableKind.journal_entries, 1017, "Demo Company (AU)", None, []),
        "chart_of_accounts.csv": (TableKind.accounts, None, None, None, [BatchIssueCode.parse_failed]),
        "receivable_invoice_detail.xlsx": (TableKind.sales_invoices, 42, "Demo Company (AU)", None, []),
    }
    for name, (kind, records, company, end, issues) in expected.items():
        [item] = (await env.upload([raw_file(name)])).items
        assert item.reading is not None and item.plan is not None
        assert (item.plan.kind, item.records, item.reading.confidence, item.reading.company) == (
            kind,
            records,
            "high",
            company,
        ), name
        assert end is None or item.reading.period_end == end
        assert (codes(item), item.selected) == (issues, not issues), name
    numbat = await Batch().upload(
        [
            demo_csv(n)
            for n in (
                "accounts",
                "trial_balance",
                "journal_entries",
                "sales_invoices",
                "bank_accounts",
                "bank_transactions",
                "delivery_notes",
            )
        ]
    )
    assert [(i.plan.kind.value if i.plan and i.plan.kind else None, i.records) for i in numbat.items] == [
        ("accounts", 23),
        ("trial_balance", 46),
        ("journal_entries", 2686),
        ("sales_invoices", 624),
        ("bank_accounts", 2),
        ("bank_transactions", 696),
        ("delivery_notes", 624),
    ]
    assert all(i.reading and i.reading.confidence == "high" and i.selected and not i.issues for i in numbat.items)


# ── 样例 7 与确认单的问题 ──


@pytest.mark.external
async def test_period_mismatch_is_flagged_and_unselected() -> None:
    """截至 2026-06-30 的项目 (画像名同 Xero 演示公司) 拖入 11-30 的试算表: 标红期间不符, 缺省不勾; 06-30 的没有问题.
    两张试算表同在一批, 都记同一张表出现两次."""
    env = Batch(profile=XERO_PROFILE)
    single = await env.upload([raw_file("trial_balance_2026-11-30.xlsx")])
    [item] = single.items
    assert codes(item) == [BatchIssueCode.period_mismatch] and not item.selected
    assert item.issues[0].params == {
        "period_start": None,
        "period_end": "2026-11-30",
        "expected_start": "2025-07-01",
        "expected_end": "2026-06-30",
    }
    assert item.records == 64  # 32 个科目, 本期上期各 32 行

    both = await env.upload([raw_file("trial_balance_2026-06-30.xlsx"), raw_file("trial_balance_2026-11-30.xlsx")])
    first, second = both.items
    assert codes(first) == [BatchIssueCode.duplicate_table]
    assert first.issues[0].params == {"kind": "trial_balance", "items": [second.id]}
    assert codes(second) == [BatchIssueCode.period_mismatch, BatchIssueCode.duplicate_table]
    assert not first.selected and not second.selected


@pytest.mark.external
async def test_company_low_confidence_failures_and_replacement_are_flagged() -> None:
    """Numbat 项目里: Xero 试算表的公司名不符; 没有 fixture 的 CSV 退回按列名匹配 (把握低); 科目表有解析失败
    (803 的 Type 为空, 附失败处); 已有数据的表将被替换 (写明现有行数); 都缺省不勾."""
    env = Batch(data=FakeDataStore())  # 种子已载入: 各表有行
    batch = await env.upload(
        [raw_file("trial_balance_2026-06-30.xlsx"), ("ledger-export.csv", demo_csv("accounts")[1])]
    )
    xero_tb, fallback = batch.items
    assert codes(xero_tb) == [BatchIssueCode.replaces_data, BatchIssueCode.company_mismatch]
    assert xero_tb.issues[1].params == {"company": "Demo Company (AU)", "expected": NUMBAT_PROFILE.legal_name}
    assert xero_tb.existing_records and xero_tb.issues[0].params == {
        "kind": "trial_balance",
        "existing": xero_tb.existing_records,
    }
    assert codes(fallback)[0] is BatchIssueCode.low_confidence
    assert fallback.reading is not None and fallback.reading.method == "column_names"
    assert not xero_tb.selected and not fallback.selected

    chart = (await env.upload([raw_file("chart_of_accounts.csv")])).items[0]
    assert BatchIssueCode.parse_failed in codes(chart) and chart.records is None
    assert (chart.failure_count, [f.row for f in chart.failures]) == (1, [44])


def test_period_rules_for_balances_and_period_reports() -> None:
    """只有截至日的须等于审计期末; 有起止的须覆盖整个审计期间 (只覆盖半年标红, 覆盖全年或更长不标)."""
    e = ENGAGEMENT

    def reading(start: date | None, end: date | None) -> SheetReading:
        plan = TablePlan(kind=None)
        return SheetReading(
            file_name="f.xlsx",
            sheet="s",
            plan=plan,
            period_start=start,
            period_end=end,
            confidence="high",
            reason="r",
            method="llm",
        )

    assert period_issue(reading(None, e.period_end), e) is None
    assert period_issue(reading(None, date(2026, 11, 30)), e) is not None
    assert period_issue(reading(e.period_start, e.period_end), e) is None
    assert period_issue(reading(date(2025, 1, 1), date(2026, 12, 31)), e) is None
    assert period_issue(reading(date(2026, 1, 1), e.period_end), e) is not None  # 只覆盖下半年
    assert period_issue(reading(e.period_start, date(2025, 12, 31)), e) is not None  # 只覆盖上半年
    assert period_issue(reading(None, None), e) is None


@pytest.mark.external
async def test_table_items_carry_columns_and_a_masked_sample() -> None:
    """表格项带方案表头行的各列与每列的样例 (调整时列对应界面用): 日记账报表表头在第 5 行, Posted By 是导出账号本人的
    姓名, 样例已遮蔽; 换了表头行的方案按新表头重算列. 断言只比遮蔽与否, 不把取值写进测试输出."""
    env = Batch(profile=XERO_PROFILE)
    batch = await env.upload([raw_file("journal_report.xlsx")])
    [item] = batch.items
    assert item.plan is not None and item.plan.header_row == 5
    assert item.columns == [
        "Date",
        "Journal ID",
        "Account Code",
        "Account",
        "Debit",
        "Credit",
        "Posted Date",
        "Posted By",
    ]
    assert (item.sample["Account Code"], item.sample["Account"]) == ("630", "Inventory")
    assert item.sample["Posted By"] == MASK
    assert not any("@" in value for value in item.sample.values())

    plan = item.plan.model_copy(update={"header_row": 1, "fields": {}, "skip": [], "group_title": None})
    edited = await env.service.edit_batch_item(EID, batch.id, item.id, BatchItemEdit(plan=plan), "U-1")
    assert len(edited.items[0].columns) == 1 and BatchIssueCode.plan_invalid in codes(edited.items[0])
    beyond = plan.model_copy(update={"header_row": 99999})
    edited = await env.service.edit_batch_item(EID, batch.id, item.id, BatchItemEdit(plan=beyond), "U-1")
    assert (edited.items[0].columns, edited.items[0].sample) == ([], {})


@pytest.mark.external
async def test_columns_and_sample_stay_masked_whatever_the_header_row() -> None:
    """列与样例的遮蔽同发给大模型的样本: 从第 1 行起整体遮蔽后再按表头行切. 日记账报表的表头行改到原表头之下
    (第 6 至 12 行) 时, 列名与样例里也没有导出账号本人的姓名与邮箱; 经调整单项返回的与存下的批次同样没有.
    只比计数, 名单只在内存里."""
    secrets = personal_values()

    def hits(columns: list[str], sample: dict[str, str]) -> int:
        texts = " ".join([*columns, *sample.values()]).lower()
        return sum(v in texts for v in secrets)

    _, rows = raw_rows("journal_report.xlsx", (raw_dir() / "journal_report.xlsx").read_bytes())
    raw = " ".join(cell_text(c) for _, cells in rows for c in cells).lower()
    assert sum(v in raw for v in secrets) > 0  # 原表里确有 (守卫有意义)
    for header_row in range(5, 13):
        assert hits(*column_sample(rows, header_row)) == 0, header_row
    columns, sample = column_sample(rows, 8)  # 表头行是一个数据行: Posted By 那一列的 "列名" 与样例都遮蔽
    assert (len(columns), columns[7], sample[columns[7]]) == (8, MASK, MASK)

    env = Batch(profile=XERO_PROFILE)
    batch = await env.upload([raw_file("journal_report.xlsx")])
    [item] = batch.items
    assert item.plan is not None
    for header_row in (6, 8, 10):
        plan = item.plan.model_copy(update={"header_row": header_row})
        edited = await env.service.edit_batch_item(EID, batch.id, item.id, BatchItemEdit(plan=plan), "U-1")
        got = edited.items[0]
        assert hits(got.columns, got.sample) == 0, header_row
        stored = env.data.import_batches[batch.id].model_dump_json().lower()
        assert sum(v in stored for v in secrets) == 0, header_row


async def test_email_headers_are_masked_in_columns() -> None:
    """列名本身是邮箱 (合成表格: Numbat 科目表的列再加一列邮箱表头, 按列名认作科目表): 表格项的列名换成 [masked],
    列与样例里都没有 "@"; 存下的批次同样没有."""
    data = b"code,name,type,finance@example.com\n1010,Cash at bank,asset,ok\n1100,Trade receivables,asset,ok\n"
    _, rows = raw_rows("ledger-accounts.csv", data)
    assert column_sample(rows, 1)[0] == ["code", "name", "type", MASK]
    env = Batch()
    batch = await env.upload([("ledger-accounts.csv", data)])
    [item] = batch.items
    assert (item.item_kind, item.plan.kind if item.plan else None) == ("table", TableKind.accounts)
    assert item.columns == ["code", "name", "type", MASK]
    assert not any("@" in v for v in [*item.columns, *item.sample.values()])
    assert "@" not in env.data.import_batches[batch.id].model_dump_json()


@pytest.mark.external
async def test_failure_values_are_masked_like_the_sample() -> None:
    """解析失败处的取值也遮蔽 (从已遮蔽的行里取): 把借方金额对应到 Posted By 列, 每行都解析失败; 确认单项的前 20 处,
    存下的批次与勾上后确认报的 IMPORT_PARSE_ERROR 里都没有导出账号本人的姓名与邮箱. 只比计数."""
    secrets = personal_values()
    env = Batch(profile=XERO_PROFILE)
    batch = await env.upload([raw_file("journal_report.xlsx")])
    [item] = batch.items
    assert item.plan is not None
    fields = {**item.plan.fields, "debit_cents": FieldSource(column="Posted By")}
    edited = await env.service.edit_batch_item(
        EID, batch.id, item.id, BatchItemEdit(plan=item.plan.model_copy(update={"fields": fields})), "U-1"
    )
    failed = edited.items[0]
    assert failed.failure_count > 0 and {f.column for f in failed.failures} == {"Posted By"}
    assert {f.value for f in failed.failures} == {MASK}

    def hits(text: str) -> int:
        return sum(v in text.lower() for v in secrets)

    assert hits(" ".join(f.value for f in failed.failures)) == 0
    assert hits(env.data.import_batches[batch.id].model_dump_json()) == 0
    await env.service.edit_batch_item(EID, batch.id, item.id, BatchItemEdit(selected=True), "U-1")
    with pytest.raises(DataRejected) as e:
        await env.service.confirm_batch(EID, batch.id, "U-1")
    assert e.value.code == "IMPORT_PARSE_ERROR" and len(e.value.extra["errors"]) == 20
    assert hits(str(e.value.extra["errors"])) == 0


def test_company_names_compare_without_company_type_words() -> None:
    assert same_company("Demo Company (AU)", "Demo Company (AU) Pty Ltd")
    assert same_company("NUMBAT ELECTRONIC COMPONENTS", NUMBAT_PROFILE.legal_name)
    assert not same_company("Demo Company (AU)", NUMBAT_PROFILE.legal_name)


async def test_workbook_without_ledger_sheets_is_a_document() -> None:
    """没有一个工作表是账表的表格文件 (报表草稿) 作一份文件处理: A3 分类为财务报表; 确认后按该类型登记."""
    env = Batch()
    batch = await env.upload([("draft-financial-statements.xlsx", workbook_bytes())])
    [item] = batch.items
    assert (item.item_kind, item.document_type, item.selected) == ("document", DocumentType.financial_statements, True)
    await env.service.confirm_batch(EID, batch.id, "U-1")
    assert (env.document(item.document_id).type, env.document(item.document_id).status) == (
        DocumentType.financial_statements,
        "classified",
    )
    assert env.data.written == [] and env.checkups == 1  # 没有表也体检一次


async def test_already_registered_file_is_not_staged_again() -> None:
    """之前经 "上传文件" 处理过的同一份合同: 沿用已有登记, 记 already_registered, 缺省不勾, 确认时不改."""
    env = Batch()
    await env.service.upload_documents(EID, [contract_pdf(1)], actor="U-1")
    await asyncio.gather(*list(env.service.tasks))
    before = env.document(next(d.id for d in env.financial.document_refs))
    batch = await env.upload([contract_pdf(1)])
    [item] = batch.items
    assert codes(item) == [BatchIssueCode.already_registered] and not item.selected
    assert item.issues[0].params == {"document_id": before.id, "type": "contract", "status": "extracted"}
    with pytest.raises(DataRejected) as e:
        await env.service.edit_batch_item(
            EID, batch.id, item.id, BatchItemEdit(document_type=DocumentType.minutes, reason="x"), "U-1"
        )
    assert e.value.code == "INVALID_VALUE" and env.document(item.document_id).type is DocumentType.contract


async def test_document_request_sets_the_type_without_classifying() -> None:
    """补交卡片上的上传带文件请求: 类型取请求的 (不调大模型分类), 确认时按它登记; 账表请求记在同表的版本上."""
    env = Batch()
    env.data.data_requests = {
        "REQ-board_minutes": DataRequest(
            id="REQ-board_minutes",
            kind="document",
            document_type=DocumentType.minutes,
            title="董事会会议记录",
            reason_code="not_provided",
            created_at=NOW,
        ),
        "REQ-accounts": DataRequest(
            id="REQ-accounts",
            kind="table",
            table_kind=TableKind.accounts,
            title="科目表",
            reason_code="not_provided",
            created_at=NOW,
        ),
    }
    batch = await env.upload([("minutes.pdf", b"%PDF minutes")], request_id="REQ-board_minutes")
    [item] = batch.items
    assert (item.document_type, item.type_reason) == (DocumentType.minutes, "按补交请求 REQ-board_minutes 上传")
    await env.service.confirm_batch(EID, batch.id, "U-1")
    assert env.document(item.document_id).type is DocumentType.minutes

    tables = await env.upload([demo_csv("accounts"), demo_csv("bank_accounts")], request_id="REQ-accounts")
    await env.service.confirm_batch(EID, tables.id, "U-1")
    origins = env.data.written[-1][1]["origins"]
    assert origins[TableKind.accounts].request_id == "REQ-accounts"
    assert origins[TableKind.bank_accounts].request_id is None


# ── 调整单项 ──


async def test_edit_plan_type_and_selection() -> None:
    """换方案: 按新方案重算并记调整人, 不再标把握低, 没手动勾选的随问题重定勾选; 改文件类型须写理由, 批次回到识别中,
    A3 在后台按新类型重新识别 (审计师的 classify 留痕); 手动勾选或取消之后不再随问题变."""
    env = Batch()
    batch = await env.upload([("ledger-export.csv", demo_csv("accounts")[1]), ("note.pdf", b"%PDF note")])
    table, document = batch.items
    assert table.reading is not None and not table.selected
    edited = await env.service.edit_batch_item(EID, batch.id, table.id, BatchItemEdit(plan=table.reading.plan), "U-2")
    table = edited.items[0]
    assert (table.adjusted_by, table.issues, table.selected, table.records) == ("U-2", [], True, 23)

    with pytest.raises(DataRejected) as e:
        await env.service.edit_batch_item(
            EID, batch.id, document.id, BatchItemEdit(document_type=DocumentType.minutes), "U-2"
        )
    assert e.value.code == "MISSING_FIELD"
    edited = await env.service.edit_batch_item(
        EID, batch.id, document.id, BatchItemEdit(document_type=DocumentType.minutes, reason="董事会记录"), "U-2"
    )
    assert edited.status == "processing" and [f.done for f in edited.files] == [True, False]
    assert edited.items[1].document_type is DocumentType.minutes  # 识别中先显示新类型
    await asyncio.gather(*list(env.service.tasks))
    edited = await env.service.batch(EID, batch.id)
    document = edited.items[1]
    assert edited.status == "ready" and [i.id for i in edited.items] == [table.id, document.id]
    assert (document.document_type, document.type_reason, document.adjusted_by) == (
        DocumentType.minutes,
        "董事会记录",
        "U-2",
    )
    trail = next(e for e in env.runner.deps.trail.entries if e.id == document.job)
    assert (trail.actor, trail.payload["document_type"], trail.payload["reason"]) == ("U-2", "minutes", "董事会记录")
    edited = await env.service.edit_batch_item(EID, batch.id, table.id, BatchItemEdit(selected=False), "U-2")
    assert (edited.items[0].selected, edited.items[0].chosen_by) == (False, "U-2")

    for item_id, edit in (
        (table.id, BatchItemEdit(document_type=DocumentType.minutes, reason="x")),
        (document.id, BatchItemEdit(plan=TablePlan(kind=None))),
    ):
        with pytest.raises(DataRejected) as e:
            await env.service.edit_batch_item(EID, batch.id, item_id, edit, "U-2")
        assert e.value.code == "INVALID_VALUE"
    with pytest.raises(LookupError):
        await env.service.edit_batch_item(EID, batch.id, "BAT-x-9", BatchItemEdit(selected=True), "U-2")

    await env.service.confirm_batch(EID, batch.id, "U-1")
    assert env.data.written == []  # 表已取消勾选
    assert env.document(document.document_id).type is DocumentType.minutes


@pytest.mark.external
async def test_confirm_rejects_duplicates_failures_and_not_ready() -> None:
    """勾了同一张表两项 INVALID_VALUE; 勾了有解析失败的表 IMPORT_PARSE_ERROR (附前 20 处); 还在识别 INVALID_VALUE;
    都不写任何表."""
    env = Batch(profile=XERO_PROFILE)
    batch = await env.upload([raw_file("trial_balance_2026-06-30.xlsx"), raw_file("trial_balance_2026-11-30.xlsx")])
    for item in batch.items:
        await env.service.edit_batch_item(EID, batch.id, item.id, BatchItemEdit(selected=True), "U-1")
    with pytest.raises(DataRejected) as e:
        await env.service.confirm_batch(EID, batch.id, "U-1")
    assert (e.value.code, e.value.message) == ("INVALID_VALUE", "同一张表勾了两项: trial_balance")

    chart = await env.upload([raw_file("chart_of_accounts.csv")])
    await env.service.edit_batch_item(EID, chart.id, chart.items[0].id, BatchItemEdit(selected=True), "U-1")
    with pytest.raises(DataRejected) as e:
        await env.service.confirm_batch(EID, chart.id, "U-1")
    assert e.value.code == "IMPORT_PARSE_ERROR"
    assert e.value.extra["errors"] == [{"row": 44, "column": "*Type", "value": ""}]

    pending = ImportBatch(
        id="BAT-pending",
        status="processing",
        created_by="U-1",
        created_at=NOW,
        files=[],
    )
    await env.data.save_batch(EID, pending)
    with pytest.raises(DataRejected) as e:
        await env.service.confirm_batch(EID, "BAT-pending", "U-1")
    assert e.value.code == "INVALID_VALUE"
    assert env.data.written == []


async def test_type_change_is_recognised_again_and_confirm_only_writes() -> None:
    """改类型时就按新类型重新识别与抽取 (新的作业, 该项回到识别中, 序号与位置不变): 合同改为会议纪要再改回合同,
    确认单上看得到新作业抽出的合同要素; 手动的勾选沿用. 确认只写不抽 (不调大模型), 合同取新作业的那一版."""
    env = Batch()
    batch = await env.upload([demo_csv("accounts"), contract_pdf(1), demo_csv("trial_balance")])
    item = batch.items[1]
    assert item.contract is not None
    first_run = item.contract.extracted_by_run
    first = await env.service.edit_batch_item(
        EID, batch.id, item.id, BatchItemEdit(document_type=DocumentType.minutes, reason="其实是会议纪要"), "U-2"
    )
    assert (first.items[1].contract, first.items[1].job) == (None, None)
    await asyncio.gather(*list(env.service.tasks))
    minutes = (await env.service.batch(EID, batch.id)).items[1]
    assert (minutes.document_type, minutes.contract, minutes.selected) == (DocumentType.minutes, None, True)
    await env.service.edit_batch_item(EID, batch.id, item.id, BatchItemEdit(selected=False), "U-2")
    restaged = await env.service.edit_batch_item(
        EID, batch.id, item.id, BatchItemEdit(document_type=DocumentType.contract, reason="还是合同"), "U-2"
    )
    assert restaged.status == "processing"
    pending = restaged.items[1]  # 识别中: 显示新类型, 旧的要素与作业已清
    assert (pending.document_type, pending.contract, pending.reply, pending.job, pending.issues) == (
        DocumentType.contract,
        None,
        None,
        None,
        [],
    )
    await asyncio.gather(*list(env.service.tasks))
    batch = await env.service.batch(EID, batch.id)
    assert batch.status == "ready" and [i.id for i in batch.items] == [f"{batch.id}-{n}" for n in (1, 2, 3)]
    item = batch.items[1]
    assert item.contract is not None and item.contract.extracted_by_run != first_run
    assert (item.document_type, item.type_reason, item.adjusted_by) == (DocumentType.contract, "还是合同", "U-2")
    assert (item.selected, item.chosen_by) == (False, "U-2")  # 手动取消的勾选沿用
    job = next(e for e in env.runner.deps.trail.entries if e.id == item.job)
    assert (job.actor, job.payload["document_type"]) == ("U-2", "contract")

    await env.service.edit_batch_item(EID, batch.id, item.id, BatchItemEdit(selected=True), "U-2")
    calls = len(trail_entries(env, TrailKind.llm_call))
    await env.service.confirm_batch(EID, batch.id, "U-1")
    assert len(trail_entries(env, TrailKind.llm_call)) == calls  # 确认只写不抽
    assert env.financial.saved_contracts["CON-001"].extracted_by_run == item.contract.extracted_by_run
    assert env.document(item.document_id).status == "extracted"


async def test_two_files_with_the_same_contract_number_chain_versions() -> None:
    """同一批里两份暂存文件抽出同一编号的合同 (如同名合同的另一稿): 确认时后一份接在前一份之后, 不互相覆盖."""
    env = Batch()
    batch = await env.upload([contract_pdf(1)])
    [item] = batch.items
    other = env.document(item.document_id).model_copy(update={"id": "DOC-other", "file_name": "CON-001 (2).pdf"})
    env.financial.document_refs.append(other)
    twin = item.model_copy(update={"id": f"{batch.id}-2", "document_id": other.id, "file_name": other.file_name})
    changes = await env.service._batch_documents(EID, batch, [], [item, twin])
    assert [[c.version for c in change.contracts] for change in changes] == [[1], [2]]


async def test_reply_and_failed_extraction_are_written_on_confirm(monkeypatch: pytest.MonkeyPatch) -> None:
    """回函副本: 确认单上带 DOC-06 的抽取结果, 确认时写 reply 第 1 版, 置 confirmation/extracted. 抽取没通过的合同:
    标红不勾, 审计师勾上后确认, 置 contract/needs_manual (extract), 不写合同. 确认前都不写."""
    import backend.agents.ingest as ingest

    env = Batch()
    env.evidence.ids.update({d.file_name: d.id for d in NUMBAT_REPLIES})
    env.evidence.page_counts.update({d.file_name: d.page_count for d in NUMBAT_REPLIES})

    async def no_contract(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(ingest, "extract_contract", no_contract)
    batch = await env.upload([("12345601.pdf", b"%PDF reply"), contract_pdf(1)])
    reply, contract = batch.items
    assert (reply.document_type, reply.selected) == (DocumentType.confirmation, True) and reply.reply is not None
    assert (contract.document_type, contract.contract, codes(contract)) == (
        DocumentType.contract,
        None,
        [BatchIssueCode.extraction_failed],
    )
    assert not contract.selected and env.data.extractions == {}
    await env.service.edit_batch_item(EID, batch.id, contract.id, BatchItemEdit(selected=True), "U-1")
    done = await env.service.confirm_batch(EID, batch.id, "U-1")

    assert (done.data_version, env.data.data_version, env.data.data_changes) == (None, 0, [])  # 没写表也没写合同
    reply_document = env.document(reply.document_id)
    assert (reply_document.type, reply_document.status) == (DocumentType.confirmation, "extracted")
    assert env.data.extractions[("reply", reply_document.id, 1)]["data"] == reply.reply
    failed = env.document(contract.document_id)
    assert (failed.type, failed.status, failed.failed_step) == (DocumentType.contract, "needs_manual", "extract")
    assert env.financial.saved_contracts == {}


async def test_unselected_files_are_not_imported_and_can_be_uploaded_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """确认时没勾选的文件转为未导入 (终态, 类型 other, 不被程序使用): 批次的文件记 not_imported, 写一条留痕 (留痕
    写失败时下次取锁补写); 文件列表里改类型被拒并提示重新上传. 重新上传同一文件: 回到暂存再识别, 勾上确认后照常
    登记. 同一文件还在别的没写入的批次里时不动."""
    env = Batch()
    batch = await env.upload([demo_csv("accounts"), contract_pdf(1)])
    table, contract = batch.items
    await env.service.edit_batch_item(EID, batch.id, contract.id, BatchItemEdit(selected=False), "U-1")
    real = data_service.record_transitions

    async def flaky(trail: Any, entries: Any) -> None:
        entries = list(entries)
        if any(e.payload.get("action") == "not_imported" for e in entries):
            raise RuntimeError("trail down")
        await real(trail, entries)

    monkeypatch.setattr(data_service, "record_transitions", flaky)
    done = await env.service.confirm_batch(EID, batch.id, "U-1")
    monkeypatch.setattr(data_service, "record_transitions", real)
    assert [f.not_imported for f in done.files] == [False, True]
    document = env.document(contract.document_id)
    assert (document.status, document.type) == ("not_imported", DocumentType.other)
    assert not unfinished(document) and await env.financial.documents(EID, DocumentType.contract) == []
    assert env.financial.saved_contracts == {} and env.document(table.document_id).status == "extracted"
    assert trail_entries(env, TrailKind.data, "not_imported") == []
    with pytest.raises(DataRejected) as e:
        await env.service.set_document_type(EID, document.id, DocumentType.contract, "是合同", "U-1")
    assert e.value.code == "INVALID_VALUE" and "重新上传" in e.value.message

    again = await env.upload([contract_pdf(1)])  # 取锁时补写了上一批的留痕
    [entry] = trail_entries(env, TrailKind.data, "not_imported")
    assert (entry.actor, entry.payload["batch_id"], entry.payload["document_ids"]) == ("U-1", batch.id, [document.id])
    [item] = again.items
    assert (item.document_id, item.document_type, item.selected) == (document.id, DocumentType.contract, True)
    assert item.contract is not None
    await env.service.confirm_batch(EID, again.id, "U-1")
    assert (env.document(document.id).status, env.document(document.id).type) == ("extracted", DocumentType.contract)
    assert sorted(env.financial.saved_contracts) == ["CON-001"]

    first = await env.upload([contract_pdf(2)])
    second = await env.upload([contract_pdf(2)])  # 同一文件也在另一批里
    await env.service.edit_batch_item(EID, first.id, first.items[0].id, BatchItemEdit(selected=False), "U-1")
    assert [f.not_imported for f in (await env.service.confirm_batch(EID, first.id, "U-1")).files] == [False]
    assert env.document(first.files[0].document_id).status == "staged"
    await env.service.confirm_batch(EID, second.id, "U-1")
    assert env.document(first.files[0].document_id).status == "extracted"


@pytest.mark.external
async def test_unselected_ledger_export_is_not_left_awaiting_review() -> None:
    """R278 审核的实例: 确认时没勾 journal_report.xlsx (另一张表照常导入): 确认后它是未导入, 不再算待确认导入,
    也不在任何还没写入的批次里."""
    env = Batch(profile=XERO_PROFILE)
    batch = await env.upload([raw_file("journal_report.xlsx"), raw_file("trial_balance_2026-06-30.xlsx")])
    journal, balances = batch.items
    await env.service.edit_batch_item(EID, batch.id, journal.id, BatchItemEdit(selected=False), "U-1")
    done = await env.service.confirm_batch(EID, batch.id, "U-1")
    assert [f.not_imported for f in done.files] == [True, False]
    assert env.document(journal.document_id).status == "not_imported"
    assert env.document(balances.document_id).status == "extracted"
    assert await env.service.open_batches(EID) == []


async def test_confirming_with_nothing_selected_closes_the_batch() -> None:
    """一项都不勾也能确认 (前端 "都不导入, 关闭这批"): 不写表, 文件都转为未导入, 批次不再列在待确认的上传里."""
    env = Batch()
    batch = await env.upload([demo_csv("accounts"), contract_pdf(1)])
    for item in batch.items:
        await env.service.edit_batch_item(EID, batch.id, item.id, BatchItemEdit(selected=False), "U-1")
    done = await env.service.confirm_batch(EID, batch.id, "U-1")
    assert (done.status, done.data_version, env.data.written) == ("committed", None, [])
    assert [f.not_imported for f in done.files] == [True, True]
    assert {env.document(f.document_id).status for f in done.files} == {"not_imported"}
    assert await env.service.open_batches(EID) == []


async def test_staged_file_type_is_changed_on_the_confirmation_sheet_only() -> None:
    """文件列表里改暂存文件的类型被拒 (INVALID_VALUE, 文件还在导入确认单里), 文件仍暂存."""
    env = Batch()
    batch = await env.upload([contract_pdf(1)])
    document_id = batch.files[0].document_id
    with pytest.raises(DataRejected) as e:
        await env.service.set_document_type(EID, document_id, DocumentType.minutes, "其实是会议纪要", "U-1")
    assert e.value.code == "INVALID_VALUE" and "确认单" in e.value.message
    assert (env.document(document_id).status, env.document(document_id).type) == ("staged", DocumentType.other)


# ── 确认导入一次写成 ──


async def test_trail_failure_after_commit_is_backfilled_not_rewritten(monkeypatch: pytest.MonkeyPatch) -> None:
    """事务提交后版本留痕写失败: 确认照常返回 committed (账表, 文件与批次已一起写成), 只记日志; 再确认时补写留痕,
    返回原结果, 不写第二个数据版本."""
    env = Batch()
    batch = await env.upload([demo_csv("accounts"), demo_csv("trial_balance")])
    real = data_service.record_transitions

    async def flaky(trail: Any, entries: Any) -> None:
        entries = list(entries)
        if any(e.payload.get("action") == "import" for e in entries):
            raise RuntimeError("trail down")
        await real(trail, entries)

    monkeypatch.setattr(data_service, "record_transitions", flaky)
    done = await env.service.confirm_batch(EID, batch.id, "U-1")
    assert (done.status, done.data_version, len(env.data.written), env.checkups) == ("committed", 1, 1, 1)
    assert trail_entries(env, TrailKind.data, "import") == []
    monkeypatch.setattr(data_service, "record_transitions", real)

    assert await env.service.confirm_batch(EID, batch.id, "U-1") == done
    assert (len(env.data.written), env.data.data_version) == (1, 1)
    assert sorted(e.payload["table_kind"] for e in trail_entries(env, TrailKind.data, "import")) == [
        "accounts",
        "trial_balance",
    ]
    assert not env.runner.is_running(EID)


async def test_exit_after_commit_then_confirm_again_writes_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """进程在事务提交与留痕, 体检之间退出: 批次已 committed; 再确认不再写, 取锁时补写留痕, 补做过期的体检."""
    env = Batch()
    batch = await env.upload([demo_csv("accounts")])
    real = data_service.record_transitions

    async def crash(trail: Any, entries: Any) -> None:
        entries = list(entries)
        if any(e.payload.get("action") == "import" for e in entries):
            raise Crash
        await real(trail, entries)

    monkeypatch.setattr(data_service, "record_transitions", crash)
    with pytest.raises(Crash):
        await env.service.confirm_batch(EID, batch.id, "U-1")
    monkeypatch.setattr(data_service, "record_transitions", real)
    assert (await env.service.batch(EID, batch.id)).status == "committed"
    assert (len(env.data.written), env.checkups, env.data.last_run) == (1, 0, None)
    assert not env.runner.is_running(EID)

    again = await env.service.confirm_batch(EID, batch.id, "U-1")
    assert (again.status, again.data_version, len(env.data.written)) == ("committed", 1, 1)
    assert [e.payload["table_kind"] for e in trail_entries(env, TrailKind.data, "import")] == ["accounts"]
    assert env.data.last_run is not None and env.data.last_run.data_version == 1  # 补做了体检


async def test_commit_failure_writes_nothing_then_retry_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    """一次写成的提交出错 (如库出错): 账表, 合同与文件登记都没写, 批次仍待确认; 重试写成一次."""
    env = Batch()
    batch = await env.upload([demo_csv("accounts"), contract_pdf(1)])
    real = env.data.commit_batch

    async def failing(*args: Any, **kwargs: Any) -> ImportBatch:
        raise RuntimeError("db down")

    monkeypatch.setattr(env.data, "commit_batch", failing)
    with pytest.raises(RuntimeError):
        await env.service.confirm_batch(EID, batch.id, "U-1")
    assert (await env.service.batch(EID, batch.id)).status == "ready"
    assert (env.data.written, env.financial.saved_contracts, env.checkups) == ([], {}, 0)
    assert {env.document(f.document_id).status for f in batch.files} == {"staged"}
    assert not env.runner.is_running(EID)
    monkeypatch.setattr(env.data, "commit_batch", real)

    done = await env.service.confirm_batch(EID, batch.id, "U-1")
    assert (done.status, len(env.data.written), sorted(env.financial.saved_contracts)) == ("committed", 1, ["CON-001"])


async def test_recognition_failure_marks_batch_failed_and_retry_resumes(monkeypatch: pytest.MonkeyPatch) -> None:
    """识别作业在逐文件之外出错 (画像查不到): 批次置 failed 并记原因, 锁已释放, 不能确认; 重新识别从没做完的文件接着
    做 (与启动补做同一段代码), 做完置 ready; 已识别完的不能再重新识别."""
    env = Batch()
    real = BatchIntake.context

    async def broken(self: BatchIntake, eid: str, engagement: Engagement) -> Any:
        raise LookupError("profile missing")

    monkeypatch.setattr(BatchIntake, "context", broken)
    batch = await env.service.start_batch(EID, [demo_csv("accounts")], actor="U-1")
    await asyncio.gather(*list(env.service.tasks))
    failed = await env.service.batch(EID, batch.id)
    assert (failed.status, failed.error, failed.items) == ("failed", "LookupError: profile missing", [])
    assert not env.runner.is_running(EID) and env.service.jobs.get(EID) is None
    with pytest.raises(DataRejected) as e:
        await env.service.confirm_batch(EID, batch.id, "U-1")
    assert e.value.code == "INVALID_VALUE"
    monkeypatch.setattr(BatchIntake, "context", real)
    await env.service.recover()  # 启动补做不自动重做识别出错的批次
    await asyncio.gather(*list(env.service.tasks))
    assert (await env.service.batch(EID, batch.id)).status == "failed"

    retried = await env.service.retry_batch(EID, batch.id)
    assert (retried.status, retried.error) == ("processing", None)
    await asyncio.gather(*list(env.service.tasks))
    done = await env.service.batch(EID, batch.id)
    assert done.status == "ready" and [i.records for i in done.items] == [23]
    with pytest.raises(DataRejected) as e:
        await env.service.retry_batch(EID, batch.id)
    assert e.value.code == "INVALID_VALUE"
    assert not env.runner.is_running(EID)


async def test_edit_confirm_and_retry_take_the_lock_first() -> None:
    """先取项目锁再读批次: 识别中的批次由识别作业持锁, 调整, 确认与重新识别都得到 ENGAGEMENT_BUSY; 项目锁定时批次
    不存在也先报锁定."""
    env = Batch()
    batch = await env.service.start_batch(EID, [demo_csv("accounts")], actor="U-1")  # 识别作业还没跑完
    attempts = (
        env.service.confirm_batch(EID, batch.id, "U-1"),
        env.service.edit_batch_item(EID, batch.id, f"{batch.id}-1", BatchItemEdit(selected=False), "U-1"),
        env.service.retry_batch(EID, batch.id),
    )
    for attempt in attempts:
        with pytest.raises(DecisionRejected) as e:
            await attempt
        assert e.value.code == "ENGAGEMENT_BUSY"
    await asyncio.gather(*list(env.service.tasks))
    store = env.runner.deps.engagement_store
    assert isinstance(store, FakeEngagementStore)
    store.engagements[0] = ENGAGEMENT.model_copy(update={"locked": True})
    for attempt in (env.service.confirm_batch(EID, "BAT-none", "U-1"), env.service.retry_batch(EID, "BAT-none")):
        with pytest.raises(DataRejected) as e:
            await attempt
        assert e.value.code == "ENGAGEMENT_LOCKED"


async def test_staged_file_reuploaded_is_processed_and_skipped_on_confirm() -> None:
    """批次里暂存的合同经 "上传文件" 重传: 照新上传处理 (分类, 抽取, extracted); 之后确认这批时跳过它, 合同不多出
    版本."""
    env = Batch()
    batch = await env.upload([contract_pdf(1)])
    await env.service.upload_documents(EID, [contract_pdf(1)], actor="U-1")
    await asyncio.gather(*list(env.service.tasks))
    document = env.document(batch.files[0].document_id)
    assert (document.status, document.type) == ("extracted", DocumentType.contract)
    versions = [v.version for v in env.financial.contract_versions["CON-001"]]
    await env.service.confirm_batch(EID, batch.id, "U-1")
    assert [v.version for v in env.financial.contract_versions["CON-001"]] == versions == [1]

    # 确认时没勾选而未导入的文件经 "上传文件" 重传: 同样照新上传处理
    dropped = await env.upload([contract_pdf(3)])
    await env.service.edit_batch_item(EID, dropped.id, dropped.items[0].id, BatchItemEdit(selected=False), "U-1")
    await env.service.confirm_batch(EID, dropped.id, "U-1")
    assert env.document(dropped.files[0].document_id).status == "not_imported"
    await env.service.upload_documents(EID, [contract_pdf(3)], actor="U-1")
    await asyncio.gather(*list(env.service.tasks))
    document = env.document(dropped.files[0].document_id)
    assert (document.status, document.type) == ("extracted", DocumentType.contract)


async def test_recover_keeps_a_request_typed_staged_file_staged() -> None:
    """带文件请求的批次: A3 写了带 request_id 的 classify 留痕, 文件仍暂存; 启动补做不把它当作按请求定类型没落到
    登记上的文件处理 (ingest.retype 排除暂存与未导入的), 确认前不被程序使用; 没勾选而未导入的确认后也不处理."""
    env = Batch()
    env.data.data_requests = {
        "REQ-board_minutes": DataRequest(
            id="REQ-board_minutes",
            kind="document",
            document_type=DocumentType.minutes,
            title="董事会会议记录",
            reason_code="not_provided",
            created_at=NOW,
        )
    }
    batch = await env.upload([("minutes.pdf", b"%PDF minutes")], request_id="REQ-board_minutes")
    assert any(
        e.payload.get("request_id") == "REQ-board_minutes" for e in trail_entries(env, TrailKind.data, "classify")
    )
    await env.service.recover()
    await asyncio.gather(*list(env.service.tasks))
    await wait_idle(env.runner, EID)
    document = env.document(batch.files[0].document_id)
    assert (document.status, document.type) == ("staged", DocumentType.other)
    # 取消勾选后确认: 未导入是终态, 启动补做也不按请求处理它
    await env.service.edit_batch_item(EID, batch.id, batch.items[0].id, BatchItemEdit(selected=False), "U-1")
    await env.service.confirm_batch(EID, batch.id, "U-1")
    await env.service.recover()
    await asyncio.gather(*list(env.service.tasks))
    await wait_idle(env.runner, EID)
    document = env.document(batch.files[0].document_id)
    assert (document.status, document.type) == ("not_imported", DocumentType.other)


# ── 重启后接着识别 ──


async def test_recover_resumes_a_processing_batch() -> None:
    """进程在识别中途退出: 启动补做从没做完的文件接着做 (先去掉它已有的项), 做完置 ready; 暂存的文件不当作
    没处理完的上传补做."""
    env = Batch()
    batch = await env.service.start_batch(EID, [demo_csv("accounts"), contract_pdf(1)], actor="U-1")
    await asyncio.gather(*list(env.service.tasks))
    done = await env.service.batch(EID, batch.id)
    # 模拟中途退出: 第二个文件没做完, 还留着半截的项
    interrupted = done.model_copy(
        update={
            "status": "processing",
            "files": [done.files[0], done.files[1].model_copy(update={"done": False})],
            "items": [done.items[0], done.items[1].model_copy(update={"document_type": None})],
        }
    )
    await env.data.save_batch(EID, interrupted)

    await env.service.recover()
    await asyncio.gather(*list(env.service.tasks))

    resumed = await env.service.batch(EID, batch.id)
    assert resumed.status == "ready" and [i.id for i in resumed.items] == [i.id for i in done.items]
    assert resumed.items[1].document_type is DocumentType.contract
    assert all(env.document(f.document_id).status == "staged" for f in resumed.files)


# ── Xero 演示项目的运行副本 ──


@needs("q1_supplements")  # 运行副本在补造目录下, 经 DATASET_ROOT 读
async def test_xero_demo_runtime_has_the_sheet_fixtures() -> None:
    """Xero 演示项目 (mock) 读补造目录的运行副本, 那里有 DOC-SHEET (生成脚本从上传固定输出复制): 网页导出的试算表
    走大模型识别, 不退回按列名匹配."""
    settings = get_settings()
    xero = Engagement.model_validate({**ENGAGEMENT.model_dump(), "dataset": "xero/20261017-q1"})
    llm = DatasetFixtureLLMClient(
        settings.LLM_FIXTURES_ROOT, settings.DATASET_ROOT, FakeEngagementStore([NUMBAT_PROFILE], [xero])
    )
    env = Batch()
    deps = env.deps()
    validator = OutputValidator(deps.financial_store, deps.library, deps.trail)
    deps = replace(deps, llm=llm, judgments=JudgmentEngine(llm, validator, deps.trail, deps.library))
    name, data = raw_file("trial_balance_2026-06-30.xlsx")
    document = await env.evidence.save(EID, name, data, type=DocumentType.other, reliability=Reliability.internal_copy)
    [reading] = await SheetIntake(deps).recognize(EID, document, data)
    assert (reading.method, reading.plan.kind, reading.company) == ("llm", TableKind.trial_balance, "Demo Company (AU)")


# ── 接口 ──


async def test_batch_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /data/batches 202 返回 processing 的批次; GET 列出还没写入的批次; GET 取确认单 (不存在 404); PUT 调整单项;
    POST confirm 202 写入; 已写入的批次重新识别 422. 单个超过上限 (50 MB, 这里调小) 413; 类型不支持 422."""
    env = Batch()
    async with api_client(env.runner) as client:
        client._transport.app.state.data_service = env.service  # type: ignore[attr-defined]
        assert (await client.get(BASE)).json() == []
        started = await client.post(BASE, files=[("files", ("accounts.csv", demo_csv("accounts")[1], "text/csv"))])
        assert started.status_code == 202 and started.json()["status"] == "processing"
        batch_id = started.json()["id"]
        await asyncio.gather(*list(env.service.tasks))
        listed = await client.get(BASE)
        assert listed.status_code == 200 and [b["id"] for b in listed.json()] == [batch_id]
        got = await client.get(f"{BASE}/{batch_id}")
        assert got.status_code == 200 and got.json()["status"] == "ready"
        item_id = got.json()["items"][0]["id"]
        edited = await client.put(f"{BASE}/{batch_id}/items/{item_id}", json={"selected": False})
        assert edited.status_code == 200 and edited.json()["items"][0]["selected"] is False
        await client.put(f"{BASE}/{batch_id}/items/{item_id}", json={"selected": True})
        confirmed = await client.post(f"{BASE}/{batch_id}/confirm")
        assert confirmed.status_code == 202 and confirmed.json()["status"] == "committed"
        assert (await client.get(BASE)).json() == []  # 写入后不再列出
        assert (await client.get(f"{BASE}/BAT-none")).status_code == 404
        bad = await client.post(BASE, files=[("files", ("x.exe", b"MZ", "application/octet-stream"))])
        assert (bad.status_code, bad.json()["detail"]["code"]) == (422, "INVALID_VALUE")
        retry = await client.post(f"{BASE}/{batch_id}/retry")
        assert (retry.status_code, retry.json()["detail"]["code"]) == (422, "INVALID_VALUE")  # 已写入
        monkeypatch.setattr(data_import, "MAX_BYTES", 10)
        big = await client.post(BASE, files=[("files", ("accounts.csv", demo_csv("accounts")[1], "text/csv"))])
        assert (big.status_code, big.json()["detail"]["code"]) == (413, "FILE_TOO_LARGE")
    assert {k for k in env.data.written[0][0]} == {TableKind.accounts}


# ── 批次里的 ASIC 摘录 (需求 R308): 确认时保留识别时的抽取结果, 与单份上传相同 ──

REGISTRY_PDF = "900043030.pdf"  # R64 的合成摘录 (Victoria Property Agency), 固定输出在上传固定输出的 DOC-REGISTRY


def registry_env(*overlays: Path) -> Batch:
    """批次环境: 摘录取固定输出里的文件 ID 与页数, REQ-registry_extract 为 open (按请求定类型, 不调分类); 大模型先读
    overlays."""
    env = Batch()
    env.evidence.ids[REGISTRY_PDF], env.evidence.page_counts[REGISTRY_PDF] = REGISTRY_DOCUMENT, 2
    env.data.data_requests[REGISTRY_REQUEST] = document_request(
        DocumentType.registry_extract, "not_provided", NOW, used_for=["ACC-04"]
    )
    if overlays:
        deps = env.service.deps
        llm = LayeredMockLLM(*overlays, UPLOAD_FIXTURES, MAIN_FIXTURES)
        validator = OutputValidator(deps.financial_store, deps.library, deps.trail)
        env.service.deps = replace(deps, llm=llm, judgments=JudgmentEngine(llm, validator, deps.trail, deps.library))
    return env


def registry_outcome(env: Batch) -> tuple[Any, ...]:
    """处理后可比的结果: 整份文件登记, 摘录的全部抽取记录 (版本, 内容, 写入人, 字段, 理由), 数据变动 (数据版本, 种类)."""
    records = {k: v for k, v in env.data.extractions.items() if k[:2] == (REGISTRY_TARGET, REGISTRY_DOCUMENT)}
    changes = [(c.data_version, c.input) for c in env.data.data_changes]
    return env.document(REGISTRY_DOCUMENT), records, changes


async def test_batch_keeps_the_registry_extraction_like_a_single_upload() -> None:
    """验收 1, 3 (R217 审核 R217-03 的探针): 批次里按请求上传的 ASIC 摘录, 识别时抽取的结果随确认单的文件项带到确认
    (staged_of 还原得出); 确认后文件 extracted, 摘录记第 1 版 (A3), 数据版本 +1 记 registry_extracts, 与单份上传
    (DataService.upload_documents 按请求) 逐字段相同."""
    env = registry_env()
    batch = await env.upload([(REGISTRY_PDF, b"%PDF registry extract")], request_id=REGISTRY_REQUEST)
    [item] = batch.items
    assert item.registry is not None and item.issues == [] and item.selected
    assert staged_of(item).registry == item.registry
    before = env.data.data_version

    await env.service.confirm_batch(EID, batch.id, "U-1")
    await asyncio.gather(*list(env.service.tasks))

    assert env.data.data_version == before + 1
    single = registry_env()
    await single.service.upload_documents(
        EID, [(REGISTRY_PDF, b"%PDF registry extract")], actor="U-1", request_id=REGISTRY_REQUEST
    )
    await asyncio.gather(*list(single.service.tasks))
    document, records, changes = batched = registry_outcome(env)
    assert batched == registry_outcome(single)
    assert document.status == "extracted" and [key[2] for key in records] == [1]
    assert next(iter(records.values()))["data"] == registry_fixture().model_dump(mode="json")
    assert DataInput.registry_extracts in [kind for _, kind in changes]


async def test_batch_registry_extraction_that_fails_still_needs_manual(tmp_path: Path) -> None:
    """验收 2: 识别时摘录抽取不通过 (固定输出缺必填字段): 文件项标 extraction_failed, 不勾选; 审计师勾选后确认,
    文件照旧 needs_manual (extract), 不写摘录, 不记 registry_extracts."""
    (tmp_path / "DOC-REGISTRY").mkdir()
    (tmp_path / "DOC-REGISTRY" / "900043030.json").write_text('{"acn": "900043030"}', encoding="utf-8")
    env = registry_env(tmp_path)
    batch = await env.upload([(REGISTRY_PDF, b"%PDF registry extract")], request_id=REGISTRY_REQUEST)
    [item] = batch.items
    assert item.registry is None and [i.code for i in item.issues] == [BatchIssueCode.extraction_failed]
    await env.service.edit_batch_item(EID, batch.id, item.id, BatchItemEdit(selected=True), "U-1")

    await env.service.confirm_batch(EID, batch.id, "U-1")
    await asyncio.gather(*list(env.service.tasks))

    document, records, changes = registry_outcome(env)
    assert (document.type, document.status, document.failed_step, records) == (
        DocumentType.registry_extract,
        "needs_manual",
        "extract",
        {},
    )
    assert DataInput.registry_extracts not in [kind for _, kind in changes]
