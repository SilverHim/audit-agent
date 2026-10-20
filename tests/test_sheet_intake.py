"""需求 R276: A3 用大模型识别上传的账表 (DOC-SHEET), 大模型不可用时退回按列名匹配.

mock 下读 tests/fixtures/llm_variants/upload/DOC-SHEET (R275 的方案样例只存这一份). Xero 网页导出只读不改;
journal_report.xlsx 的 Posted By 列是导出账号的姓名 (计划决定 12): 只在内存里读出来, 用来断言它不出现在提示词与留痕里,
断言只比较计数, 不把值写进任何文件与断言消息; 套用方案前先在内存里遮蔽该列 (同 R275 的测试).
"""

import functools
import hashlib
import json
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from backend.adapters.llm import UnconfiguredLLMClient, make_llm_client
from backend.agents.ingest import sheet_first_page
from backend.agents.sheets import (
    FALLBACK,
    SAMPLE_ROWS,
    SHEET,
    TABLES,
    SheetIntake,
    fixture_key,
    sample_rows,
    sample_text,
)
from backend.contracts.data import ParseFailure, SheetReading, TableKind, TablePlan
from backend.contracts.llm_outputs import SheetRecognition
from backend.contracts.models import DocumentRef, DocumentType, Reliability, TrailEntry, TrailKind
from backend.contracts.ports import LLMClient
from backend.contracts.records import Account, JournalEntry, SalesInvoice, TrialBalanceRow
from backend.services import data_import as d
from backend.services import table_plan as tp
from backend.services.judgment_engine import OutputValidator, structure_only
from tests.external import live_llm_settings, raw_dir, snapshot_dir
from tests.fakes import ENGAGEMENT, UPLOAD_FIXTURES, FakeRegistry, FakeTrail, LayeredMockLLM, ScriptedLLM, make_deps
from tests.test_table_plan import masked
from tests.tools.live_budget import Budget, BudgetedLLMClient, report

EID = ENGAGEMENT.id
REPO = Path(__file__).resolve().parents[1]
DEMO = REPO / "data" / "demo"
FIXTURES = UPLOAD_FIXTURES / "DOC-SHEET"
NOW = datetime(2026, 10, 19, tzinfo=UTC)
NUMBAT_KINDS = [k for k in TableKind if (DEMO / f"{k}.csv").is_file()]  # data/demo 的 7 张表 (按文件推出)


def document(file_name: str, data: bytes) -> DocumentRef:
    """审计师上传的一份表格文件的登记 (类型未定)."""
    sha = hashlib.sha256(data).hexdigest()
    return DocumentRef(
        id=f"DOC-{sha[:12]}",
        type=DocumentType.other,
        file_name=file_name,
        sha256=sha,
        page_count=1,
        reliability=Reliability.internal_copy,
        status="received",
        uploaded_at=NOW,
        media_type=d.media_type(file_name),
        uploaded_by="U-1",
    )


async def recognize(
    path: Path, llm: LLMClient | None = None, name: str | None = None
) -> tuple[list[SheetReading], FakeTrail]:
    """按上传文件处理 path (可换个文件名), 缺省读上传的固定输出. 返回识别结果与留痕."""
    trail = FakeTrail()
    deps = make_deps(FakeRegistry(), trail, llm=llm if llm is not None else LayeredMockLLM(UPLOAD_FIXTURES))
    data = path.read_bytes()
    readings = await SheetIntake(deps).recognize(EID, document(name or path.name, data), data)
    return readings, trail


def calls(trail: FakeTrail) -> list[TrailEntry]:
    return [e for e in trail.entries if e.kind is TrailKind.llm_call]


def prompt_of(entry: TrailEntry) -> str:
    content = entry.payload["messages"][0]["content"]
    assert isinstance(content, str)
    return content


def fixture(key: str) -> SheetRecognition:
    return SheetRecognition.model_validate_json((FIXTURES / f"{key}.json").read_text(encoding="utf-8"))


