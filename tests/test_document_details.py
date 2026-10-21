"""需求 R90: 上传文件的补登记 (可靠性, 文件日期与关联对象), 按关联对象取文件, 补登记的版本与留痕, 过期.

不连库: 假存储 (文件登记在 FakeFinancialStore, 抽取版本在 FakeDataStore). 文件按上传后的状态登记 (渠道
auditor_upload, 可靠性按类型缺省, 没有文件日期与关联对象). Xero 的 16 份取补造清单 (manifest) 的取值.
"""

from datetime import date
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import ValidationError

from backend.agents.ingest import RELIABILITY, registered_reliability
from backend.contracts.data import DataChange, DataInput, DocumentDetailsEdit
from backend.contracts.errors import DataRejected, DecisionRejected
from backend.contracts.models import (
    AgentId,
    DocumentRef,
    DocumentStatus,
    DocumentType,
    EvidencePointer,
    EvidenceType,
    Reliability,
    TrailEntry,
    TrailKind,
)
from backend.services.data_service import DataService
from backend.services.judgment_engine import OutputValidator, PointerResolver
from tests.fakes import (
    ENGAGEMENT,
    NOW,
    NUMBAT_AUDITOR,
    FakeDataStore,
    FakeEngagementStore,
    FakeTrail,
    api_client,
    xero_document,
    xero_documents,
)
from tests.test_documents_upload import data_trail, drain, setup
from tests.test_rerun_stale import demo_at_fin_gate, overview

EID = ENGAGEMENT.id
BASE = f"/api/v1/engagements/{EID}"
ROOT = Path(__file__).resolve().parents[1]


def uploaded(
    name: str, kind: DocumentType = DocumentType.agreement, status: DocumentStatus = "classified"
) -> DocumentRef:
    """审计师上传后由 A3 分类过的一份文件 (可靠性按类型缺省, 没有文件日期与关联对象)."""
    return DocumentRef(
        id=f"DOC-{name.split('.')[0][:12]:0<12}",
        type=kind,
        file_name=name,
        sha256=name,
        page_count=2,
        reliability=RELIABILITY[kind],
        status=status,
        uploaded_at=NOW,
        uploaded_by="U-1",
    )


LEASE = uploaded("lease.pdf")
MEMO = uploaded("memo.pdf", DocumentType.internal_record)


def edit(**values: Any) -> DocumentDetailsEdit:
    return DocumentDetailsEdit.model_validate({"reason": "核对原件", **values})


def service_with(*documents: DocumentRef) -> tuple[Any, DataService, Any, Any, FakeDataStore]:
    runner, service, financial, evidence, data = setup()
    financial.document_refs.extend(documents)
    return runner, service, financial, evidence, data


def current(financial: Any, document_id: str) -> DocumentRef:
    return next(d for d in financial.document_refs if d.id == document_id)


# ── 契约 (验收 1) ───────────────


def test_details_edit_defaults_to_no_change() -> None:
    """三项缺省为 None (不改), 理由选填, 缺省为空串 (Issue #61); 多给字段报错."""
    found = DocumentDetailsEdit.model_validate({})
    assert (found.reliability, found.document_date, found.related_ids, found.reason) == (None, None, None, "")
    with pytest.raises(ValidationError):
        DocumentDetailsEdit.model_validate({"reason": "r", "type": "contract"})


async def test_documents_filter_by_related_id_and_type() -> None:
    """related_id 只取关联对象含它的; 与 type 同时给时都须满足; 排序同登记顺序."""
    _, service, financial, _, _ = service_with(LEASE, MEMO)
    await service.set_document_details(
        EID, {LEASE.id: edit(related_ids=["XJ-1", "XJ-2"]), MEMO.id: edit(related_ids=["XJ-2"])}, "U-1"
    )
    assert [d.id for d in await financial.documents(EID, related_id="XJ-2")] == [LEASE.id, MEMO.id]
    assert [d.id for d in await financial.documents(EID, related_id="XJ-1")] == [LEASE.id]
    assert [d.id for d in await financial.documents(EID, DocumentType.internal_record, "XJ-1")] == []
    assert [d.id for d in await financial.documents(EID, DocumentType.internal_record, "XJ-2")] == [MEMO.id]
    assert await financial.documents(EID, related_id="XJ-9") == []


