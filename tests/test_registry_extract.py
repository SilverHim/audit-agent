"""需求 R290: ASIC 摘录上传, A3 抽取 (DOC-REGISTRY) 与修改; 新建的客户的 ACC-04 用摘录核对公司已注册, 名称与董事;
是公司而还没有摘录时 ACC-04 之前等摘录 (p0_inputs).

新客户用 R64 补造的合成摘录 900043030 (Victoria Property Agency Pty Ltd, 董事 Dana Morgan 与 Alex Morgan; ABN
27900043030 见补造目录 registry/abn.json). ABN Lookup 用假实现 (ABR 适配器本身见 test_abn_lookup), 大模型读上传固定
输出 tests/fixtures/llm_variants/upload/DOC-REGISTRY (由 R64 的 JSON 写成). 存储用假实现, 末尾一个连库用例.
"""

import asyncio
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from backend.adapters.registry import CompositeRegistrySource, UploadedExtracts
from backend.adapters.registry_mock import DatasetRegistrySource
from backend.agents.client_intel import Acc04Findings, ClientIntelAgent
from backend.agents.ingest import REGISTRY, DocumentIntake, Requested
from backend.contracts.data import REGISTRY_TARGET, DataInput, DocumentChange, ExtractionEdit, ImportBatch
from backend.contracts.errors import DataRejected, RegistryReason, RegistryUnavailable
from backend.contracts.llm_outputs import RegistryExtraction
from backend.contracts.models import (
    AbnRecord,
    AcceptanceScorecard,
    AuditException,
    DocumentRef,
    DocumentType,
    Engagement,
    Procedure,
    ProcedureRun,
    ProcedureStatus,
    Reliability,
    TrailKind,
)
from backend.contracts.services import ValidationContext
from backend.contracts.state import initial_state
from backend.orchestration.runner import GraphRunner, awaiting_inputs, fixed_procedures
from backend.services.data_service import DataService
from backend.services.extraction_edits import apply_registry_edit
from backend.services.judgment_engine import OutputValidator, check_extraction
from tests.external import require_xero_supplements
from tests.fakes import (
    ENGAGEMENT,
    NOW,
    NUMBAT_AUDITOR,
    NUMBAT_PROFILE,
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
    make_graph_nodes,
    make_runner,
    wait_idle,
)

EID = ENGAGEMENT.id
ABN = "27900043030"
ACN = "900043030"
DOCUMENT = "DOC-d7305364b2ae"  # 900043030.pdf 的文件 ID (sha256 前 12 位, 同 manifest)
REQUEST = "REQ-registry_extract"
FIXTURE = UPLOAD_FIXTURES / "DOC-REGISTRY" / f"{ACN}.json"
VICTORIA = AbnRecord(
    abn=ABN,
    acn=ACN,
    entity_name="Victoria Property Agency Pty Ltd",
    status="Active",
    entity_type="Australian Private Company",
)
PROFILE = NUMBAT_PROFILE.model_copy(
    update={
        "id": "CLIENT-M-0123456789ab",
        "legal_name": "Victoria Property Agency Pty Ltd",
        "abn": ABN,
        "directors": ["Dana Morgan", "Alex Morgan"],
    }
)
NEW_CLIENT = ENGAGEMENT.model_copy(update={"dataset": None, "client_id": PROFILE.id})
BASE = f"/api/v1/engagements/{EID}"


def extract_pdf() -> bytes:
    """R64 补造的合成摘录 900043030.pdf (2 页)."""
    return (require_xero_supplements("20261017-q1") / "registry" / "extracts" / f"{ACN}.pdf").read_bytes()


def fixture_extraction(**changes: Any) -> RegistryExtraction:
    """DOC-REGISTRY 对 900043030.pdf 的固定输出; changes 覆盖字段."""
    return RegistryExtraction.model_validate_json(FIXTURE.read_text(encoding="utf-8")).model_copy(update=changes)


class StaticAbn:
    """ABN Lookup 的假实现: 按 ABN 返回预置的登记记录, 记下每次查询; 给了 reason 时一律查不了."""

    def __init__(self, records: dict[str, AbnRecord], reason: RegistryReason | None = None) -> None:
        self.records = records
        self.reason = reason
        self.queries: list[str] = []

    async def abn(self, abn: str) -> AbnRecord | None:
        self.queries.append(abn)
        if self.reason is not None:
            raise RegistryUnavailable("假 ABN Lookup 查不了", reason=self.reason)
        return self.records.get(abn)


