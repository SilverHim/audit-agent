import re
from datetime import date, datetime
from functools import cache
from pathlib import Path
from typing import Any

import pdfplumber
import pytest
import yaml
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
    assert len(ProcedureLibrary(ROOT).all()) == 18  # Demo 的 17 条加 GEN-09 (A7 独立复核)


def test_standards_referenced_are_registered() -> None:
    lib = ProcedureLibrary(ROOT)
    missing = {s for t in lib.all() for s in t.standards if lib.fragment(s) is None}
    assert not missing


REFERENCE = Path(__file__).parents[1] / "docs" / "参考资料"  # 准则原文 PDF
STANDARD_PDFS = {  # 准则 YAML 文件名 -> 原文 PDF
    "AASB15": "03_会计准则_AASB/AASB_15.pdf",
    "ASA320": "01_审计准则_ASA/ASA_320.pdf",
    "ASA450": "01_审计准则_ASA/ASA_450.pdf",
    "ASA505": "01_审计准则_ASA/ASA_505.pdf",
    "ASA700": "01_审计准则_ASA/ASA_700.pdf",
    "ASA705": "01_审计准则_ASA/ASA_705.pdf",
}
EXCERPTED = [
    "AASB15-P31",
    "AASB15-P38",
    "ASA320-P10",
    "ASA320-P11",
    "ASA450-P05",
    "ASA450-P11",
    "ASA505-P07",
    "ASA505-P14",
    "ASA700-P23",
    "ASA700-P28",
    "ASA705-P07",
    "ASA705-P08",
    "ASA705-P16",
    "ASA705-P20",
    "ASA705-P21",
]
# 页眉页脚行: "ASA 505 – compiled - 10 - AUDITING STANDARD", "AASB 15-compiled 21 STANDARD",
# "Authorised Version F2023C00402 registered 03/05/2023", 页首 "Auditing Standard ASA 505"
RUNNING_LINE = re.compile(
    r"(?:ASA|AASB) \d+\s*[-–]\s*compiled\b.*STANDARD|Authorised Version \S+ registered \S+|Auditing Standard ASA \d+"
)


def normalise(text: str) -> str:
    """去掉 (Ref: Para. ...) 交叉引用, 行尾连字符直接接下一行 (bill-and-hold), 空白归一为单个空格."""
    text = re.sub(r"\s*\(Ref: ?Paras?\.[^)]*\)", "", text)
    text = re.sub(r"-\n", "-", text)
    return " ".join(text.split())


def standard_doc(name: str) -> dict[str, Any]:
    return yaml.safe_load((ROOT / "standards" / f"{name}.yaml").read_text(encoding="utf-8"))


@cache
def standard_text(name: str) -> str:
    """逐页抽准则 PDF 的文本层, 去掉页眉页脚行与页首的准则标题行后归一.

    本准则摘录的片段都已出现后再多读一页即停 (片段之后的下一段可能在下一页), 免读整份.
    """
    title = standard_doc(name)["title"]
    wanted = [normalise(f["text"]) for f in standard_doc(name)["fragments"] if f["text"]]
    lines: list[str] = []
    found_at: int | None = None
    with pdfplumber.open(REFERENCE / STANDARD_PDFS[name]) as pdf:
        for i, page in enumerate(pdf.pages):
            page_lines = (page.extract_text() or "").splitlines()
            lines += [line for line in page_lines if not RUNNING_LINE.fullmatch(line) and line != title]
            if found_at is None and all(w in normalise("\n".join(lines)) for w in wanted):
                found_at = i
            if found_at is not None and i > found_at:
                break
    return normalise("\n".join(lines))


@pytest.mark.parametrize("fragment_id", EXCERPTED)
def test_fragment_text_is_verbatim_from_pdf(fragment_id: str) -> None:
    fragment = ProcedureLibrary(ROOT).fragment(fragment_id)
    assert fragment is not None and fragment.text.strip()
    excerpt = normalise(fragment.text)
    source = standard_text(fragment_id.split("-")[0])
    assert excerpt in source
    # 整段: 段号紧挨着正文 (ASA 为 "10. ", AASB 为 "31 "), 正文之后紧接下一段的段号, 中间至多隔一行不带句号的小标题
    after = rf"(?: [A-Z][^.]*?)? {int(fragment.paragraph) + 1}\.? [A-Z(]"
    assert re.search(rf"(?:^| ){re.escape(fragment.paragraph)}\.? {re.escape(excerpt)}{after}", source)