# ── 补登记的写入 (验收 3) ───────────────


async def test_details_write_versions_trail_and_one_data_version() -> None:
    """第一次补登记先补记修改前的一版 (A3, field 空), 再记改后的一版 (改了的字段按顺序以逗号连接); 登记的改动在
    审计产出事务内; 数据版本 +1 并记 documents; 提交后写 data 留痕. 再改一项接着出第 3 版, 不再补记."""
    runner, service, financial, evidence, data = service_with(LEASE)
    before = data.data_version

    [updated] = await service.set_document_details(
        EID,
        {LEASE.id: edit(reliability="external_original", document_date="2026-09-22", related_ids=[" ACN-1 ", "XJ-9"])},
        "U-1",
    )

    assert (updated.reliability, updated.document_date, updated.related_ids) == (
        Reliability.external_original,
        date(2026, 9, 22),
        ["ACN-1", "XJ-9"],  # 各 ID 去首尾空白
    )
    assert current(financial, LEASE.id) == updated
    assert evidence.detail_updates == [(LEASE.id, True)]  # 在审计产出事务内
    history = await data.extraction_versions(EID, "document", LEASE.id)
    assert [(r.version, r.changed_by, r.field, r.reason) for r in history] == [
        (1, AgentId.A3, None, None),
        (2, "U-1", "reliability,document_date,related_ids", "核对原件"),
    ]
    assert history[0].data == {
        "type": "agreement",
        "reliability": "external_copy",
        "document_date": None,
        "related_ids": [],
    }
    assert history[1].data == {
        "type": "agreement",
        "reliability": "external_original",
        "document_date": "2026-09-22",
        "related_ids": ["ACN-1", "XJ-9"],
    }
    assert data.data_version == before + 1
    # 记下补登记的文件类型 (需求 R304): 过期只看读该类文件登记的程序
    assert data.data_changes[-1] == DataChange(
        data_version=before + 1, input=DataInput.documents, document_types=[DocumentType.agreement]
    )
    trail = runner.deps.trail
    assert isinstance(trail, FakeTrail)
    [entry] = [e for e in trail.entries if e.payload.get("action") == "edit"]
    assert (entry.kind, entry.actor, entry.idempotency_key) == (
        TrailKind.data,
        "U-1",
        f"{EID}:data:edit:document:{LEASE.id}:v2",
    )
    assert entry.payload == {
        "action": "edit",
        "document_type": "agreement",
        "document_id": LEASE.id,
        "field": "reliability,document_date,related_ids",
        "reason": "核对原件",
    }

    await service.set_document_details(EID, {LEASE.id: edit(document_date="2026-09-23", reason="改正日期")}, "U-2")
    history = await data.extraction_versions(EID, "document", LEASE.id)
    assert [(r.version, r.changed_by, r.field) for r in history][1:] == [
        (2, "U-1", "reliability,document_date,related_ids"),
        (3, "U-2", "document_date"),
    ]
    assert history[2].data["reliability"] == "external_original"  # 没给的不改
    assert data.data_version == before + 2


async def test_batch_of_documents_bumps_data_version_once() -> None:
    """一批补登记几份文件: 每份各记版本与留痕, 数据版本只 +1."""
    runner, service, _, _, data = service_with(LEASE, MEMO)
    before = data.data_version
    found = await service.set_document_details(
        EID, {MEMO.id: edit(document_date="2026-10-16"), LEASE.id: edit(reliability="external_original")}, "U-1"
    )
    assert [d.id for d in found] == [MEMO.id, LEASE.id]  # 按请求的顺序
    assert data.data_version == before + 1
    keys = [e.idempotency_key for e in runner.deps.trail.entries if e.payload.get("action") == "edit"]
    assert keys == [f"{EID}:data:edit:document:{MEMO.id}:v2", f"{EID}:data:edit:document:{LEASE.id}:v2"]


