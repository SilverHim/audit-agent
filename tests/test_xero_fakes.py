"""需求 R97 tests/xero_fakes.py: Xero 演示的假存储与样例取到的是补造目录与快照的真实数据, make_xero_deps 下真实
Agent 能跑; DOC-04 三份 fixture 与合同 PDF 的文本层一致. 快照与补造各只读一次 (xero_fakes 内缓存, 下面的 module 级
fixture 只建一次假存储与依赖)."""

import json
import re
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from backend.agents.client_intel import ClientIntelAgent
from backend.agents.ingest import IngestAgent
from backend.contracts.data import TableKind
from backend.contracts.llm_outputs import ContractExtraction
from backend.contracts.models import DocumentType, TrailKind
from backend.contracts.ports import ConfirmationRequest
from tests import xero_fakes as x
from tests.fakes import FakeFinancialStore, FakeTrail, make_ctx, make_run

pytestmark = pytest.mark.external
EID = x.ENGAGEMENT.id
REPLY_12345679 = "confirmations/12345679.pdf"


def by_path() -> dict[str, str]:
    """manifest 登记的 PDF: 相对补造目录的路径 → 文件 ID."""
    return {entry["path"]: document_id for document_id, entry in x.manifest_documents().items()}


def normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


@pytest.fixture(scope="module")
def store() -> FakeFinancialStore:
    return x.xero_financial_store()


@pytest.fixture(scope="module")
def deps() -> Any:
    return x.make_xero_deps()


def test_supplement_samples() -> None:
    """只需补造目录的一组: 画像, 计划预置, 声明, 清单, 阶段登记, 文本层, 更正分录, 工资册."""
    profile = x.xero_profile()
    assert (profile.id, profile.version) == ("CLIENT-XERO-3C1332BB-Q1", 1)
    assert [len(p) for p in x.xero_presets()] == [3, 3]
    assert x.xero_traits().dataset == x.XERO_DATASET
    assert Counter(u.stage for u in x.xero_uploads()) == {
        "acceptance": 2,
        "fieldwork": 19,
        "statements": 1,
        "manual": 1,
    }
    assert [u.stage for u in x.xero_uploads("statements")] == ["statements"]
    fieldwork = x.xero_documents("fieldwork")
    assert [len(x.xero_documents(s)) for s in ("acceptance", "fieldwork", "statements", "manual")] == [2, 21, 22, 23]
    assert {d.id for d in fieldwork} <= set(x.manifest_documents())
    assert Counter(d.type for d in fieldwork) == {
        DocumentType.contract: 3,
        DocumentType.minutes: 1,
        DocumentType.engagement_letter: 1,
        DocumentType.agreement: 5,
        DocumentType.correspondence: 4,
        DocumentType.internal_record: 6,
        DocumentType.invoice: 1,
    }
    assert {(d.type is DocumentType.contract, d.status) for d in fieldwork} == {
        (True, "extracted"),
        (False, "classified"),
    }
    assert {(d.channel, d.uploaded_by) for d in fieldwork} == {("auditor_upload", x.ENGAGEMENT.owner_id)}
    uploads = {u.document_id: u for u in x.xero_uploads()}
    assert [(d.reliability, d.document_date, d.related_ids) for d in fieldwork] == [
        (uploads[d.id].reliability, uploads[d.id].document_date, uploads[d.id].related_ids) for d in fieldwork
    ]
    loan = next(d for d in fieldwork if d.file_name == "director_loan_agreement.pdf")
    assert (loan.reliability, str(loan.document_date), loan.related_ids) == (
        "internal_original",
        "2018-09-10",
        ["XJ-397"],
    )
    assert [c.extracted_by_run for c in x.xero_contracts()] == [
        f"UPLOAD-{c.document_id}-JOB" for c in x.xero_contracts()
    ]
    assert "AUD 48.53" in x.xero_pages(by_path()[REPLY_12345679])[0]
    assert (len(x.xero_adjustments()), {r.entry_no for r in x.xero_adjustments(["AJE-X01"])}) == (11, {"AJE-X01"})
    assert [e.employee_id for e in x.xero_employees()] == ["E001", "E002", "E003"]
    registry = x.xero_registry()  # 装上了登记 mock 的摘录与人员检索 (逐份读法见 test_registry_mock)
    assert (len(registry.extracts), len(registry.persons)) == (3, 1)


