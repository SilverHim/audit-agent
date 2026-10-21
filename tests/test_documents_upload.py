"""需求 R14: 文件上传, A3 分类与抽取, 人工指定类型, 原件按媒体类型返回, 合同与回函抽取结果的修改与版本."""

import asyncio
import io
import json
import zipfile
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from backend.agents import ingest, sheets
from backend.agents.base import AgentDeps
from backend.agents.ingest import (
    DocumentIntake,
    IngestAgent,
    save_new_contract,
)
from backend.contracts.data import FS_TARGET, BatchItem, DataInput, ExtractionEdit, ExtractionRecord
from backend.contracts.errors import DataRejected, DecisionRejected
from backend.contracts.models import (
    Confirmation,
    Contract,
    Decision,
    DocumentRef,
    DocumentType,
    GateVerdict,
    ProcedureRun,
    Reliability,
    ReviewTask,
    RunStatus,
    TaskStatus,
    TrailEntry,
    TrailKind,
    WorkingLanguage,
)
from backend.orchestration.decisions import GraphDecisionSubmit
from backend.orchestration.runner import GraphRunner
from backend.services import data_import
from backend.services.data_service import DataService, edit_entry
from backend.services.import_batches import staged_of
from backend.services.judgment_engine import JudgmentEngine, OutputValidator
from tests.external import REPO
from tests.fakes import (
    ENGAGEMENT,
    NUMBAT_CONTRACTS,
    NUMBAT_PROFILE,
    NUMBAT_RECORDS,
    NUMBAT_REPLIES,
    NUMBAT_TRIAL_BALANCE,
    UPLOAD_FIXTURES,
    FakeDataStore,
    FakeEngagementStore,
    FakeEvidenceStore,
    FakeFinancialStore,
    FakeRegistry,
    FakeTrail,
    LayeredMockLLM,
    api_client,
    make_ctx,
    make_deps,
    make_runner,
    wait_idle,
)
from tests.test_decisions import acc01_decision, pln_decision
from tests.test_e2e_mock import real_agents, start_demo
from tests.test_flow import FIN_APPROVE, REVIEWS, RPT_APPROVE, RPT_REJECT, submit_gate
from tests.test_p2_agents_demo import replies
from tests.test_review_decisions import record

EID = ENGAGEMENT.id
BASE = f"/api/v1/engagements/{EID}"
CON1, CON2 = (
    next(d.id for d in NUMBAT_CONTRACTS if d.file_name == f"CON-00{n}.pdf") for n in (1, 2)
)  # 演示合同的文档 ID
MAIN_FIXTURES = UPLOAD_FIXTURES.parents[1] / "llm"


def upload_deps(runner: GraphRunner) -> AgentDeps:
    """runner 的依赖换成读上传固定输出的大模型 (分类, 回函副本), 存储仍是同一套."""
    deps = runner.deps
    llm = LayeredMockLLM(UPLOAD_FIXTURES, MAIN_FIXTURES)
    validator = OutputValidator(deps.financial_store, deps.library, deps.trail)
    return replace(deps, llm=llm, judgments=JudgmentEngine(llm, validator, deps.trail, deps.library))


def setup() -> tuple[GraphRunner, DataService, FakeFinancialStore, FakeEvidenceStore, FakeDataStore]:
    data = FakeDataStore(ready=False)
    financial = FakeFinancialStore()
    runner, _ = make_runner(
        engagement_store=FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT]), data=data, financial_store=financial
    )
    financial.document_refs.clear()  # 建图辅助按种子登记的 3 份合同不要, 这里都靠上传
    evidence = runner.deps.evidence_store
    assert isinstance(evidence, FakeEvidenceStore)
    # 上传的假文件取演示 PDF 的文档 ID 与页数 (manifest; 固定输出的证据指针指向它们, 页码要落在页数内)
    evidence.ids.update({d.file_name: d.id for d in [*NUMBAT_CONTRACTS, *NUMBAT_REPLIES]})
    evidence.page_counts.update({d.file_name: d.page_count for d in [*NUMBAT_CONTRACTS, *NUMBAT_REPLIES]})
    return runner, DataService(upload_deps(runner), runner), financial, evidence, data


async def drain(service: DataService) -> None:
    await asyncio.gather(*list(service.tasks))


def document(financial: FakeFinancialStore, name: str) -> DocumentRef:
    return next(d for d in financial.document_refs if d.file_name == name)


def docx(text: str) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("word/document.xml", f"<w:document><w:body><w:p><w:t>{text}</w:t></w:p></w:body></w:document>")
    return stream.getvalue()


CHECKUP_ACTIONS = {"check", "request_raised", "request_received"}  # A3 体检与补交请求的留痕 (需求 R15, 另测)


def data_trail(runner: GraphRunner) -> list[dict[str, object]]:
    """data 留痕的 payload, 不含体检与补交请求的."""
    trail = runner.deps.trail
    assert isinstance(trail, FakeTrail)
    return [e.payload for e in trail.entries if e.kind is TrailKind.data and e.payload["action"] not in CHECKUP_ACTIONS]


async def test_uploaded_contract_is_classified_and_extracted_like_doc04() -> None:
    """验收: 上传 CON-001.pdf, A3 分类为合同, 按 DOC-04 抽取, 结果与 DOC-04 的组装一致, 文件置 extracted."""
    runner, service, financial, _, _ = setup()

    [received] = await service.upload_documents(EID, [("CON-001.pdf", b"%PDF CON-001")], actor="U-1", request_id="R-1")

    assert (received.id, received.status, received.type, received.uploaded_by) == (
        CON1,
        "received",
        DocumentType.other,
        "U-1",
    )
    assert service.processing(EID) == [frozenset({CON1})]  # 后台作业涉及这份文件
    assert runner.is_running(EID)
    await drain(service)
    assert not runner.is_running(EID) and service.processing(EID) == []
    doc = document(financial, "CON-001.pdf")
    assert (doc.type, doc.reliability, doc.status, doc.failed_step) == (
        DocumentType.contract,
        Reliability.external_copy,
        "extracted",
        None,
    )
    contract = financial.saved_contracts["CON-001"]
    trail = runner.deps.trail
    assert isinstance(trail, FakeTrail)
    [classify] = [e for e in trail.entries if e.payload.get("action") == "classify"]
    # 抽取的作业号为定下类型的那条分类留痕的 ID
    assert (contract.version, contract.document_id, contract.extracted_by_run) == (
        1,
        doc.id,
        f"UPLOAD-{CON1}-{classify.id}",
    )
    # 与编排里 DOC-04 (IngestAgent) 对同一份合同的抽取一致, 只差执行编号
    assert contract.model_copy(update={"extracted_by_run": "X"}) == (await doc04_contract("CON-001")).model_copy(
        update={"extracted_by_run": "X"}
    )
    assert contract.terms and contract.evidence
    assert [p for p in data_trail(runner)] == [
        {"action": "upload", "file_name": "CON-001.pdf", "document_id": doc.id, "request_id": "R-1"},
        {"action": "classify", "document_id": doc.id, "document_type": "contract", "reason": contract_reason()},
    ]
    history = await financial.contract_history(EID, "CON-001")
    assert [(v.version, v.changed_by, v.field) for v in history] == [(1, "A3", None)]


async def doc04_contract(cid: str) -> Contract:
    """编排里的 DOC-04: IngestAgent 对种子登记的同一份合同 (同一文件 ID 与页数) 抽取出的合同."""
    financial = FakeFinancialStore([c for c in NUMBAT_CONTRACTS if c.file_name == f"{cid}.pdf"])
    deps = make_deps(FakeRegistry(), FakeTrail(), financial_store=financial)
    evidence = deps.evidence_store
    assert isinstance(evidence, FakeEvidenceStore)
    await IngestAgent(deps).run(make_ctx(deps, "DOC-04"))
    return financial.saved_contracts[cid]


