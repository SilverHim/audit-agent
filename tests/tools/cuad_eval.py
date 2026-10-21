"""CUAD 合同评测的评分与反编造函数 (需求 R246; docs/云端开发/design/external/cuad.md 第 3, 4 节), 与入库的逐页文本.

- parse_cuad_date: CUAD CSV 的签订日 (m/d/yy, yy >= 90 补 19yy), 含遮盖 "[]" 的不计.
- normalize_party, same_party: 当事方名称的规范化与比对 (去公司后缀与括号里的定义名, difflib 相似度 >= 0.9 或互相包含).
- split_omitted, anchor, locate: CUAD 摘录按 <omitted> 拆段, 取前 60 个字母数字作锚点, 在逐页文本里定位页码.
- starts_on, fabricated: 锚点是否从某页开始; 反编造检查: 条款文本的锚点不从它声明的那一页开始 (只在评测里算, 不进
  正式校验).
- contract_key, csv_rows, pdf_files, excerpts, segment_location (需求 R248): 本地 CUAD 的 CSV 行与 PDF 按文件名对应,
  CSV 摘录 (Python 列表字符串) 的读取与逐页定位; LOCATED_CATEGORIES 为定位统计的 20 类 (cuad.md 第 1 节列出的类别).
- 金标准 (需求 R247): tests/fixtures/cuad/gold.json, 10 份合同 (GoldContract, load_gold), 开发者逐份读 PDF 起草, 用户
  抽查过的标 "用户抽查"; SHA256SUMS 记这 10 份 PDF 与 master_clauses.csv, CUAD_v1.json 的 sha256.
- 逐页文本: tests/fixtures/cuad/pages/<别名>.json (PAGE_FIXTURES 三份), 由 pdf_pages 从 data/external/cuad 的 PDF
  抽出 (同 FileEvidenceStore.pages 的 pdfplumber 调用); 重生成逐页文本与 SHA256SUMS: uv run python -m
  tests.tools.cuad_eval [--cuad data/external/cuad]. 测试只读入库的文件, 不读 data/external.

合同 ID 与文件 ID 用别名 CUAD-nn: 原文件名含逗号, 空格与 #. 别名到原文件见 gold.json 的 pdf 与
tests/fixtures/cuad/README.md.
"""

import argparse
import ast
import csv
import difflib
import hashlib
import json
import re
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, TypeAdapter, model_validator

from backend.contracts.llm_outputs import ExtractedTerm
from backend.contracts.models import TermType

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "cuad"
PAGES = FIXTURES / "pages"  # 逐页文本: <别名>.json
LLM = FIXTURES / "llm"  # 离线 mock 输出: <程序>/<别名>.json (不放 tests/fixtures/llm, 免得弄红 Numbat 的守卫)
GOLD = FIXTURES / "gold.json"  # 金标准 (需求 R247)
SUMS = FIXTURES / "SHA256SUMS"  # 金标准 PDF 与两个标注文件的 sha256, 路径相对 CUAD_v1/ (sha256sum 的格式)
PAGE_FIXTURES = ("CUAD-01", "CUAD-03", "CUAD-05")  # 入库逐页文本的合同
ANNOTATIONS = ("master_clauses.csv", "CUAD_v1.json")  # SHA256SUMS 里 PDF 以外的两个文件
# 货物合同金标准的四类条款: CUAD 没有标注, 人工补标
GOODS_TERMS = (TermType.payment, TermType.control_transfer, TermType.return_right, TermType.rebate)
ANCHOR = 60  # 锚点取的字母数字个数 (cuad.md 第 2 节)
SIMILAR = 0.9  # 当事方名称相似度门槛
# 当事方名称里去掉的公司后缀 (规范化后按整词比对; d/b/a 规范化后为 "d b a")
SUFFIXES = {"inc", "corp", "corporation", "llc", "ltd", "limited", "co", "pty", "plc", "lp", "llp", "gmbh", "kg"}
DATE = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})\s*$")
CROSS_TAIL = 40  # 首段锚点哪页都找不到时, 用末尾这么多个字母数字判断是否跨页
# 定位统计的 20 类摘录 (cuad.md 第 1 节与本系统字段对照时列出的类别)
LOCATED_CATEGORIES = (
    "Parties",
    "Agreement Date",
    "Effective Date",
    "Expiration Date",
    "Renewal Term",
    "Notice Period To Terminate Renewal",
    "Governing Law",
    "Termination For Convenience",
    "Post-Termination Services",
    "Warranty Duration",
    "Most Favored Nation",
    "Price Restrictions",
    "Revenue/Profit Sharing",
    "Minimum Commitment",
    "Volume Restriction",
    "Affiliate License-Licensor",
    "Affiliate License-Licensee",
    "Cap On Liability",
    "Uncapped Liability",
    "Liquidated Damages",
)
HARPOON = "_Option Agreement"  # Harpoon 那份 PDF 的文件名比 CSV 多出的后缀


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