def composite(store: FakeEngagementStore, abn: StaticAbn, financial: FakeFinancialStore, data: FakeDataStore) -> Any:
    """组装根的组合登记来源: 数据集一侧不该被查 (新客户没有数据集), 上传摘录一侧读给定的存储. 证据存储由调用方在
    依赖建好后接上 (registry.extracts.evidence)."""
    return CompositeRegistrySource(
        store,
        DatasetRegistrySource(store, lambda dataset: None),
        abn,
        UploadedExtracts(financial, data, FakeEvidenceStore(financial)),
    )


@dataclass
class NewClient:
    """新建的客户 Victoria Property Agency 的项目: runner, 数据服务, 存储与假 ABN Lookup."""

    runner: GraphRunner
    service: DataService
    financial: FakeFinancialStore
    data: FakeDataStore
    abn: StaticAbn

    async def acc04(self) -> Procedure:
        [procedure] = await self.runner.deps.output_store.latest(EID, Procedure, id="ACC-04")
        return procedure

    async def request(self) -> tuple[str, list[str], str]:
        found = next(r for r in await self.data.requests(EID) if r.id == REQUEST)
        return found.title, found.used_for, found.status

    def external_calls(self) -> list[Any]:
        trail = self.runner.deps.trail
        assert isinstance(trail, FakeTrail)
        return [e for e in trail.entries if e.kind is TrailKind.external_call]


async def started_new_client() -> NewClient:
    """新客户的项目启动, 等后台停下. 大模型读上传固定输出 (分类与摘录抽取), 主固定输出兜底."""
    financial, data = FakeFinancialStore(), FakeDataStore(ready=False)
    store = FakeEngagementStore([PROFILE], [NEW_CLIENT])
    abn = StaticAbn({ABN: VICTORIA})
    registry = composite(store, abn, financial, data)
    llm = LayeredMockLLM(UPLOAD_FIXTURES, UPLOAD_FIXTURES.parents[1] / "llm")
    runner, _ = make_runner(registry, store, financial_store=financial, data=data, llm=llm)
    registry.extracts.evidence = runner.deps.evidence_store
    financial.document_refs.clear()  # 建图辅助按种子登记的 Numbat 合同不要
    await runner.start(EID)
    await wait_idle(runner, EID)
    return NewClient(runner, DataService(runner.deps, runner), financial, data, abn)


# ── 新客户等摘录, 上传后接着跑 (验收 1, 2, 3) ──