def apply(reading: SheetReading, path: Path) -> tuple[list[Any], list[ParseFailure]]:
    """把识别出的方案套到全表 (Posted By 先在内存里遮蔽)."""
    name, rows = d.raw_rows(path.name, path.read_bytes(), reading.sheet)
    return tp.apply_plan(reading.plan, name, masked(rows))


@functools.cache
def posted_by_values() -> frozenset[str]:
    """journal_report.xlsx 的 Posted By 取值 (只在内存里用). 这份导出 1000 多行, 解析约 0.5 秒, 几个用例共用一次."""
    _, rows = d.raw_rows("journal_report.xlsx", (raw_dir() / "journal_report.xlsx").read_bytes())
    values: set[str] = set()
    column: int | None = None
    for _, cells in rows:
        texts = [d.cell_text(c) for c in cells]
        if column is None:
            column = texts.index("Posted By") if "Posted By" in texts else None
        elif column < len(texts) and texts[column]:
            values.add(texts[column])
    return frozenset(values)


# ── 验收 4: 测试样例在 mock 下的识别结果 ──


@pytest.mark.external
async def test_xero_trial_balance() -> None:
    """样例 1: 前 3 行标题, 第 5 行表头, 本期借贷两列, 上期一列, 末行 Total; 读到公司与截至日."""
    path = raw_dir() / "trial_balance_2026-06-30.xlsx"
    [reading], trail = await recognize(path)

    assert (reading.method, reading.confidence, reading.sheet) == ("llm", "high", "Trial Balance")
    assert reading.plan == fixture("trial_balance_2026-06-30").plan
    assert (reading.plan.kind, reading.plan.header_row) == (TableKind.trial_balance, 5)
    assert (reading.company, reading.period_start, reading.period_end) == ("Demo Company (AU)", None, date(2026, 6, 30))
    rows, failures = apply(reading, path)
    assert failures == [] and all(isinstance(r, TrialBalanceRow) for r in rows)
    assert Counter(r.period for r in rows) == {"current": 6, "prior": 6}
    assert len({r.account_code for r in rows}) == 6
    [call] = calls(trail)
    assert (call.procedure_id, call.run_id, call.payload["fixture_key"]) == (
        "DOC-SHEET",
        f"UPLOAD-{document(path.name, path.read_bytes()).id}",
        "trial_balance_2026-06-30",
    )
    assert reading.trail_ids == [call.id]


@pytest.mark.external
async def test_xero_journal_report_keeps_posted_by_out_of_prompt_and_trail() -> None:
    """样例 2: 标题 3 行, 按 Journal ID 分组; 368 笔 1017 行. Posted By 的值不进提示词与留痕 (表头照留)."""
    path = raw_dir() / "journal_report.xlsx"
    [reading], trail = await recognize(path)

    assert (reading.method, reading.confidence) == ("llm", "high")
    assert reading.plan == fixture("journal_report").plan and reading.plan.kind is TableKind.journal_entries
    assert (reading.company, reading.period_start, reading.period_end) == (
        "Demo Company (AU)",
        date(2024, 1, 1),
        date(2027, 12, 31),
    )
    rows, failures = apply(reading, path)
    assert failures == [] and all(isinstance(r, JournalEntry) for r in rows)
    assert len(rows) == 1017 and len({r.entry_no for r in rows}) == 368
    assert "Posted By" not in {f.column for f in reading.plan.fields.values()}  # 导出账号, 不是录入人 (决定 12)
    # 断言只比较布尔值与计数: 失败时 pytest 也不会把提示词或 Posted By 的值打进输出
    prompt = prompt_of(calls(trail)[0])
    header_kept, values_masked = "Posted By" in prompt, d.MASK in prompt
    assert header_kept and values_masked
    secrets = posted_by_values()
    dumped = json.dumps([e.model_dump(mode="json") for e in trail.entries], ensure_ascii=False)
    found, leaked = len(secrets), sum(value in dumped for value in secrets)
    assert found > 0 and leaked == 0