def contract_reason() -> str:
    return "The first page is headed Sales Agreement and names the customer and the supplier."


async def test_uploaded_confirmation_reply_is_extracted_without_linking() -> None:
    _, service, financial, _, data = setup()

    await service.upload_documents(EID, [("12345601.pdf", b"%PDF reply")], actor="U-1")
    await drain(service)

    doc = document(financial, "12345601.pdf")
    assert (doc.type, doc.status, doc.reliability) == (
        DocumentType.confirmation,
        "extracted",
        Reliability.external_copy,
    )
    extraction = data.extractions[("reply", doc.id, 1)]
    assert (extraction["data"]["account_no"], extraction["data"]["confirmed_balance_cents"]) == ("12345601", 120840000)


async def test_upload_runs_take_the_project_language(monkeypatch: pytest.MonkeyPatch) -> None:
    """需求 R339 验收 3 (审核 R339-07): 上传即处理的执行 (upload_run) 也记处理时项目的工作语言. 项目改为 zh 后, 直接上传
    (分类, 合同与回函的抽取), A3 按指定类型抽取 ASIC 摘录, 智能导入 (分类与合同抽取, 账表识别) 建的执行都是 zh."""
    built: list[tuple[str, str]] = []
    real = ingest.upload_run

    def recording(
        procedure_id: str, doc: DocumentRef, language: WorkingLanguage, job: str | None = None
    ) -> ProcedureRun:
        built.append((procedure_id, language))
        return real(procedure_id, doc, language, job)

    monkeypatch.setattr(ingest, "upload_run", recording)
    monkeypatch.setattr(sheets, "upload_run", recording)
    runner, service, _, evidence, _ = setup()
    assert await runner.deps.engagement_store.set_working_language(EID, "zh") == "en"
    engagement = await runner.deps.engagement_store.get(EID)
    assert engagement is not None

    await service.upload_documents(
        EID, [("CON-001.pdf", b"%PDF CON-001"), ("12345601.pdf", b"%PDF reply")], actor="U-1"
    )
    await drain(service)
    extract = await evidence.save(
        EID, "900043030.pdf", b"%PDF registry", type=DocumentType.other, reliability=Reliability.internal_copy
    )
    await DocumentIntake(upload_deps(runner)).classified(EID, extract, DocumentType.registry_extract, engagement)
    accounts = (REPO / "data" / "demo" / "accounts.csv").read_bytes()
    await service.start_batch(EID, [("CON-002.pdf", b"%PDF CON-002"), ("accounts.csv", accounts)], actor="U-1")
    await drain(service)

    assert sorted(set(built)) == [(p, "zh") for p in ("DOC-04", "DOC-06", "DOC-CLASSIFY", "DOC-REGISTRY", "DOC-SHEET")]


async def test_unknown_type_needs_manual_then_type_set_by_auditor() -> None:
    runner, service, financial, _, _ = setup()
    await service.upload_documents(EID, [("scan-0042.png", b"\x89PNG fake")], actor="U-1")
    await drain(service)
    scan = document(financial, "scan-0042.png")
    assert (scan.status, scan.failed_step, scan.media_type) == ("needs_manual", "classify", "image/png")

    with pytest.raises(DataRejected) as missing:
        await service.set_document_type(EID, scan.id, DocumentType.minutes, " ", "U-1")
    assert missing.value.code == "MISSING_FIELD"
    updated = await service.set_document_type(EID, scan.id, DocumentType.minutes, "董事会纪要扫描件", "U-1")
    await drain(service)

    assert (updated.type, updated.status, updated.failed_step) == (DocumentType.minutes, "classified", None)
    assert document(financial, "scan-0042.png").status == "classified"  # 纪要不需抽取
    assert data_trail(runner)[-1] == {
        "action": "classify",
        "document_id": scan.id,
        "document_type": "minutes",
        "reason": "董事会纪要扫描件",
    }


@pytest.mark.parametrize(
    ("kind", "reliability"),
    [
        (DocumentType.agreement, Reliability.external_copy),  # 收入合同以外的协议
        (DocumentType.financial_statements, Reliability.internal_copy),  # 财务报表以账套为准 (Issue #46 验收 1)
    ],
)
async def test_type_set_without_extraction(kind: DocumentType, reliability: Reliability) -> None:
    """人工指定为协议或财务报表: 登记为该类型与其可靠性, classified, 不抽取 (只有合同, 回函与 ASIC 摘录抽取);
    大模型只有上传时的分类一次, 数据版本与数据变动都不变."""
    runner, service, financial, _, data = setup()
    await service.upload_documents(EID, [("scan-0043.png", b"\x89PNG lease")], actor="U-1")
    await drain(service)
    before = (data.data_version, list(data.data_changes))
    scan = document(financial, "scan-0043.png")
    updated = await service.set_document_type(EID, scan.id, kind, "人工指定类型", "U-1")
    await drain(service)

    assert (updated.type, updated.reliability, updated.status) == (kind, reliability, "classified")
    assert document(financial, "scan-0043.png").status == "classified" and financial.save_calls == []
    trail = runner.deps.trail
    assert isinstance(trail, FakeTrail)
    assert [e.procedure_id for e in trail.entries if e.kind is TrailKind.llm_call] == ["DOC-CLASSIFY"]
    assert (data.data_version, data.data_changes) == before


def test_upload_reliability_covers_every_type() -> None:
    """上传与人工指定类型的可靠性覆盖全部文件类型; 新增 7 类按出具方 (外部副本或内部副本)."""
    assert set(ingest.RELIABILITY) == set(DocumentType)
    assert {t: ingest.RELIABILITY[t] for t in list(DocumentType)[6:13]} == {
        DocumentType.registry_extract: Reliability.external_copy,
        DocumentType.engagement_letter: Reliability.external_copy,
        DocumentType.representation_letter: Reliability.internal_copy,
        DocumentType.correspondence: Reliability.internal_copy,
        DocumentType.governance_communication: Reliability.internal_copy,
        DocumentType.agreement: Reliability.external_copy,
        DocumentType.internal_record: Reliability.internal_copy,
    }


def test_classify_prompt_lists_every_type_once() -> None:
    """DOC-CLASSIFY 的类型表恰为全部 14 类, 每类一行; contract 只指与客户的收入合同, 租约, 借款, 采购协议与客户订单归
    agreement."""
    deps = make_deps(FakeRegistry(), FakeTrail())
    prompt = deps.judgments.render(
        ingest.CLASSIFY, {"file_name": "x.pdf", "media_type": "application/pdf", "first_page": ""}
    )
    rows = [line.split("|")[1].strip() for line in prompt.splitlines() if line.startswith("| ") and "|---" not in line]
    assert rows[0] == "document_type" and rows[1:] == [t.value for t in DocumentType]
    contract = next(line for line in prompt.splitlines() if line.startswith("| contract |"))
    agreement = next(line for line in prompt.splitlines() if line.startswith("| agreement |"))
    assert "a contract with a customer" in contract and "supplier" not in contract.split(";")[0]
    for kind in ("lease", "loan agreement", "purchase or supplier agreement", "customer purchase order"):
        assert kind in agreement, kind


async def test_type_set_to_contract_runs_extraction() -> None:
    _, service, financial, evidence, _ = setup()
    evidence.ids["agreement.pdf"] = CON2
    evidence.page_counts["agreement.pdf"] = 3
    await service.upload_documents(EID, [("agreement.pdf", b"%PDF agreement")], actor="U-1")
    await drain(service)
    assert document(financial, "agreement.pdf").failed_step == "classify"  # 文件名看不出

    await service.set_document_type(EID, CON2, DocumentType.contract, "销售合同", "U-1")
    await drain(service)

    assert document(financial, "agreement.pdf").status == "extracted"
    assert financial.saved_contracts["agreement"].document_id == CON2