async def test_financial_store_holds_the_imported_tables(store: FakeFinancialStore) -> None:
    """八张表 (行数同 R72 验收 2; 应付账单 6 行, 需求 R178) 按真实读取方法取得到, 文件与合同按阶段预置."""
    assert {name: len(rows) for name, rows in x.xero_tables().items()} == {
        "accounts": 62,
        "trial_balance": 33,
        "journal_entries": 1017,
        "sales_invoices": 42,
        "supplier_bills": 6,
        "delivery_notes": 24,
        "bank_accounts": 2,
        "bank_transactions": 146,
    }
    bill = await store.record(EID, "supplier_bill", "PB-76bcb361-f93b-4513-b312-5a4af306d276")
    assert (bill or {}).get("outstanding_cents") == 200000
    invoice = await store.record(EID, "sales_invoice", "SI-77b338ef-ecc0-4b95-a0d7-2617b0054611")
    note = await store.record(EID, "delivery_note", "SR-ORC1026")
    assert (invoice or {}).get("contract_id") == "CON-X01"
    assert str((note or {}).get("signed_date")) == "2026-09-17"
    assert [r.balance_cents for r in await store.trial_balance(EID, "current") if r.account_code == "091"] == [1158123]
    assert ((await store.record(EID, "trial_balance", "091")) or {}).get("balance_cents") == 1158123  # 本期那一行
    assert await store.record(EID, "bank_transaction", "BT-091-0001") is not None
    engagement = x.xero_engagement()
    assert (engagement.dataset, engagement.period_start, engagement.period_end) == (
        x.XERO_DATASET,
        date(2026, 7, 1),
        date(2026, 9, 30),
    )
    assert (engagement.client_id, engagement.client_profile_version) == ("CLIENT-XERO-3C1332BB-Q1", 1)
    assert {a.bank_name for a in await store.bank_accounts(EID)} == {"Wattlebrook Bank Limited"}
    assert len(await store.documents(EID, related_id="ACN-900043030")) == 3
    assert [j.id for j in await store.journal_entries(EID, entry_nos=["XJ-580"])] == [
        "XJ-580-1",
        "XJ-580-2",
        "XJ-580-3",
    ]
    assert [e.employee_id for e in await store.employees(EID)] == ["E001", "E002", "E003"]
    assert await store.contracts(EID) == x.xero_contracts()
    with pytest.raises(ValueError, match="fieldwork"):
        x.xero_financial_store("acceptance")
    assert len(x.xero_financial_store("acceptance", contracts=False).document_refs) == 2


EXTRACTIONS = {
    "CON-X01": ("DIISR - Small Business Services", None, 650000, "2026-09-01"),
    "CON-X02": ("Hamilton Smith Pty", "19900043058", None, "2026-06-24"),
    "CON-X03": ("Maddox Publishing Group", "21900043067", 420000, "2026-08-10"),
}
TERMS = {  # (条款类型, 页, 子条款号), 按 fixture 中的顺序
    "CON-X01": [("control_transfer", 2, "4.1"), ("payment", 2, "5.1 5.2"), ("termination", 3, "7.1 7.2")],
    "CON-X02": [
        ("control_transfer", 2, "4.1"),
        ("payment", 2, "5.1 5.2"),
        ("rebate", 3, "6.1"),
        ("termination", 3, "7.1"),
    ],
    "CON-X03": [("control_transfer", 2, "4.1"), ("payment", 2, "5.1"), ("rebate", 3, "6.1"), ("termination", 3, "7.1")],
}