@pytest.mark.external
async def test_new_company_waits_for_the_extract_then_checks_it() -> None:
    """是公司 (ABN 登记有 ACN) 而没有摘录: ACC-04 之前查一次 ABN, ACC-04 记等待数据, 不执行, A3 提出补交请求 "ASIC
    摘录". 按请求上传 R64 的合成摘录: A3 抽取 (第 1 版, 记数据变动), 请求转 received, 图自动继续, ACC-04 四项都按摘录
    通过, 公司两项的指针指向摘录文件; 共查 ABN 两次 (p0_inputs 与 ACC-04 各一次), 各一条留痕. 之后在 ACC-01 闸门处
    经接口修改抽取结果: 出第 2 版, 写留痕, 记数据变动, 历史两版."""
    s = await started_new_client()

    snapshot = await s.runner.app.aget_state(GraphRunner.config(EID))
    assert snapshot.next == ("p0_inputs",) and awaiting_inputs(snapshot)
    waiting = await s.acc04()
    assert (waiting.status, waiting.data_request_ids) == (ProcedureStatus.awaiting_data, [REQUEST])
    assert await s.runner.deps.output_store.latest(EID, ProcedureRun) == []  # 等数据不建执行记录
    assert await s.request() == ("ASIC 摘录", ["ACC-04"], "open")
    assert s.abn.queries == [ABN]

    await s.service.upload_documents(EID, [(f"{ACN}.pdf", extract_pdf())], actor=NUMBAT_AUDITOR.id, request_id=REQUEST)
    await asyncio.gather(*list(s.service.tasks))
    await wait_idle(s.runner, EID)

    [document] = await s.financial.documents(EID, DocumentType.registry_extract)
    assert (document.id, document.status, document.reliability) == (DOCUMENT, "extracted", Reliability.external_copy)
    [first] = await s.data.extraction_versions(EID, REGISTRY_TARGET, DOCUMENT)
    assert (first.version, first.changed_by, first.field) == (1, "A3", None)
    assert RegistryExtraction.model_validate(first.data) == fixture_extraction()
    assert await s.request() == ("ASIC 摘录", ["ACC-04"], "received")
    assert (await s.acc04()).status is ProcedureStatus.completed
    assert (await s.runner.app.aget_state(GraphRunner.config(EID))).next == ("gate_acc01",)
    [card] = await s.runner.deps.output_store.latest(EID, AcceptanceScorecard)
    assert [(c.check, c.passed) for c in card.registry_checks] == [
        ("abn_active", True),
        ("name_match", True),
        ("company_registered", True),
        ("directors_match", True),
    ]
    pointers = [c.evidence for c in card.registry_checks[2:]]
    assert [(p.source_type, p.source_id, p.locator, p.page, p.reliability) for p in pointers] == [
        ("document", DOCUMENT, "status", 1, Reliability.external_copy),
        ("document", DOCUMENT, "officeholders", 1, Reliability.external_copy),
    ]
    assert s.abn.queries == [ABN, ABN]
    precheck, acc04 = s.external_calls()
    assert (precheck.run_id, acc04.run_id) == (None, "RUN-ACC-04-1")
    assert precheck.payload == acc04.payload == {"abn": ABN, "result": VICTORIA.model_dump(mode="json")}
    assert DataInput.registry_extracts in {c.input for c in await s.data.changes(EID)}

    edit = {"field": "officeholders[1].name", "value": "Alexander Morgan", "reason": "摘录第 1 页写的是全名"}
    async with api_client(s.runner) as client:
        same = await client.put(f"{BASE}/registry-extracts/{DOCUMENT}", json={**edit, "value": "Alex Morgan"})
        missing = await client.put(f"{BASE}/registry-extracts/DOC-000000000000", json=edit)
        changed = await client.put(f"{BASE}/registry-extracts/{DOCUMENT}", json=edit)
        listed = (await client.get(f"{BASE}/registry-extracts")).json()
        history = (await client.get(f"{BASE}/registry-extracts/{DOCUMENT}/history")).json()
    assert (same.status_code, same.json()["detail"]["code"]) == (422, "INVALID_VALUE")
    assert missing.status_code == 404
    assert changed.status_code == 200, changed.text
    assert [(v["version"], v["field"], v["changed_by"]) for v in history] == [
        (1, None, "A3"),
        (2, "officeholders[1].name", NUMBAT_AUDITOR.id),
    ]
    assert [(v["version"], v["extraction"]) for v in listed] == [(2, changed.json()["extraction"])]
    assert listed[0]["extraction"]["officeholders"][1]["name"] == "Alexander Morgan"
    trail = s.runner.deps.trail
    assert isinstance(trail, FakeTrail)
    [edited] = [e for e in trail.entries if e.kind is TrailKind.data and e.payload.get("action") == "edit"]
    assert (edited.actor, edited.payload["document_type"], edited.payload["document_id"]) == (
        NUMBAT_AUDITOR.id,
        "registry_extract",
        DOCUMENT,
    )
    assert [c.input for c in await s.data.changes(EID)].count(DataInput.registry_extracts) == 2


async def test_new_company_without_an_extract_runs_acc04_with_manual_checks() -> None:
    """验收 3: 审计师在补交卡片上标客户无此类资料 (写理由): 图自动继续, ACC-04 照常执行 (不记 no_data, ACC-01 闸门要
    评分卡), 公司两项待人工确认, 不出例外, 摘录一项记一条 "没有查询" 的留痕 (no_extract); ABN 共查两次."""
    s = await started_new_client()

    await s.service.mark_unavailable(EID, REQUEST, "新设公司, 尚未向 ASIC 购买摘录", NUMBAT_AUDITOR.id)
    await wait_idle(s.runner, EID)

    assert (await s.acc04()).status is ProcedureStatus.completed
    [card] = await s.runner.deps.output_store.latest(EID, AcceptanceScorecard)
    assert [c.passed for c in card.registry_checks] == [True, True, None, None]
    assert "marked it unavailable" in card.registry_checks[2].detail
    assert await s.runner.deps.output_store.latest(EID, AuditException) == []
    assert s.abn.queries == [ABN, ABN]
    assert s.external_calls()[-1].payload == {"acn": ACN, "available": False, "reason": "no_extract", "result": None}
    assert (await s.runner.app.aget_state(GraphRunner.config(EID))).next == ("gate_acc01",)


# ── p0_inputs 不等的情形 ──