@pytest.mark.external
async def test_xero_chart_of_accounts() -> None:
    """样例 3: 59 个科目, Xero 类型映射到五类 (803 的 Type 为空, 记失败处, 同 R275)."""
    path = raw_dir() / "chart_of_accounts.csv"
    [reading], _ = await recognize(path)

    assert (reading.method, reading.confidence, reading.sheet) == ("llm", "high", "chart_of_accounts")
    assert reading.plan == fixture("chart_of_accounts").plan and reading.plan.kind is TableKind.accounts
    assert (reading.company, reading.period_end) == (None, None)
    rows, failures = apply(reading, path)
    assert all(isinstance(r, Account) for r in rows)
    assert len(rows) + len(failures) == 59 and [f.row for f in failures] == [44]
    assert {r.type for r in rows} == {"asset", "liability", "equity", "revenue", "expense"}


@pytest.mark.external
async def test_xero_receivable_invoice_detail() -> None:
    """样例 4: 按发票分组, 有 "Total ..." 合计行; 识别为销售发票."""
    path = raw_dir() / "receivable_invoice_detail.xlsx"
    [reading], _ = await recognize(path)

    assert (reading.method, reading.confidence) == ("llm", "high")
    assert reading.plan == fixture("receivable_invoice_detail").plan and reading.plan.kind is TableKind.sales_invoices
    rows, failures = apply(reading, path)
    assert failures == [] and all(isinstance(r, SalesInvoice) for r in rows)
    assert len(rows) == 42 and not any(r.invoice_no.startswith("Total") for r in rows)


@pytest.mark.parametrize("kind", NUMBAT_KINDS)
async def test_numbat_tables_are_high_and_match_column_names(kind: TableKind) -> None:
    """样例 5: Numbat 标准列的 7 张表全部高把握, 方案与按列名匹配 (退回时的结果) 相同, 套用结果与账表导入一致."""
    path = DEMO / f"{kind}.csv"
    [reading], _ = await recognize(path)
    [fallback], _ = await recognize(path, name=f"other-{kind}.csv")  # 没有 fixture 的文件名: 退回按列名匹配

    assert (reading.method, reading.confidence, reading.plan.kind) == ("llm", "high", kind)
    assert (fallback.method, fallback.confidence) == ("column_names", "low")
    assert reading.plan == fallback.plan
    # 遮蔽的只是取值: 人名一类的列照常对应 (提示词也这样写)
    mapped = {name for name, f in reading.plan.fields.items() if f.column == name}
    masked_columns = {"created_by", "approved_by", "signed_by"} & set(
        d.read_sheet(path.name, path.read_bytes()).columns
    )
    assert masked_columns <= mapped
    rows, failures = apply(reading, path)
    sheet = d.read_sheet(path.name, path.read_bytes())
    preview = d.preview("IMP-1", path.name, [kind.value], sheet)
    expected, _ = tp.convert(kind, sheet, preview.suggested_mapping, preview.suggested_date_format)
    assert failures == [] and rows == expected and rows


# ── 验收 1: 只发开头的样本行, 人名列与邮箱遮蔽 ──


@pytest.mark.external
async def test_only_the_first_rows_are_sent() -> None:
    """样本行含标题行, 至多 30 行; 第 31 行起不进提示词. 提示词列出 10 种账表. (用应收发票明细: 有标题行, 行数多,
    没有人名列; Posted By 的遮蔽见日记账报表一例.)"""
    path = raw_dir() / "receivable_invoice_detail.xlsx"
    _, trail = await recognize(path)
    prompt = prompt_of(calls(trail)[0])
    block = prompt[prompt.index("<rows>\n") + len("<rows>\n") : prompt.index("\n</rows>")].splitlines()

    assert SAMPLE_ROWS == 30 and len(block) == 30
    assert [line.split(":", 1)[0] for line in block] == [str(n) for n in range(1, 31)]
    assert block[0] == '1: ["Receivable Invoice Detail"]' and block[2].startswith('3: ["For the period 1 January 2024')
    assert block[3] == "4: []"  # 空行照发, 行号连续
    assert "The sheet has 231 rows" in prompt
    assert "The columns themselves are still mapped by their headings as usual" in prompt
    assert "Posted By column of a Xero journal report" in prompt and "leave it unmapped" in prompt
    assert "Do not map those columns" not in prompt
    assert all(f"- `{t['kind']}`:" in prompt for t in TABLES) and len(TABLES) == 10