async def test_word_and_spreadsheet_first_pages_and_unreadable_files() -> None:
    _, service, financial, _, _ = setup()
    files = [
        ("board-minutes.docx", docx("Minutes of a meeting of directors")),
        ("draft-financial-statements.xlsx", workbook_bytes()),
        ("broken.docx", b"not a zip"),
    ]
    await service.upload_documents(EID, files, actor="U-1")
    await drain(service)

    assert document(financial, "board-minutes.docx").type is DocumentType.minutes
    assert document(financial, "draft-financial-statements.xlsx").type is DocumentType.financial_statements
    broken = document(financial, "broken.docx")
    assert (broken.status, broken.failed_step) == ("needs_manual", "read")


POSTER, POSTER_EMAIL = "Quenda Testposter", "quenda.poster@example.test"  # 测试里编的姓名与邮箱, 不是真实账号
SECOND_POSTER = "Wambenger Testsecond"  # 重名的第二个 Posted By 列里编的姓名


def journal_workbook() -> bytes:
    """合成的日记账导出: 第 1 行表头带两个同名的 Posted By 列 (显示名第二个为 "Posted By (2)"), 备注里有邮箱 (首页文字
    取第一个非空行作表头, 各列都进首页). Xero 网页导出的报表第 1 行是标题, 首页文字只有第一列, 那时 Posted By 本就
    不进; 这里测表头在第 1 行的情形."""
    import openpyxl

    book = openpyxl.Workbook()
    sheet = book.active
    assert sheet is not None
    for row in (
        ["Date", "Account Code", "Debit", "Credit", "Posted By", "Note", "Posted By"],
        ["2025-07-01", "200", 10, None, POSTER, f"queried by {POSTER_EMAIL}", SECOND_POSTER],
        ["2025-07-01", "090", None, 10, POSTER, None, SECOND_POSTER],
    ):
        sheet.append(row)
    stream = io.BytesIO()
    book.save(stream)
    return stream.getvalue()


async def test_spreadsheet_first_page_masks_personal_columns() -> None:
    """经 "上传文件" 上传含 Posted By 列的表格: 分类 (DOC-CLASSIFY) 的首页文字经 data_import.mask_sample 遮蔽, 姓名与
    邮箱不进提示词与留痕; Posted By 表头照留."""
    runner, service, financial, _, _ = setup()
    await service.upload_documents(EID, [("synthetic-journal.xlsx", journal_workbook())], actor="U-1")
    await drain(service)

    uploaded = document(financial, "synthetic-journal.xlsx")
    assert (uploaded.status, uploaded.failed_step) == ("needs_manual", "classify")  # 读出了首页, 兜底输出判断不了
    trail = runner.deps.trail
    assert isinstance(trail, FakeTrail)
    [call] = [e for e in trail.entries if e.kind is TrailKind.llm_call and e.procedure_id == "DOC-CLASSIFY"]
    prompt = call.payload["messages"][0]["content"]
    assert "Date, Account Code, Debit, Credit, Posted By, Note, Posted By (2)" in prompt and data_import.MASK in prompt
    dumped = json.dumps([e.model_dump(mode="json") for e in trail.entries], ensure_ascii=False)
    assert POSTER not in dumped and POSTER_EMAIL not in dumped and SECOND_POSTER not in dumped


def workbook_bytes() -> bytes:
    import openpyxl

    book = openpyxl.Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.append(["Statement of financial position", "2026"])
    stream = io.BytesIO()
    book.save(stream)
    return stream.getvalue()


async def test_upload_endpoint_validates_and_returns_registrations(monkeypatch: pytest.MonkeyPatch) -> None:
    runner, service, financial, _, _ = setup()
    async with api_client(runner) as client:
        client._transport.app.state.data_service = service  # type: ignore[attr-defined]
        ok = await client.post(
            f"{BASE}/documents",
            files=[
                ("files", ("CON-001.pdf", b"%PDF CON-001", "application/pdf")),
                ("files", ("x.csv", b"a", "text/csv")),
            ],
        )
        single = await client.post(
            f"{BASE}/documents", files={"files": ("CON-001.pdf", b"%PDF CON-001", "application/pdf")}
        )
        monkeypatch.setattr(data_import, "MAX_BYTES", 5)
        big = await client.post(f"{BASE}/documents", files={"files": ("big.pdf", b"%PDF too big", "application/pdf")})
    assert (ok.status_code, ok.json()["detail"]["code"]) == (422, "INVALID_VALUE")  # CSV 不收 (账表走导入)
    assert single.status_code == 200
    assert [d["id"] for d in single.json()] == [CON1]
    assert (big.status_code, big.json()["detail"]["code"]) == (413, "FILE_TOO_LARGE")
    await drain(service)
    again = await service.upload_documents(EID, [("CON-001.pdf", b"%PDF CON-001")], actor="U-1")
    assert again[0].status == "extracted" and service.processing(EID) == []  # 重复上传返回已有的, 不再处理
    assert len([d for d in financial.document_refs if d.file_name == "CON-001.pdf"]) == 1


async def test_upload_rejected_when_busy_or_locked() -> None:
    runner, service, _, _, _ = setup()
    runner.acquire(EID)
    with pytest.raises(DecisionRejected) as busy:
        await service.upload_documents(EID, [("CON-001.pdf", b"%PDF")], actor="U-1")
    runner.release(EID)
    engagements = runner.deps.engagement_store
    assert isinstance(engagements, FakeEngagementStore)
    await engagements.update(EID, locked=True)
    with pytest.raises(DataRejected) as locked:
        await service.upload_documents(EID, [("CON-001.pdf", b"%PDF")], actor="U-1")
    assert (busy.value.code, locked.value.code) == ("ENGAGEMENT_BUSY", "ENGAGEMENT_LOCKED")


async def test_evidence_is_served_with_its_media_type() -> None:
    runner, service, _, _, _ = setup()
    await service.upload_documents(
        EID, [("CON-001.pdf", b"%PDF CON-001"), ("board-minutes.docx", docx("Minutes"))], actor="U-1"
    )
    await drain(service)
    async with api_client(runner) as client:
        pdf = await client.get(f"{BASE}/evidence/{CON1}")
        minutes = next(d for d in runner.deps.financial_store.document_refs if d.file_name == "board-minutes.docx")  # type: ignore[attr-defined]
        word = await client.get(f"{BASE}/evidence/{minutes.id}")
        missing = await client.get(f"{BASE}/evidence/DOC-nope")
    assert (pdf.headers["content-type"], pdf.headers["content-disposition"].split(";")[0]) == (
        "application/pdf",
        "inline",
    )
    assert word.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument.wordprocessingml")
    assert word.headers["content-disposition"].startswith("attachment")
    assert missing.status_code == 404


# ── 合同抽取结果的修改与版本 ──


async def extracted_contract() -> tuple[GraphRunner, DataService, FakeFinancialStore, FakeDataStore]:
    runner, service, financial, _, data = setup()
    await service.upload_documents(EID, [("CON-001.pdf", b"%PDF CON-001")], actor="U-1")
    await drain(service)
    return runner, service, financial, data


