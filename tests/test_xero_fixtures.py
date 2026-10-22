"""Xero 演示的大模型 fixture 守卫 (R78 验收 4 (b) 至 (d)): tests/fixtures/llm_xero/<程序>/<对象>.json. 上传分类
(DOC-CLASSIFY, 需求 R267) 的 fixture 按文件名主干取, 补造的每份要上传的 PDF 一份, 另有 mock 下 Xero 项目上传的分类.
登记了对应的程序 (fixture_sets.expected_documents), document 指针还须指向对应的补造 PDF (需求 R108,
xero_expected_document); 没登记的程序只做通用核对, 后续需求要核对应时再登记. REV-03 三份合同的 fixture 经真实
判断引擎一次通过, 结论与证据按 Xero 演示数据集设计第 6.2 节 (需求 R109). DOC-06 两份回函与 CSH-02 BA-091 的 fixture
经 CSH-04 → DOC-06 → CSH-02 一次执行验收 (需求 R111). REV-02 六张发票的 fixture 经 REV-08 → REV-03 → REV-02
一次执行验收, 指针只引该发票上下文里有的来源 (需求 R110). 只开现有 17 条路径的 RPT-03 两稿另放
tests/fixtures/llm_xero_only17 (需求 R117), 不进 FIXTURE_SETS, 守卫见文末; 正本还没有的程序由它补进运行副本 (需求 R287,
FixtureSet.fill).

正本与运行副本 (data/xero_demo/20261017-q1/llm) 相同由 tests/test_knowledge.py 按 FIXTURE_SETS 核对. 这里按集合包含
断言, 不写死文件清单, 目录为空时通过; 各程序段加 fixture 时不必改这里 (要核对文件对应的在
fixture_sets.expected_documents 登记). 自测用 tmp 目录造不合格的 fixture.
"""

import asyncio
import csv
import functools
import hashlib
import json
import re
import shutil
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path, PurePath
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from backend.adapters.llm import MockLLMClient
from backend.agents.base import AgentDeps, RunContext
from backend.agents.evidence import EvidenceAgent
from backend.agents.fieldwork import FieldworkAgent
from backend.agents.ingest import CLASSIFY, IngestAgent
from backend.agents.review import GEN09, VERDICT_SPEC
from backend.contracts import llm_outputs
from backend.contracts.knowledge import ProcedureTemplate
from backend.contracts.llm_outputs import ConfirmationExtraction, DocumentClassification, ReportDraft
from backend.contracts.models import (
    AuditException,
    AutonomyLevel,
    ConfirmationResult,
    CorrectionEntry,
    CriticReasonCode,
    CriticVerdict,
    DocumentType,
    ExceptionDisposition,
    MisstatementDraft,
    OpinionType,
    Phase,
    ProcedureRun,
    ProposedAction,
    ReviewStatus,
    RunStatus,
    StepKind,
    TaskKind,
    TrailEntry,
    TrailKind,
)
from backend.contracts.services import JudgmentContext, JudgmentOutcome, ValidationContext
from backend.contracts.state import EngagementState, StatePatch
from backend.core.config import get_settings
from backend.orchestration.decisions import confirm_effects
from backend.orchestration.nodes import apply_patch, build_run
from backend.services.data_service import DataService
from backend.services.judgment_engine import DOCUMENT_EVIDENCE, output_model, report_checks
from backend.services.procedure_library import ProcedureLibrary
from backend.services.sampling import sample_point
from tests.external import supplements_dir
from tests.fakes import (
    ENGAGEMENT,
    NOW,
    NUMBAT_PROFILE,
    FakeDataStore,
    FakeEngagementStore,
    FakeFinancialStore,
    FakeRuleEngine,
    FakeTrail,
    make_ctx,
    make_runner,
)
from tests.fixture_sets import XERO, ExpectedDocuments, FixtureSet, expected_documents
from tests.test_knowledge import document_pointers
from tests.xero_fakes import (
    ORC1026,
    XERO_RULES,
    fixture_judgment,
    make_xero_deps,
    xero_contracts,
    xero_engagement,
    xero_profile,
    xero_state,
    xero_tables,
    xero_traits,
    xero_uploads,
)

LIBRARY = ProcedureLibrary(get_settings().KNOWLEDGE_ROOT)
EVIDENCE_TYPES = {t.value for t in DOCUMENT_EVIDENCE}  # 能作证据的文件类型, 判断引擎的文件指针映射为准
# 上传处理 (agents/ingest) 的模板: 不在程序库里而在 llm_xero 有 fixture, fixture_key 为上传的文件名主干. 与生成脚本的
# UPLOAD_PROGRAMS (从上传固定输出复制进运行副本的程序) 不是一回事
INTAKE_TEMPLATES = {CLASSIFY.id: CLASSIFY}  # 上传分类 (财务报表以账套为准, 上传的报表只分类不抽取, Issue #46)
# 邮箱地址 (fixture 会进留痕与页面, 不放任何人的邮箱): 对 JSON 解码后的字符串匹配 (\u0040 转义的也查得出); 顶级域名
# 须以字母开头, "10@650.00" 这类数字不算; 不带点的域名 (如 x@localhost) 不查
EMAIL = re.compile(r"[\w.%+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z][\w-]*")


@functools.cache
def invoice_contracts() -> dict[str, str]:
    """销售发票 ID → 关联的合同编号 (演示数据导入之后的 sales_invoices, 合同取自补造的 contract_links.csv). 读快照,
    只在有 REV-02 (或复核它的 GEN-09) 的 fixture 时用到."""
    return {r.id: r.contract_id for r in xero_tables()["sales_invoices"] if r.contract_id}


@functools.cache
def account_numbers() -> dict[str, str]:
    """银行账户 ID → 账号 (补造的 bank_accounts.csv; BA-090 → 12345678, BA-091 → 12345679); 补造目录经 tests.external
    定位."""
    with (supplements_dir("20261017-q1") / "bank_accounts.csv").open(encoding="utf-8", newline="") as f:
        return {row["id"]: row["account_no"] for row in csv.DictReader(f)}


# Xero fixture 的 document 指针应指向的补造 PDF (需求 R108): 同 Numbat 的登记 (fixture_sets.expected_documents), 发票的
# 合同与账户的账号查补造与快照. 不登记进 FIXTURE_SETS, 只在这里核对
xero_expected_document = expected_documents(
    lambda key: invoice_contracts().get(key), lambda key: account_numbers().get(key)
)