@pytest.mark.parametrize("name", list(STANDARD_PDFS))
def test_compiled_date_matches_pdf(name: str) -> None:
    with pdfplumber.open(REFERENCE / STANDARD_PDFS[name]) as pdf:
        cover = pdf.pages[0].extract_text() or ""
    found = re.search(r"Compilation [Dd]ate: (\d{1,2} \w+ \d{4})", cover)
    assert found is not None
    assert standard_doc(name)["compiled"] == datetime.strptime(found[1], "%d %B %Y").date().isoformat()


def test_normalise_joins_hyphen_and_drops_refs() -> None:
    assert normalise("bill-and-\nhold (Ref: Para. A3-A6)\n  next") == "bill-and-hold next"


@pytest.mark.parametrize(
    "procedure_id", ["PLN-01", "CSH-04", "CSH-02", "REV-03", "REV-02", "FIN-04", "FIN-01", "RPT-03"]
)
def test_procedure_standards_have_text(procedure_id: str) -> None:
    lib = ProcedureLibrary(ROOT)
    template = lib.load(procedure_id)
    assert template.standards
    for fragment_id in template.standards:
        fragment = lib.fragment(fragment_id)
        assert fragment is not None and fragment.text.strip(), fragment_id


REPORT_SECTIONS = ["directors_responsibilities", "auditor_responsibilities", "other_legal_and_regulatory", "signature"]


def report_template() -> dict[str, Any]:
    return yaml.safe_load((ROOT / "templates" / "report.yaml").read_text(encoding="utf-8"))


@cache
def illustration_1a() -> str:
    """ASA 700 [Aus] Illustration 1A 的正文 (编译版第 39-42 页), 去页眉页脚后归一."""
    with pdfplumber.open(REFERENCE / STANDARD_PDFS["ASA700"]) as pdf:
        pages = [pdf.pages[i].extract_text() or "" for i in range(38, 42)]
    assert "[Aus] Illustration 1A:" in pages[0]
    title = standard_doc("ASA700")["title"]
    lines = [line for page in pages for line in page.splitlines() if not RUNNING_LINE.fullmatch(line) and line != title]
    return normalise("\n".join(lines))


@pytest.mark.parametrize("opinion", ["unqualified", "qualified"])
def test_report_template_sections(opinion: str) -> None:
    sections = report_template()[opinion]
    assert list(sections) == REPORT_SECTIONS
    for name in ("directors_responsibilities", "auditor_responsibilities", "signature"):
        assert sections[name].strip(), name
    assert sections["other_legal_and_regulatory"] == ""  # Q14: 签名块另放 signature 段


def test_report_fixed_text_equals_illustration() -> None:
    """董事责任与审计师责任两段拼起来, 与 ASA 700 Illustration 1A 中对应区段逐字相同 (完整, 有序), 区段扣除按约定排除的:
    "说明见 AUASB 网站" 的方括号段与其后两条版式说明 (第 41 段), 以及第 40(b) 段起的上市主体内容."""
    source = illustration_1a()
    body = source[
        source.index("Responsibilities of the Directors for the Financial Report") : source.index(
            "We also provide the directors with a statement"
        )
    ]
    expected = body[: body.index("[A further description")] + body[body.index("As part of an audit in accordance") :]
    sections = report_template()["unqualified"]
    actual = normalise(sections["directors_responsibilities"] + "\n" + sections["auditor_responsibilities"])
    assert actual == expected.strip()


def test_report_template_placeholders_and_listed_entity_content() -> None:
    template = report_template()
    assert template["qualified"] == template["unqualified"]  # ASA 705 Illustration 1A 这几段按 ASA 700 写
    text = "\n".join(template["unqualified"].values())
    assert set(re.findall(r"{(\w+)}", text)) <= {"entity_name", "period_end", "auditor_name"}
    filled = text.format(
        entity_name="Numbat Electronic Components Pty Ltd", period_end="30 June 2026", auditor_name="A"
    )
    assert "{" not in filled and "}" not in filled
    assert "{auditor_name}" in template["unqualified"]["signature"]
    lowered = text.lower()
    for listed_only in (
        "key audit matter",
        "other information",
        "remuneration report",
        "public interest",
        "auasb.gov.au",
    ):
        assert listed_only not in lowered, listed_only


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