async def test_failed_write_rolls_back_registration_and_trail_is_backfilled(monkeypatch: pytest.MonkeyPatch) -> None:
    """数据版本写失败: 登记不变 (同一事务); 写成而留痕写失败时, 下次取锁补写 (backfill_trails), 同一修改重试返回
    当前登记, 不再写."""
    runner, service, financial, _, data = service_with(LEASE)
    bump = data.bump_version

    async def broken_bump(eid: str, input: DataInput, **_: Any) -> int:
        raise RuntimeError("数据版本写失败")

    monkeypatch.setattr(data, "bump_version", broken_bump)
    with pytest.raises(RuntimeError):
        await service.set_document_details(EID, {LEASE.id: edit(reliability="external_original")}, "U-1")
    assert current(financial, LEASE.id) == LEASE
    data.extractions.clear()  # 真实存储里抽取版本随事务回滚 (见连库测试); 假存储不回滚, 这里手动去掉
    monkeypatch.setattr(data, "bump_version", bump)

    trail = runner.deps.trail
    assert isinstance(trail, FakeTrail)
    write = trail.record

    async def no_edit_trail(entry: TrailEntry) -> str:
        if entry.payload.get("action") == "edit":
            raise RuntimeError("留痕不可用")
        return await write(entry)

    monkeypatch.setattr(trail, "record", no_edit_trail)
    with pytest.raises(RuntimeError):
        await service.set_document_details(EID, {LEASE.id: edit(reliability="external_original")}, "U-1")
    monkeypatch.setattr(trail, "record", write)
    version = data.data_version
    edited = current(financial, LEASE.id)
    assert edited.reliability is Reliability.external_original

    again = await service.set_document_details(EID, {LEASE.id: edit(reliability="external_original")}, "U-1")

    assert again == [edited] and data.data_version == version  # 重试不再写
    assert [e.idempotency_key for e in trail.entries if e.payload.get("action") == "edit"] == [
        f"{EID}:data:edit:document:{LEASE.id}:v2"
    ]  # 取锁时补写
    assert len(await data.extraction_versions(EID, "document", LEASE.id)) == 2


# ── 拒绝 (验收 4) ───────────────


@pytest.mark.parametrize(
    ("document_id", "change", "code"),
    [
        (LEASE.id, edit(), "MISSING_FIELD"),  # 三项都没给 (审核 R90-03)
        (LEASE.id, edit(reliability="external_copy"), "INVALID_VALUE"),  # 与当前相同
        (LEASE.id, edit(related_ids=["XJ-1", " "]), "INVALID_VALUE"),  # 空串
        (LEASE.id, edit(related_ids=["XJ-1", "XJ-1 "]), "INVALID_VALUE"),  # 重复 (去空白后)
        ("DOC-staged0000", edit(reliability="external_original"), "INVALID_VALUE"),  # 还在导入确认单里
        ("DOC-notimport00", edit(reliability="external_original"), "INVALID_VALUE"),  # 确认时没勾选
    ],
)
async def test_bad_details_are_rejected_and_nothing_written(
    document_id: str, change: DocumentDetailsEdit, code: str
) -> None:
    """拒绝时一份都不写: 一批里有一份不过, 前面合格的也不写."""
    staged = uploaded("staged.pdf", DocumentType.other, "staged").model_copy(update={"id": "DOC-staged0000"})
    held = uploaded("notimport.pdf", DocumentType.other, "not_imported").model_copy(update={"id": "DOC-notimport00"})
    runner, service, financial, _, data = service_with(LEASE, MEMO, staged, held)
    before = (list(financial.document_refs), data.data_version)
    with pytest.raises(DataRejected) as rejected:
        await service.set_document_details(EID, {MEMO.id: edit(document_date="2026-10-16"), document_id: change}, "U-1")
    assert rejected.value.code == code
    assert (financial.document_refs, data.data_version) == before
    assert data.extractions == {} and not [e for e in runner.deps.trail.entries if e.payload.get("action") == "edit"]


async def test_unknown_document_busy_and_locked() -> None:
    runner, service, _, _, _ = service_with(LEASE)
    with pytest.raises(LookupError):
        await service.set_document_details(EID, {"DOC-nothing": edit(reliability="external_original")}, "U-1")
    runner.acquire(EID)
    with pytest.raises(DecisionRejected) as busy:
        await service.set_document_details(EID, {LEASE.id: edit(reliability="external_original")}, "U-1")
    runner.release(EID)
    engagements = runner.deps.engagement_store
    assert isinstance(engagements, FakeEngagementStore)
    await engagements.update(EID, locked=True)
    with pytest.raises(DataRejected) as locked:
        await service.set_document_details(EID, {LEASE.id: edit(reliability="external_original")}, "U-1")
    assert (busy.value.code, locked.value.code) == ("ENGAGEMENT_BUSY", "ENGAGEMENT_LOCKED")