@pytest.mark.parametrize(
    ("dataset", "records", "reason", "documents", "queries"),
    [
        ("demo", {ABN: VICTORIA}, None, [], 0),  # 有数据集: 登记走数据集的 mock, 不查 ABN
        (None, {ABN: VICTORIA.model_copy(update={"acn": None})}, None, [], 1),  # 不是公司 (没有 ACN)
        (None, {}, None, [], 1),  # ABN 查不到
        (None, {ABN: VICTORIA}, "unreachable", [], 1),  # ABN 查不了: ACC-04 执行时同样待人工确认
        (None, {ABN: VICTORIA}, None, [DocumentType.registry_extract], 1),  # 已有摘录
    ],
    ids=["dataset", "not_company", "abn_not_found", "abn_unreachable", "extract_on_hand"],
)
async def test_acc04_does_not_wait_when_no_extract_is_needed(
    dataset: str | None,
    records: dict[str, AbnRecord],
    reason: RegistryReason | None,
    documents: list[DocumentType],
    queries: int,
) -> None:
    """p0_inputs 不改 ACC-04 (仍 planned, 不写 Store), 不等待, 往下到 p0_intel; 查 ABN 的次数见参数, 查了就留痕."""
    financial, data = FakeFinancialStore(), FakeDataStore()
    engagement = NEW_CLIENT.model_copy(update={"dataset": dataset})
    store = FakeEngagementStore([PROFILE], [engagement])
    abn = StaticAbn(records, reason)
    trail = FakeTrail()
    deps = make_deps(composite(store, abn, financial, data), trail, store, financial_store=financial, data=data)
    financial.document_refs.clear()
    evidence = deps.evidence_store
    for kind in documents:  # 有一份该类型的文件即算有 (同 R272 的口径)
        await evidence.save(EID, f"{kind.value}.pdf", b"%PDF", type=kind, reliability=Reliability.external_copy)
    nodes = make_graph_nodes(deps)
    state = initial_state(engagement, fixed_procedures(deps.library), PROFILE.version)

    assert await nodes.p0_inputs(state) == {}
    assert len(abn.queries) == queries
    assert len([e for e in trail.entries if e.kind is TrailKind.external_call]) == queries
    assert await deps.output_store.latest(EID, Procedure) == []


# ── ACC-04 按摘录核对 (验收 2) ──


@pytest.mark.parametrize(
    ("changes", "profile", "passed", "detail"),
    [
        ({}, {}, [True, True], None),
        # 秘书不比, 姓名规范化后比 (大小写, 空白)
        (
            {
                "officeholders": [
                    *fixture_extraction().officeholders,
                    fixture_extraction()
                    .officeholders[0]
                    .model_copy(update={"name": "Sam Whitfield", "role": "secretary"}),
                ]
            },
            {"directors": ["DANA MORGAN", "alex  morgan"]},
            [True, True],
            None,
        ),
        ({}, {"directors": ["Dana Morgan", "Alex Morgan", "Sam Whitfield"]}, [True, False], "not on the extract: Sam"),
        ({}, {"directors": ["Dana Morgan"]}, [True, False], "on the extract but not declared: Alex Morgan"),
        ({"name": "Victoria Property Holdings Pty Ltd"}, {}, [False, True], "name Victoria Property Holdings"),
        ({"status": "Deregistered"}, {}, [False, True], "status Deregistered"),
        ({"acn": "900043049"}, {}, [False, None], f"No uploaded ASIC extract for ACN {ACN}"),
    ],
    ids=["match", "secretary_and_spelling", "director_missing", "director_undeclared", "name", "deregistered", "acn"],
)
async def test_acc04_checks_the_company_against_the_extract(
    changes: dict[str, Any], profile: dict[str, Any], passed: list[bool | None], detail: str | None
) -> None:
    """新客户的 company_registered (状态, 名称) 与 directors_match (画像董事对摘录董事) 按上传摘录的最新一版判; 不通过
    的各出一条例外 (rule_id 为检查名); 摘录的 ACN 对不上时 company_registered 不通过, 董事待人工确认. 按摘录判的两项
    指针指向摘录文件, 可靠性取文件现在的登记 (这里审计师补登记成了 external_original, 抽取时的指针是 external_copy)."""
    financial, data = FakeFinancialStore(), FakeDataStore()
    store = FakeEngagementStore([PROFILE], [NEW_CLIENT])
    registry = composite(store, StaticAbn({ABN: VICTORIA}), financial, data)
    deps = make_deps(registry, FakeTrail(), store, financial_store=financial, data=data)
    evidence = deps.evidence_store
    assert isinstance(evidence, FakeEvidenceStore)
    registry.extracts.evidence = evidence
    evidence.ids[f"{ACN}.pdf"] = DOCUMENT  # 与固定输出的指针同一文件 ID
    document = await evidence.save(
        EID,
        f"{ACN}.pdf",
        b"%PDF registry extract",
        type=DocumentType.registry_extract,
        reliability=Reliability.external_original,
    )
    extraction = fixture_extraction(**changes)
    await data.save_extraction(
        EID, REGISTRY_TARGET, document.id, 1, extraction.model_dump(mode="json"), changed_by="A3"
    )
    agent = ClientIntelAgent(deps)
    ctx = make_ctx(deps, engagement=NEW_CLIENT)
    declared = PROFILE.model_copy(update=profile)
    found = await agent.collect_checks(ctx, declared)

    assert [c.passed for c in found[2:]] == passed
    if detail is not None:
        assert detail in next(c.detail for c in found[2:] if c.passed is not True)
    on_extract = [
        (c.evidence.source_id, c.evidence.reliability) for c in found[2:] if c.evidence.source_type == "document"
    ]
    assert on_extract == ([] if "acn" in changes else [(DOCUMENT, Reliability.external_original)] * 2)
    patch = await agent.interpret(ctx, Acc04Findings(profile=declared, checks=found))
    assert [x.rule_id for x in patch["exceptions"]] == [c.check for c in found if c.passed is False]


