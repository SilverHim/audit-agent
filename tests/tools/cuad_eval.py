"""CUAD 合同评测的评分与反编造函数 (需求 R246; docs/云端开发/design/external/cuad.md 第 3, 4 节), 与入库的逐页文本.

- parse_cuad_date: CUAD CSV 的签订日 (m/d/yy, yy >= 90 补 19yy), 含遮盖 "[]" 的不计.
- normalize_party, same_party: 当事方名称的规范化与比对 (去公司后缀与括号里的定义名, difflib 相似度 >= 0.9 或互相包含).
- split_omitted, anchor, locate: CUAD 摘录按 <omitted> 拆段, 取前 60 个字母数字作锚点, 在逐页文本里定位页码.
- fabricated: 反编造检查: 条款文本的锚点不从它声明的那一页开始 (只在评测里算, 不进正式校验).
- 逐页文本: tests/fixtures/cuad/pages/<别名>.json, 由 pdf_pages 从 data/external/cuad 的 PDF 抽出 (同
  FileEvidenceStore.pages 的 pdfplumber 调用); 重生成: uv run python -m tests.tools.cuad_eval
  [--cuad data/external/cuad]. 测试只读入库的 JSON, 不读 data/external.

合同 ID 与文件 ID 用别名 CUAD-nn: 原文件名含逗号, 空格与 #. 别名到原文件名见 ALIASES 与 tests/fixtures/cuad/README.md.
"""

import argparse
import difflib
import hashlib
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

from backend.contracts.llm_outputs import ExtractedTerm

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "cuad"
PAGES = FIXTURES / "pages"  # 逐页文本: <别名>.json
LLM = FIXTURES / "llm"  # 离线 mock 输出: <程序>/<别名>.json (不放 tests/fixtures/llm, 免得弄红 Numbat 的守卫)
# 别名 → CUAD_v1/full_contract_pdf 下的相对路径 (master_clauses.csv 的 Filename 为其文件名)
ALIASES = {
    "CUAD-01": "Part_II/Distributor/NETGEAR,INC_04_21_2003-EX-10.16-DISTRIBUTOR AGREEMENT.pdf",
    "CUAD-03": "Part_III/Distributor/LIMEENERGYCO_09_09_1999-EX-10-DISTRIBUTOR AGREEMENT.PDF",
    "CUAD-05": "Part_III/Supply/ULTRAGENYXPHARMACEUTICALINC_12_23_2013-EX-10.9-SUPPLY AGREEMENT.PDF",
}
ANCHOR = 60  # 锚点取的字母数字个数 (cuad.md 第 2 节)
SIMILAR = 0.9  # 当事方名称相似度门槛
# 当事方名称里去掉的公司后缀 (规范化后按整词比对; d/b/a 规范化后为 "d b a")
SUFFIXES = {"inc", "corp", "corporation", "llc", "ltd", "limited", "co", "pty", "plc", "lp", "llp", "gmbh", "kg"}
DATE = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})\s*$")


def parse_cuad_date(text: str) -> date | None:
    """CUAD 的签订日 (Agreement Date-Answer 等, m/d/yy): 两位年份 >= 90 补 19yy, 其余补 20yy (数据只有 1990-2020);
    四位年份原样. 含遮盖 "[]" 或不是这种写法的为 None (不计分)."""
    found = DATE.match(text)
    if found is None:
        return None
    month, day, year = (int(part) for part in found.groups())
    if len(found.group(3)) == 2:
        year += 1900 if year >= 90 else 2000
    try:
        return date(year, month, day)
    except ValueError:
        return None


def normalize_party(name: str) -> str:
    """当事方名称的规范化: 去括号里的定义名 (如 ("Distributor")), 小写, 非字母数字换成空格, 去公司后缀词."""
    text = re.sub(r"\([^)]*\)", " ", name).lower()
    words = re.sub(r"[^0-9a-z]+", " ", text).split()
    return " ".join(w for w in words if w not in SUFFIXES)


def same_party(a: str, b: str) -> bool:
    """两个名称是否指同一方: 规范化后 difflib 相似度 >= SIMILAR, 或一方包含另一方 (都不为空)."""
    x, y = normalize_party(a), normalize_party(b)
    if not x or not y:
        return False
    return x in y or y in x or difflib.SequenceMatcher(None, x, y).ratio() >= SIMILAR


def split_omitted(excerpt: str) -> list[str]:
    """CUAD 摘录按 <omitted> 拆成不连续的片段, 去首尾空白, 去空段."""
    return [part.strip() for part in excerpt.split("<omitted>") if part.strip()]


def alnum(text: str) -> str:
    """只留字母数字, 小写 (定位与比对用, 不受换行, 空白与标点影响)."""
    return re.sub(r"[^0-9a-z]", "", text.lower())


def anchor(text: str) -> str:
    """文本的锚点: 去非字母数字后的前 ANCHOR 个字符."""
    return alnum(text)[:ANCHOR]


def locate(pages: list[str], excerpt: str) -> list[int]:
    """摘录 (首段) 的锚点出现在哪些页 (从 1 起, 升序); 没找到为空."""
    parts = split_omitted(excerpt)
    if not parts:
        return []
    key = anchor(parts[0])
    return [n for n, page in enumerate(pages, 1) if key and key in alnum(page)]


def fabricated(term: ExtractedTerm, pages: list[str]) -> bool:
    """反编造检查: 条款文本的锚点不从它声明的那一页开始 (页码越界也算). 跨页的条款, 锚点可延续到下一页, 但须从声明页
    开始. 文本没有字母数字时算编造."""
    key = anchor(term.text)
    if not key or not 1 <= term.page <= len(pages):
        return True
    page = alnum(pages[term.page - 1])
    found = (page + alnum(pages[term.page]) if term.page < len(pages) else page).find(key)
    return not 0 <= found < len(page)


def pdf_pages(path: Path) -> list[str]:
    """PDF 文本层各页, 同 FileEvidenceStore.pages (pdfplumber, 没有文本的页为空串)."""
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        return [page.extract_text() or "" for page in pdf.pages]


def page_fixture(alias: str, pdf: Path) -> dict[str, Any]:
    """一份逐页文本 fixture: 别名, 原文件 (相对 full_contract_pdf), PDF 的 sha256, 页数与各页文本."""
    pages = pdf_pages(pdf)
    return {
        "alias": alias,
        "source": ALIASES[alias],
        "pdf_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
        "page_count": len(pages),
        "pages": pages,
    }


def load_pages(alias: str) -> dict[str, Any]:
    """入库的逐页文本 fixture."""
    value: dict[str, Any] = json.loads((PAGES / f"{alias}.json").read_text(encoding="utf-8"))
    return value


def main() -> None:
    """从 data/external/cuad 的 PDF 重生成 tests/fixtures/cuad/pages 下的逐页文本 (PDF 只当数据读)."""
    parser = argparse.ArgumentParser(description="CUAD 逐页文本 fixture")
    parser.add_argument("--cuad", type=Path, default=REPO / "data" / "external" / "cuad")
    args = parser.parse_args()
    root = args.cuad / "CUAD_v1" / "full_contract_pdf"
    PAGES.mkdir(parents=True, exist_ok=True)
    for alias, relative in ALIASES.items():
        value = page_fixture(alias, root / relative)
        text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        (PAGES / f"{alias}.json").write_text(text, encoding="utf-8")
        print(alias, value["page_count"])


if __name__ == "__main__":
    main()