@pytest.mark.parametrize("cid", sorted(EXTRACTIONS))
def test_doc04_fixture_matches_the_contract_pdf(cid: str) -> None:
    """验收 5 (a)-(c): 要素; 每个非空要素一条第 1 页的指针, 指向这份合同; 条款的类型, 页与子条款号, 原文 (归一空白)
    是该页文本层的子串, 以子条款号开头 (不含标题)."""
    extraction = ContractExtraction.model_validate_json((x.DOC04_FIXTURES / f"{cid}.json").read_bytes())
    elements = (extraction.counterparty, extraction.counterparty_abn, extraction.amount_cents, extraction.sign_date)
    assert tuple(str(v) if v is not None and k == 3 else v for k, v in enumerate(elements)) == EXTRACTIONS[cid]
    document_id = by_path()[f"contracts/{cid}.pdf"]
    given = [
        f for f, v in zip(("counterparty", "counterparty_abn", "amount_cents", "sign_date"), elements, strict=True) if v
    ]
    assert [(p.source_id, p.page, p.locator) for p in extraction.evidence] == [(document_id, 1, f) for f in given]
    pages = [normalized(page) for page in x.xero_pages(document_id)]
    found = [(t.term_type.value, t.page, " ".join(re.findall(r"^(\d+\.\d+) ", t.text, re.M))) for t in extraction.terms]
    assert found == TERMS[cid]
    assert all(normalized(t.text) in pages[t.page - 1] for t in extraction.terms)
    # 不含标题: 每行以子条款号开头, 中间与末尾也没有下一节的节标题 (如 "5. Invoicing and Payment")
    assert all(re.match(r"\d+\.\d+ ", line) for t in extraction.terms for line in t.text.splitlines())
    assert not [t.text for t in extraction.terms if re.search(r"(^| )\d+\. [A-Z]", normalized(t.text))]


def test_doc04_fixture_files() -> None:
    """三份 fixture, 共 11 条条款, 没有 default.json (运行副本与正本相同由 test_knowledge 的 fixture 守卫核对)."""
    assert sorted(p.name for p in x.DOC04_FIXTURES.iterdir()) == ["CON-X01.json", "CON-X02.json", "CON-X03.json"]
    assert sum(len(c.terms) for c in x.xero_contracts()) == 11


async def test_doc04_in_the_graph_extracts_what_the_upload_would(deps: Any) -> None:
    """图里的 DOC-04 (IngestAgent) 在只登记了文件 (合同文件 received) 而没有合同时 (不经上传, 同数据集载入后的情形)
    调 3 次大模型 (mock 读 Xero 的运行副本, 文本层取 XeroFakeEvidenceStore), 各一次通过, 各 save_contract 一次; 抽出的
    合同即上传时抽的 xero_contracts(), 只差执行 ID (RUN-DOC-04-1); 3 份文件交回 extracted (需求 R108 验收 2 的对照).
    防上传与图里两条路抽出的合同不一样."""
    financial = x.xero_financial_store("fieldwork", contracts=False)
    financial.document_refs = [
        d.model_copy(update={"status": "received"}) if d.type is DocumentType.contract else d
        for d in financial.document_refs
    ]
    trail = FakeTrail()
    graph = x.make_xero_deps(trail=trail, financial_store=financial)
    patch = await IngestAgent(graph).run(make_ctx(graph, "DOC-04", engagement=x.xero_engagement()))
    calls = [e for e in trail.entries if e.kind is TrailKind.llm_call]
    assert sorted((e.procedure_id, e.payload["fixture_key"], e.payload["attempt"]) for e in calls) == [
        ("DOC-04", cid, 1) for cid in ("CON-X01", "CON-X02", "CON-X03")
    ]
    assert sorted(financial.save_calls) == ["CON-X01", "CON-X02", "CON-X03"]
    assert sorted(financial.saved_contracts.values(), key=lambda c: c.id) == [
        c.model_copy(update={"extracted_by_run": "RUN-DOC-04-1"}) for c in x.xero_contracts()
    ]
    assert sorted((d.file_name, d.status) for d in patch["documents"]) == [
        (f"CON-X0{n}.pdf", "extracted") for n in (1, 2, 3)
    ]
    assert "6,500.00" in (await deps.evidence_store.pages(EID, by_path()["contracts/CON-X01.pdf"]))[0]


async def test_acc04_with_the_xero_registry(deps: Any) -> None:
    """真实 A2 执行 ACC-04: 登记系统按 Xero 补造的 ABN 与 ASIC 记录返回 (换成空的假登记时三项都不通过), 评分卡三项
    通过, 写 2 行外部调用留痕."""
    agent = ClientIntelAgent(deps)
    patch = await agent.run(make_ctx(deps, "ACC-04", engagement=x.xero_engagement()))
    assert [c.passed for c in patch["acceptance_scorecard"].registry_checks] == [True, True, True]
    calls = [e for e in deps.trail.entries if e.kind is TrailKind.external_call]
    assert len(calls) == 2 and deps.registry.asic_queries == ["900043021"]