def test_sample_rows_mask_personal_columns_and_emails() -> None:
    """人名一类的列 (表头规范化后含 contactname, firstname, person 等, 或以 by 结尾) 在表头之后各行遮蔽, 表头照留,
    重名的列都遮; 任何列里的邮箱遮蔽; 行末空格去掉. 只认像表头的行: 数据行里的科目名 Telephone & Internet 不算."""
    rows: d.RawRows = [
        (1, ["Report for someone@example.test", None, None]),
        (2, [None, None, None]),
        (3, ["Date", "Posted By", "created_by", "Note", "Posted By"]),
        (4, ["2026-06-30", "Synthetic Poster", "synthetic.clerk", "see a.b@example.test now", "Other Poster"]),
        (5, ["2026-07-01", None, "", "plain", None]),
    ]
    assert sample_rows(rows) == [
        (1, [f"Report for {d.MASK}"]),
        (2, []),
        (3, ["Date", "Posted By", "created_by", "Note", "Posted By"]),
        (4, ["2026-06-30", d.MASK, d.MASK, f"see {d.MASK} now", d.MASK]),
        (5, ["2026-07-01", "", "", "plain"]),
    ]
    headers = ["*ContactName", "FirstName", "Person2LastName", "Mobile Number", "Reviewed By", "POAttentionTo"]
    headers += ["SkypeName", "Code", "Account"]
    values = ["Synth Co", "Ann", "Bee", "0400 000 000", "R. Synthetic", "Att. Syn", "syn.skype", "489", "x"]
    assert sample_rows([(1, headers), (2, values)])[1] == (2, [d.MASK] * 7 + ["489", "x"])
    accounts: d.RawRows = [(1, ["Code", "Name"]), (2, ["489", "Telephone & Internet"]), (3, ["493", "Travel"])]
    assert sample_rows(accounts)[2] == (3, ["493", "Travel"])  # 数据行里含片段的科目名不当表头
    assert [d.personal_column(h) for h in ["Posted By", "posted_by_2", "Signed by", "User Name", "Debit"]] == [
        True,
        True,
        True,
        True,
        False,
    ]


@pytest.mark.parametrize(
    ("header", "row"),
    [
        (["Account", 2026.0, 2025.0, "Posted By"], ["Sales", 1.0, 2.0, "Syn Poster"]),  # Excel 数值的年份列名
        (["Account", "2026", "2025", "Posted By"], ["Sales", "1", "2", "Syn Poster"]),  # CSV 文本的年份列名
        (["Account", datetime(2026, 6, 30), "Created By"], ["Sales", 1.0, "Syn Poster"]),  # Excel 日期列名
        (["Posted By"], ["Syn Poster"]),  # 只有一列
        ([None, "Created By"], ["x", "Syn Poster"]),  # 首格为空, 只有一个人名表头
    ],
)
def test_exact_personal_headers_are_masked_whatever_the_row(header: list[Any], row: list[Any]) -> None:
    """恰为人名表头的单元格不论所在行像不像表头都认: 年份或日期作列名, 只有一列, 首格为空的表头, 该列取值都遮."""
    [_, (_, masked_row)] = sample_rows([(1, header), (2, row)])
    assert "Syn Poster" not in masked_row and d.MASK in masked_row


