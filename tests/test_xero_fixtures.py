"""Xero 演示的大模型 fixture 守卫 (R78 验收 4 (b) 至 (d)): tests/fixtures/llm_xero/<程序>/<对象>.json. 上传分类
(DOC-CLASSIFY, 需求 R267) 的 fixture 按文件名主干取, 补造的每份要上传的 PDF 一份, 另有 mock 下 Xero 项目上传的分类.

正本与运行副本 (data/xero_demo/20261017-q1/llm) 相同由 tests/test_knowledge.py 按 FIXTURE_SETS 核对. 这里按集合包含
断言, 不写死文件清单, 目录为空时通过; 各程序段加 fixture 时不必改这里. 自测用 tmp 目录造不合格的 fixture.
"""

import asyncio
import json
import re
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path, PurePath
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from backend.agents.ingest import CLASSIFY
from backend.agents.review import GEN09, VERDICT_SPEC
from backend.contracts import llm_outputs
from backend.contracts.llm_outputs import DocumentClassification
from backend.contracts.models import DocumentType, TrailKind
from backend.core.config import get_settings
from backend.services.data_service import DataService
from backend.services.judgment_engine import DOCUMENT_EVIDENCE, output_model
from backend.services.procedure_library import ProcedureLibrary
from tests.fakes import (
    ENGAGEMENT,
    NUMBAT_PROFILE,
    FakeDataStore,
    FakeEngagementStore,
    FakeFinancialStore,
    FakeTrail,
    make_runner,
)
from tests.fixture_sets import XERO, XERO_DEMO, FixtureSet
from tests.test_knowledge import document_pointers
from tests.xero_fakes import xero_uploads

LIBRARY = ProcedureLibrary(get_settings().KNOWLEDGE_ROOT)
EVIDENCE_TYPES = {t.value for t in DOCUMENT_EVIDENCE}  # 能作证据的文件类型, 判断引擎的文件指针映射为准
# 上传处理 (agents/ingest) 的模板: 不在程序库里而在 llm_xero 有 fixture, fixture_key 为上传的文件名主干. 与生成脚本的
# UPLOAD_PROGRAMS (从上传固定输出复制进运行副本的程序) 不是一回事
INTAKE_TEMPLATES = {CLASSIFY.id: CLASSIFY}
# 邮箱地址 (fixture 会进留痕与页面, 不放任何人的邮箱): 对 JSON 解码后的字符串匹配 (\u0040 转义的也查得出); 顶级域名
# 须以字母开头, "10@650.00" 这类数字不算; 不带点的域名 (如 x@localhost) 不查
EMAIL = re.compile(r"[\w.%+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z][\w-]*")