async def test_edit_contract_term_creates_version_two_with_trail() -> None:
    """验收: 改条款文本生成第 2 版并留痕; 历史两版; 数据版本 +1."""
    runner, service, financial, data = await extracted_contract()
    before = data.data_version
    original = financial.saved_contracts["CON-001"]

    edit = ExtractionEdit(field="terms[0].text", value="Payment due 45 days after invoice.", reason="原文第 2 页")
    updated = await service.edit_contract(EID, "CON-001", edit, "U-1")

    assert updated.version == 2 and updated.terms[0].text == "Payment due 45 days after invoice."
    assert updated.terms[1:] == original.terms[1:] and updated.evidence == original.evidence
    history = await service.contract_history(EID, "CON-001")
    assert [(v.version, v.changed_by, v.field, v.reason) for v in history] == [
        (1, "A3", None, None),
        (2, "U-1", "terms[0].text", "原文第 2 页"),
    ]
    assert history[0].contract == original
    assert data.data_version == before + 1
    assert data_trail(runner)[-1] == {
        "action": "edit",
        "document_type": "contract",
        "field": "terms[0].text",
        "reason": "原文第 2 页",
        "document_id": CON1,
    }


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("terms[99].text", "x", "INVALID_VALUE"),
        ("terms[0].term_type", "warranty", "INVALID_VALUE"),
        ("counterparty_abn", "123", "INVALID_VALUE"),
        ("amount_cents", "12.5", "INVALID_VALUE"),
        ("sign_date", "2026-02-30", "INVALID_VALUE"),
        ("document_id", "DOC-x", "INVALID_VALUE"),
        ("counterparty", "", "INVALID_VALUE"),
    ],
)
async def test_edit_contract_rejects_bad_fields_and_values(field: str, value: str, code: str) -> None:
    _, service, financial, _ = await extracted_contract()
    with pytest.raises(DataRejected) as rejected:
        await service.edit_contract(EID, "CON-001", ExtractionEdit(field=field, value=value, reason="r"), "U-1")
    assert rejected.value.code == code
    assert financial.saved_contracts["CON-001"].version == 1


async def test_edit_contract_other_fields_and_same_value() -> None:
    _, service, financial, _ = await extracted_contract()
    contract = financial.saved_contracts["CON-001"]
    v2 = await service.edit_contract(
        EID, "CON-001", ExtractionEdit(field="amount_cents", value=None, reason="r"), "U-1"
    )
    v3 = await service.edit_contract(
        EID, "CON-001", ExtractionEdit(field="sign_date", value="2025-07-02", reason="r"), "U-1"
    )
    v4 = await service.edit_contract(
        EID, "CON-001", ExtractionEdit(field="counterparty_abn", value="24 907 051 601", reason="r"), "U-1"
    )
    assert (v2.amount_cents, v3.sign_date, v4.counterparty_abn, v4.version) == (
        None,
        date(2025, 7, 2),
        "24907051601",
        4,
    )
    with pytest.raises(DataRejected) as same:
        await service.edit_contract(
            EID, "CON-001", ExtractionEdit(field="sign_date", value="2025-07-02", reason="r"), "U-1"
        )
    assert same.value.code == "INVALID_VALUE"
    with pytest.raises(DataRejected) as no_reason:
        await service.edit_contract(EID, "CON-001", ExtractionEdit(field="counterparty", value="X", reason=""), "U-1")
    assert no_reason.value.code == "MISSING_FIELD"
    with pytest.raises(LookupError):
        await service.edit_contract(EID, "CON-404", ExtractionEdit(field="counterparty", value="X", reason="r"), "U-1")
    assert contract.version == 1


async def test_legacy_contract_without_history_records_base_version_first() -> None:
    """迁移 0004 之前抽取的合同没有版本记录: 历史以当前内容作唯一一版; 第一次修改时先补记修改前的一版."""
    _, service, financial, _, _ = setup()
    legacy = Contract(
        id="CON-009",
        document_id="DOC-LEGACY",
        counterparty="Old Co",
        counterparty_abn=None,
        amount_cents=None,
        sign_date=None,
        terms=[],
        extracted_by_run="RUN-DOC-04-1",
    )
    financial.saved_contracts["CON-009"] = legacy  # 直接预置, 不经 save_contract (没有版本记录)

    assert [(v.version, v.contract) for v in await service.contract_history(EID, "CON-009")] == [(1, legacy)]
    await service.edit_contract(EID, "CON-009", ExtractionEdit(field="counterparty", value="New Co", reason="r"), "U-1")
    history = await service.contract_history(EID, "CON-009")
    assert [(v.version, v.contract.counterparty, v.changed_by) for v in history] == [
        (1, "Old Co", "A3"),
        (2, "New Co", "U-1"),
    ]


async def test_contract_endpoints() -> None:
    runner, service, _, _ = await extracted_contract()
    async with api_client(runner) as client:
        client._transport.app.state.data_service = service  # type: ignore[attr-defined]
        put = await client.put(
            f"{BASE}/contracts/CON-001", json={"field": "counterparty", "value": "Renamed Pty Ltd", "reason": "更名"}
        )
        history = await client.get(f"{BASE}/contracts/CON-001/history")
        listed = await client.get(f"{BASE}/contracts")
        missing = await client.get(f"{BASE}/contracts/CON-404/history")
    assert (put.status_code, put.json()["version"], put.json()["counterparty"]) == (200, 2, "Renamed Pty Ltd")
    assert [v["version"] for v in history.json()] == [1, 2]
    assert [(c["id"], c["version"]) for c in listed.json()] == [("CON-001", 2)]
    assert missing.status_code == 404


# ── 回函抽取结果的修改: 只在图停在闸门或等复核时 ──


async def test_edit_confirmation_while_waiting_for_review_then_finish_flow() -> None:
    """P2 等复核时改回函余额: 函证出新版本写库; 之后 (含重启) 的复核恢复从库读回, 流程走完不撞版本."""
    s = await start_demo()
    await wait_idle(s.runner, EID)
    assert await s.next_nodes() == ("await_events",), s.runner.run_error(EID)
    service = DataService(s.runner.deps, s.runner)
    before = await s.runner.deps.output_store.get(EID, Confirmation, "C-BA-01")
    assert before is not None

    edited = await service.edit_confirmation(
        EID, "C-BA-01", ExtractionEdit(field="confirmed_balance_cents", value=120850000, reason="回函原文"), "U-1"
    )

    assert (edited.version, edited.confirmed_balance_cents) == (before.version + 1, 120850000)
    data = s.runner.deps.data
    assert isinstance(data, FakeDataStore)
    assert data.extractions[("confirmation", "C-BA-01", edited.version)]["field"] == "confirmed_balance_cents"
    # 模拟重启: 新 runner 接同一检查点, recover 后照常提交复核
    s.runner = GraphRunner(s.runner.app, s.runner.deps)
    await s.runner.recover()
    s.submit = type(s.submit)(s.runner.app, s.runner.deps, s.runner)
    for decision in REVIEWS:
        assert await s.next_nodes() == ("await_events",), (decision.task_id, s.runner.run_error(EID))
        await s.submit.submit(EID, decision)
        await wait_idle(s.runner, EID)
    assert await s.next_nodes() == ("gate_fin",), s.runner.run_error(EID)
    state = (await s.runner.app.aget_state(GraphRunner.config(EID))).values
    latest = await s.runner.deps.output_store.get(EID, Confirmation, "C-BA-01")
    in_state = next(c for c in state["confirmations"] if c.id == "C-BA-01")
    assert latest is not None and in_state == latest and latest.version >= edited.version
    assert latest.confirmed_balance_cents == 120850000


async def test_edit_confirmation_at_fin_gate_then_sign() -> None:
    s = await start_demo()
    await wait_idle(s.runner, EID)
    for decision in REVIEWS:
        await s.submit.submit(EID, decision)
        await wait_idle(s.runner, EID)
    assert await s.next_nodes() == ("gate_fin",)
    service = DataService(s.runner.deps, s.runner)

    edited = await service.edit_confirmation(
        EID, "C-BA-02", ExtractionEdit(field="confirming_party", value="Harbourline Bank Ltd", reason="全称"), "U-1"
    )

    assert edited.confirming_party == "Harbourline Bank Ltd"
    await submit_gate(s, FIN_APPROVE, ("gate_rpt01",))
    await submit_gate(s, RPT_REJECT, ("gate_rpt01",))
    await submit_gate(s, RPT_APPROVE, ())
    assert s.runner.run_error(EID) is None