async def test_same_values_by_someone_else_are_not_a_retry() -> None:
    """重试须是同一人以同一理由: 别人 (或换了理由) 补登记成同样的值是没有要改的, INVALID_VALUE."""
    _, service, _, _, _ = service_with(LEASE)
    await service.set_document_details(EID, {LEASE.id: edit(document_date="2026-09-22")}, "U-1")
    for actor, reason in (("U-2", "核对原件"), ("U-1", "另一理由")):
        with pytest.raises(DataRejected) as same:
            await service.set_document_details(EID, {LEASE.id: edit(document_date="2026-09-22", reason=reason)}, actor)
        assert same.value.code == "INVALID_VALUE"


# ── 接口 (验收 5) ───────────────


async def test_details_api_history_and_related_filter() -> None:
    """PUT details 返回改后的登记; GET documents?related_id= 过滤; GET history 按版本升序, 没改过的以当前登记作
    唯一一版; 理由选填 (Issue #61): 只有空白的理由照常补登记, 版本与留痕记为空串; 错误码: 404, 422, 409 (忙)."""
    runner, service, _, _, _ = service_with(LEASE, MEMO)
    async with api_client(runner) as client:
        client._transport.app.state.data_service = service  # type: ignore[attr-defined]
        untouched = await client.get(f"{BASE}/documents/{MEMO.id}/history")
        put = await client.put(
            f"{BASE}/documents/{LEASE.id}/details",
            json={"reliability": "external_original", "related_ids": ["ACN-900043030"], "reason": "核对原件"},
        )
        filtered = await client.get(f"{BASE}/documents", params={"related_id": "ACN-900043030"})
        everything = await client.get(f"{BASE}/documents")
        history = await client.get(f"{BASE}/documents/{LEASE.id}/history")
        missing = await client.put(
            f"{BASE}/documents/DOC-nothing/details", json={"reliability": "external_original", "reason": "r"}
        )
        no_history = await client.get(f"{BASE}/documents/DOC-nothing/history")
        blank = await client.put(
            f"{BASE}/documents/{LEASE.id}/details", json={"document_date": "2026-01-01", "reason": " "}
        )
        unstated = await client.put(f"{BASE}/documents/{LEASE.id}/details", json={"document_date": "2026-01-02"})
        after = await client.get(f"{BASE}/documents/{LEASE.id}/history")
        same = await client.put(
            f"{BASE}/documents/{LEASE.id}/details", json={"reliability": "external_original", "reason": "r2"}
        )
        runner.acquire(EID)
        busy = await client.put(
            f"{BASE}/documents/{LEASE.id}/details", json={"document_date": "2026-01-01", "reason": "r"}
        )
        runner.release(EID)
    assert put.status_code == 200, put.text
    assert (put.json()["reliability"], put.json()["related_ids"]) == ("external_original", ["ACN-900043030"])
    assert [d["id"] for d in filtered.json()] == [LEASE.id]
    assert len(everything.json()) == 2
    assert [(v["version"], v["changed_by"], v["field"]) for v in history.json()] == [
        (1, "A3", None),
        (2, NUMBAT_AUDITOR.id, "reliability,related_ids"),
    ]
    [only] = untouched.json()
    assert (only["version"], only["changed_by"], only["field"], only["data"]["reliability"]) == (
        1,
        "A3",
        None,
        "internal_copy",
    )
    assert [r.status_code for r in (missing, no_history)] == [404, 404]
    assert [r.status_code for r in (blank, unstated)] == [200, 200], (blank.text, unstated.text)
    assert [(v["version"], v["field"], v["reason"]) for v in after.json()[-2:]] == [
        (3, "document_date", ""),
        (4, "document_date", ""),
    ]
    trail = runner.deps.trail
    assert isinstance(trail, FakeTrail)
    edits = [e.payload for e in trail.entries if e.payload.get("action") == "edit"]
    assert [p["reason"] for p in edits[-2:]] == ["", ""]
    assert (same.status_code, same.json()["detail"]["code"]) == (422, "INVALID_VALUE")
    assert (busy.status_code, busy.json()["detail"]["code"]) == (409, "ENGAGEMENT_BUSY")


