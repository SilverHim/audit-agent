import re
from datetime import date
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

from backend.contracts.llm_outputs import ContractExtraction
from backend.contracts.models import (
    AuditObjective,
    EvidencePointer,
    EvidenceType,
    Phase,
    Procedure,
    RiskLevel,
    TermType,
)
from backend.contracts.state import initial_state
from backend.core.config import get_settings
from backend.orchestration.runner import fixed_procedures
from backend.orchestration.scheduler import build_schedule
from backend.services.procedure_library import ProcedureLibrary, highest_risk
from tests.fakes import ENGAGEMENT, NUMBAT_CONTRACTS
from tests.fakes import NUMBAT_OBJECTIVES as OBJECTIVES

ROOT = get_settings().KNOWLEDGE_ROOT
P2 = {"REV-08", "REV-03", "REV-02", "CSH-04", "DOC-06", "CSH-05", "CSH-02"}


def test_all_procedures_load() -> None:
    assert len(ProcedureLibrary(ROOT).all()) == 17


def test_standards_referenced_are_registered() -> None:
    lib = ProcedureLibrary(ROOT)
    missing = {s for t in lib.all() for s in t.standards if lib.fragment(s) is None}
    assert not missing


def test_assembly_and_layers() -> None:
    lib = ProcedureLibrary(ROOT)
    matched = {t.id for o in OBJECTIVES for t in lib.match(o)}
    assembled = {t.id for t in lib.closure(matched) if t.phase is Phase.P2_Executing}
    assert assembled == P2

    pack = fixed_procedures(lib) + [
        Procedure(id=t.id, phase=t.phase, template_commit=lib.commit, autonomy_level=t.autonomy_level)
        for t in lib.all()
        if t.id in P2
    ]
    layers: dict[int, set[str]] = {}
    for row in build_schedule(pack, lib, cap=4):
        layers.setdefault(row.layer, set()).update(row.procedure_ids)
    assert layers == {0: {"REV-08", "REV-03", "CSH-04", "CSH-05"}, 1: {"REV-02", "DOC-06"}, 2: {"CSH-02"}}


def test_prompts_parse() -> None:
    env = Environment(loader=FileSystemLoader(ROOT / "prompts"))
    for name in env.list_templates():
        env.get_template(name)


# ── R01 参数解析: 风险等级取最高, by_risk 与 ref 取不到值时报错 ──


@pytest.mark.parametrize(
    ("lower", "higher"),
    [("low", "medium"), ("medium", "high"), ("high", "significant"), ("low", "significant")],
)
def test_highest_risk_adjacent_levels(lower: str, higher: str) -> None:
    """每对相邻等级都比一次, 两种排列顺序结果相同: 顺序写错任何一处都会失败."""
    objectives = [
        AuditObjective(id="O-1", account="Revenue", assertion="Occurrence", risk_level=lower, is_material=True),
        AuditObjective(id="O-2", account="Revenue", assertion="Cutoff", risk_level=higher, is_material=True),
    ]

    assert highest_risk(objectives, ["O-1", "O-2"]) == higher
    assert highest_risk(objectives, ["O-2", "O-1"]) == higher


def test_highest_risk_takes_max_of_linked_objectives() -> None:
    """按风险高低比, 不按字母序 (字母序下 medium 最大); 未关联的特别风险目标不算."""
    objectives = [
        AuditObjective(id="O-LOW", account="Revenue", assertion="Occurrence", risk_level="low", is_material=False),
        AuditObjective(id="O-HIGH", account="Revenue", assertion="Cutoff", risk_level="high", is_material=True),
        AuditObjective(id="O-MED", account="Cash", assertion="Existence", risk_level="medium", is_material=True),
        AuditObjective(id="O-SIG", account="Cash", assertion="Existence", risk_level="significant", is_material=True),
    ]

    assert highest_risk(objectives, ["O-LOW", "O-HIGH", "O-MED"]) is RiskLevel.high
    assert highest_risk(objectives, []) is None
    with pytest.raises(LookupError, match="O-MISSING"):
        highest_risk(objectives, ["O-LOW", "O-MISSING"])


def test_resolve_params_by_risk_needs_risk_level() -> None:
    """REV-08 的 window_days 按风险取值, 没有风险等级就报错; CSH-04 没有 by_risk 参数, 不需要风险等级."""
    lib = ProcedureLibrary(ROOT)
    state = initial_state(ENGAGEMENT, [], 1)

    with pytest.raises(ValueError, match=r"REV-08\.window_days"):
        lib.resolve_params(lib.load("REV-08"), ENGAGEMENT, state, None)
    assert lib.resolve_params(lib.load("CSH-04"), ENGAGEMENT, state, None) == {"as_of": date(2026, 6, 30)}


def test_resolve_params_by_risk_without_that_level() -> None:
    """REV-02 只给了 medium 以上的取值, 低风险时报错而不是 KeyError."""
    lib = ProcedureLibrary(ROOT)

    with pytest.raises(ValueError, match="low"):
        lib.resolve_params(lib.load("REV-02"), ENGAGEMENT, initial_state(ENGAGEMENT, [], 1), RiskLevel.low)


def test_resolve_params_state_ref_not_set() -> None:
    """重要性还没经闸门确定时执行 FIN-04: 报错, 不以 None 作参数."""
    lib = ProcedureLibrary(ROOT)

    with pytest.raises(ValueError, match="materiality_cents"):
        lib.resolve_params(lib.load("FIN-04"), ENGAGEMENT, initial_state(ENGAGEMENT, [], 1), None)