async def test_edit_confirmation_rejected_when_not_paused_or_bad() -> None:
    """图不在闸门也不在等复核 (如 P1 前等数据) 时 NOT_EDITABLE; 函证不存在 404; 字段不认识 INVALID_VALUE."""
    data = FakeDataStore(ready=False)
    runner, store = make_runner(engagement_store=FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT]), data=data)
    await runner.start(EID)
    await wait_idle(runner, EID)
    await GraphDecisionSubmit(runner.app, runner.deps, runner).submit(EID, acc01_decision(GateVerdict.approve))
    await wait_idle(runner, EID)
    confirmation = Confirmation(
        id="C-BA-01",
        bank_account_id="BA-01",
        confirming_party="Harbourline Bank Limited",
        idempotency_key="k",
        sent_at=ENGAGEMENT.created_at,
        confirmed_balance_cents=1,
    )
    store.rows.append(confirmation)
    service = DataService(runner.deps, runner)
    edit = ExtractionEdit(field="confirmed_balance_cents", value=2, reason="r")

    with pytest.raises(DataRejected) as waiting_data:
        await service.edit_confirmation(EID, "C-BA-01", edit, "U-1")
    with pytest.raises(LookupError):
        await service.edit_confirmation(EID, "C-BA-09", edit, "U-1")
    assert waiting_data.value.code == "NOT_EDITABLE"
    assert not runner.is_running(EID)


# ── 审核 FD-R14 第 1 轮后补充 (多数来自 Codex 的探针) ──


async def test_contract_uploaded_before_p1_flows_into_rev03() -> None:
    """P1 之前上传并抽取的合同: 图里 DOC-04 不重抽, 文件登记照样并入状态, REV-03 正常判断."""
    financial = FakeFinancialStore(replies(), dict(NUMBAT_RECORDS), trial_balance=NUMBAT_TRIAL_BALANCE)
    runner, store = make_runner(
        engagement_store=FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT]),
        agents=real_agents,
        financial_store=financial,
    )
    financial.document_refs[:] = [d for d in financial.document_refs if d.file_name != "CON-001.pdf"]
    evidence = runner.deps.evidence_store
    assert isinstance(evidence, FakeEvidenceStore)
    evidence.ids["CON-001.pdf"], evidence.page_counts["CON-001.pdf"] = CON1, 3
    service = DataService(upload_deps(runner), runner)
    await service.upload_documents(EID, [("CON-001.pdf", b"%PDF CON-001")], actor="U-1")
    await drain(service)
    uploaded = financial.saved_contracts["CON-001"]
    submit = GraphDecisionSubmit(runner.app, runner.deps, runner)

    await runner.start(EID)
    await wait_idle(runner, EID)
    await submit.submit(EID, acc01_decision(GateVerdict.approve, id="D-ACC"))
    await wait_idle(runner, EID)
    await submit.submit(EID, pln_decision(GateVerdict.approve))
    await wait_idle(runner, EID)

    assert financial.saved_contracts["CON-001"] == uploaded  # DOC-04 没有重抽
    state = (await runner.app.aget_state(GraphRunner.config(EID))).values
    assert CON1 in {d.id for d in state["documents"]}
    run = await store.get(EID, ProcedureRun, "RUN-REV-03-1")
    assert run is not None and run.status is RunStatus.succeeded, run and run.failure_reason


async def test_doc04_rerun_after_failure_keeps_auditor_edit(monkeypatch: pytest.MonkeyPatch) -> None:
    """DOC-04 整次失败 (CON-002 读不了) 前已写入 CON-001, 审计师改成第 2 版; rerun 时 CON-001 已有, 不重抽, 不覆盖
    修改与历史, 只补 CON-002 (Codex 探针的场景; 探针期望重抽成第 3 版, 这里取不覆盖审计师修改的做法)."""
    runner, store = make_runner(
        engagement_store=FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT]),
        agents=lambda deps: {"DOC-04": IngestAgent(deps)},
    )
    evidence = runner.deps.evidence_store
    pages = evidence.pages

    async def broken(eid: str, document_id: str) -> list[str]:
        if document_id == CON2:
            raise OSError("CON-002 读不了")
        return await pages(eid, document_id)

    monkeypatch.setattr(evidence, "pages", broken)
    await runner.start(EID)
    await wait_idle(runner, EID)
    submit = GraphDecisionSubmit(runner.app, runner.deps, runner)
    await submit.submit(EID, acc01_decision(GateVerdict.approve, id="D-ACC"))
    await wait_idle(runner, EID)
    financial = runner.deps.financial_store
    original = (await financial.contracts(EID, ["CON-001"]))[0]
    service = DataService(runner.deps, runner)
    await service.edit_contract(
        EID, "CON-001", ExtractionEdit(field="counterparty", value="Corrected", reason="原文"), "U-1"
    )
    [task] = await store.latest(EID, ReviewTask, procedure_id="DOC-04", status=TaskStatus.pending)
    monkeypatch.setattr(evidence, "pages", pages)

    await submit.submit(EID, record(task.id, Decision.rerun, id="D-RERUN", target_version=task.target_version))
    await wait_idle(runner, EID)

    assert runner.run_error(EID) is None
    current = (await financial.contracts(EID, ["CON-001"]))[0]
    assert (current.version, current.counterparty) == (2, "Corrected")
    history = await service.contract_history(EID, "CON-001")
    assert [v.version for v in history] == [1, 2] and history[0].contract == original
    assert (await financial.contracts(EID, ["CON-002"]))[0].version == 1


async def test_new_extraction_versions_continue_and_redo_overwrites() -> None:
    """save_new_contract: 已有审计师改过的第 2 版时, 另一次执行抽取接成第 3 版; 同一次执行重做覆盖它自己写的那一版."""
    _, service, financial, _ = await extracted_contract()
    await service.edit_contract(EID, "CON-001", ExtractionEdit(field="counterparty", value="Edited", reason="r"), "U-1")
    fresh = financial.saved_contracts["CON-001"].model_copy(update={"version": 1, "extracted_by_run": "RUN-DOC-04-2"})
    deps = service.deps

    third = await save_new_contract(deps, EID, fresh)
    redo = await save_new_contract(deps, EID, fresh)

    assert (third.version, redo.version) == (3, 3)
    assert [v.version for v in await financial.contract_history(EID, "CON-001")] == [1, 2, 3]


async def test_upload_retry_after_trail_failure_still_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    """上传的留痕写失败 (文件已登记): 再上传同一文件时补写留痕并照常处理 (Codex 探针)."""
    runner, service, financial, _, _ = setup()
    trail = runner.deps.trail
    assert isinstance(trail, FakeTrail)
    original = trail.record

    async def fail(entry: TrailEntry) -> str:
        if entry.payload.get("action") == "upload":
            raise RuntimeError("留痕不可用")
        return await original(entry)

    monkeypatch.setattr(trail, "record", fail)
    with pytest.raises(RuntimeError):
        await service.upload_documents(EID, [("CON-001.pdf", b"%PDF CON-001")], actor="U-1")
    assert not runner.is_running(EID) and service.processing(EID) == []
    monkeypatch.setattr(trail, "record", original)

    await service.upload_documents(EID, [("CON-001.pdf", b"%PDF CON-001")], actor="U-1")
    await drain(service)

    assert document(financial, "CON-001.pdf").status == "extracted"
    assert [p["action"] for p in data_trail(runner)] == ["upload", "classify"]