# ── Xero 清单 (验收 6) ───────────────


def listed() -> list[str]:
    """补造清单中关联方与审计证据两组的 16 份 (经 tests.external 定位补造目录)."""
    return sorted(p for p in xero_documents() if p.startswith(("documents/related_party/", "documents/evidence/")))


FIRST = ("documents/evidence/fixed_asset_register.pdf", "documents/evidence/accounting_policies_extract.pdf")


def as_uploaded(path: str) -> DocumentRef:
    """清单中一份文件上传后的登记: 类型为清单类型, 可靠性按类型缺省, 没有文件日期与关联对象."""
    found = xero_document(path)
    return found.model_copy(
        update={
            "reliability": RELIABILITY[found.type],
            "status": "classified",
            "uploaded_by": "U-1",
            "document_date": None,
            "related_ids": [],
        }
    )


def listed_edit(path: str) -> DocumentDetailsEdit:
    entry = xero_documents()[path]
    return edit(
        reliability=entry["reliability"], document_date=entry["document_date"], related_ids=entry["related_ids"]
    )


def names(documents: list[DocumentRef]) -> list[str]:
    return [Path(d.file_name).stem for d in documents]


@pytest.mark.external
async def test_xero_list_details_and_lookup_by_related_object() -> None:
    """补造清单的 16 份 (关联方与审计证据两组) 按清单补登记后按关联对象取文件; 补登记后 OutputValidator 解析固定
    资产登记簿第 1 页的指针得 internal_original (缺省时为 internal_copy)."""
    documents = listed()
    assert len(documents) == 16
    runner, service, financial, _, _ = service_with(*(as_uploaded(p) for p in documents))
    register = as_uploaded(FIRST[0])
    pointer = EvidencePointer(
        type=EvidenceType.voucher, source_type="document", source_id=register.id, locator="page 1", page=1
    )
    validator = OutputValidator(financial, runner.deps.library, runner.deps.trail)
    resolved, _ = await PointerResolver(validator, EID, None).resolve(pointer)
    assert resolved is not None and resolved.reliability is Reliability.internal_copy

    await service.set_document_details(EID, {p_id(p): listed_edit(p) for p in FIRST}, "U-1")
    assert sorted(names(await financial.documents(EID, related_id="XJ-499"))) == [
        "accounting_policies_extract",
        "fixed_asset_register",
    ]
    rest = [p for p in documents if p not in FIRST]
    assert len(rest) == 14
    await service.set_document_details(EID, {p_id(p): listed_edit(p) for p in rest}, "U-1")

    assert sorted(names(await financial.documents(EID, related_id="ACN-900043030"))) == [
        "harbourview_lease",
        "vpa_rent_letter",
        "vpa_sale_termination_notice",
    ]
    assert sorted(names(await financial.documents(EID, related_id="CLIENT-XERO-3C1332BB-Q1"))) == [
        "director_loan_confirmation",
        "director_support_letter",
        "going_concern_memo_forecast",
    ]
    for related in ("XJ-548", "BT-090-0097"):
        assert names(await financial.documents(EID, related_id=related)) == ["smart_agency_invoice_SM0195-70135"]
    assert names(await financial.documents(EID, related_id="E003")) == ["expense_claim_E003"]
    assert await financial.documents(EID, related_id="XJ-0") == []
    for path in documents:  # 登记与清单逐项一致
        found = current(financial, p_id(path))
        entry = xero_documents()[path]
        assert (found.reliability, found.document_date, found.related_ids) == (
            Reliability(entry["reliability"]),
            date.fromisoformat(entry["document_date"]),
            entry["related_ids"],
        ), path
    # 清单里每个关联对象逐个按对象取文件, 恰为清单里含它的几份 (审核 R90-04)
    expected: dict[str, set[str]] = {}
    for path in documents:
        for related in xero_documents()[path]["related_ids"]:
            expected.setdefault(related, set()).add(Path(path).stem)
    for related, stems in expected.items():
        assert set(names(await financial.documents(EID, related_id=related))) == stems, related
    assert expected["XJ-397"] == {"director_loan_agreement", "director_loan_confirmation"}
    # field 只记改了的项: 协议的清单可靠性同类型缺省 (external_copy), 不算改了 (审核 R90-03)
    [*_, last] = await service.deps.data.extraction_versions(
        EID, "document", p_id("documents/related_party/harbourview_lease.pdf")
    )
    assert last.field == "document_date,related_ids"
    resolved, _ = await PointerResolver(validator, EID, None).resolve(pointer)
    assert resolved is not None and resolved.reliability is Reliability.internal_original