def starts_on(pages: list[str], page: int, key: str) -> bool:
    """锚点 key 是否从第 page 页开始: 跨页的条款, 锚点可延续到下一页, 但须从这一页开始. 页码越界或 key 为空为假."""
    if not key or not 1 <= page <= len(pages):
        return False
    text = alnum(pages[page - 1])
    found = (text + alnum(pages[page]) if page < len(pages) else text).find(key)
    return 0 <= found < len(text)


def fabricated(term: ExtractedTerm, pages: list[str]) -> bool:
    """反编造检查: 条款文本的锚点不从它声明的那一页开始 (页码越界, 文本没有字母数字也算)."""
    return not starts_on(pages, term.page, anchor(term.text))


class GoldClause(BaseModel):
    """金标准的一条条款: 条款号 (给人看), 起始页, 原文锚点 (从条款正文开头取, 不含标题; 见 anchor)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    clause: str
    page: int
    anchor: str


class GoldContract(BaseModel):
    """一份合同的金标准 (需求 R247 验收 1-3). 开发者逐份读 PDF 起草 (review "开发者起草"), 用户抽查过的改为
    "用户抽查"."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    alias: str
    review: Literal["开发者起草", "用户抽查"]
    csv_filename: str  # master_clauses.csv 的 Filename
    pdf: str  # CUAD_v1/full_contract_pdf 下的相对路径
    category: str  # CUAD 的合同类型 (目录名)
    page_count: int
    client_name: str  # 卖方 (被审计单位, DOC-04 的 client_name), 逐份定: 申报公司不一定是卖方
    counterparty: str  # 对方
    counterparty_aliases: list[str]  # CSV Parties 摘录中指对方的 (名称比对的别名)
    counterparty_pages: list[int]  # 别名出现的页 (Parties 的接受页集合, 由 locate 得出)
    sign_date: list[date] | None  # 可接受的签订日; None 为不计分 (日被遮盖或空着)
    sign_date_pages: list[int]  # 签订日出现的页 (首段与签字栏)
    sign_date_note: str | None  # 与 CSV 不同或取舍的说明
    counterparty_abn: None  # 恒为 null: CUAD 合同都没有 ABN
    amount_cents: int | None  # 合同总价; 框架协议只定单价, 为 null
    terms: dict[TermType, list[GoldClause]] | None  # 货物合同的 GOODS_TERMS 四类 (没有的为空列表); 其余为 None
    rev03: Literal["accept", "more_evidence"] | None  # 货物合同 REV-03 的 proposed_action; 其余为 None
    rev03_note: str | None  # REV-03 结论的依据

    @model_validator(mode="after")
    def consistent(self) -> Self:
        """签订日集合非空或显式 null; 货物合同四类条款与 REV-03 结论都有, 其余都没有."""
        if self.sign_date == []:
            raise ValueError(f"{self.alias}: sign_date 为空集合, 不计分时写 null")
        if (self.terms is None) != (self.rev03 is None):
            raise ValueError(f"{self.alias}: terms 与 rev03 须同时有或同时没有")
        if self.terms is not None and tuple(self.terms) != GOODS_TERMS:
            raise ValueError(f"{self.alias}: terms 须依次为 {', '.join(GOODS_TERMS)}")
        return self


def gold_by_alias(contracts: list[GoldContract]) -> dict[str, GoldContract]:
    """按别名 (原顺序); 别名重复时报错."""
    by_alias = {c.alias: c for c in contracts}
    if len(by_alias) != len(contracts):
        raise ValueError("金标准的别名重复")
    return by_alias


def load_gold() -> dict[str, GoldContract]:
    """入库的金标准, 按别名 (文件中的顺序)."""
    return gold_by_alias(TypeAdapter(list[GoldContract]).validate_json(GOLD.read_bytes()))


