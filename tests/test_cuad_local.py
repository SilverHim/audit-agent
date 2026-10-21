"""本地 CUAD 原件的核对 (需求 R248): 文件名映射, 金标准 PDF 与两个标注文件的哈希, 入库逐页文本与 PDF 一致, 金标准的
定位页与条款锚点, 七类合同的全量定位统计 (slow). 经 require_external 定位 data/external/cuad (取数见该目录的 README),
没有时跳过; 只读, 不改原件. 摘录与条款出自 CUAD v1 (The Atticus Project, CC BY 4.0), 署名见
tests/fixtures/cuad/README.md."""

import functools
from collections import Counter
from pathlib import Path

import pytest

from tests.external import require_external
from tests.tools.cuad_eval import (
    LOCATED_CATEGORIES,
    PAGE_FIXTURES,
    SUMS,
    contract_key,
    csv_rows,
    excerpts,
    load_gold,
    load_pages,
    locate,
    pdf_files,
    pdf_pages,
    segment_location,
    split_omitted,
    starts_on,
    sums,
)

pytestmark = pytest.mark.external
# 七类合同的份数 (目录名; cuad.md 第 2 节, 与 CUAD README 的类型计数一致)
TYPES = {
    "Distributor": 32,
    "Supply": 18,
    "Reseller": 12,
    "Manufacturing": 17,
    "Service": 28,
    "Maintenance": 34,
    "License_Agreements": 33,
}
# 七类 174 份上 LOCATED_CATEGORIES 各段摘录的定位 (现状实算; 段里没有字母数字的 8 段不计)
LOCATED = {"single": 2006, "multi": 713}
LIMITS = {"cross": 7, "missing": 8}  # 跨页与找不到的上限


@pytest.fixture(scope="module")
def cuad() -> Path:
    """本地 CUAD 的 CUAD_v1/ 目录."""
    files = ["CUAD_v1/master_clauses.csv", "CUAD_v1/CUAD_v1.json", "CUAD_v1/full_contract_pdf"]
    return require_external("cuad", files) / "CUAD_v1"


@functools.cache
def pages_of(path: Path) -> list[str]:
    """PDF 的逐页文本 (pdf_pages, 同 FileEvidenceStore.pages), 本模块内每份只抽一次."""
    return pdf_pages(path)


def test_file_names_map_one_to_one(cuad: Path) -> None:
    """验收 2: master_clauses.csv 510 行与 510 个 PDF 按 contract_key 一一对应 (NFC, 去扩展名与首尾空白, & 与 ' 换成
    _, Kallo 与 Harpoon 特判); 七类合同的份数同 TYPES, 共 174 份."""
    rows, pdfs = csv_rows(cuad), pdf_files(cuad)
    assert (len(rows), len(pdfs)) == (510, 510)
    assert set(rows) == set(pdfs)
    assert Counter(p.parent.name for p in pdfs.values() if p.parent.name in TYPES) == TYPES


def test_hashes_match_sums(cuad: Path) -> None:
    """验收 3: 金标准 10 份 PDF 与 master_clauses.csv, CUAD_v1.json 的 sha256 等于入库的 SHA256SUMS (不等时按行列出
    差异)."""
    assert sums(cuad) == SUMS.read_text(encoding="utf-8")


@pytest.mark.parametrize("alias", PAGE_FIXTURES)
def test_page_fixtures_match_pdfs(cuad: Path, alias: str) -> None:
    """验收 3: 入库的逐页文本与 pdfplumber 从原 PDF 抽出的逐页相等 (不等时报出页号)."""
    committed = load_pages(alias)["pages"]
    extracted = pages_of(cuad / "full_contract_pdf" / load_gold()[alias].pdf)
    assert len(extracted) == len(committed)
    assert [n for n, (a, b) in enumerate(zip(extracted, committed, strict=True), 1) if a != b] == []


@pytest.mark.parametrize("alias", list(load_gold()))
def test_gold_locations(cuad: Path, alias: str) -> None:
    """验收 4: 金标准的 CSV 行与 PDF 互相对应, 页数同 PDF; Parties 与 Agreement Date 的摘录都能定位; 对方的别名都是
    Parties 摘录, counterparty_pages 即别名定位到的页 (接受页集合); Agreement Date 定位到的页都在 sign_date_pages 里;
    货物合同的条款锚点都从所记的页开始 (含没有入库逐页文本的 CUAD-02, 04, 06)."""
    contract = load_gold()[alias]
    key = contract_key(contract.csv_filename)
    row = csv_rows(cuad)[key]
    assert pdf_files(cuad)[key] == cuad / "full_contract_pdf" / contract.pdf
    pages = pages_of(cuad / "full_contract_pdf" / contract.pdf)
    assert len(pages) == contract.page_count
    parties, dates = excerpts(row, "Parties"), excerpts(row, "Agreement Date")
    assert [e for e in parties + dates if not locate(pages, e)] == []
    assert set(contract.counterparty_aliases) <= set(parties)
    assert contract.counterparty_pages == sorted({n for a in contract.counterparty_aliases for n in locate(pages, a)})
    assert {n for e in dates for n in locate(pages, e)} <= set(contract.sign_date_pages)
    clauses = [c for found in (contract.terms or {}).values() for c in found]
    assert [c.clause for c in clauses if not starts_on(pages, c.page, c.anchor)] == []


@pytest.mark.slow
def test_located_categories_over_seven_types(cuad: Path) -> None:
    """验收 5: 七类 174 份上 LOCATED_CATEGORIES 各段摘录 (按 <omitted> 拆开) 的定位: 只在一页与多页的段数同 LOCATED,
    跨页与找不到的不超过 LIMITS. 不符时打印各类的计数与跨页, 找不到的段."""
    rows = csv_rows(cuad)
    counts: Counter[str] = Counter()
    by_category: dict[str, Counter[str]] = {c: Counter() for c in LOCATED_CATEGORIES}
    unlocated = []
    for key, path in pdf_files(cuad).items():
        if path.parent.name not in TYPES:
            continue
        pages = pages_of(path)
        for category in LOCATED_CATEGORIES:
            for excerpt in excerpts(rows[key], category):
                for segment in split_omitted(excerpt):
                    found = segment_location(pages, segment)
                    if found is None:
                        continue
                    counts[found] += 1
                    by_category[category][found] += 1
                    if found in LIMITS:
                        unlocated.append((found, path.name, category, segment[:80]))
    summary = {c: dict(v) for c, v in by_category.items()}
    assert {k: counts[k] for k in LOCATED} == LOCATED, (summary, unlocated)
    assert all(counts[k] <= limit for k, limit in LIMITS.items()), (summary, unlocated)