def strings(value: Any) -> Iterator[str]:
    """JSON 值里的全部字符串 (键与值), 递归."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def fixture_model(library: ProcedureLibrary, procedure_id: str, fixture_key: str) -> type[BaseModel]:
    """该 fixture 应符合的输出模型: 模板 llm.output_model (上传分类取 agents/ingest 的模板); GEN-09 的第二轮
    ({对象}-r2) 为 VERDICT_SPEC 的."""
    if procedure_id in INTAKE_TEMPLATES:
        return output_model(INTAKE_TEMPLATES[procedure_id])
    if procedure_id == GEN09 and fixture_key.endswith("-r2"):
        model = getattr(llm_outputs, VERDICT_SPEC.output_model)
        assert isinstance(model, type) and issubclass(model, BaseModel)
        return model
    return output_model(library.load(procedure_id))


def fixture_problems(fs: FixtureSet, library: ProcedureLibrary = LIBRARY) -> list[str]:
    """不合格的 fixture (只写文件, 程序与指针 ID, 不写内容):
    (b) document 指针的 source_id 须在 manifest.documents 中, 1 ≤ page ≤ page_count, 所指文件类型能作证据
    (在 judgment_engine.DOCUMENT_EVIDENCE 中; 如 financial_statements 不作证据, 判断引报表行);
    (c) 目录名须是程序库中 (含未启用的) 带 llm 段的程序或上传分类 (DOC-CLASSIFY), 文件须能按该程序的输出模型解析;
    上传分类的文件名主干 (default 除外) 须是 manifest.documents 中的文件 (按文件名取, 别的名字永远用不上);
    不含邮箱地址. fixture 根不存在或为空时没有问题."""
    problems: list[str] = []
    documents = fs.documents()
    stems = {Path(d["path"]).stem for d in documents.values()}
    programs = {t.id for t in library.all(include_disabled=True) if t.llm is not None} | INTAKE_TEMPLATES.keys()
    for folder in sorted(p for p in fs.fixtures.iterdir() if p.is_dir()) if fs.fixtures.is_dir() else []:
        if folder.name not in programs:
            problems.append(f"{folder.name}: 不是程序库中带 llm 段的程序")
            continue
        for path in sorted(folder.glob("*.json")):
            name = f"{folder.name}/{path.stem}"
            raw = path.read_text(encoding="utf-8")
            try:
                fixture_model(library, folder.name, path.stem).model_validate_json(raw)
            except ValidationError:
                problems.append(f"{name}: 不符合输出模型")
                continue
            if any(EMAIL.search(text) for text in strings(json.loads(raw))):
                problems.append(f"{name}: 含邮箱地址")
            if folder.name in INTAKE_TEMPLATES and path.stem != "default" and path.stem not in stems:
                problems.append(f"{name}: 不是 manifest.documents 中的文件")
            for pointer in document_pointers(json.loads(raw)):
                entry = documents.get(pointer["source_id"])
                if entry is None:
                    problems.append(f"{name}: {pointer['source_id']} 不在 manifest.documents 中")
                elif not isinstance(pointer.get("page"), int) or not 1 <= pointer["page"] <= entry["page_count"]:
                    problems.append(f"{name}: {pointer['source_id']} 的页码超出 1 至 {entry['page_count']}")
                elif entry["type"] not in EVIDENCE_TYPES:
                    problems.append(f"{name}: {pointer['source_id']} 指向不作证据的 {entry['type']} 类文件")
    return problems


def test_xero_fixtures_are_valid() -> None:
    """(b) 至 (d): 正本中每个文件都合格; 按集合包含核对, 目录为空时通过."""
    assert fixture_problems(XERO) == []
    present = {p.parent.name for p in XERO.fixtures.glob("*/*.json")}
    assert present <= {t.id for t in LIBRARY.all(include_disabled=True) if t.llm is not None} | INTAKE_TEMPLATES.keys()


def write(root: Path, rel: str, value: Any) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_fixture_problems_are_reported(tmp_path: Path) -> None:
    """自测: 合格的 CSH-02 与上传分类 fixture 不报; 未知程序目录, 没有 llm 段的程序, 不符合输出模型, 指针不在 manifest,
    页码超出 (含 0 与空), 指向不作证据的文件 (待审财报 fs_v1.pdf), 含邮箱, 上传分类的文件名不在 manifest 都报出,
    不抛异常."""
    documents = XERO.documents()
    reply = next(d for d in documents.values() if d["type"] == "confirmation")
    statements = next(d for d in documents.values() if d["type"] == "financial_statements")
    good = json.loads((get_settings().LLM_FIXTURES_ROOT / "CSH-02" / "BA-01.json").read_text(encoding="utf-8"))

    def with_pointer(document_id: str, page: int | None) -> dict[str, Any]:
        """Numbat 的 CSH-02 fixture, 其中每个 document 指针都改成指向 document_id 的第 page 页."""
        value: dict[str, Any] = json.loads(json.dumps(good))
        for pointer in document_pointers(value):
            pointer.update(source_id=document_id, page=page)
        return value

    root = tmp_path / "llm_xero"
    write(root, "CSH-02/OK.json", with_pointer(reply["document_id"], reply["page_count"]))
    write(root, "DOC-CLASSIFY/fs_v1.json", {"document_type": "financial_statements", "reason": "Draft statements."})
    write(root, "DOC-CLASSIFY/default.json", {"document_type": None, "reason": "Unknown."})
    write(root, "CSH-02/PRICE.json", {**with_pointer(reply["document_id"], 1), "conclusion": "16 days 10@650.00."})
    assert fixture_problems(replace(XERO, fixtures=root)) == []  # 单价里的 @ 不算邮箱
    write(root, "DOC-CLASSIFY/unknown.json", {"document_type": "invoice", "reason": "Not a demo document."})
    write(root, "DOC-CLASSIFY/fs_v2.json", {"document_type": "spreadsheet", "reason": "Not a document type."})
    write(
        root, "CSH-02/EMAIL.json", {**with_pointer(reply["document_id"], 1), "conclusion": "Sent to x.y@bank.example."}
    )
    escaped = json.dumps({**with_pointer(reply["document_id"], 1), "conclusion": "Sent to a@b.example."})
    (root / "CSH-02" / "ESCAPED.json").write_text(escaped.replace("@", "\\u0040"), encoding="utf-8")
    write(root, "X-UNKNOWN/a.json", good)
    write(root, "ACC-01/a.json", good)
    write(root, "CSH-02/BAD.json", {"conclusion": 1})
    write(root, "CSH-02/MISSING.json", with_pointer("DOC-000000000000", 1))
    write(root, "CSH-02/PAGE.json", with_pointer(reply["document_id"], reply["page_count"] + 1))
    write(root, "CSH-02/PAGE0.json", with_pointer(reply["document_id"], 0))
    write(root, "CSH-02/PAGENULL.json", with_pointer(reply["document_id"], None))  # EvidencePointer 允许 page 为空
    write(root, "CSH-02/STATEMENTS.json", with_pointer(statements["document_id"], 1))
    problems = fixture_problems(replace(XERO, fixtures=root))
    assert sorted({p.split(":")[0] for p in problems}) == [  # 每个不合格的指针各报一条, 这里按文件看
        "ACC-01",
        "CSH-02/BAD",
        "CSH-02/EMAIL",
        "CSH-02/ESCAPED",
        "CSH-02/MISSING",
        "CSH-02/PAGE",
        "CSH-02/PAGE0",
        "CSH-02/PAGENULL",
        "CSH-02/STATEMENTS",
        "DOC-CLASSIFY/fs_v2",
        "DOC-CLASSIFY/unknown",
        "X-UNKNOWN",
    ]
    assert f"CSH-02/STATEMENTS: {statements['document_id']} 指向不作证据的 financial_statements 类文件" in problems


def test_gen09_second_round_uses_verdict_model() -> None:
    """GEN-09 的 {对象}-r2 按 VERDICT_SPEC 的输出模型解析, 其余按模板的."""
    assert fixture_model(LIBRARY, "GEN-09", "BA-01-r2").__name__ == VERDICT_SPEC.output_model
    assert fixture_model(LIBRARY, "GEN-09", "BA-01-r1") is output_model(LIBRARY.load("GEN-09"))
    with pytest.raises(ValueError):
        fixture_model(LIBRARY, "ACC-01", "x")  # 没有 llm 段


# ── 上传分类 (DOC-CLASSIFY, 需求 R267): 补造的 23 份 PDF 各一份, 按文件名主干取 ──


@pytest.mark.external
def test_classify_fixtures_follow_the_manifest() -> None:
    """每份要上传的补造 PDF 各有一份分类 fixture, 类型同 manifest.documents 中同名文件的类型 (合同只指 3 份收入合同,
    租约, 借款协议与客户订单为 agreement); 理由为一句英文; default.json 判断不了 (document_type 为空)."""
    folder = XERO.fixtures / CLASSIFY.id
    fixtures = {
        p.stem: DocumentClassification.model_validate_json(p.read_text(encoding="utf-8")) for p in folder.glob("*.json")
    }
    default = fixtures.pop("default")
    expected = {PurePath(u.file_name).stem: u.document_type for u in xero_uploads()}  # 演示导入上传的 23 份
    assert len(expected) == 23 and {stem: f.document_type for stem, f in fixtures.items()} == expected
    assert all(f.reason.isascii() and f.reason.endswith(".") and ". " not in f.reason for f in fixtures.values())
    assert default.document_type is None


@pytest.mark.external
async def test_xero_upload_is_classified_by_the_runtime_copy() -> None:
    """mock 下 Xero 演示项目经 DataService.upload_documents 上传补造的发票: 读运行副本 llm/DOC-CLASSIFY 的同名
    fixture 得 invoice, classified, 分类的大模型留痕 run_id 为 UPLOAD-{文件 ID}, 理由同运行副本; 没有同名 fixture 的
    (unknown.pdf) 读 default.json, 转人工 (classify). 测试的 Numbat 固定输出里没有 DOC-CLASSIFY, 读错目录也转人工."""
    xero = ENGAGEMENT.model_copy(update={"dataset": XERO.dataset})
    financial = FakeFinancialStore()
    engagements = FakeEngagementStore([NUMBAT_PROFILE], [xero])
    runner, _ = make_runner(engagement_store=engagements, data=FakeDataStore(ready=False), financial_store=financial)
    financial.document_refs.clear()
    service = DataService(runner.deps, runner)
    entry = next(u for u in xero_uploads("fieldwork") if u.file_name == "smart_agency_invoice_SM0195-70135.pdf")
    pdf = XERO_DEMO / entry.path
    await service.upload_documents(
        xero.id, [(pdf.name, pdf.read_bytes()), ("unknown.pdf", b"%PDF-1.4 not a demo document")], actor="U-1"
    )
    await asyncio.gather(*list(service.tasks))
    refs = {d.file_name: d for d in financial.document_refs}
    invoice, unknown = refs[pdf.name], refs["unknown.pdf"]
    assert (invoice.id, invoice.type, invoice.status) == ("DOC-0db690a7c05e", DocumentType.invoice, "classified")
    assert (unknown.status, unknown.failed_step) == ("needs_manual", "classify")
    trail = runner.deps.trail
    assert isinstance(trail, FakeTrail)
    calls = [e.run_id for e in trail.entries if e.kind is TrailKind.llm_call and e.procedure_id == CLASSIFY.id]
    assert sorted(calls) == sorted(["UPLOAD-DOC-0db690a7c05e", f"UPLOAD-{unknown.id}"])
    runtime = json.loads((XERO.runtime / CLASSIFY.id / f"{pdf.stem}.json").read_text(encoding="utf-8"))
    [classified] = [
        e.payload
        for e in trail.entries
        if e.kind is TrailKind.data and e.payload.get("document_id") == invoice.id and e.payload["action"] == "classify"
    ]
    assert classified["reason"] == runtime["reason"]
