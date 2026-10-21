"""CUAD 评测的评分与反编造函数, 入库的逐页文本, DOC-04 离线 mock 输出 (需求 R246) 与金标准的自洽 (需求 R247). 只读
tests/fixtures/cuad, 不读 data/external, 不联网, 默认就跑. 文中的摘录与条款出自 CUAD v1 (The Atticus Project,
CC BY 4.0), 署名见 tests/fixtures/cuad/README.md."""

import json
from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError

from backend.agents.ingest import build_contract
from backend.contracts.llm_outputs import ContractExtraction, ExtractedTerm
from backend.contracts.models import DocumentRef
from backend.contracts.ports import LLMResponse
from backend.contracts.services import ValidationContext
from backend.core.config import get_settings
from backend.services.judgment_engine import OutputValidator
from backend.services.procedure_library import ProcedureLibrary
from tests.fakes import NOW, FakeFinancialStore, FakeTrail
from tests.tools.cuad_eval import (
    FIXTURES,
    GOLD,
    GOODS_TERMS,
    LLM,
    PAGE_FIXTURES,
    PAGES,
    SUMS,
    GoldContract,
    fabricated,
    gold_by_alias,
    load_gold,
    load_pages,
    locate,
    normalize_party,
    parse_cuad_date,
    same_party,
    split_omitted,
    starts_on,
)
from tests.tools.cuad_fetch import ATTRIBUTION, DISCLAIMER, LICENSE_URL, RECORD_URL

LIBRARY = ProcedureLibrary(get_settings().KNOWLEDGE_ROOT)
SIGN_DATE = "November 5, 1996"  # CUAD-01 (NETGEAR) 在 master_clauses.csv 的 Agreement Date 摘录


def mock_output(alias: str) -> dict[str, object]:
    value: dict[str, object] = json.loads((LLM / "DOC-04" / f"{alias}.json").read_text(encoding="utf-8"))
    return value


def document(alias: str) -> DocumentRef:
    """别名为 alias 的合同 PDF 登记 (文件 ID 即别名, 页数与哈希取逐页文本 fixture)."""
    pages = load_pages(alias)
    return DocumentRef(
        id=alias,
        type="contract",
        file_name=f"{alias}.pdf",
        sha256=pages["pdf_sha256"],
        page_count=pages["page_count"],
        reliability="external_copy",
        status="received",
        uploaded_at=NOW,
    )


async def validate(alias: str, value: object) -> object:
    """DOC-04 输出经 OutputValidator 四级校验 (本次抽取的文件为 document(alias)), 返回校验报告."""
    validator = OutputValidator(FakeFinancialStore(), LIBRARY, FakeTrail())
    response = LLMResponse[ContractExtraction](  # type: ignore[valid-type]
        output=None, raw=json.dumps(value), model="mock", input_tokens=0, output_tokens=0, error=None
    )
    ctx = ValidationContext(document=document(alias), standards_of=None)
    return await validator.validate("ENG", LIBRARY.load("DOC-04"), response, ctx)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("11/5/96", date(1996, 11, 5)),
        ("7/7/99", date(1999, 7, 7)),
        ("10/24/05", date(2005, 10, 24)),
        ("05/[]/2000", None),  # 遮盖的不计
        ("3/1/96", date(1996, 3, 1)),
        ("2/30/01", None),  # 不是有效日期
    ],
)
def test_parse_cuad_date(text: str, expected: date | None) -> None:
    """验收 1: m/d/yy, 两位年份 >= 90 补 19yy, 其余 20yy; 含 "[]" 或无效的为 None."""
    assert parse_cuad_date(text) == expected


def test_party_names_and_omitted_segments() -> None:
    """验收 2: 去括号里的定义名与公司后缀后, 'Ingram Micro ("Distributor")' 与 'INGRAM MICRO INC.' 为同一方, 与
    NETGEAR 不是; 相似度 >= 0.9 也算 (少一个字母); 摘录按 <omitted> 拆段, 去空段."""
    assert normalize_party('Ingram Micro ("Distributor")') == normalize_party("INGRAM MICRO INC.") == "ingram micro"
    assert same_party('Ingram Micro ("Distributor")', "INGRAM MICRO INC.")
    assert same_party("Electric City of Illinois L.L.C.", "Electric City of Illinois LLC")
    assert same_party("Cremer Oleo GmbH & Co KG", "Cremer Olio GmbH")  # 相似度约 0.92
    assert not same_party('Ingram Micro ("Distributor")', "NETGEAR, Inc.")
    assert not same_party("Inc.", "Ltd")  # 只剩后缀的不算
    assert split_omitted("First part. <omitted> Second part.<omitted> ") == ["First part.", "Second part."]