def p_id(path: str) -> str:
    return str(xero_documents()[path]["document_id"])


# ── 过期 (验收 7) ───────────────


async def test_details_make_only_document_reading_programs_stale() -> None:
    """Numbat 停在 FIN-01 时 (全流程只走一次) 依次补登记三类文件, 每次数据版本 +1, 数据变动记 documents 并带该文件的
    类型 (需求 R304), 只有读该类文件登记的程序过期 (过期名单逐步累积):
    - 新登记一份财务报表再补登记: 读合同, 回函登记的 REV-03, REV-02, CSH-02 都不过期 (R287 实测的情形);
    - 补登记回函: 只有 CSH-02;
    - 补登记合同的可靠性: 再加 REV-03, REV-02 (抽合同的 DOC-04 不读登记, 不过期).
    补登记的留痕不进体检与补交请求 (体检记录与请求都不变)."""
    s = await demo_at_fin_gate()
    service = DataService(s.runner.deps, s.runner)
    data = s.runner.deps.data
    assert isinstance(data, FakeDataStore)
    assert (await overview(s)).stale_procedure_ids == []
    documents = await s.runner.deps.financial_store.documents(EID)
    reply = next(d for d in documents if d.type is DocumentType.confirmation)
    contract = next(d for d in documents if d.type is DocumentType.contract)
    statements = await s.runner.deps.evidence_store.save(
        EID,
        "fs_v1.pdf",
        b"%PDF fs",
        type=DocumentType.financial_statements,
        reliability=Reliability.internal_copy,
        uploaded_by="U-1",
    )
    checks, requests = await data.check_run(EID), await data.requests(EID)
    steps = [
        (statements, edit(document_date="2026-10-22"), []),
        (reply, edit(document_date="2026-10-16"), ["CSH-02"]),
        (contract, edit(reliability="external_original"), ["CSH-02", "REV-02", "REV-03"]),
    ]
    for document, details, expected in steps:
        version = data.data_version

        await service.set_document_details(EID, {document.id: details}, "U-1")

        assert data.data_changes[-1] == DataChange(
            data_version=version + 1, input=DataInput.documents, document_types=[document.type]
        )
        assert sorted((await overview(s)).stale_procedure_ids) == expected, document.type
    assert (await data.check_run(EID), await data.requests(EID)) == (checks, requests)
    assert data_trail(s.runner)[-1]["action"] == "edit"


# ── 改类型时的可靠性 (裁定 2: 补登记过的保留) ───────────────


async def test_set_type_keeps_registered_reliability_only_after_details() -> None:
    """补登记过可靠性的文件再指定类型 (含随后 A3 的抽取写回登记): 可靠性保留; 只补登记了日期的照旧取新类型缺省."""
    lease, memo = LEASE, MEMO
    _, service, financial, _, _ = service_with(lease, memo)
    await service.set_document_details(EID, {lease.id: edit(reliability="internal_original")}, "U-1")
    await service.set_document_details(EID, {memo.id: edit(document_date="2026-10-16")}, "U-1")

    kept = await service.set_document_type(EID, lease.id, DocumentType.minutes, "其实是纪要", "U-1")
    await drain(service)  # 指定类型后 A3 在后台处理, 持项目锁
    reset = await service.set_document_type(EID, memo.id, DocumentType.correspondence, "其实是往来函件", "U-1")
    await drain(service)

    assert kept.reliability is Reliability.internal_original
    assert current(financial, lease.id).reliability is Reliability.internal_original  # 后台处理不覆盖
    assert reset.reliability is RELIABILITY[DocumentType.correspondence]
    assert current(financial, memo.id).reliability is RELIABILITY[DocumentType.correspondence]