@pytest.mark.parametrize(
    ("header", "row"),
    [
        (["Date", "Contact Phone"], ["2026-06-30", "0400 000 000"]),  # 两格的表头, 只含片段 phone
        (["Code", "Customer Full Name", "Amount"], ["C1", "Syn Customer", "10"]),  # 含片段 fullname
    ],
)
def test_partial_personal_headers_need_a_header_like_row(header: list[Any], row: list[Any]) -> None:
    """只含人名片段的表头在像表头的行 (至少两格, 没有数值或 ISO 日期) 里认, 两格的表头也算."""
    [_, (_, masked_row)] = sample_rows([(1, header), (2, row)])
    assert masked_row[1] == d.MASK
    assert len(sample_rows([(n, ["x"]) for n in range(1, 50)])) == SAMPLE_ROWS


def personal_values() -> list[str]:
    """Xero 登录账号联系人的全名与邮箱 (contacts.json 中不是客户也不是供应商而有名与邮箱的那一位) 与日记账报表的
    Posted By 取值, 都转小写. 只在内存里用, 不写进任何文件与断言消息."""
    data = json.loads((snapshot_dir("20261017-q1") / "contacts.json").read_text(encoding="utf-8"))
    rows = data if isinstance(data, list) else data.get("Contacts", [])
    owners = [
        c
        for c in rows
        if not c.get("IsCustomer") and not c.get("IsSupplier") and c.get("FirstName") and c.get("EmailAddress")
    ]
    found = len(owners)
    assert found == 1
    owner = owners[0]
    name = f"{owner['FirstName']} {owner.get('LastName', '')}".strip()
    return sorted({v.lower() for v in [name, owner["EmailAddress"], *posted_by_values()] if v})


@pytest.mark.external
def test_samples_of_all_xero_exports_carry_no_personal_data() -> None:
    """data/external/xero/raw 下全部文件 (含联系人导出 contacts.csv) 与 Numbat CSV 当作账表上传: 每个工作表的样本行
    (DOC-SHEET) 与首页文字 (DOC-CLASSIFY) 遮蔽后命中个人信息的个数为 0; 遮蔽前联系人导出与日记账报表确有命中
    (守卫有意义). 只比计数."""
    secrets = personal_values()
    files = sorted(raw_dir().iterdir()) + sorted(DEMO.glob("*.csv"))
    raw_hits: dict[str, int] = {}
    masked_hits = 0
    for path in files:
        data = path.read_bytes()
        names = d.sheet_names(path.name, data)
        for name in names:
            _, rows = d.raw_rows(path.name, data, name)
            before = "\n".join(" ".join(d.cell_text(c) for c in cells) for _, cells in rows[:SAMPLE_ROWS]).lower()
            after = sample_text(sample_rows(rows)).lower()
            raw_hits[path.name] = raw_hits.get(path.name, 0) + sum(v in before for v in secrets)
            masked_hits += sum(v in after for v in secrets)
        page = sheet_first_page(d.read_sheet(path.name, data)).lower()
        masked_hits += sum(v in page for v in secrets)
    contacts, journal = raw_hits["contacts.csv"], raw_hits["journal_report.xlsx"]  # 遮蔽前两个文件各自有命中
    assert contacts > 0 and journal > 0 and masked_hits == 0


# ── 验收 3: 大模型不可用时退回按列名匹配 ──