def contract_key(name: str) -> str:
    """CSV 的 Filename 与 PDF 文件名对应用的键 (cuad.md 第 0 节): NFC 规范化, 去扩展名 (含 Kallo 一行末尾多出的
    ".pdf'") 与首尾空白, & 与 ' 换成 _; Harpoon 的 PDF 去掉多出的后缀."""
    text = unicodedata.normalize("NFC", name).strip()
    text = re.sub(r"\.pdf'?$", "", text, flags=re.IGNORECASE).strip().removesuffix(HARPOON)
    return text.replace("&", "_").replace("'", "_")


def csv_rows(cuad: Path) -> dict[str, dict[str, str]]:
    """master_clauses.csv 的各行, 按 contract_key; cuad 为 CUAD_v1/. 键重复时报错."""
    with (cuad / "master_clauses.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    by_key = {contract_key(r["Filename"]): r for r in rows}
    if len(by_key) != len(rows):
        raise ValueError("master_clauses.csv 的文件名对应不唯一")
    return by_key


def pdf_files(cuad: Path) -> dict[str, Path]:
    """full_contract_pdf 下的全部 PDF, 按 contract_key. 键重复时报错."""
    paths = [p for p in (cuad / "full_contract_pdf").rglob("*") if p.is_file()]
    by_key = {contract_key(p.name): p for p in paths}
    if len(by_key) != len(paths):
        raise ValueError("PDF 的文件名对应不唯一")
    return by_key


def excerpts(row: dict[str, str], category: str) -> list[str]:
    """CSV 一行某类的摘录: 摘录列是 Python 列表的字符串 (只当字面量读)."""
    value = ast.literal_eval(row[category])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{category} 的摘录不是字符串列表")
    return value


def segment_location(pages: list[str], segment: str) -> Literal["single", "multi", "cross", "missing"] | None:
    """一段摘录的定位 (cuad.md 第 2 节): 锚点只出现在一页, 出现在多页, 哪页都没有而末尾 CROSS_TAIL 个字母数字能找到
    (跨页), 都找不到; 段里没有字母数字时为 None (不计)."""
    key = anchor(segment)
    if not key:
        return None
    texts = [alnum(page) for page in pages]
    found = sum(key in text for text in texts)
    if found:
        return "single" if found == 1 else "multi"
    tail = alnum(segment)[-CROSS_TAIL:]
    return "cross" if any(tail in text for text in texts) else "missing"


def pdf_pages(path: Path) -> list[str]:
    """PDF 文本层各页, 同 FileEvidenceStore.pages (pdfplumber, 没有文本的页为空串)."""
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        return [page.extract_text() or "" for page in pdf.pages]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def page_fixture(alias: str, root: Path) -> dict[str, Any]:
    """一份逐页文本 fixture: 别名, 原文件 (相对 full_contract_pdf, 取金标准), PDF 的 sha256, 页数与各页文本. root 为
    CUAD_v1/full_contract_pdf."""
    source = load_gold()[alias].pdf
    pdf = root / source
    pages = pdf_pages(pdf)
    return {
        "alias": alias,
        "source": source,
        "pdf_sha256": sha256(pdf),
        "page_count": len(pages),
        "pages": pages,
    }


def load_pages(alias: str) -> dict[str, Any]:
    """入库的逐页文本 fixture."""
    value: dict[str, Any] = json.loads((PAGES / f"{alias}.json").read_text(encoding="utf-8"))
    return value


def sums(cuad: Path) -> str:
    """SHA256SUMS 的全文: 金标准 PDF (按别名) 与 ANNOTATIONS, 路径相对 cuad (CUAD_v1/)."""
    paths = [f"full_contract_pdf/{c.pdf}" for c in load_gold().values()] + list(ANNOTATIONS)
    return "".join(f"{sha256(cuad / path)}  {path}\n" for path in paths)


def main() -> None:
    """从 data/external/cuad 重生成 tests/fixtures/cuad 下的逐页文本与 SHA256SUMS (PDF 只当数据读)."""
    parser = argparse.ArgumentParser(description="CUAD 逐页文本与 SHA256SUMS")
    parser.add_argument("--cuad", type=Path, default=REPO / "data" / "external" / "cuad")
    args = parser.parse_args()
    cuad = args.cuad / "CUAD_v1"
    PAGES.mkdir(parents=True, exist_ok=True)
    for alias in PAGE_FIXTURES:
        value = page_fixture(alias, cuad / "full_contract_pdf")
        text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        (PAGES / f"{alias}.json").write_text(text, encoding="utf-8")
        print(alias, value["page_count"])
    SUMS.write_text(sums(cuad), encoding="utf-8")


if __name__ == "__main__":
    main()