async def test_reuploaded_extract_of_the_same_company_wins() -> None:
    """同一 ACN 有几份摘录时取最后上传的 (重新购买的新摘录): 按上传时间, 不按文件 ID."""
    financial, data = FakeFinancialStore(), FakeDataStore()
    evidence = FakeEvidenceStore(financial)
    for document_id, status, age in (("DOC-zzzzzzzzzzzz", "Deregistered", 1), ("DOC-aaaaaaaaaaaa", "Registered", 0)):
        evidence.ids[f"{document_id}.pdf"] = document_id
        saved = await evidence.save(
            EID,
            f"{document_id}.pdf",
            document_id.encode(),
            type=DocumentType.registry_extract,
            reliability=Reliability.external_copy,
        )
        refs = financial.document_refs
        refs[refs.index(saved)] = saved.model_copy(update={"uploaded_at": NOW - timedelta(days=age)})
        extraction = fixture_extraction(status=status).model_dump(mode="json")
        await data.save_extraction(EID, REGISTRY_TARGET, document_id, 1, extraction, changed_by="A3")

    found = await UploadedExtracts(financial, data, evidence).extract(EID, ACN)
    assert found is not None and (found.document_id, found.status) == ("DOC-aaaaaaaaaaaa", "Registered")


# ── 抽取的校验, 提示词与修改 ──


@pytest.mark.parametrize(
    ("drop", "page", "error"),
    [
        (None, None, None),
        ("officeholders", None, "摘录的 officeholders 没有 locator 为 officeholders 的指针"),
        ("acn", None, "摘录的 acn 没有 locator 为 acn 的指针"),
        (None, 3, "页码 3 不在 1-2 之间"),
    ],
    ids=["ok", "no_officeholders_pointer", "no_acn_pointer", "page_out_of_range"],
)
async def test_extraction_needs_pointers_to_this_document(
    drop: str | None, page: int | None, error: str | None
) -> None:
    """引用级: acn, name, status, officeholders 各须有该字段的本文档指针, 页码在页数内; 通过时指针的可靠性改写为文件
    登记的."""
    extraction = fixture_extraction()
    evidence = [p for p in extraction.evidence if p.locator != drop]
    if page is not None:
        evidence[0] = evidence[0].model_copy(update={"page": page})
    document = DocumentRef(
        id=DOCUMENT,
        type=DocumentType.registry_extract,
        file_name=f"{ACN}.pdf",
        sha256="0" * 64,
        page_count=2,
        reliability=Reliability.external_original,
        status="classified",
        uploaded_at=datetime(2026, 10, 19, tzinfo=UTC),
    )
    deps = make_deps(FakeRegistry(), FakeTrail())
    validator = OutputValidator(deps.financial_store, deps.library, deps.trail)
    output = extraction.model_copy(update={"evidence": evidence})
    result = await check_extraction(validator, EID, output, ValidationContext(document=document))
    if error is None:
        assert result.level is None and {p.reliability for p in result.output.evidence} == {
            Reliability.external_original
        }
    else:
        assert result.level == "reference" and any(error in e for e in result.errors)