def test_resolve_params_literals_are_copies() -> None:
    """改解析结果里的列表与字典, 模板不受影响."""
    lib = ProcedureLibrary(ROOT)

    out = lib.resolve_params(lib.load("PLN-01"), ENGAGEMENT, initial_state(ENGAGEMENT, [], 1), None)
    out["candidates"][0]["pct_low"] = 0
    out["candidates"].append({"basis": "x"})

    candidates = lib.load("PLN-01").params["candidates"]
    assert len(candidates) == 3
    assert candidates[0]["pct_low"] == 0.005


# ── R36: DOC-04 fixture ──

DATASET = Path(__file__).resolve().parents[1] / "docs" / "合成数据集设计.md"
DOC04 = get_settings().LLM_FIXTURES_ROOT / "DOC-04"
CONTRACT_PAGES = {  # 合同 -> 合成数据集设计第 6 节中正文所在小节 (起止标题); CON-002 的第 5, 6, 8 条与 CON-001 相同
    "CON-001": ("### 6.1 CON-001.pdf", "### 6.2 CON-002.pdf"),
    "CON-002": ("### 6.1 CON-001.pdf", "### 6.2 CON-002.pdf"),
    "CON-003": ("### 6.3 CON-003.pdf", "### 6.4 "),
}


def clause_text(contract_id: str, number: int) -> tuple[str, int]:
    """合成数据集设计第 6 节合同正文中第 number 条的全文与页码: 含子条款号, 不含标题; 子条款间换行,
    同一子条款内 PDF 的折行以空格相连."""
    doc = DATASET.read_text(encoding="utf-8")
    start, end = CONTRACT_PAGES[contract_id]
    pages = re.findall(r"```\n(.*?)```", doc[doc.index(start) : doc.index(end)], re.S)
    for page, block in enumerate(pages, start=1):
        lines = block.split("\n")
        heading = next((i for i, line in enumerate(lines) if line.startswith(f"{number}. ")), None)
        if heading is None:
            continue
        clauses: list[str] = []
        for line in lines[heading + 1 :]:
            if not line.strip():
                break
            if re.match(r"\d+\.\d+ ", line):
                clauses.append(line)
            else:
                clauses[-1] += f" {line}"
        return "\n".join(clauses), page
    raise LookupError(f"{contract_id} 第 {number} 条")


def load_extraction(contract_id: str) -> ContractExtraction:
    return ContractExtraction.model_validate_json((DOC04 / f"{contract_id}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("contract_id", "header"),
    [
        ("CON-001", ("Bilby Robotics Pty Ltd", "11901043383", 14200000, date(2026, 5, 20))),
        ("CON-002", ("Quoll Automation Pty Ltd", "46902044759", 9500000, date(2026, 5, 25))),
        ("CON-003", ("Galah Retail Electronics Pty Ltd", "20903046122", None, date(2025, 6, 16))),
    ],
)
def test_doc04_fixture_header(contract_id: str, header: tuple[object, ...]) -> None:
    """验收 1-3: 能通过 ContractExtraction 校验; 要素同合成数据集第 6.6 节; 每个非空要素一条第 1 页的证据指针,
    指向该合同文件, locator 为字段名 (CON-003 没有金额指针)."""
    extraction = load_extraction(contract_id)

    assert (extraction.counterparty, extraction.counterparty_abn, extraction.amount_cents, extraction.sign_date) == (
        header
    )
    fields = ["counterparty", "counterparty_abn", "amount_cents", "sign_date"]
    present = [f for f, value in zip(fields, header, strict=True) if value is not None]
    document_id = next(d.id for d in NUMBAT_CONTRACTS if d.file_name == f"{contract_id}.pdf")
    assert extraction.evidence == [
        EvidencePointer(type=EvidenceType.contract, source_type="document", source_id=document_id, locator=f, page=1)
        for f in present
    ]


@pytest.mark.parametrize(
    ("contract_id", "terms"),
    [
        ("CON-001", [(TermType.control_transfer, 5), (TermType.payment, 6), (TermType.termination, 8)]),
        ("CON-002", [(TermType.control_transfer, 5), (TermType.payment, 6), (TermType.termination, 8)]),
        (
            "CON-003",
            [
                (TermType.payment, 3),
                (TermType.control_transfer, 4),
                (TermType.return_right, 5),
                (TermType.rebate, 6),
                (TermType.termination, 8),
            ],
        ),
    ],
)
def test_doc04_fixture_terms(contract_id: str, terms: list[tuple[TermType, int]]) -> None:
    """验收 4: 条款依次为第 6.6 节所列类型; 正文与第 6.1-6.3 节原文逐字一致, 页码为该条所在页."""
    extraction = load_extraction(contract_id)

    assert [(t.term_type, t.text, t.page) for t in extraction.terms] == [
        (term_type, *clause_text(contract_id, number)) for term_type, number in terms
    ]


def test_doc04_fixture_set() -> None:
    """验收 5: CON-001 第 7 条标准质保不列为 guarantee; DOC-04 目录下只有三份合同的 fixture, 没有 default.json."""
    assert all(t.term_type is not TermType.guarantee for t in load_extraction("CON-001").terms)
    assert sorted(p.name for p in DOC04.iterdir()) == ["CON-001.json", "CON-002.json", "CON-003.json"]