def test_locate_and_fabricated() -> None:
    """验收 3: CUAD-01 的签订日摘录只定位到第 1 页; 第 3 页的 5.D 条款 (所有权与风险转移) 声明在第 3 页时不算编造,
    声明第 2 页时算; 跨页条款 (CUAD-03 的 2.3 交货, 从第 3 页延续到第 4 页) 声明起始页不算编造, 声明第 4 页算; 两份
    mock 输出的全部条款都不算编造."""
    netgear, lime = load_pages("CUAD-01")["pages"], load_pages("CUAD-03")["pages"]
    assert locate(netgear, SIGN_DATE) == [1]
    terms = [ExtractedTerm.model_validate(t) for t in mock_output("CUAD-01")["terms"]]  # type: ignore[attr-defined]
    title = next(t for t in terms if t.text.startswith("D. TITLE, RISK OF LOSS"))
    assert title.page == 3 and not fabricated(title, netgear)
    assert fabricated(title.model_copy(update={"page": 2}), netgear)
    assert fabricated(title.model_copy(update={"page": 9}), netgear)  # 越界
    delivery = next(
        ExtractedTerm.model_validate(t)
        for t in mock_output("CUAD-03")["terms"]  # type: ignore[attr-defined]
        if t["text"].startswith("2.3 Delivery.")
    )
    assert delivery.page == 3 and not fabricated(delivery, lime)
    assert fabricated(delivery.model_copy(update={"page": 4}), lime)
    for alias, pages in (("CUAD-01", netgear), ("CUAD-03", lime)):
        found = [ExtractedTerm.model_validate(t) for t in mock_output(alias)["terms"]]  # type: ignore[attr-defined]
        assert [t.term_type for t in found if fabricated(t, pages)] == [], alias


async def test_validator_and_contract() -> None:
    """验收 4: CUAD-01 的 mock 输出原样通过四级校验, 经 build_contract 得到 ABN 与金额都为空的合同 (编号, 文件 ID
    为别名); CUAD-03 (page_count 10) 的 mock 原样通过, 把一条条款的页码写成印刷页码 14 时判 reference 级失败."""
    report = await validate("CUAD-01", mock_output("CUAD-01"))
    assert (report.level_failed, report.errors) == (None, [])  # type: ignore[attr-defined]
    extraction = report.output  # type: ignore[attr-defined]
    assert isinstance(extraction, ContractExtraction)
    contract = build_contract("CUAD-01", document("CUAD-01"), extraction, "RUN-DOC-04-1")
    assert (contract.id, contract.document_id, contract.counterparty) == ("CUAD-01", "CUAD-01", "Ingram Micro")
    assert (contract.counterparty_abn, contract.amount_cents, contract.sign_date) == (None, None, date(1996, 11, 5))
    assert [t.id for t in contract.terms] == [f"CUAD-01-T{n}" for n in range(1, 7)]
    assert (await validate("CUAD-03", mock_output("CUAD-03"))).level_failed is None  # type: ignore[attr-defined]
    printed = mock_output("CUAD-03")
    printed["terms"][2]["page"] = 14  # type: ignore[index]
    failed = await validate("CUAD-03", printed)
    assert failed.level_failed == "reference"  # type: ignore[attr-defined]
    assert any("页码 14 不在 1-10 之间" in e for e in failed.errors)  # type: ignore[attr-defined]


def test_readme_credits_every_cuad_file() -> None:
    """验收 5: README 写明署名句, 作者, 数据集名与版本, CC BY 4.0 与链接, Zenodo 链接, CUAD 的免责句, 改动说明, 别名到
    原文件名的映射 (金标准 10 份), 并列出入库的每个含 CUAD 文本的文件; 目录里没有 PDF; 逐页文本即 PAGE_FIXTURES."""
    readme = (FIXTURES / "README.md").read_text(encoding="utf-8")
    for text in (ATTRIBUTION, "The Atticus Project", "CUAD", "v1", LICENSE_URL, RECORD_URL, DISCLAIMER):
        assert text in readme, text
    for change in ("pdfplumber", "节选", "人工标注", "模型生成"):
        assert change in readme, change
    for alias, contract in load_gold().items():
        assert f"| {alias} | `{contract.pdf}` |" in readme, alias
    committed = sorted(p.relative_to(FIXTURES).as_posix() for p in FIXTURES.rglob("*") if p.is_file())
    assert [p for p in committed if p.lower().endswith(".pdf")] == []
    for path in [p for p in committed if p != "README.md"] + ["tests/test_cuad_eval.py"]:
        assert f"`{path}`" in readme, path
    assert sorted(p.stem for p in PAGES.glob("*.json")) == list(PAGE_FIXTURES)
    assert all(load_pages(alias)["page_count"] == len(load_pages(alias)["pages"]) for alias in PAGE_FIXTURES)