# DOC-REGISTRY 提示词的口径: 输出一节里须有一行同时含这几个词 (不分大小写), 不比逐字原文
REGISTRY_RULES = {
    "ACN 为 9 位不带空格": ("`acn`", "9 digits", "without spaces"),
    "没写 ABN 为 null": ("`abn`", "null", "does not state"),
    "只取现任": ("current details only", "former"),
    "取代码不取说明": ("code", "not its description"),
    "两职各一项": ("both roles", "twice"),
    "持股比例为小数": ("`pct`", "fraction"),
    "四个字段必须有指针": ("`acn`", "`name`", "`status`", "`officeholders`", "always need"),
    "原文当数据": ("data", "ignore", "instruction"),
}


def test_registry_prompt_states_how_to_read_the_extract() -> None:
    """DOC-REGISTRY 提示词渲染一次 (页面文字取两页的占位), 按行查各条口径的关键词."""
    document = {"id": DOCUMENT, "file_name": f"{ACN}.pdf", "page_count": 2, "reliability": "external_copy"}
    rendered = make_deps(FakeRegistry(), FakeTrail()).judgments.render(
        REGISTRY, {"document": document, "pages": ["page one", "page two"]}
    )
    lines = [line.casefold() for line in rendered.splitlines()]
    missing = [rule for rule, words in REGISTRY_RULES.items() if not any(all(w in x for w in words) for x in lines)]
    assert missing == []
    assert '<page number="2">' in rendered and DOCUMENT in rendered


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("name", " Victoria Property Agency Pty Limited ", ("name", "Victoria Property Agency Pty Limited")),
        ("acn", "900 043 031", ("acn", "900043031")),
        ("abn", "27 900 043 030", ("abn", ABN)),
        (
            "registered_address",
            "1 Example St, Sydney NSW 2000",
            ("registered_address", "1 Example St, Sydney NSW 2000"),
        ),
        ("officeholders[1].role", "secretary", ("officeholders", "secretary")),
        ("officeholders[0].appointed", "2014-03-04", ("officeholders", date(2014, 3, 4))),
        ("officeholders[2].name", "Sam Whitfield", "没有第 2 名高管"),
        ("officeholders[0].role", "chair", "须为 director, secretary 之一"),
        ("acn", "12345", "须为 9 位数字"),
        ("status", "Registered", "与当前值相同"),
        ("shareholders[0].name", "X", "不能改 shareholders[0].name"),
    ],
)
def test_registry_edits(field: str, value: Any, expected: tuple[str, Any] | str) -> None:
    """可改登记字段与高管的姓名, 职务, 任职日 (下标从 0 起, 不增删); 股东只读; 值不合法, 越界或与当前相同拒绝."""
    extraction = fixture_extraction()
    if isinstance(expected, str):
        with pytest.raises(DataRejected, match=re.escape(expected)):
            apply_registry_edit(extraction, field, value)
        return
    updated = apply_registry_edit(extraction, field, value)
    name, new = expected
    if name == "officeholders":
        index, part = int(field[14]), field.split(".")[1]
        assert getattr(updated.officeholders[index], part) == new
    else:
        assert getattr(updated, name) == new
    assert updated.evidence == extraction.evidence  # 指针不变, 仍指向原文


async def test_staged_import_extracts_the_registry_and_commit_writes_the_next_version() -> None:
    """智能导入里的 ASIC 摘录 (需求 R277 的批次): 识别时按定下的类型抽取 (不写入), 确认时接在已有版本之后写一版,
    记数据变动 registry_extracts."""
    financial, data = FakeFinancialStore(), FakeDataStore()
    llm = LayeredMockLLM(UPLOAD_FIXTURES)
    deps = make_deps(FakeRegistry(), FakeTrail(), financial_store=financial, data=data, llm=llm)
    data.financial = financial
    evidence = deps.evidence_store
    assert isinstance(evidence, FakeEvidenceStore)
    evidence.ids[f"{ACN}.pdf"] = DOCUMENT
    document = await evidence.save(
        EID, f"{ACN}.pdf", b"%PDF registry extract", type=DocumentType.other, reliability=Reliability.internal_copy
    )
    await data.save_extraction(
        EID, REGISTRY_TARGET, DOCUMENT, 1, fixture_extraction().model_dump(mode="json"), changed_by="A3"
    )
    intake = DocumentIntake(deps)
    staged = await intake.stage(EID, document, ENGAGEMENT, Requested(REQUEST, DocumentType.registry_extract))
    change = await intake.staged_change(EID, document, staged)
    assert (change.type, change.status, change.registry_extract) == (
        DocumentType.registry_extract,
        "extracted",
        fixture_extraction().model_dump(mode="json"),
    )
    batch = ImportBatch(id="BAT-1", status="committed", created_by="U-1", created_at=datetime.now(UTC), files=[])
    data.import_batches[batch.id] = batch.model_copy(update={"status": "ready"})
    before = data.data_version
    await data.commit_batch(EID, batch, {}, actor="U-1", origins={}, documents=[change])
    assert [r.version for r in await data.extraction_versions(EID, REGISTRY_TARGET, DOCUMENT)] == [1, 2]
    assert data.data_version == before + 1
    assert data.data_changes[-1].input is DataInput.registry_extracts