async def test_falls_back_without_a_fixture() -> None:
    """mock 下没有 fixture: 三次尝试都是 fixture_not_found, 退回按列名匹配, 把握低, 理由以 "按列名匹配, 请核对" 开头."""
    path = DEMO / "journal_entries.csv"
    [reading], trail = await recognize(path, name="ledger-export.csv")

    sheet = d.read_sheet(path.name, path.read_bytes())
    preview = d.preview("", "ledger-export.csv", ["ledger-export"], sheet)
    assert (reading.method, reading.confidence, reading.sheet) == ("column_names", "low", "ledger-export")
    assert reading.plan == tp.mapping_plan(
        TableKind.journal_entries, preview.suggested_mapping, preview.suggested_date_format
    )
    assert reading.reason == f"{FALLBACK}: 大模型没有给出合格的识别结果, 按列名认作 journal_entries"
    assert (reading.company, reading.period_start, reading.period_end) == (None, None, None)
    assert reading.trail_ids == [e.id for e in calls(trail)] and len(reading.trail_ids) == 3
    assert [e.payload["level_failed"] for e in calls(trail)] == ["syntax"] * 3


async def test_fallback_finds_the_header_after_a_blank_first_line(tmp_path: Path) -> None:
    """开头空一行的 CSV (没有 fixture) 退回按列名匹配: 表头取第一个非空行 (第 2 行), 方案套到全表没有失败处."""
    path = tmp_path / "blank-first-line.csv"
    path.write_bytes(b"\n" + (DEMO / "accounts.csv").read_bytes())
    [reading], _ = await recognize(path)

    assert (reading.method, reading.plan.kind, reading.plan.header_row) == ("column_names", TableKind.accounts, 2)
    rows, failures = tp.apply_plan(reading.plan, *d.raw_rows(path.name, path.read_bytes()))
    assert failures == [] and len(rows) == 23


async def test_falls_back_when_the_model_is_not_configured() -> None:
    """未配密钥 (调用即抛 LLMTransportError): 退回按列名匹配, 没有 llm_call 留痕."""
    path = DEMO / "accounts.csv"
    [reading], trail = await recognize(path, llm=UnconfiguredLLMClient("缺密钥"))

    assert (reading.method, reading.confidence, reading.plan.kind) == ("column_names", "low", TableKind.accounts)
    assert reading.reason == f"{FALLBACK}: 大模型调用失败, 按列名认作 accounts" and reading.trail_ids == []
    assert calls(trail) == []


async def test_falls_back_when_the_output_never_validates() -> None:
    """输出三次都不合格式 (方案自相矛盾): 退回按列名匹配, 记着三次失败的留痕."""
    bad = json.dumps({**fixture("accounts").model_dump(mode="json"), "plan": {"kind": None, "fields": {"a": {}}}})
    path = DEMO / "accounts.csv"
    [reading], trail = await recognize(path, llm=ScriptedLLM({"accounts": [bad] * 3}))

    assert (reading.method, reading.plan.kind) == ("column_names", TableKind.accounts)
    assert [e.payload["level_failed"] for e in calls(trail)] == ["structure"] * 3


@pytest.mark.external
async def test_falls_back_to_not_a_table_when_column_names_say_nothing() -> None:
    """Xero 试算表按列名认不出 (第一个非空行是报表标题): 退回时方案为不是账表."""
    [reading], _ = await recognize(raw_dir() / "trial_balance_2026-06-30.xlsx", name="tb-export.xlsx")

    assert (reading.method, reading.plan) == ("column_names", TablePlan(kind=None))
    assert reading.reason == f"{FALLBACK}: 大模型没有给出合格的识别结果, 按列名认不出是哪张账表"


@pytest.mark.external
async def test_each_sheet_of_a_workbook_is_one_reading() -> None:
    """一个工作簿的每个工作表各一项, 按工作表顺序; fixture_key 加 "#工作表名"."""
    path = raw_dir() / "bank_statement_090.xlsx"
    readings, trail = await recognize(path)
    names = d.sheet_names(path.name, path.read_bytes())

    assert len(names) == 4 and [r.sheet for r in readings] == names
    assert {r.method for r in readings} == {"column_names"}  # 这些工作表没有 fixture
    assert [e.payload["fixture_key"] for e in calls(trail)][::3] == [f"bank_statement_090#{n}" for n in names]
    assert fixture_key("a.csv", "a", 1) == "a" and fixture_key("b.xlsx", "S 1", 2) == "b#S 1"