async def test_edit_failure_commits_nothing_and_retry_backfills_trail(monkeypatch: pytest.MonkeyPatch) -> None:
    """修改回函时数据版本写失败: 函证不出新版本 (同一事务); 修改成功而留痕写失败时,
    同一修改重试返回当前版本并补写留痕."""
    s = await start_demo()
    await wait_idle(s.runner, EID)
    service = DataService(s.runner.deps, s.runner)
    data = s.runner.deps.data
    assert isinstance(data, FakeDataStore)
    before = await s.runner.deps.output_store.get(EID, Confirmation, "C-BA-01")
    bump = data.bump_version

    async def broken_bump(eid: str, input: DataInput) -> int:
        raise RuntimeError("数据版本写失败")

    monkeypatch.setattr(data, "bump_version", broken_bump)
    edit = ExtractionEdit(field="confirmed_balance_cents", value=123, reason="原文")
    with pytest.raises(RuntimeError):
        await service.edit_confirmation(EID, "C-BA-01", edit, "U-1")
    assert await s.runner.deps.output_store.get(EID, Confirmation, "C-BA-01") == before
    assert before is not None
    # 真实存储里抽取版本与函证同一事务回滚 (见 test_data_store_postgres); 假存储不回滚, 这里手动去掉
    data.extractions.pop(("confirmation", "C-BA-01", before.version + 1))
    monkeypatch.setattr(data, "bump_version", bump)

    trail = s.runner.deps.trail
    assert isinstance(trail, FakeTrail)
    write = trail.record

    async def no_edit_trail(entry: TrailEntry) -> str:
        if entry.payload.get("action") == "edit":
            raise RuntimeError("留痕不可用")
        return await write(entry)

    monkeypatch.setattr(trail, "record", no_edit_trail)
    with pytest.raises(RuntimeError):
        await service.edit_confirmation(EID, "C-BA-01", edit, "U-1")
    monkeypatch.setattr(trail, "record", write)
    edited = await s.runner.deps.output_store.get(EID, Confirmation, "C-BA-01")
    assert edited is not None and edited.version == before.version + 1

    again = await service.edit_confirmation(EID, "C-BA-01", edit, "U-1")

    assert again == edited
    entries = [e for e in trail.entries if e.payload.get("action") == "edit"]
    assert [e.idempotency_key for e in entries] == [f"{EID}:data:edit:confirmation:C-BA-01:v{edited.version}"]
    with pytest.raises(DataRejected):  # 别人以别的理由改成同值: 不算重试
        await service.edit_confirmation(EID, "C-BA-01", edit.model_copy(update={"reason": "另一理由"}), "U-1")


async def test_boolean_is_not_integer_cents() -> None:
    runner, service, _, _ = await extracted_contract()
    async with api_client(runner) as client:
        client._transport.app.state.data_service = service  # type: ignore[attr-defined]
        flag = await client.put(
            f"{BASE}/contracts/CON-001", json={"field": "amount_cents", "value": True, "reason": "r"}
        )
        text = await client.put(
            f"{BASE}/contracts/CON-001", json={"field": "amount_cents", "value": "12", "reason": "r"}
        )
    assert flag.status_code == 422
    assert (text.status_code, text.json()["detail"]["code"]) == (422, "INVALID_VALUE")


async def test_unreadable_pdf_is_registered_then_needs_manual_read(tmp_path: Path) -> None:
    """读不出的 PDF: 照样登记 (页数 0), A3 处理时置 needs_manual, 卡在读取. 假存储与真实解析器两种."""
    _, service, financial, _, _ = setup()
    await service.upload_documents(EID, [("broken.pdf", b"not a pdf")], actor="U-1")
    await drain(service)
    broken = document(financial, "broken.pdf")
    assert (broken.page_count, broken.status, broken.failed_step) == (0, "needs_manual", "read")