async def test_confirmations_reply_with_the_demo_pdfs() -> None:
    """假函证平台对两个账户回的是补造目录的回函 PDF (不是假平台缺省的 "{账号} reply"), 渠道为直达邮件."""
    channel = x.xero_confirmations()
    request = ConfirmationRequest(
        bank_account_id="BA-091",
        bank_name="Wattlebrook Bank Limited",
        bsb="306234",
        account_no="12345679",
        as_of=x.NOW.date(),
    )
    receipt = await channel.submit(EID, request, "K-1")
    root = x.supplement().root
    assert receipt.reply_bytes == (root / REPLY_12345679).read_bytes()
    assert receipt.receipt_channel == "direct_email"


async def test_data_store_counts_and_writes_through() -> None:
    """XeroFakeDataStore: 行数与体检汇总按 Xero 的表 (分录 1017 行, 数据集 xero/20261017-q1); 写入的账表整表转交给
    同一个假财务存储. 另建一套依赖, 不改 module 级的那套."""
    deps = x.make_xero_deps()
    data = deps.data
    stats = await data.stats(EID)
    assert (stats[TableKind.journal_entries].records, stats[TableKind.trial_balance].current) == (1017, 27)
    assert stats[TableKind.employees].records == 3  # 演示导入之后工资册已写入
    inputs = await data.check_inputs(EID, x.xero_engagement().period_start, x.xero_engagement().period_end)
    assert (inputs.journal_rows, inputs.dataset) == (1017, x.XERO_DATASET)
    first, second = x.xero_tables()["delivery_notes"][:2]
    changed = first.model_copy(update={"signed_by": "Changed"})
    await data.write_tables(EID, {TableKind.delivery_notes: [changed]}, source="import", actor="U-1")
    assert await deps.financial_store.delivery_notes(EID, [first.invoice_id, second.invoice_id]) == [changed]


async def test_dataset_loader_reads_the_supplements() -> None:
    """XeroFakeDatasetLoader: load 只记调用, 不登记文件 (同真实载入器只载快照, 文件由演示数据导入上传);
    计划预置, 画像, 声明, 期间与更正分录取补造与快照."""
    store = x.xero_financial_store()
    loader = x.XeroFakeDatasetLoader(store)
    before = list(store.document_refs)
    await loader.load(EID, x.XERO_DATASET)
    assert (store.document_refs, loader.loads) == (before, [(EID, x.XERO_DATASET)])
    assert loader.planning_presets(x.XERO_DATASET) == x.xero_presets()
    assert loader.client_profile(x.XERO_DATASET).id == "CLIENT-XERO-3C1332BB-Q1"
    assert loader.dataset_traits(x.XERO_DATASET) == x.xero_traits()
    assert loader.period(x.XERO_DATASET) == (date(2026, 7, 1), date(2026, 9, 30))
    assert loader.adjustment_entry_nos(x.XERO_DATASET) == ["AJE-X01", "AJE-X02", "AJE-X03", "AJE-X04", "AJE-X05"]
    assert [r.id for r in await loader.load_adjustments(EID, x.XERO_DATASET, ["AJE-X05"])] == ["AJE-X05-1", "AJE-X05-2"]


async def test_rules_default_to_no_preset_results(deps: Any) -> None:
    """make_xero_deps 的规则引擎缺省没有预置结果: 不会拿到 Numbat 的例外 (如 REV-08 的 SI-2026-0612)."""
    result = await deps.rules.run(EID, deps.library.load("REV-08"), make_run("REV-08"), {})
    assert result.exceptions == []


def test_snapshot_functions_skip_without_a_snapshot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """查快照的仓库根指向空目录 (不是 git 仓库, 不算跟踪): 需要快照的函数跳过, 补造一组照常 (只核补造文件)."""
    monkeypatch.setattr(x, "SNAPSHOT_REPO", tmp_path)
    monkeypatch.delenv("EXTERNAL_DATA_ROOT", raising=False)
    for needs_snapshot in (x.xero_tables, x.xero_engagement, x.xero_financial_store, x.make_xero_deps):
        with pytest.raises(pytest.skip.Exception, match="没有 Xero 快照"):
            needs_snapshot()
    assert x.located_snapshot(tmp_path) is None
    assert (x.xero_profile().id, len(x.xero_documents("fieldwork")), len(x.xero_contracts())) == (
        "CLIENT-XERO-3C1332BB-Q1",
        21,
        3,
    )
    assert json.loads((x.supplement().root / "manifest.json").read_text(encoding="utf-8"))["dataset"] == x.XERO_DATASET