# ── 验收 2: 输出模型 ──


def test_recognition_output_model() -> None:
    """识别结果: 方案, 公司名, 期间, 把握 (high 或 low) 与理由; 期间起日不能晚于止日; 只做语法与结构两级校验."""
    good = fixture("journal_report").model_dump(mode="json")
    assert SheetRecognition.model_validate(good).plan.kind is TableKind.journal_entries
    for change in (
        {"confidence": "medium"},
        {"period_start": "2028-01-01"},
        {"plan": {**good["plan"], "kind": "ledger"}},
        {"rows": []},
    ):
        with pytest.raises(ValidationError):
            SheetRecognition.model_validate({**good, **change})
    assert OutputValidator.CHECKS[SheetRecognition] is structure_only
    assert SHEET.llm is not None and SHEET.llm.output_model == "SheetRecognition"


def test_fixtures_cover_the_samples() -> None:
    """固定输出: 5 份 Xero 导出 (含期间不符一例用的 11-30 试算表) 与 Numbat 的 7 张表各一份, 都是高把握; Xero 的读到
    Demo Company (AU)."""
    keys = sorted(p.stem for p in FIXTURES.glob("*.json"))
    xero = [
        "chart_of_accounts",
        "journal_report",
        "receivable_invoice_detail",
        "trial_balance_2026-06-30",
        "trial_balance_2026-11-30",
    ]
    assert keys == sorted([*xero, *(k.value for k in NUMBAT_KINDS)])
    assert {fixture(k).confidence for k in keys} == {"high"}
    assert {fixture(k).company for k in xero if k != "chart_of_accounts"} == {"Demo Company (AU)"}
    assert not (FIXTURES / "default.json").exists()  # 没有 fixture 的文件退回按列名匹配, 不读兜底


# ── 验收 5: live 下各样例跑一次 (联网, 需配好 LLM_MODEL 与 LLM_API_KEY; 只发公开的 Xero 演示与合成数据) ──

XERO_SAMPLES = [
    ("trial_balance_2026-06-30.xlsx", TableKind.trial_balance),
    ("journal_report.xlsx", TableKind.journal_entries),
    ("chart_of_accounts.csv", TableKind.accounts),
    ("receivable_invoice_detail.xlsx", TableKind.sales_invoices),
]
LIVE_SAMPLES = [  # (是否网页导出, 文件名, 表种类); 网页导出在用例里经 raw_dir 核对
    *(
        pytest.param(True, name, kind, id=f"{name}-{kind.value}", marks=pytest.mark.external)
        for name, kind in XERO_SAMPLES
    ),
    *(pytest.param(False, f"{k}.csv", k, id=f"{k}.csv-{k.value}") for k in NUMBAT_KINDS),
]


@pytest.mark.slow
@pytest.mark.live(max_calls=3)  # 一个工作表一次识别, 最多 3 次尝试 (需求 R115 的预算)
@pytest.mark.parametrize(("xero", "name", "kind"), LIVE_SAMPLES)
async def test_live_recognizes_samples(
    xero: bool, name: str, kind: TableKind, live_budget: Budget, request: pytest.FixtureRequest
) -> None:
    """真实大模型识别各样例: 走大模型 (不退回), 认对表种类; 方案能套到全表 (失败处由确认单标红, 这里只记个数).
    配置取环境变量或 .env (tests.external.live_llm_settings); 调用计入预算, 超限即失败, 结束时写用量汇总."""
    path = (raw_dir() if xero else DEMO) / name
    settings = live_llm_settings()
    [reading], trail = await recognize(path, llm=BudgetedLLMClient(make_llm_client(settings), live_budget))
    report(request.node.name, trail.entries, live_budget)
    live_budget.check()

    assert (reading.method, reading.plan.kind) == ("llm", kind)
    assert all(settings.LLM_API_KEY not in json.dumps(e.model_dump(mode="json")) for e in trail.entries)