async def test_registered_reliability_reads_the_details_history() -> None:
    """共用函数: 有 field 含 reliability 的补登记版本才保留登记上的值; 别的对象或别的字段的版本不算."""
    runner, _, _, _, data = service_with(LEASE)
    deps = runner.deps
    original = LEASE.model_copy(update={"reliability": Reliability.external_original})
    assert await registered_reliability(deps, EID, original, DocumentType.minutes) is Reliability.internal_copy
    record: dict[str, Any] = {"data": {}, "changed_by": "U-1", "reason": "r"}
    data.extractions[("document", LEASE.id, 2)] = {**record, "field": "document_date,related_ids"}
    data.extractions[("contract", LEASE.id, 2)] = {**record, "field": "reliability"}
    assert await registered_reliability(deps, EID, original, DocumentType.minutes) is Reliability.internal_copy
    data.extractions[("document", LEASE.id, 3)] = {**record, "field": "reliability,document_date"}
    assert await registered_reliability(deps, EID, original, DocumentType.minutes) is Reliability.external_original


# ── 迁移 (验收 2 的单头) ───────────────


def test_migration_chain_has_one_head_with_document_details() -> None:
    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    [head] = script.get_heads()
    assert "0012" in {r.revision for r in script.walk_revisions("base", head)}


# ── 第 1 轮审核的修改 ───────────────


async def test_nothing_given_is_missing_field_even_after_an_edit() -> None:
    """三项都没给报 MISSING_FIELD, 不当成重试 (即使上一版正是同一人以同一理由补登记的; 审核 R90-03)."""
    _, service, _, _, _ = service_with(LEASE)
    await service.set_document_details(EID, {LEASE.id: edit(document_date="2026-09-22")}, "U-1")
    with pytest.raises(DataRejected) as nothing:
        await service.set_document_details(EID, {LEASE.id: edit()}, "U-1")
    assert nothing.value.code == "MISSING_FIELD"