async def test_real_store_counts_corrupt_pdf_as_zero_pages(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """真实 FileEvidenceStore 的 PDF 解析 (只替换数据库边界): 损坏的 PDF 登记为 0 页, 不抛错 (Codex 探针)."""
    from unittest.mock import AsyncMock

    from backend.persistence import stores

    @asynccontextmanager
    async def no_transaction(*args: Any, **kwargs: Any) -> Any:
        yield AsyncMock()

    monkeypatch.setattr(stores, "transaction", no_transaction)
    monkeypatch.setattr(stores, "writable", AsyncMock())
    monkeypatch.setattr(stores.PostgresFinancialDataStore, "documents", AsyncMock(return_value=[]))
    evidence = stores.FileEvidenceStore(tmp_path, engine=object())  # type: ignore[arg-type]

    doc = await evidence.save(
        EID, "broken.pdf", b"not a pdf", type=DocumentType.other, reliability=Reliability.internal_copy
    )

    assert (doc.page_count, doc.status) == (0, "received")


async def test_doc04_keeps_current_source_when_same_name_is_reuploaded(monkeypatch: pytest.MonkeyPatch) -> None:
    """同名合同重新上传了不同内容: 新原件抽成第 2 版, 审计师改成第 3 版; 之后 DOC-04 不再抽取已被取代的旧原件,
    第 3 版保留 (审核 FD-R14 第 2 轮探针)."""
    runner, service, financial, _ = await extracted_contract()
    prototype = financial.saved_contracts["CON-001"]
    old_document_id = prototype.document_id

    async def extraction(deps: Any, eid: str, template: Any, run: Any, document: Any, profile: Any) -> Any:
        return prototype.model_copy(
            update={
                "document_id": document.id,
                "counterparty": f"Extracted from {document.id}",
                "evidence": [p.model_copy(update={"source_id": document.id}) for p in prototype.evidence],
                "extracted_by_run": run.id,
            }
        )

    monkeypatch.setattr(ingest, "extract_contract", extraction)
    evidence = runner.deps.evidence_store
    assert isinstance(evidence, FakeEvidenceStore)
    evidence.ids.pop("CON-001.pdf")  # 第二份原件按内容取得不同的文件 ID
    await service.upload_documents(EID, [("CON-001.pdf", b"%PDF revised CON-001")], actor="U-1")
    await drain(service)
    newer = financial.saved_contracts["CON-001"]
    assert newer.version == 2 and newer.document_id != old_document_id
    edit = ExtractionEdit(field="counterparty", value="Auditor corrected revised original", reason="原文")
    edited = await service.edit_contract(EID, "CON-001", edit, "U-1")

    findings = await IngestAgent(service.deps).extract_contracts(make_ctx(service.deps, "DOC-04"))

    assert financial.saved_contracts["CON-001"] == edited
    assert [(f.document.id, f.output) for f in findings if f.subject_id == "CON-001"] == [(newer.document_id, edited)]


async def test_reclassification_is_a_new_extraction_job(monkeypatch: pytest.MonkeyPatch) -> None:
    """审计师改成其他再指定回合同: 重抽是新的作业, 接成第 2 版, 第 1 版留在历史里 (审核 FD-R14 第 2 轮探针)."""
    _, service, financial, _ = await extracted_contract()
    original = financial.saved_contracts["CON-001"]

    async def corrected(deps: Any, eid: str, template: Any, run: Any, document: Any, profile: Any) -> Any:
        return original.model_copy(update={"counterparty": "New extraction", "extracted_by_run": run.id})

    monkeypatch.setattr(ingest, "extract_contract", corrected)
    await service.set_document_type(EID, original.document_id, DocumentType.other, "先改为其他", "U-1")
    await drain(service)
    await service.set_document_type(EID, original.document_id, DocumentType.contract, "重新指定合同并抽取", "U-1")
    await drain(service)

    current = financial.saved_contracts["CON-001"]
    history = await service.contract_history(EID, "CON-001")
    assert (current.version, current.counterparty) == (2, "New extraction")
    assert current.extracted_by_run != original.extracted_by_run
    assert [v.version for v in history] == [1, 2] and history[0].contract == original


async def test_interrupted_extraction_resumes_as_same_job(monkeypatch: pytest.MonkeyPatch) -> None:
    """合同写入后, 置 extracted 之前作业中断: 重传时按同一作业补做, 覆盖上次写入的第 1 版, 不多出版本."""
    runner, service, financial, evidence, _ = setup()
    update = evidence.update_document
    failed = False

    async def fail_once(eid: str, document_id: str, **values: Any) -> DocumentRef:
        nonlocal failed
        if values.get("status") == "extracted" and not failed:
            failed = True
            raise RuntimeError("作业在置 extracted 前中断")
        return await update(eid, document_id, **values)

    monkeypatch.setattr(evidence, "update_document", fail_once)
    await service.upload_documents(EID, [("CON-001.pdf", b"%PDF CON-001")], actor="U-1")
    await drain(service)
    stuck = document(financial, "CON-001.pdf")
    first = financial.saved_contracts["CON-001"]
    assert (stuck.status, stuck.type, first.version) == ("classified", DocumentType.contract, 1)

    await service.upload_documents(EID, [("CON-001.pdf", b"%PDF CON-001")], actor="U-1")
    await drain(service)

    assert document(financial, "CON-001.pdf").status == "extracted"
    current = financial.saved_contracts["CON-001"]
    assert (current.version, current.extracted_by_run) == (1, first.extracted_by_run)
    assert [v.version for v in await service.contract_history(EID, "CON-001")] == [1]
    assert [p["action"] for p in data_trail(runner)].count("upload") == 1  # 上传留痕不重复


async def cancelled_after_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[GraphRunner, DataService, FakeFinancialStore, DocumentRef]:
    """上传合同, 在分类之后, 抽取当中关停 (应用退出走的取消与释放锁): 文件停在 classified, 还没有合同."""
    runner, service, financial, _, _ = setup()
    original = ingest.extract_contract
    started, never = asyncio.Event(), asyncio.Event()

    async def pending(*args: Any, **kwargs: Any) -> Any:
        started.set()
        await never.wait()

    monkeypatch.setattr(ingest, "extract_contract", pending)
    await service.upload_documents(EID, [("CON-001.pdf", b"%PDF CON-001")], actor="U-1")
    await asyncio.wait_for(started.wait(), timeout=5)
    doc = document(financial, "CON-001.pdf")
    assert doc.status == "classified" and "CON-001" not in financial.saved_contracts
    await service.shutdown()
    assert not runner.is_running(EID) and service.processing(EID) == []
    monkeypatch.setattr(ingest, "extract_contract", original)
    return runner, service, financial, doc


async def test_reupload_resumes_contract_cancelled_after_classification(monkeypatch: pytest.MonkeyPatch) -> None:
    """分类后抽取前被取消的合同, 重传时补做抽取 (审核 FD-R14 第 2 轮探针)."""
    _, service, financial, doc = await cancelled_after_classification(monkeypatch)

    await service.upload_documents(EID, [("CON-001.pdf", b"%PDF CON-001")], actor="U-1")
    await drain(service)

    assert document(financial, "CON-001.pdf").status == "extracted"
    assert financial.saved_contracts["CON-001"].document_id == doc.id


async def test_startup_recover_resumes_unfinished_documents(monkeypatch: pytest.MonkeyPatch) -> None:
    """重启: 新的 DataService.recover 补做没做完的文件, 做完锁交给 runner.proceed 按 recover 规则带动图 (这里没有
    检查点, 以初始状态推进, 需求 R272); 项目锁被占用或已处理完的不动."""
    runner, service, financial, _ = await cancelled_after_classification(monkeypatch)
    restarted = DataService(service.deps, runner)

    runner.acquire(EID)  # 项目锁被占用: 这次跳过
    assert await restarted.recover() == set()
    assert not restarted.tasks
    runner.release(EID)

    assert await restarted.recover() == {EID}  # 交给 runner.recover(skip=...) 跳过
    assert restarted.processing(EID) == [frozenset({document(financial, "CON-001.pdf").id})]
    await drain(restarted)
    await wait_idle(runner, EID)

    assert document(financial, "CON-001.pdf").status == "extracted"
    assert "CON-001" in financial.saved_contracts and not runner.is_running(EID)
    assert (await runner.app.aget_state(GraphRunner.config(EID))).next == ("gate_acc01",)  # 图由补做的作业带动
    await restarted.recover()  # 都处理完了: 不再起作业
    assert not restarted.tasks


def test_term_index_follows_extraction_order() -> None:
    """修改用的条款下标按编号序号 (抽取时的顺序), 不受读出顺序影响 (如按文本排序的 T1, T10, T11, T2...)."""
    from backend.contracts.models import ContractTerm, TermType
    from backend.services.extraction_edits import apply_contract_edit

    numbers = sorted(range(1, 12), key=str)  # 1, 10, 11, 2, ...
    terms = [ContractTerm(id=f"C-T{n}", term_type=TermType.payment, text=f"Clause {n}", page=1) for n in numbers]
    contract = Contract(
        id="C",
        document_id="DOC-X",
        counterparty="X",
        counterparty_abn=None,
        amount_cents=None,
        sign_date=None,
        terms=terms,
        extracted_by_run="RUN-DOC-04-1",
    )

    updated = apply_contract_edit(contract, "terms[1].text", "Second clause revised")

    assert [t.id for t in updated.terms] == [f"C-T{n}" for n in range(1, 12)]
    assert (updated.terms[1].id, updated.terms[1].text) == ("C-T2", "Second clause revised")


async def test_recover_keeps_auditor_edit_of_contract_saved_before_interruption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """合同写入后置 extracted 前中断, 审计师随后改成第 2 版: 启动补做认出这个作业已写入, 不重抽, 只补文件状态
    (审核 FD-R14 第 3 轮探针)."""
    runner, service, financial, evidence, _ = setup()
    update = evidence.update_document
    failed = False

    async def fail_once(eid: str, document_id: str, **values: Any) -> DocumentRef:
        nonlocal failed
        if values.get("status") == "extracted" and not failed:
            failed = True
            raise RuntimeError("作业在置 extracted 前中断")
        return await update(eid, document_id, **values)

    monkeypatch.setattr(evidence, "update_document", fail_once)
    await service.upload_documents(EID, [("CON-001.pdf", b"%PDF CON-001")], actor="U-1")
    await drain(service)
    edit = ExtractionEdit(field="counterparty", value="Auditor corrected", reason="原文")
    edited = await service.edit_contract(EID, "CON-001", edit, "U-1")

    restarted = DataService(service.deps, runner)
    await restarted.recover()
    await drain(restarted)

    assert document(financial, "CON-001.pdf").status == "extracted"
    assert financial.saved_contracts["CON-001"] == edited


async def test_resume_does_not_let_old_job_override_newer_original(monkeypatch: pytest.MonkeyPatch) -> None:
    """旧原件的作业停在 classified, 之后同名新原件抽成合同: 补做旧作业时不覆盖, 旧原件转人工 (extract)."""
    runner, service, financial, evidence, _ = setup()
    prototype = await doc04_contract("CON-001")
    started, never = asyncio.Event(), asyncio.Event()

    async def pending(*args: Any, **kwargs: Any) -> Any:
        started.set()
        await never.wait()

    monkeypatch.setattr(ingest, "extract_contract", pending)
    await service.upload_documents(EID, [("CON-001.pdf", b"%PDF CON-001")], actor="U-1")
    await asyncio.wait_for(started.wait(), timeout=5)
    await service.shutdown()
    old = document(financial, "CON-001.pdf")

    async def extraction(deps: Any, eid: str, template: Any, run: Any, document: Any, profile: Any) -> Any:
        evidence = [p.model_copy(update={"source_id": document.id}) for p in prototype.evidence]
        update = {"document_id": document.id, "evidence": evidence, "extracted_by_run": run.id}
        return prototype.model_copy(update=update)

    monkeypatch.setattr(ingest, "extract_contract", extraction)
    evidence.ids["CON-001.pdf"] = "DOC-CON001NEW000"  # 同名新原件取得别的文件 ID
    await service.upload_documents(EID, [("CON-001.pdf", b"%PDF CON-001 revised")], actor="U-1")
    await drain(service)
    newer = financial.saved_contracts["CON-001"]
    assert newer.document_id == "DOC-CON001NEW000"

    restarted = DataService(service.deps, runner)
    await restarted.recover()
    await drain(restarted)

    assert financial.saved_contracts["CON-001"] == newer
    stale = next(d for d in financial.document_refs if d.id == old.id)
    assert (stale.status, stale.failed_step) == ("needs_manual", "extract")


async def test_recover_leaves_system_archived_documents_to_orchestration() -> None:
    """CSH-04 存档的银行直达回函 (没有上传人) 停在 received: 启动补做不碰它 (审核 FD-R14 第 3 轮探针)."""
    runner, service, financial, evidence, data = setup()
    direct = await evidence.save(
        EID,
        "12345601.pdf",
        b"%PDF direct bank reply",
        type=DocumentType.confirmation,
        reliability=Reliability.external_original,
        channel="bank_direct",
    )

    restarted = DataService(service.deps, runner)
    await restarted.recover()

    assert not restarted.tasks and financial.document_refs == [direct] and not data.extractions


async def test_recover_and_reupload_backfill_missing_upload_trail(monkeypatch: pytest.MonkeyPatch) -> None:
    """登记后上传留痕没写成: 启动补做先补上传留痕再处理; 之后重传同一文件不再多记 (审核 FD-R14 第 3 轮探针)."""
    runner, service, financial, _, _ = setup()
    trail = runner.deps.trail
    write = trail.record

    async def no_upload_trail(entry: TrailEntry) -> str:
        if entry.kind is TrailKind.data and entry.payload.get("action") == "upload":
            raise RuntimeError("上传留痕写失败")
        return await write(entry)

    monkeypatch.setattr(trail, "record", no_upload_trail)
    with pytest.raises(RuntimeError):
        await service.upload_documents(EID, [("CON-001.pdf", b"%PDF CON-001")], actor="U-1")
    monkeypatch.setattr(trail, "record", write)

    restarted = DataService(service.deps, runner)
    await restarted.recover()
    await drain(restarted)
    await wait_idle(runner, EID)  # 补做完由 runner.proceed 带动图 (没有检查点, 以初始状态推进到 ACC-01 闸门)
    await restarted.upload_documents(EID, [("CON-001.pdf", b"%PDF CON-001")], actor="U-2")
    await drain(restarted)

    assert document(financial, "CON-001.pdf").status == "extracted"
    entries = [e for e in trail.entries if e.kind is TrailKind.data and e.payload["action"] not in CHECKUP_ACTIONS]
    assert [e.payload["action"] for e in entries] == ["upload", "classify"]
    assert entries[0].actor == "U-1"


# ── 审计师自己上传的财务报表: 只分类, 不抽取 (Issue #46) ──

LLM_XERO = UPLOAD_FIXTURES.parents[1] / "llm_xero"  # Xero 演示的固定输出正本, 含 DOC-CLASSIFY/fs_v1.json
FS_PDF = "fs_v1.pdf"  # 补造的财务报表样例 (不在补造清单的登记里), DOC-CLASSIFY 的固定输出定为 financial_statements


def statements_setup() -> tuple[GraphRunner, DataService, FakeFinancialStore, FakeEvidenceStore, FakeDataStore]:
    """setup() 的存储, 大模型先读 Xero 的固定输出正本 (DOC-CLASSIFY/fs_v1), 再读上传固定输出."""
    runner, _, financial, evidence, data = setup()
    llm = LayeredMockLLM(LLM_XERO, UPLOAD_FIXTURES)
    deps = runner.deps
    validator = OutputValidator(deps.financial_store, deps.library, deps.trail)
    deps = replace(deps, llm=llm, judgments=JudgmentEngine(llm, validator, deps.trail, deps.library))
    return runner, DataService(deps, runner), financial, evidence, data


async def test_uploaded_statements_are_only_classified() -> None:
    """Issue #46 验收 1: 上传 fs_v1.pdf (A3 按 DOC-CLASSIFY 定为 financial_statements): 文件停在 classified, 不算
    没做完 (启动不补做); 大模型只有分类一次, 数据版本与数据变动都不变 (没有程序因它过期). 改类型一例见
    test_type_set_without_extraction, 智能导入一例见 test_import_batches."""
    runner, service, financial, _, data = statements_setup()
    before = (data.data_version, list(data.data_changes))
    await service.upload_documents(EID, [(FS_PDF, b"%PDF statements")], actor="U-1")
    await drain(service)

    doc = document(financial, FS_PDF)
    assert (doc.type, doc.status, doc.failed_step) == (DocumentType.financial_statements, "classified", None)
    assert not ingest.unfinished(doc)
    trail = runner.deps.trail
    assert isinstance(trail, FakeTrail)
    assert [e.procedure_id for e in trail.entries if e.kind is TrailKind.llm_call] == ["DOC-CLASSIFY"]
    assert (data.data_version, data.data_changes) == before


OLD_STATEMENTS = {"prepared_at": "2026-10-22", "lines": []}  # 旧批次里 R217 抽出的报表 (StatementsExtraction 的 JSON)


async def old_statements_edit() -> tuple[object, object]:
    """旧项目里审计师改过待审财报 (抽取版本表里带 field 的 FS_TARGET 一版): 补写留痕的 payload 与摘要."""
    record = ExtractionRecord(
        target_type=FS_TARGET,
        target_id="FS",
        version=2,
        data={"id": "FS", "version": 2, "document_id": "DOC-e2670a8e43ac", "lines": []},
        field="prepared_at",
        reason="按董事声明日改",
        changed_by="U-1",
        changed_at=datetime(2026, 10, 23, tzinfo=UTC),
    )
    entry = edit_entry(EID, record)
    payload = {
        "action": "edit",
        "document_type": "financial_statements",
        "field": "prepared_at",
        "reason": "按董事声明日改",
        "document_id": "DOC-e2670a8e43ac",
    }
    return (entry.payload, entry.summary), (payload, "修改待审财报 FS 的抽取结果 prepared_at, 第 2 版")


async def old_batch_item() -> tuple[object, object]:
    """旧批次里带待审财报抽取结果 (statements) 的文件项: 读得回, 确认时只分类."""
    item = BatchItem(
        id="BAT-1-1",
        file_name=FS_PDF,
        document_id="DOC-e2670a8e43ac",
        item_kind="document",
        document_type=DocumentType.financial_statements,
        statements=OLD_STATEMENTS,
    )
    item = BatchItem.model_validate_json(item.model_dump_json())
    _, service, _, evidence, _ = statements_setup()
    staged = await evidence.save(
        EID, FS_PDF, b"%PDF fs_v1", type=DocumentType.other, reliability=Reliability.internal_copy, status="staged"
    )
    change = await DocumentIntake(service.deps).staged_change(EID, staged, staged_of(item))
    actual = (item.statements, change.type, change.status, change.failed_step)
    return actual, (OLD_STATEMENTS, DocumentType.financial_statements, "classified", None)


@pytest.mark.parametrize("case", [old_statements_edit, old_batch_item], ids=["edit_trail", "batch_item"])
async def test_old_statements_data_still_reads(case: Callable[[], Awaitable[tuple[object, object]]]) -> None:
    """Issue #46 删去待审财报后为读旧数据保留的项: TARGET_NAMES 与 EDITED_TYPES 的财务报表一项 (FS_TARGET),
    BatchItem.statements. 改过待审财报的旧项目补写留痕照常, 带 statements 的旧批次读得回且确认时只分类. 防把这些
    保留项当死代码删掉: 只删 TARGET_NAMES 那项时 backfill_trails 抛 KeyError, 该项目的数据写入全部失败."""
    actual, expected = await case()
    assert actual == expected