@pytest.mark.parametrize(
    ("since", "versions"),
    [
        (None, [1, 2]),  # 新的作业 (重新上传, 改类型): 接在审计师那一版之后, 不覆盖
        (NOW - timedelta(days=1), [1]),  # 补做: 已有作业开始之后的一版, 不拿旧作业覆盖
        (NOW + timedelta(days=1), [1, 2]),  # 补做: 现有的版本都早于作业开始, 照常抽
    ],
    ids=["new_job", "resume_after_newer_version", "resume_before_any"],
)
async def test_reextraction_appends_after_the_existing_versions(since: datetime | None, versions: list[int]) -> None:
    """A3 重抽 ASIC 摘录 (DocumentIntake.registry): 版本号接在已有的最大版本之后, 记 A3, 同一事务内数据版本 +1;
    补做时已有作业开始之后的版本的不重抽 (假存储的版本时间都是 NOW)."""
    financial, data = FakeFinancialStore(), FakeDataStore()
    deps = make_deps(
        FakeRegistry(), FakeTrail(), financial_store=financial, data=data, llm=LayeredMockLLM(UPLOAD_FIXTURES)
    )
    evidence = deps.evidence_store
    assert isinstance(evidence, FakeEvidenceStore)
    evidence.ids[f"{ACN}.pdf"] = DOCUMENT
    document = await evidence.save(
        EID,
        f"{ACN}.pdf",
        b"%PDF registry extract",
        type=DocumentType.registry_extract,
        reliability=Reliability.external_copy,
    )
    edited = fixture_extraction(name="Victoria Property Agency Pty Limited").model_dump(mode="json")
    await data.save_extraction(
        EID, REGISTRY_TARGET, DOCUMENT, 1, edited, changed_by=NUMBAT_AUDITOR.id, field="name", reason="核对"
    )
    before = data.data_version

    assert await DocumentIntake(deps).registry(EID, document, since) is True
    records = await data.extraction_versions(EID, REGISTRY_TARGET, DOCUMENT)
    assert [(r.version, r.changed_by) for r in records] == [(1, NUMBAT_AUDITOR.id), *[(2, "A3")][: len(versions) - 1]]
    assert data.data_version == before + len(versions) - 1
    if len(versions) == 2:
        assert RegistryExtraction.model_validate(records[-1].data) == fixture_extraction()


# ── 连库 ──