async def test_rerun_after_details_uses_current_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    """补登记合同的可靠性后重跑过期的 REV-03: 提示词里的文件登记, 新判断的文件指针都是补登记后的可靠性 (以存储的
    文件登记为准, 不用编排状态里的旧副本; 审核 R90-01)."""
    from backend.contracts.models import Judgment
    from tests.test_rerun_stale import rerun

    s = await demo_at_fin_gate()
    service = DataService(s.runner.deps, s.runner)
    contract = next(d for d in await s.runner.deps.financial_store.documents(EID) if d.type is DocumentType.contract)
    assert contract.reliability is Reliability.external_copy
    await service.set_document_details(EID, {contract.id: edit(reliability="external_original")}, "U-1")
    seen: list[tuple[str, dict[str, Any]]] = []
    engine = s.runner.deps.judgments
    original = engine.call_llm

    async def spy(eid: str, template: Any, run: Any, variables: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        seen.append((template.id, variables))
        return await original(eid, template, run, variables, *args, **kwargs)

    monkeypatch.setattr(engine, "call_llm", spy)
    await rerun(s)
    prompts = [
        v["data"]["document"] for pid, v in seen if pid == "REV-03" and v["data"]["document"]["id"] == contract.id
    ]
    assert prompts and {d["reliability"] for d in prompts} == {"external_original"}
    rerun_judgments = [
        j for j in s.output_store.rows if isinstance(j, Judgment) and j.procedure_id == "REV-03" and "-2-" in j.id
    ]
    pointers = [p for j in rerun_judgments for p in j.evidence if p.source_id == contract.id]
    assert pointers and {p.reliability for p in pointers} == {Reliability.external_original}


async def test_csh02_rerun_takes_reply_reliability_from_registration() -> None:
    """补登记回函的可靠性后重跑过期的 CSH-02: 新 D01 例外与新判断里回函页的指针都按回函当前的登记 (经
    judgment_engine.document_pointer), 不沿用 DOC-06 当时写进函证的值 (审核 R90-01)."""
    from backend.contracts.models import AuditException, Judgment
    from tests.test_rerun_stale import rerun

    s = await demo_at_fin_gate()
    service = DataService(s.runner.deps, s.runner)
    replies = [d for d in await s.runner.deps.financial_store.documents(EID) if d.type is DocumentType.confirmation]
    assert replies and {d.reliability for d in replies} == {Reliability.external_original}
    await service.set_document_details(EID, {d.id: edit(reliability="external_copy") for d in replies}, "U-1")
    await rerun(s)
    ids = {d.id for d in replies}
    rows = [r for r in s.output_store.rows if getattr(r, "procedure_id", None) == "CSH-02" and "-2-" in r.id]
    d01 = [r for r in rows if isinstance(r, AuditException) and r.rule_id == "D01"]
    judged = [r for r in rows if isinstance(r, Judgment)]
    assert d01 and judged
    for row in [*d01, *judged]:
        reply_pointers = [p for p in row.evidence if p.source_id in ids]
        assert reply_pointers and {p.reliability for p in reply_pointers} == {Reliability.external_copy}, row.id


async def test_request_upload_keeps_registered_reliability_and_records_the_type() -> None:
    """补登记过可靠性的文件再对着类型不同的补交请求重传 (按请求定类型, DocumentIntake.mark): 类型按请求改, 可靠性
    保留; 文件登记的版本随之再记一版 (field type), history 的最新一版即当前登记 (审核 R90-02, R90-05)."""
    import asyncio

    from tests.test_data_checkup import CONTRACT_REQUEST, service_for

    data = FakeDataStore(ready=False)
    _runner, service, financial = service_for(data, contract_wanted=True)
    async with service.holding(EID, []):
        await service.checkup(EID)  # 提出合同请求
    [document] = await service.upload_documents(
        EID, [("minutes.pdf", b"%PDF minutes")], actor="U-1", request_id=CONTRACT_REQUEST
    )
    await asyncio.gather(*list(service.tasks))
    await service.set_document_type(EID, document.id, DocumentType.minutes, "其实是董事会纪要", "U-1")
    await asyncio.gather(*list(service.tasks))
    assert await data.extraction_versions(EID, "document", document.id) == []  # 没补登记过: 改类型不记版本
    await service.set_document_details(EID, {document.id: edit(reliability="internal_original")}, "U-1")

    await service.upload_documents(EID, [("minutes.pdf", b"%PDF minutes")], actor="U-1", request_id=CONTRACT_REQUEST)
    await asyncio.gather(*list(service.tasks))

    found = next(d for d in financial.document_refs if d.id == document.id)
    assert (found.type, found.reliability) == (DocumentType.contract, Reliability.internal_original)
    history = await service.document_history(EID, document.id)
    assert [(r.version, r.field) for r in history] == [(1, None), (2, "reliability"), (3, "type")]
    assert history[-1].data == {
        "type": "contract",
        "reliability": "internal_original",
        "document_date": None,
        "related_ids": [],
    }


async def test_set_type_after_details_writes_a_registration_version() -> None:
    """补登记之后再指定类型: 文件登记再记一版 (field 为变了的 type, 可靠性变了一并记), 写 data 留痕, history 的最新
    一版与当前登记一致; 只补登记过日期的, 可靠性随新类型的缺省变, field 为 type,reliability (审核 R90-05)."""
    runner, service, financial, _, data = service_with(LEASE, MEMO)
    await service.set_document_details(EID, {LEASE.id: edit(reliability="internal_original")}, "U-1")
    await service.set_document_details(EID, {MEMO.id: edit(document_date="2026-10-16")}, "U-1")
    await service.set_document_type(EID, LEASE.id, DocumentType.minutes, "其实是纪要", "U-2")
    await drain(service)
    await service.set_document_type(EID, MEMO.id, DocumentType.agreement, "其实是协议", "U-2")  # 缺省 external_copy
    await drain(service)

    for document_id, field in ((LEASE.id, "type"), (MEMO.id, "type,reliability")):
        [*_, last] = await service.document_history(EID, document_id)
        found = current(financial, document_id)
        assert (last.version, last.field, last.changed_by) == (3, field, "U-2")
        assert last.data == found.model_dump(
            mode="json", include={"type", "reliability", "document_date", "related_ids"}
        )
    trail = runner.deps.trail
    assert isinstance(trail, FakeTrail)
    keys = [e.idempotency_key for e in trail.entries if e.payload.get("action") == "edit"]
    assert f"{EID}:data:edit:document:{LEASE.id}:v3" in keys and f"{EID}:data:edit:document:{MEMO.id}:v3" in keys
    before = data.data_version
    await service.set_document_type(EID, LEASE.id, DocumentType.minutes, "再确认一次", "U-2")
    await drain(service)
    assert len(await service.document_history(EID, LEASE.id)) == 3 and data.data_version == before  # 类型没变不记