# 需求 R247 验收 1: 别名, 类型与页数
GOLD_LIST = [
    ("CUAD-01", "Distributor", 8),
    ("CUAD-02", "Distributor", 16),
    ("CUAD-03", "Distributor", 10),
    ("CUAD-04", "Supply", 13),
    ("CUAD-05", "Supply", 7),
    ("CUAD-06", "Reseller", 5),
    ("CUAD-07", "Manufacturing", 21),
    ("CUAD-08", "Service", 7),
    ("CUAD-09", "Maintenance", 7),
    ("CUAD-10", "License", 12),
]


def test_gold_contracts() -> None:
    """需求 R247 验收 1-3: 10 份依次为 GOLD_LIST; 签订日的三处特例 (LIMEENERGY 只认首段并注明 CUAD 原值, PAXMEDICA 首段
    与签字栏都认, SPARKLING 不计分); 金额都为 null (都没有总价); 货物合同 CUAD-01 至 06 有四类条款与 REV-03 结论,
    Healthcare (06) 为 more_evidence, 其余 accept; Apollo (07) 起只评 DOC-04; 起草状态都在两种之内."""
    gold = load_gold()
    assert [(c.alias, c.category, c.page_count) for c in gold.values()] == GOLD_LIST
    assert (gold["CUAD-03"].sign_date, "7/7/99" in (gold["CUAD-03"].sign_date_note or "")) == ([date(1999, 9, 7)], True)
    assert gold["CUAD-08"].sign_date == [date(2018, 5, 25), date(2008, 5, 25)]
    assert gold["CUAD-09"].sign_date is None
    assert {c.amount_cents for c in gold.values()} == {None}
    assert {a: c.rev03 for a, c in gold.items() if c.rev03} == {
        "CUAD-01": "accept",
        "CUAD-02": "accept",
        "CUAD-03": "accept",
        "CUAD-04": "accept",
        "CUAD-05": "accept",
        "CUAD-06": "more_evidence",
    }


@pytest.mark.parametrize("alias", PAGE_FIXTURES)
def test_gold_matches_page_fixtures(alias: str) -> None:
    """需求 R247 验收 4: 有逐页文本的三份, 金标准的原文件, 页数与 PDF 哈希 (SHA256SUMS) 同逐页文本; 每条条款的锚点
    从所记的页开始; 对方出现的页即其别名定位到的页."""
    contract, fixture = load_gold()[alias], load_pages(alias)
    pages = fixture["pages"]
    assert (contract.pdf, contract.page_count) == (fixture["source"], fixture["page_count"])
    assert f"{fixture['pdf_sha256']}  full_contract_pdf/{contract.pdf}\n" in SUMS.read_text(encoding="utf-8")
    clauses = [c for found in (contract.terms or {}).values() for c in found]
    assert clauses and [c.clause for c in clauses if not starts_on(pages, c.page, c.anchor)] == []
    assert contract.counterparty_pages == sorted({n for a in contract.counterparty_aliases for n in locate(pages, a)})


def gold_entry(**changes: Any) -> dict[str, Any]:
    """金标准 CUAD-01 一条的 JSON 形式, 改 changes 中的字段."""
    [entry] = [e for e in json.loads(GOLD.read_text(encoding="utf-8")) if e["alias"] == "CUAD-01"]
    return entry | changes


@pytest.mark.parametrize(
    "changes",
    [{"sign_date": []}, {"rev03": None}, {"terms": None}, {"terms": {t: [] for t in GOODS_TERMS[:3]}}],
    ids=["empty_sign_date", "terms_without_rev03", "rev03_without_terms", "missing_term_type"],
)
def test_gold_rejects_inconsistent_entries(changes: dict[str, Any]) -> None:
    """需求 R247 验收 4: 签订日集合为空 (不计分须写 null), 四类条款与 REV-03 结论缺一, 或少一类条款时读不进来."""
    GoldContract.model_validate(gold_entry())
    with pytest.raises(ValidationError):
        GoldContract.model_validate(gold_entry(**changes))


def test_gold_aliases_unique() -> None:
    """需求 R247 验收 4: 别名重复时读不进来."""
    first = GoldContract.model_validate(gold_entry())
    with pytest.raises(ValueError, match="别名重复"):
        gold_by_alias([first, first])