@pytest.mark.slow
@pytest.mark.external
async def test_registry_extract_on_real_store(tmp_path: Path) -> None:
    """真实存储: 上传的摘录按请求定类型并抽取 (第 1 版, 数据变动 registry_extracts), UploadedExtracts 读回带原件与
    指针 (可靠性取文件登记), 其他 ACN 为 None; 修改出第 2 版; 智能导入确认时接在已有版本之后写第 3 版."""
    from dataclasses import replace

    from sqlalchemy import insert

    from backend.core.config import Settings
    from backend.main import build_deps
    from backend.persistence import models as m
    from backend.persistence.stores import PostgresEngagementStore, now
    from backend.services.judgment_engine import JudgmentEngine

    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("需要 TEST_DATABASE_URL")
    deps = build_deps(Settings(DATABASE_URL=url, EVIDENCE_ROOT=tmp_path, LLM_MODE="mock"))
    llm = LayeredMockLLM(UPLOAD_FIXTURES)
    judgments = JudgmentEngine(
        llm, OutputValidator(deps.financial_store, deps.library, deps.trail), deps.trail, deps.library
    )
    deps = replace(deps, llm=llm, judgments=judgments)
    engine = cast(Any, deps.financial_store).engine
    user = "USR-" + uuid4().hex
    async with engine.begin() as conn:
        await conn.execute(
            insert(m.users).values(
                id=user, email=user + "@example.com", full_name="R290", password_hash="x", created_at=now()
            )
        )
    profile = PROFILE.model_copy(update={"id": "CLIENT-M-" + uuid4().hex[:12], "abn": ABN})
    engagement: Engagement = await PostgresEngagementStore(engine).create(
        owner_id=user, period_start=date(2025, 7, 1), period_end=date(2026, 6, 30), dataset=None, profile=profile
    )
    eid = engagement.id
    service = DataService(deps, GraphRunner(cast(Any, None), deps, {"mode": "mock", "model": "mock"}))
    data = deps.data

    [uploaded] = await service.upload_documents(eid, [(f"{ACN}.pdf", extract_pdf())], actor=user)
    await asyncio.gather(*list(service.tasks))  # 分类没有固定输出, 转人工指定类型
    await service.set_document_type(eid, uploaded.id, DocumentType.registry_extract, "ASIC 公司摘录", user)
    await asyncio.gather(*list(service.tasks))
    [document] = await deps.financial_store.documents(eid, DocumentType.registry_extract)
    assert (document.id, document.status) == (DOCUMENT, "extracted")
    [first] = await data.extraction_versions(eid, REGISTRY_TARGET, DOCUMENT)
    assert RegistryExtraction.model_validate(first.data) == fixture_extraction()
    assert DataInput.registry_extracts in {c.input for c in await data.changes(eid)}
    uploads = UploadedExtracts(deps.financial_store, data, deps.evidence_store)
    found = await uploads.extract(eid, ACN)
    assert found is not None and found.file_bytes == extract_pdf() and found.document_id == DOCUMENT
    assert {p.reliability for p in found.evidence} == {Reliability.external_copy}
    assert await uploads.extract(eid, "900043021") is None

    edit = ExtractionEdit(field="officeholders[0].name", value="Dana Jane Morgan", reason="核对第 1 页")
    await service.edit_registry_extract(eid, DOCUMENT, edit, user)
    batch = ImportBatch(id="BAT-" + uuid4().hex[:12], status="ready", created_by=user, created_at=now(), files=[])
    await data.save_batch(eid, batch)
    change = DocumentChange(
        document_id=DOCUMENT,
        type=DocumentType.registry_extract,
        reliability=Reliability.external_copy,
        status="extracted",
        registry_extract=fixture_extraction().model_dump(mode="json"),
    )
    committed = batch.model_copy(update={"status": "committed", "committed_by": user, "committed_at": now()})
    await data.commit_batch(eid, committed, {}, actor=user, origins={}, documents=[change])
    versions = await data.extraction_versions(eid, REGISTRY_TARGET, DOCUMENT)
    assert [(r.version, r.changed_by, r.field) for r in versions] == [
        (1, "A3", None),
        (2, user, "officeholders[0].name"),
        (3, "A3", None),
    ]
    assert [c.input for c in await data.changes(eid)].count(DataInput.registry_extracts) == 3
    await engine.dispose()


def test_fixtures_are_the_r64_extracts() -> None:
    """守卫: DOC-REGISTRY 的固定输出由 R64 的合成摘录 JSON 写成, 登记字段, 高管与股东逐项相同 (改了补造数据而没
    同步固定输出时报出); 文件 ID 为同名 PDF 的 sha256 前 12 位."""
    import hashlib

    root = require_xero_supplements("20261017-q1") / "registry" / "extracts"
    for path in sorted((UPLOAD_FIXTURES / "DOC-REGISTRY").glob("*.json")):
        fixture = RegistryExtraction.model_validate_json(path.read_text(encoding="utf-8"))
        source = json.loads((root / path.name).read_text(encoding="utf-8"))
        same = {k: source[k] for k in ("acn", "name", "type", "subclass", "status", "registered_date", "paid_up_cents")}
        assert fixture.model_dump(mode="json", include=set(same)) == same, path.name
        assert fixture.company_class == source["class"]
        assert [o.model_dump(mode="json") for o in fixture.officeholders] == source["officeholders"]
        assert [h.model_dump(mode="json") for h in fixture.shareholders] == source["shareholders"]
        digest = "DOC-" + hashlib.sha256((root / source["file_name"]).read_bytes()).hexdigest()[:12]
        assert {p.source_id for p in fixture.evidence} == {digest}, path.name