def document_id_of(path: Path) -> str:
    """文件 ID: "DOC-" 加该文件 sha256 的前 12 位 (同存档时的算法), 从文件本身算, 不取 manifest."""
    return "DOC-" + hashlib.sha256(path.read_bytes()).hexdigest()[:12]


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
    ({对象}-r2) 为 VERDICT_SPEC 的; PLN-07 的 basis 与 screen 各用一个输出模型 (需求 R371)."""
    if procedure_id in INTAKE_TEMPLATES:
        return output_model(INTAKE_TEMPLATES[procedure_id])
    if procedure_id == GEN09 and fixture_key.endswith("-r2"):
        model = getattr(llm_outputs, VERDICT_SPEC.output_model)
        assert isinstance(model, type) and issubclass(model, BaseModel)
        return model
    if procedure_id == "PLN-07":
        name = "AnalyticsScreeningDraft" if fixture_key == "screen" else "AnalyticsBasisDraft"
        model = getattr(llm_outputs, name)
        assert isinstance(model, type) and issubclass(model, BaseModel)
        return model
    return output_model(library.load(procedure_id))


def fixture_problems(
    fs: FixtureSet,
    library: ProcedureLibrary = LIBRARY,
    expected: ExpectedDocuments = xero_expected_document,
) -> list[str]:
    """不合格的 fixture (只写文件, 程序与指针 ID, 不写内容):
    (b) document 指针的 source_id 须在 manifest.documents 中, 1 ≤ page ≤ page_count, 所指文件类型能作证据
    (在 judgment_engine.DOCUMENT_EVIDENCE 中; 如财务报表不作证据); 登记了对应的程序,
    指针还须指向 expected 给出的文件之一 (补造目录下的路径, 文件 ID 由文件内容算; 每个指错的指针各报一条; 对象没有
    对应文件, 即抛 LookupError 的, 不应有 document 指针); 没登记的程序只做以上核对;
    (c) 目录名须是程序库中 (含未启用的) 带 llm 段的程序或上传处理的模板 (DOC-CLASSIFY), 文件须能按该
    程序的输出模型解析;
    上传处理的文件名主干 (default 除外) 须是补造目录中的 PDF (manifest.files 中的, 含不登记的 fs_v1.pdf; 按文件名
    取, 别的名字永远用不上); 不含邮箱地址. fixture 根不存在或为空时没有问题."""
    problems: list[str] = []
    documents = fs.documents()
    listed = json.loads(fs.manifest.read_text(encoding="utf-8"))["files"]
    stems = {Path(f["path"]).stem for f in listed if f["path"].endswith(".pdf")}
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
                problems.append(f"{name}: 不是补造目录中的 PDF")
            pointers = document_pointers(json.loads(raw))
            if pointers:
                try:
                    targets = expected(folder.name, path.stem)
                except LookupError as e:
                    problems.append(f"{name}: 不应有 document 指针 ({e})")
                else:
                    if targets is not None:
                        files = [fs.manifest.parent / t for t in sorted(targets)]
                        allowed = {document_id_of(f) for f in files if f.is_file()}
                        wanted = ", ".join(sorted(targets))
                        problems += [
                            f"{name}: {p['source_id']} 不是应指向的 {wanted}"
                            for p in pointers
                            if p["source_id"] not in allowed
                        ]
            for pointer in pointers:
                entry = documents.get(pointer["source_id"])
                if entry is None:
                    problems.append(f"{name}: {pointer['source_id']} 不在 manifest.documents 中")
                elif not isinstance(pointer.get("page"), int) or not 1 <= pointer["page"] <= entry["page_count"]:
                    problems.append(f"{name}: {pointer['source_id']} 的页码超出 1 至 {entry['page_count']}")
                elif entry["type"] not in EVIDENCE_TYPES:
                    problems.append(f"{name}: {pointer['source_id']} 指向不作证据的 {entry['type']} 类文件")
    return problems


@pytest.mark.external  # REV-02 (与复核它的 GEN-09) 的 fixture 按快照的销售发票找关联合同
def test_xero_fixtures_are_valid() -> None:
    """(b) 至 (d): 正本中每个文件都合格, document 指针指向该程序该对象应指向的文件; 按集合包含核对, 目录为空时通过."""
    assert fixture_problems(XERO) == []
    present = {p.parent.name for p in XERO.fixtures.glob("*/*.json")}
    assert present <= {t.id for t in LIBRARY.all(include_disabled=True) if t.llm is not None} | INTAKE_TEMPLATES.keys()


def write(root: Path, rel: str, value: Any) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.mark.external
def test_fixture_problems_are_reported(tmp_path: Path) -> None:
    """自测: 合格的 CSH-02 与上传分类 fixture 不报; 未知程序目录, 没有 llm 段的程序, 不符合输出模型, 指针不在 manifest,
    页码超出 (含 0 与空), 指向不作证据的文件, 含邮箱, 上传分类的文件名不是补造的 PDF 都报出, 不抛异常. 按程序的文档
    对应另有自测 (test_documents_follow_the_program), 这里一律以那份回函为应指向的文件. 正式清单已没有不作证据的登记
    (fs_v1.pdf 不登记, Issue #46), 这里在 manifest 副本里造一份 financial_statements 登记 (副本旁放回函的副本)."""
    documents = XERO.documents()
    reply = next(d for d in documents.values() if d["type"] == "confirmation")
    statements = {"path": "statements.pdf", "document_id": "DOC-000000000fs1", "type": "financial_statements"}
    supplement = tmp_path / "supplement"
    (supplement / reply["path"]).parent.mkdir(parents=True)
    shutil.copyfile(XERO.manifest.parent / reply["path"], supplement / reply["path"])
    manifest = json.loads(XERO.manifest.read_text(encoding="utf-8"))
    manifest["documents"].append({**statements, "page_count": 1})
    (supplement / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    good = json.loads((get_settings().LLM_FIXTURES_ROOT / "CSH-02" / "BA-01.json").read_text(encoding="utf-8"))

    def to_reply(procedure_id: str, fixture_key: str) -> frozenset[str]:
        return frozenset({str(reply["path"])})

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
    fs = replace(XERO, fixtures=root, manifest=supplement / "manifest.json")
    assert fixture_problems(fs, expected=to_reply) == []  # 单价里的 @ 不算邮箱
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
    problems = fixture_problems(fs, expected=to_reply)
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


@pytest.mark.parametrize(
    ("fixture", "target", "problem"),
    [
        ("DOC-04/CON-X01", "contracts/CON-X01.pdf", None),
        ("REV-03/CON-X02", "contracts/CON-X01.pdf", "不是应指向的 contracts/CON-X02.pdf"),  # 别的合同
        ("CSH-02/BA-090", "confirmations/12345678.pdf", None),
        ("DOC-06/BA-091", "confirmations/12345678.pdf", "不是应指向的 confirmations/12345679.pdf"),  # 别的账户的回函
        ("DOC-06/BA-999", "confirmations/12345678.pdf", "不应有 document 指针"),  # 补造里没有这个账户
        ("GEN-09/BA-091-r1", "confirmations/12345679.pdf", None),  # 复核 CSH-02 的判断, 同 CSH-02
        ("GEN-09/CON-X03-r1", "contracts/CON-X01.pdf", "不是应指向的 contracts/CON-X03.pdf"),
        ("GEN-09/BT-090-0092-r1", "contracts/CON-X01.pdf", None),  # 前缀没登记对应: 只做通用核对
        pytest.param(  # ORC1026, contract_links.csv 关联 CON-X01
            "REV-02/SI-77b338ef-ecc0-4b95-a0d7-2617b0054611", "contracts/CON-X01.pdf", None, marks=pytest.mark.external
        ),
        pytest.param(  # ORC1020 关联 CON-X02
            "REV-02/SI-5f6deadf-36a2-495a-9980-ceb11e8af9a9",
            "contracts/CON-X01.pdf",
            "不是应指向的 contracts/CON-X02.pdf",
            marks=pytest.mark.external,
        ),
        pytest.param(  # 没有关联合同的发票
            "REV-02/SI-3b28bf11-ed2f-4cf4-8e9e-fcae730cc292",
            "contracts/CON-X01.pdf",
            "不应有 document 指针",
            marks=pytest.mark.external,
        ),
    ],
)
@pytest.mark.external  # 文件 ID 取补造的 manifest (XERO.documents)
def test_documents_follow_the_program(tmp_path: Path, fixture: str, target: str, problem: str | None) -> None:
    """按程序的文档对应 (需求 R108): 取该程序的一份 Numbat fixture, 把 document 指针都改指补造目录的 target (第 1 页,
    文件 ID 取 manifest): 是该程序该对象应指向的文件, 或没登记对应时不报, 否则每个指针报一条; 登记了而对象没有对应
    文件 (账户不在补造里, 发票没有关联合同) 报一条不应有 document 指针. 防 fixture 引了别的合同或别的账户的回函
    (页码与类型都合格, 只有这里查得出)."""
    procedure_id, key = fixture.split("/")
    template = sorted(
        (get_settings().LLM_FIXTURES_ROOT / procedure_id).glob("*-r1.json" if key.endswith("-r1") else "*.json")
    )[0]
    value: dict[str, Any] = json.loads(template.read_text(encoding="utf-8"))
    document_id = next(i for i, d in XERO.documents().items() if d["path"] == target)
    for pointer in document_pointers(value):
        pointer.update(source_id=document_id, page=1)
    assert document_pointers(value)
    write(tmp_path / "llm_xero", f"{fixture}.json", value)
    problems = fixture_problems(replace(XERO, fixtures=tmp_path / "llm_xero"))
    if problem is None:
        assert problems == []
    else:
        count = 1 if problem == "不应有 document 指针" else len(document_pointers(value))
        assert len(problems) == count and all(p.startswith(f"{fixture}: ") and problem in p for p in problems)


def test_unregistered_programs_are_not_checked() -> None:
    """没登记对应的程序 (如以后的 GEN-04, GEN-06) 与前缀没登记的 GEN-09 对象, 期望文件为 None, 只做通用核对. 防守卫
    变成封闭的, 后续需求的 fixture 引了文件就不过."""
    keys = [("GEN-04", "CLIENT-XERO-3C1332BB-Q1"), ("GEN-06", "BT-090-0092"), ("GEN-09", "XJ-580-r1")]
    assert [xero_expected_document(p, k) for p, k in keys] == [None, None, None]


@pytest.mark.external  # 文件 ID 取补造的 manifest
def test_only_the_wrong_pointers_are_reported(tmp_path: Path) -> None:
    """一份 fixture 里第一个指针指对 (REV-03/CON-X02 指 CON-X02.pdf), 其余指向 CON-X01.pdf: 只报其余的, 各一条.
    防只核对第一个指针."""
    template = sorted((get_settings().LLM_FIXTURES_ROOT / "REV-03").glob("*.json"))[0]
    value: dict[str, Any] = json.loads(template.read_text(encoding="utf-8"))
    ids = {d["path"]: i for i, d in XERO.documents().items()}
    pointers = document_pointers(value)
    for n, pointer in enumerate(pointers):
        pointer.update(source_id=ids["contracts/CON-X02.pdf" if n == 0 else "contracts/CON-X01.pdf"], page=1)
    assert len(pointers) > 1
    write(tmp_path / "llm_xero", "REV-03/CON-X02.json", value)
    problems = fixture_problems(replace(XERO, fixtures=tmp_path / "llm_xero"))
    assert problems == [f"REV-03/CON-X02: {ids['contracts/CON-X01.pdf']} 不是应指向的 contracts/CON-X02.pdf"] * (
        len(pointers) - 1
    )


def test_gen09_second_round_uses_verdict_model() -> None:
    """GEN-09 的 {对象}-r2 按 VERDICT_SPEC 的输出模型解析, 其余按模板的."""
    assert fixture_model(LIBRARY, "GEN-09", "BA-01-r2").__name__ == VERDICT_SPEC.output_model
    assert fixture_model(LIBRARY, "GEN-09", "BA-01-r1") is output_model(LIBRARY.load("GEN-09"))
    with pytest.raises(ValueError):
        fixture_model(LIBRARY, "ACC-01", "x")  # 没有 llm 段


# ── 上传分类 (DOC-CLASSIFY, 需求 R267): 补造的 22 份上传 PDF 与 fs_v1.pdf 各一份, 按文件名主干取 ──


@pytest.mark.external
def test_classify_fixtures_follow_the_manifest() -> None:
    """每份要上传的补造 PDF 各有一份分类 fixture, 类型同 manifest.documents 中同名文件的类型 (合同只指 3 份收入合同,
    租约, 借款协议与客户订单为 agreement); 不登记的 fs_v1.pdf (审计师自己上传报表的样例, Issue #46) 另有一份, 为
    financial_statements; 理由为一句英文; default.json 判断不了 (document_type 为空)."""
    folder = XERO.fixtures / CLASSIFY.id
    fixtures = {
        p.stem: DocumentClassification.model_validate_json(p.read_text(encoding="utf-8")) for p in folder.glob("*.json")
    }
    default = fixtures.pop("default")
    expected = {PurePath(u.file_name).stem: u.document_type for u in xero_uploads()}  # 演示导入上传的 22 份
    assert len(expected) == 22
    assert {stem: f.document_type for stem, f in fixtures.items()} == {
        **expected,
        "fs_v1": DocumentType.financial_statements,
    }
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
    pdf = supplements_dir("20261017-q1") / entry.path
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


# ── Xero 项目上依次执行 P2 程序的辅助 (REV-03, 函证, REV-02 三段共用) ──

REVENUE_OBJECTIVES = ["OBJ-REV-OCC", "OBJ-REV-CUT"]  # Xero 预置的收入两条目标, 都是 significant
CASH_OBJECTIVES = ["OBJ-CASH-EXI"]  # Xero 预置的现金目标, high


@dataclass
class RecordingJudgments:
    """真实判断引擎外包一层, 记下每次的判断上下文 (交给大模型的数据)."""

    inner: Any
    contexts: list[JudgmentContext] = field(default_factory=list)

    async def judge(
        self, eid: str, template: ProcedureTemplate, run: ProcedureRun, ctx: JudgmentContext, performed_by: str
    ) -> JudgmentOutcome:
        self.contexts.append(ctx)
        outcome: JudgmentOutcome = await self.inner.judge(eid, template, run, ctx, performed_by)
        return outcome


def xero_context(deps: AgentDeps, state: EngagementState, procedure_id: str, objective_ids: list[str]) -> RunContext:
    """Xero 项目上某条 P2 程序的下一次执行: 关联给定的目标, 执行记录同 GraphNodes.perform 用 build_run 建 (参数按
    风险等级, 状态与数据集声明解析, 次数接着状态里的执行记录)."""
    engagement = xero_engagement()
    ctx = make_ctx(deps, procedure_id, engagement=engagement)
    procedure = ctx.procedure.model_copy(update={"phase": Phase.P2_Executing, "objective_ids": objective_ids})
    run = build_run(
        deps.library, ctx.template, procedure, engagement, state, NOW, traits=xero_traits(), profile=xero_profile()
    )
    return RunContext(engagement, procedure, ctx.template, run, state | {"procedure_pack": [procedure]})  # type: ignore[arg-type]


@dataclass
class Revenue:
    rev08: StatePatch  # REV-08 (规则: R03-1)
    rev03: StatePatch  # REV-03 (三份合同的判断, 作 REV-02 的 contract_judgment)
    rev02: StatePatch  # REV-02 (六张发票的判断)
    calls: list[TrailEntry]  # 大模型调用的留痕, 按调用顺序
    contexts: dict[str, JudgmentContext]  # REV-02 的判断上下文, 按发票 ID


async def run_revenue() -> Revenue:
    """REV-08 → REV-03 → REV-02 依次执行一次, 后一条看前一条并入后的状态 (同 GraphNodes 的 apply_patch). Xero 假存储,
    规则引擎为 FakeRuleEngine(XERO_RULES) (S01 六张与 R03-1), 执行重要性为闸门定的 16216; 主模型与复核模型都读正本
    (llm_xero)."""
    trail = FakeTrail()
    client = MockLLMClient(XERO.fixtures)
    deps = make_xero_deps(trail=trail, llm=client, review_llm=client, rules=FakeRuleEngine(XERO_RULES))
    state = xero_state()
    rev08 = await FieldworkAgent(deps).run(xero_context(deps, state, "REV-08", REVENUE_OBJECTIVES))  # type: ignore[arg-type]
    state = apply_patch(state, rev08)  # type: ignore[arg-type]
    rev03 = await FieldworkAgent(deps).run(xero_context(deps, state, "REV-03", REVENUE_OBJECTIVES))
    state = apply_patch(state, rev03)
    judgments = RecordingJudgments(deps.judgments)
    judging = replace(deps, judgments=judgments)
    rev02 = await FieldworkAgent(judging).run(xero_context(judging, state, "REV-02", REVENUE_OBJECTIVES))
    calls = [e for e in trail.entries if e.kind is TrailKind.llm_call]
    return Revenue(rev08, rev03, rev02, calls, {c.subject_id: c for c in judgments.contexts})


@pytest.fixture(scope="module")
def revenue() -> Revenue:
    return asyncio.run(run_revenue())


# ── 收入一串 (需求 R109, R110): REV-03 与 REV-02 的固定输出经真实判断引擎一次通过 ──

REV03_CONTRACTS = ["CON-X01", "CON-X02", "CON-X03"]
ORC = {  # 发票号 → 发票 ID, 按发票 ID 排序 (即 REV-02 判断对象的次序)
    "ORC1021": "SI-3b28bf11-ed2f-4cf4-8e9e-fcae730cc292",
    "ORC1024": "SI-5aadcd34-01a9-4b8d-a2bb-d7cc1de9fa45",
    "ORC1020": "SI-5f6deadf-36a2-495a-9980-ceb11e8af9a9",
    "ORC1026": ORC1026,
    "ORC1022": "SI-a9f765e6-b9bc-4505-a47b-fb3ecb327e7b",
    "ORC1023": "SI-bcd8a71f-aa31-4d0f-8a01-13ea26363ddf",
}


@pytest.mark.external
@pytest.mark.parametrize(
    ("procedure_id", "subjects", "exceptions"),
    [
        ("REV-03", REV03_CONTRACTS, {}),  # 需求 R109: 三份收入合同
        ("REV-02", list(ORC.values()), {ORC1026: ["X-REV-08-1-R03-1"]}),  # 需求 R110: S01 选中的六张发票
    ],
)
def test_revenue_fixtures_pass_real_engine(
    revenue: Revenue, procedure_id: str, subjects: list[str], exceptions: dict[str, list[str]]
) -> None:
    """REV-03 验收 1, 5 与 REV-02 验收 2, 7 的 A7 部分 (run_revenue 那一次执行): 每个判断对象调一次大模型, 第 1 次就
    通过, 提示词的 Standards 依次列出 AASB15-P31, P35, P38 (两条程序的 standards 相同); 判断都是 accept, 没有错报
    草稿, 推理链没有 assumption 步骤, A7 按 L2 跳过; 只有所列对象解释例外 (REV-02 的 ORC1026 解释 R03-1); 每条判断
    一条 normal 任务, 没有 failed 任务; 执行成功."""
    patch = {"REV-03": revenue.rev03, "REV-02": revenue.rev02}[procedure_id]
    calls = [e for e in revenue.calls if e.procedure_id == procedure_id]
    assert [e.payload["fixture_key"] for e in calls] == subjects
    assert {(e.payload["attempt"], e.payload["level_failed"]) for e in calls} == {(1, None)}
    for entry in calls:
        prompt = entry.payload["messages"][0]["content"]
        listed = prompt.split("Standards (cite only these IDs):\n", 1)[1].split("\n## ", 1)[0].splitlines()
        cited = [line.split(" ", 2)[1] for line in listed if line.startswith("- ")]
        assert cited == ["AASB15-P31", "AASB15-P35", "AASB15-P38"], entry.payload["fixture_key"]
    judgments = patch["judgments"]
    assert [j.id for j in judgments] == [f"J-{procedure_id}-1-{s}" for s in subjects]
    for j in judgments:
        assert (j.proposed_action, j.misstatement_draft, j.critic_verdict) == (
            ProposedAction.accept,
            None,
            CriticVerdict.skipped,
        )
        assert j.critic_reason is not None and j.critic_reason.startswith("L2")
        assert j.exception_ids == exceptions.get(j.subject_id, []), j.id
        assert StepKind.assumption not in {s.kind for s in j.reasoning_chain}, j.id
    assert [(t.id, t.kind) for t in patch["review_tasks"]] == [
        (f"T-J-{procedure_id}-1-{s}-v1", TaskKind.normal) for s in subjects
    ]
    [run] = patch["procedure_runs"]
    assert run.status is RunStatus.succeeded


# ── REV-03 (需求 R109): 三份收入合同的条款评价 ──


def sentence_with(text: str, anchor: str) -> str:
    """结论中含 anchor (条款 ID) 的那一句."""
    [found] = [s for s in re.split(r"(?<=\.)\s+", text) if anchor in s]
    return found


@pytest.mark.external
@pytest.mark.parametrize(
    ("contract", "transfer", "other", "standards"),
    [
        (  # 完工日 (部署且 Project Sponsor 签完工证书) 某一时点转移; 7.2 完工前终止无权收款, 第 35(b)(c) 段不满足
            "CON-X01",
            ("Completion Date", "deployed", "Project Sponsor", "Completion Certificate", "point in time", "AASB15-P38"),
            ("7.2", "terminated before the Completion Date", "not entitled to payment", "35(b) and (c)", "not met"),
            ["AASB15-P31", "AASB15-P35", "AASB15-P38"],
        ),
        (  # 每个自然月随服务提供取得利益, 随时间转移; 6.1 响应超时可索赔 10%, 取决于未来事件, 对价可变
            "CON-X02",
            ("each calendar month", "simultaneously receives and consumes", "over time", "35(a)"),
            ("6.1", "10%", "4 business hours", "future event", "consideration variable"),
            ["AASB15-P31", "AASB15-P35"],
        ),
        (  # 每个驻场日按客户签字的工时表确认, 随时间转移; 6.1 项目经理缺席每半日贷记 350, 取决于未来事件, 对价可变
            "CON-X03",
            ("On-site Day", "signs the timesheet", "over time", "35(a)"),
            ("6.1", "$350.00", "half day", "absent", "future event", "consideration variable"),
            ["AASB15-P31", "AASB15-P35"],
        ),
    ],
)
def test_rev03_conclusion_and_evidence(
    revenue: Revenue, contract: str, transfer: tuple[str, ...], other: tuple[str, ...], standards: list[str]
) -> None:
    """验收 2 至 4: 结论里定转移的一句引 T1 (转移事件与方式), 另一句引 T3 (影响收入的其他条款); 证据含该合同文件
    T1 (第 2 页), T3 (第 3 页) 两条款的指针与所列准则片段. 只查关键词, 不比整句. 推理链的指针也核对 (页面的推理链与
    A7 第一轮证据都用它们, 判断引擎不查这几项): evidence 恰为推理链各步指针去重后的并集; 全部文件指针的 (条款 ID, 页)
    与该合同的条款一致; 准则片段都在 REV-03 的 standards 内."""
    [j] = [j for j in revenue.rev03["judgments"] if j.subject_id == contract]
    first, second = sentence_with(j.conclusion, f"{contract}-T1"), sentence_with(j.conclusion, f"{contract}-T3")
    for text in transfer:
        assert text in first, text
    for text in other:
        assert text in second, text
    document = {c.id: c.document_id for c in xero_contracts()}[contract]
    pointers = {(p.source_type, p.source_id, p.locator, p.page) for p in j.evidence}
    assert {("document", document, f"{contract}-T1", 2), ("document", document, f"{contract}-T3", 3)} <= pointers
    assert {(p.source_id, p.locator) for p in j.evidence if p.source_type == "standard"} >= {
        (s, f"para {s.removeprefix('AASB15-P')}") for s in standards
    }
    chain = [p for step in j.reasoning_chain for p in step.evidence]
    keys = [(p.source_type, p.source_id, p.locator, p.page) for p in j.evidence]
    assert len(keys) == len(set(keys)) and set(keys) == {(p.source_type, p.source_id, p.locator, p.page) for p in chain}
    [found] = [c for c in xero_contracts() if c.id == contract]
    terms = {(t.id, t.page) for t in found.terms}
    assert {(p.locator, p.page) for p in [*chain, *j.evidence] if p.source_type == "document"} <= terms
    allowed = set(LIBRARY.load("REV-03").standards)
    assert {p.source_id for p in [*chain, *j.evidence] if p.source_type == "standard"} <= allowed


# ── 函证 (需求 R111): 两份回函抽取 (DOC-06) 与 BA-091 的函证判断 (CSH-02), 经真实判断引擎 ──

REPLY_IDS = {"BA-090": "DOC-d111883a3741", "BA-091": "DOC-66b0be79cdd4"}  # confirmations/12345678.pdf, 12345679.pdf


@dataclass
class Cash:
    sent: StatePatch  # CSH-04 (A5 发函, 存回函, 建函证第 1 版)
    extracted: StatePatch  # DOC-06 (A3 抽回函)
    judged: StatePatch  # CSH-02 (A6 比对余额与应计利息, 判断)
    calls: list[TrailEntry]  # 大模型调用的留痕, 按调用顺序
    contexts: list[JudgmentContext]  # CSH-02 的判断上下文


async def run_cash() -> Cash:
    """CSH-04 → DOC-06 → CSH-02 依次执行一次, 后一条看前一条并入后的状态 (同 GraphNodes 的 apply_patch). Xero 假存储,
    假函证平台回补造的回函 PDF; 规则引擎为 FakeRuleEngine(XERO_RULES), 明显微小为闸门定的 1622; 主模型与复核模型
    都读正本 (llm_xero)."""
    trail = FakeTrail()
    client = MockLLMClient(XERO.fixtures)
    deps = make_xero_deps(trail=trail, llm=client, review_llm=client, rules=FakeRuleEngine(XERO_RULES))
    state = xero_state()
    sent = await EvidenceAgent(deps).run(xero_context(deps, state, "CSH-04", CASH_OBJECTIVES))  # type: ignore[arg-type]
    state = apply_patch(state, sent)  # type: ignore[arg-type]
    extracted = await IngestAgent(deps).run(xero_context(deps, state, "DOC-06", CASH_OBJECTIVES))
    state = apply_patch(state, extracted)
    judgments = RecordingJudgments(deps.judgments)  # 只为 CSH-02 记判断上下文 (DOC-06 经 call_llm 调大模型)
    judging = replace(deps, judgments=judgments)
    judged = await FieldworkAgent(judging).run(xero_context(judging, state, "CSH-02", CASH_OBJECTIVES))
    calls = [e for e in trail.entries if e.kind is TrailKind.llm_call]
    return Cash(sent, extracted, judged, calls, judgments.contexts)


@pytest.fixture(scope="module")
def cash() -> Cash:
    return asyncio.run(run_cash())


@pytest.mark.external
def test_doc06_fixtures_extract_both_replies(cash: Cash) -> None:
    """验收 1, 2: CSH-04 建 C-BA-090, C-BA-091 第 1 版并存回函; DOC-06 读回函抽出第 2 版 (BA-090 透支为负, 回函写 nil
    的应计利息为 0; BA-091 应计利息 4853, 在客户一方为正), 两份回函 extracted; 两次调用一次通过 (含账号的语义校验),
    没有提示 (回函写明的渠道与回执相符). 抽取结果按 fixture 的输出 (留痕的 raw) 整组比对: 六个字段的值与各自的页
    (渠道在第 2 页, 其余第 1 页), 都指向本户回函; 函证第 2 版只并入余额, 余额日, 应计利息与指针 (银行名与渠道是
    CSH-04 写下的)."""
    assert [(c.id, c.version, c.reply_document_id) for c in cash.sent["confirmations"]] == [
        ("C-BA-090", 1, REPLY_IDS["BA-090"]),
        ("C-BA-091", 1, REPLY_IDS["BA-091"]),
    ]
    extracted = {c.id: c for c in cash.extracted["confirmations"]}
    assert {
        c.id: (c.version, c.confirmed_balance_cents, c.balance_date, c.accrued_interest_cents)
        for c in extracted.values()
    } == {
        "C-BA-090": (2, -1090044, date(2026, 9, 30), 0),
        "C-BA-091": (2, 1158123, date(2026, 9, 30), 4853),
    }
    doc06 = {e.payload["fixture_key"]: e.payload for e in cash.calls if e.procedure_id == "DOC-06"}
    expected = {
        "BA-090": ("12345678", -1090044, 0),
        "BA-091": ("12345679", 1158123, 4853),
    }
    for account, (account_no, balance, accrued) in expected.items():
        output = ConfirmationExtraction.model_validate_json(doc06[account]["raw"])
        fields = output.model_dump(mode="json", exclude={"evidence"})
        assert fields == {
            "confirming_party": "Wattlebrook Bank Limited",
            "account_no": account_no,
            "confirmed_balance_cents": balance,
            "balance_date": "2026-09-30",
            "receipt_channel": "direct_email",
            "accrued_interest_cents": accrued,
        }, account
        reply = REPLY_IDS[account]
        assert {p.locator: (p.source_id, p.page) for p in output.evidence} == {
            name: (reply, 2 if name == "receipt_channel" else 1) for name in fields
        }, account
        assert doc06[account]["notes"] == [], account
        assert [p.page for p in extracted[f"C-{account}"].evidence] == [p.page for p in output.evidence]
    assert sorted((d.id, d.status) for d in cash.extracted["documents"]) == sorted(
        (reply, "extracted") for reply in REPLY_IDS.values()
    )
    doc06 = [e for e in cash.calls if e.procedure_id == "DOC-06"]
    assert [(e.payload["fixture_key"], e.payload["attempt"], e.payload["level_failed"]) for e in doc06] == [
        ("BA-090", 1, None),
        ("BA-091", 1, None),
    ]


@pytest.mark.external
def test_csh02_judges_only_the_unaccrued_interest(cash: Cash) -> None:
    """验收 3: 两户余额都相符 (第 3 版 agreed, 差额 0); 只有 BA-091 的 X-CSH-02-1-D04-1 (4853, 搜索区间 09-25 至 09-30,
    没有匹配的行), 没有 D01; 只判断 BA-091 (BA-090 不调大模型). BA-091 的上下文: 差额 0, 期后没有本户流水, 期末前分录
    只有 XJ-601-1 (091 贷 350000), 规则例外只有该 D04."""
    confirmations = {c.id: c for c in cash.judged["confirmations"]}
    assert {c.id: (c.version, c.book_balance_cents, c.difference_cents, c.result) for c in confirmations.values()} == {
        "C-BA-090": (3, -1090044, 0, ConfirmationResult.agreed),
        "C-BA-091": (3, 1158123, 0, ConfirmationResult.agreed),
    }
    [d04] = cash.judged["exceptions"]
    assert (d04.id, d04.rule_id, d04.source_ids, d04.amount_cents) == ("X-CSH-02-1-D04-1", "D04", ["BA-091"], 4853)
    assert d04.detail == {
        "accrued_interest_cents": 4853,
        "search_from": "2026-09-25",
        "search_to": "2026-09-30",
        "matched_line_ids": [],
    }
    assert [(e.procedure_id, e.payload["fixture_key"]) for e in cash.calls if e.procedure_id != "DOC-06"] == [
        ("CSH-02", "BA-091")
    ]
    [jctx] = cash.contexts
    assert (jctx.subject_id, jctx.data["difference_cents"], jctx.data["subsequent_transactions"]) == ("BA-091", 0, [])
    assert [(r["id"], r["credit_cents"]) for r in jctx.data["period_end_entries"]] == [("XJ-601-1", 350000)]
    assert [x.id for x in jctx.rule_findings] == ["X-CSH-02-1-D04-1"]


@pytest.mark.external
def test_csh02_ba091_fixture_is_an_accumulated_misstatement(cash: Cash) -> None:
    """验收 4, 5: BA-091 的固定输出经真实判断引擎一次通过 (草稿金额 4853 等于归因例外的金额): misstatement, 草稿
    {factual, Interest income, Completeness, 4853, understated}; 结论写明 09-30 已产生而尚未入账的利息 48.53, 窗口内
    总账没有同额计提, 利息收入与应收少记, 高于明显微小 16.22 须累计; 证据为回函两处 (第 1 页), 试算表 091, XJ-601-1
    与两段准则, 恰为推理链各步指针去重后的并集. 判断 pending, 解释该 D04; 因 always_review 建 normal 任务. A7 未命中
    触发规则 (建议 misstatement 不中主规则, 没有 assumption 步骤, 抽样点不小于 0.05), 记 skipped."""
    [call] = [e for e in cash.calls if e.procedure_id == "CSH-02"]
    assert (call.payload["attempt"], call.payload["level_failed"]) == (1, None)
    [j] = cash.judged["judgments"]
    assert (j.id, j.proposed_action, j.review_status, j.exception_ids) == (
        "J-CSH-02-1-BA-091",
        ProposedAction.misstatement,
        ReviewStatus.pending,
        ["X-CSH-02-1-D04-1"],
    )
    assert j.misstatement_draft == MisstatementDraft(
        type="factual",
        account="Interest income",
        assertion="Completeness",
        amount_cents=4853,
        direction="understated",
        correction=[  # 需求 R367: 借 620 Prepayments, 贷 270 Interest Income
            CorrectionEntry(
                account_code="620",
                account_name="Prepayments",
                debit_cents=4853,
                description="Recognise the interest receivable accrued to 30 September 2026.",
            ),
            CorrectionEntry(
                account_code="270",
                account_name="Interest Income",
                credit_cents=4853,
                description="Recognise the interest income accrued to 30 September 2026.",
            ),
        ],
    )
    unaccrued = sentence_with(j.conclusion, "48.53 earned")
    assert all(text in unaccrued for text in ("30 September 2026", "not yet credited")), unaccrued
    understated = sentence_with(j.conclusion, "understated")
    assert all(text in understated for text in ("25 to 30 September 2026", "interest income", "receivable"))
    trivial = sentence_with(j.conclusion, "clearly trivial")
    assert all(text in trivial for text in ("16.22", "accumulated", "ASA 450 para 5")), trivial
    reply = REPLY_IDS["BA-091"]
    assert {(p.source_type, p.source_id, p.locator, p.page) for p in j.evidence} == {
        ("document", reply, "accrued_interest_cents", 1),
        ("document", reply, "confirmed_balance_cents", 1),
        ("trial_balance", "091", "balance_cents", None),
        ("journal_entry", "XJ-601-1", "credit_cents", None),
        ("standard", "ASA505-P14", "para 14", None),
        ("standard", "ASA450-P05", "para 5", None),
    }
    chain = [(p.source_type, p.source_id, p.locator, p.page) for s in j.reasoning_chain for p in s.evidence]
    keys = [(p.source_type, p.source_id, p.locator, p.page) for p in j.evidence]
    assert len(keys) == len(set(keys)) and set(keys) == set(chain)  # evidence 为推理链指针去重后的并集
    assert StepKind.assumption not in {s.kind for s in j.reasoning_chain}
    assert [(t.id, t.kind) for t in cash.judged["review_tasks"]] == [("T-J-CSH-02-1-BA-091-v1", TaskKind.normal)]
    assert (j.critic_verdict, j.critic_reason) == (CriticVerdict.skipped, "未命中独立复核的触发规则")
    assert sample_point(j.id) >= 0.05  # GEN-09 的 sample_rate
    [run] = cash.judged["procedure_runs"]
    assert run.status is RunStatus.succeeded


@pytest.mark.external
def test_xero_skip_reasons_carry_codes(cash: Cash, revenue: Revenue) -> None:
    """R309 验收 1 的后端部分: J-CSH-02-1-BA-091 未命中触发规则, REV-03 三条与 REV-02 六条按 L2 跳过, 都带原因代码
    (前端按界面语言显示), 中文原句照旧, 没有技术细节."""
    [j] = cash.judged["judgments"]
    assert (j.id, j.critic_reason_code, j.critic_reason_detail) == (
        "J-CSH-02-1-BA-091",
        CriticReasonCode.not_triggered,
        None,
    )
    skipped = [*revenue.rev03["judgments"], *revenue.rev02["judgments"]]
    assert len(skipped) == 9
    assert {(j.critic_verdict, j.critic_reason, j.critic_reason_code, j.critic_reason_detail) for j in skipped} == {
        (CriticVerdict.skipped, "L2: 审计师逐条复核, 不经独立复核 (GEN-09 跳过规则)", CriticReasonCode.level_l2, None)
    }


# ── REV-02 (需求 R110): S01 选中的六张发票的截止性判断, 经 REV-08 → REV-03 → REV-02 一次执行 ──


@pytest.mark.external
def test_rev02_subjects_and_context(revenue: Revenue) -> None:
    """验收 1: 判断对象为 S01 的六张, 按发票 ID; 只有 ORC1026 带 REV-08 的 R03-1; ORC1026 挂 CON-X01, ORC1020 挂
    CON-X02 (各带 REV-03 的判断), 其余没有合同; ORC1026 的分录行为 XJ-580 的三行."""
    assert list(revenue.contexts) == list(ORC.values())
    assert {i: [x.id for x in c.rule_findings] for i, c in revenue.contexts.items()} == {
        i: (["X-REV-08-1-R03-1"] if i == ORC1026 else []) for i in ORC.values()
    }
    contracts = {i: (c.data["contract"] or {}).get("id") for i, c in revenue.contexts.items()}
    assert contracts == {i: {"ORC1026": "CON-X01", "ORC1020": "CON-X02"}.get(n) for n, i in ORC.items()}
    judged = {i: (c.data["contract_judgment"] or {}).get("id") for i, c in revenue.contexts.items()}
    assert judged == {i: f"J-REV-03-1-{c}" if c else None for i, c in contracts.items()}
    lines = revenue.contexts[ORC1026].data["journal_lines"]
    assert [
        (r["id"], r["account_code"], r["account_category"], r["debit_cents"], r["credit_cents"]) for r in lines
    ] == [
        ("XJ-580-1", "200", "revenue", 0, 650000),
        ("XJ-580-2", "610", "receivables", 715000, 0),
        ("XJ-580-3", "820", "tax_payable", 0, 65000),
    ]


@pytest.mark.external
@pytest.mark.parametrize(
    ("number", "transfer", "evidence"),
    [
        (  # 服务完成记录 09-17 签署, 依 CON-X01-T1 (Project Sponsor 签完工证书即完工, 控制随之转移) 读作完工证书,
            # 9 月确认正确; R03-1 只是收款与对账问题
            "ORC1026",
            ("SR-ORC1026", "17 September 2026", "completion certificate", "CON-X01-T1", "in September is correct"),
            {
                ("sales_invoice", ORC1026, "posting_date", None),
                ("journal_entry", "XJ-580-1", "credit_cents", None),
                ("journal_entry", "XJ-580-2", "debit_cents", None),
                ("journal_entry", "XJ-580-3", "credit_cents", None),
                ("delivery_note", "SR-ORC1026", "signed_date", None),
                ("document", "DOC-7795e9312e9f", "CON-X01-T1", 2),
                ("bank_transaction", "BT-090-0124", "amount_cents", None),
                ("standard", "AASB15-P31", "para 31", None),
                ("standard", "AASB15-P38", "para 38", None),
            },
        ),
        (  # 9 月服务 09-01 至 09-30, CON-X02-T1 随时间转移
            "ORC1020",
            ("SR-ORC1020", "1 to 30 September 2026", "CON-X02-T1", "over time"),
            {
                ("sales_invoice", ORC["ORC1020"], "posting_date", None),
                ("delivery_note", "SR-ORC1020", "signed_date", None),
                ("document", "DOC-02c5c90d4ffb", "CON-X02-T1", 2),
                ("standard", "AASB15-P31", "para 31", None),
                ("standard", "AASB15-P35", "para 35", None),
            },
        ),
        *(
            (  # 没有合同, 按服务完成记录 09-01 至 09-30
                number,
                (f"SR-{number}", "no contract", "1 to 30 September 2026", "over time"),
                {
                    ("sales_invoice", ORC[number], "posting_date", None),
                    ("delivery_note", f"SR-{number}", "signed_date", None),
                    ("standard", "AASB15-P31", "para 31", None),
                    ("standard", "AASB15-P35", "para 35", None),
                },
            )
            for number in ("ORC1021", "ORC1022", "ORC1023")
        ),
        (  # 没有合同, 服务完成记录同一天 (08-19) 开始并确认完成, 某一时点 (是培训, 但上下文里没有这个词, 输出不写)
            "ORC1024",
            ("SR-ORC1024", "no contract", "same day, 19 August 2026", "point in time"),
            {
                ("sales_invoice", ORC["ORC1024"], "posting_date", None),
                ("delivery_note", "SR-ORC1024", "shipped_date", None),
                ("delivery_note", "SR-ORC1024", "signed_date", None),
                ("standard", "AASB15-P31", "para 31", None),
                ("standard", "AASB15-P38", "para 38", None),
            },
        ),
    ],
)
def test_rev02_conclusion_and_evidence(
    revenue: Revenue, number: str, transfer: tuple[str, ...], evidence: set[tuple[str, str, str, int | None]]
) -> None:
    """验收 3 至 5: 结论第一句写发票号, 所属期间, 转移事件与日期 (只查关键词, 不比句首); 证据含所列指针. ORC1026
    另一句回应 R03-1: 期后匹配到的收款 3,850.00 少于发票 7,150.00, 属收款与对账问题, 不改变转移日, 不构成截止错报.
    关键词取能分辨肯定与否定的写法 (如 "in September is correct", "not change the transfer date")."""
    [j] = [j for j in revenue.rev02["judgments"] if j.subject_id == ORC[number]]
    first = re.split(r"(?<=\.)\s+", j.conclusion)[0]
    for text in (f"Invoice {number}", "period ending 30 September 2026", *transfer):
        assert text in first, text
    assert evidence <= {(p.source_type, p.source_id, p.locator, p.page) for p in j.evidence}
    if number == "ORC1026":
        finding = sentence_with(j.conclusion, "X-REV-08-1-R03-1")
        for text in (
            "BT-090-0124",
            "3,850.00",
            "7,150.00",
            "collection and reconciliation",
            "not change the transfer date",
            "not a cutoff misstatement",
        ):
            assert text in finding, text


def context_problems(name: str, value: dict[str, Any], jctx: JudgmentContext, standards: list[str]) -> list[str]:
    """REV-02 fixture 中引了上下文以外来源的指针 (只写 fixture 与指针, 不写内容): 允许的来源为本发票, 其过账分录行,
    服务完成记录, 所挂合同的条款 (locator 为条款 ID, page 为该条款的页; 合同要素字段算越界), rule_findings 的
    detail.bank_transaction_ids 列出的银行流水, 以及模板 standards 的片段 (提示词证据表)."""
    data = jctx.data
    allowed: dict[str, set[Any]] = {
        "sales_invoice": {data["invoice"]["id"]},
        "journal_entry": {r["id"] for r in data["journal_lines"]},
        "delivery_note": {n["id"] for n in data["delivery_notes"]},
        "bank_transaction": {
            t for x in jctx.rule_findings for t in str(x.detail.get("bank_transaction_ids") or "").split(",") if t
        },
        "standard": set(standards),
        "document": set(),
    }
    if data["contract"] is not None:
        allowed["document"] = {(data["contract"]["document_id"], t["id"], t["page"]) for t in data["contract"]["terms"]}
    problems = []
    for p in [*value["evidence"], *(q for s in value["reasoning_chain"] for q in s["evidence"])]:
        key = (p["source_id"], p.get("locator"), p.get("page")) if p["source_type"] == "document" else p["source_id"]
        if key not in allowed.get(p["source_type"], set()):
            problems.append(f"{name}: {p['source_type']} {p['source_id']} {p.get('locator')} 不在该发票的上下文中")
    return sorted(set(problems))


@pytest.mark.external
def test_rev02_fixtures_cite_only_the_context(revenue: Revenue) -> None:
    """验收 6: 六份正本 fixture 的全部指针都在该发票上下文能列出的来源内 (context_problems)."""
    standards = LIBRARY.load("REV-02").standards
    for invoice_id, jctx in revenue.contexts.items():
        value = json.loads((XERO.fixtures / "REV-02" / f"{invoice_id}.json").read_text(encoding="utf-8"))
        assert context_problems(invoice_id, value, jctx, standards) == []


@pytest.mark.external
@pytest.mark.parametrize("where", ["evidence", "fact step"])
@pytest.mark.parametrize(
    "pointer",
    [
        {"source_type": "bank_transaction", "source_id": "BT-090-0085", "locator": "amount_cents"},  # 参考号误写的收款
        {"source_type": "journal_entry", "source_id": "XJ-720-2", "locator": "credit_cents"},  # 上下文外的分录行
        {"source_type": "document", "source_id": "DOC-7795e9312e9f", "locator": "counterparty", "page": 1},  # 合同要素
        {"source_type": "document", "source_id": "DOC-7795e9312e9f", "locator": "CON-X01-T1", "page": 3},  # 页码不对
        {"source_type": "document", "source_id": "DOC-02c5c90d4ffb", "locator": "CON-X02-T1", "page": 2},  # 别的合同
        {"source_type": "sales_invoice", "source_id": ORC["ORC1020"], "locator": "posting_date"},  # 别的发票
        {"source_type": "delivery_note", "source_id": "SR-ORC1021", "locator": "signed_date"},  # 别的发票的服务记录
        {"source_type": "standard", "source_id": "AASB15-P36", "locator": "para 36"},  # 模板 standards 以外的片段
    ],
)
def test_context_guard_reports_pointers_outside_the_context(
    revenue: Revenue, pointer: dict[str, Any], where: str
) -> None:
    """自测: ORC1026 的 fixture 副本另引一个上下文以外的来源 (需求点名的 BT-090-0085, XJ-720-2, 合同要素字段,
    页码不对的条款, 别的合同的条款, 别的发票, 别的发票的服务完成记录, 模板以外的准则片段), 守卫报且只报这一条.
    指针放在顶层 evidence 或第一个 fact 步骤的 evidence 各跑一次: 判断引擎不核对推理链的指针是否都在 evidence 里,
    只写在推理链里的越界指针只有守卫拦得下."""
    value = json.loads((XERO.fixtures / "REV-02" / f"{ORC1026}.json").read_text(encoding="utf-8"))
    added = {"type": "voucher", "page": None, "reliability": None, **pointer}
    if where == "evidence":
        value["evidence"].append(added)
    else:
        next(s for s in value["reasoning_chain"] if s["kind"] == "fact")["evidence"].append(added)
    problems = context_problems("ORC1026", value, revenue.contexts[ORC1026], LIBRARY.load("REV-02").standards)
    assert problems == [
        f"ORC1026: {pointer['source_type']} {pointer['source_id']} {pointer['locator']} 不在该发票的上下文中"
    ]


@pytest.mark.external
def test_rev02_confirm_attributes_the_receipt_finding(revenue: Revenue) -> None:
    """验收 7: 审计师确认 ORC1026 的判断 (confirm_effects) 后, R03-1 出第 2 版, 归因于该判断."""
    [j] = [j for j in revenue.rev02["judgments"] if j.subject_id == ORC1026]
    [r03] = [x for x in revenue.rev08["exceptions"] if x.id == "X-REV-08-1-R03-1"]
    effects = confirm_effects(j, [r03])
    [attributed] = [e for e in effects if isinstance(e, AuditException)]
    assert (attributed.id, attributed.version, attributed.disposition, attributed.attributed_by) == (
        "X-REV-08-1-R03-1",
        2,
        ExceptionDisposition.attributed,
        j.id,
    )


# ── RPT-03 两稿: 只开现有 17 条路径的无保留意见 (需求 R117) 由 R118 的分层 mock 排在运行副本之前, 其余程序读运行副本;
# 正本按意见类型各有两稿 (需求 R260): 保留意见供演示走通, 无保留意见同 only17, 供应用里只开现有程序时 ──

# 正本的另一个根; manifest 等同 XERO (补造目录)
ONLY17 = replace(XERO, name="xero_only17", fixtures=XERO.fixtures.with_name("llm_xero_only17"))
# r2 回应退回批注加的一句
FRAMEWORK = (
    "The financial report has been prepared in accordance with Australian Accounting Standards"
    " - Simplified Disclosures. "
)


def xero_report(fs: FixtureSet, opinion: OpinionType, revision: int) -> ReportDraft:
    """RPT-03 第 revision 稿的固定输出 (fixture_key 为 {意见类型}-r{修订号})."""
    path = fs.fixtures / "RPT-03" / f"{opinion.value}-r{revision}.json"
    return ReportDraft.model_validate_json(path.read_text(encoding="utf-8"))


@pytest.mark.external  # document 指针核对取补造的 manifest
def test_only17_fixtures_are_valid() -> None:
    """目录里恰为 RPT-03 无保留意见的两稿 (fixture_key 为 {意见类型}-r{修订号}), 按 fixture_problems 合格: 能按
    RPT-03 的输出模型解析, document 指针在补造 manifest 中且页码不超过页数 (ReportDraft 现在没有指针字段, 防以后加了
    引错). 目录缺失时 fixture_problems 不报, 所以先核对文件清单."""
    assert sorted(f"{p.parent.name}/{p.stem}" for p in ONLY17.fixtures.glob("*/*.json")) == [
        "RPT-03/unqualified-r1",
        "RPT-03/unqualified-r2",
    ]
    assert fixture_problems(ONLY17) == []


@pytest.mark.parametrize(
    ("fs", "opinion", "sources"),
    [
        pytest.param(ONLY17, OpinionType.unqualified, [], id="only17_unqualified"),
        pytest.param(XERO, OpinionType.unqualified, [], id="unqualified"),
        pytest.param(XERO, OpinionType.qualified, [("CSH-02", "BA-091"), ("GEN-02", "XJ-499")], id="qualified"),
    ],
)
@pytest.mark.external  # 单位名称与期末取补造画像与快照
def test_xero_reports(fs: FixtureSet, opinion: OpinionType, sources: list[tuple[str, str]]) -> None:
    """Xero 的 RPT-03 两稿: xero17 走通 (FIN-01 选无保留) 读 only17 的无保留意见; 演示走通 (需求 R260: 两笔未更正
    错报合计超过整体重要性, FIN-01 选保留) 读正本的保留意见; 应用里只开现有程序 (只有 BA-091 一笔错报, FIN-01 选
    无保留) 读正本的无保留意见. r1 经 report_checks (引用与语义两级) 通过: 可引用的
    判断为两笔错报的来源判断 (第 2 版 confirmed), 引用键恰为这些; 单位名称与期末同补造画像与快照; 没有持续经营段与
    其他事项段. r2 回应 RPT-01 的退回批注 (XERO17_ANNOTATION: 基础段写明编制框架): 只在基础段的标准段落前加
    FRAMEWORK 一句, 意见段, 引用与其余文字不变 (同 Numbat r2 的写法)."""
    r1, r2 = xero_report(fs, opinion, 1), xero_report(fs, opinion, 2)
    judgments = [fixture_judgment(pid, subject, AutonomyLevel.L2) for pid, subject in sources]
    ctx = ValidationContext(opinion=opinion, judgments=judgments, standards_of=None)
    assert [report_checks(r, ctx, LIBRARY.report_headings("en")) for r in (r1, r2)] == [(None, [])] * 2
    assert r1.judgment_keys == r2.judgment_keys == [f"{pid}:{subject}" for pid, subject in sources]
    period = LIBRARY.report_date(xero_engagement().period_end, "en")
    assert f"the financial report of {xero_profile().legal_name} (the Company)" in r1.opinion
    assert r1.opinion.count(f"as at {period}") == 2
    for text in (r1.opinion, r1.basis_for_opinion, r2.basis_for_opinion):
        assert "going concern" not in text.lower() and "other matter" not in text.lower()
    assert "Simplified Disclosures" not in r1.basis_for_opinion
    assert r2.basis_for_opinion.count(f"\n\n{FRAMEWORK}We conducted our audit") == 1
    assert r2.opinion == r1.opinion and r2.basis_for_opinion.replace(FRAMEWORK, "") == r1.basis_for_opinion
