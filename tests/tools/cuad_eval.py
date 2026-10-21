"""CUAD 合同评测的评分与反编造函数 (需求 R246; docs/云端开发/design/external/cuad.md 第 3, 4 节), 与入库的逐页文本.

- parse_cuad_date: CUAD CSV 的签订日 (m/d/yy, yy >= 90 补 19yy), 含遮盖 "[]" 的不计.
- normalize_party, same_party: 当事方名称的规范化与比对 (去公司后缀与括号里的定义名, difflib 相似度 >= 0.9 或互相包含).
- split_omitted, anchor, locate: CUAD 摘录按 <omitted> 拆段, 取前 60 个字母数字作锚点, 在逐页文本里定位页码.
- starts_on, fabricated: 锚点是否从某页开始; 反编造检查: 条款文本的锚点不从它声明的那一页开始 (锚点也可去掉开头的
  条款号再取, 页文本不算单独一行的印刷页码; 只在评测里算, 不进正式校验).
- contract_key, csv_rows, pdf_files, excerpts, segment_location (需求 R248): 本地 CUAD 的 CSV 行与 PDF 按文件名对应,
  CSV 摘录 (Python 列表字符串) 的读取与逐页定位; LOCATED_CATEGORIES 为定位统计的 20 类 (cuad.md 第 1 节列出的类别).
- 评分 (需求 R249, cuad.md 第 3 节): termination_truth (CSV 三类终止摘录, 评测时生成), score_contract (一份 DOC-04
  输出对金标准: 当事方, 签订日, 金额, ABN, 页码命中, 反编造, 终止召回, 四类条款), aggregate (汇总与硬性, 软性检查);
  扩展集 (extended.json, 30 份, 只比 CSV 自动真值) 由 csv_truth 从 CSV 生成真值.
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
from collections.abc import Collection
from datetime import date
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, TypeAdapter, model_validator

from backend.contracts.llm_outputs import ExtractedTerm
from backend.contracts.models import Contract, ContractTerm, TermType

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "cuad"
PAGES = FIXTURES / "pages"  # 逐页文本: <别名>.json
LLM = FIXTURES / "llm"  # 离线 mock 输出: <程序>/<别名>.json (不放 tests/fixtures/llm, 免得弄红 Numbat 的守卫)
GOLD = FIXTURES / "gold.json"  # 金标准 (需求 R247)
EXTENDED = FIXTURES / "extended.json"  # 扩展集清单 (需求 R249 验收 6)
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
# 条款文本开头的条款号 (反编造检查去掉它再取锚点). DOC-04 的提示词要求子条款连号照抄, 不带标题, 真实模型因此写成
# "2.3 Other than ..." (原文 "2.3 Delivery. Other than ..."), "2.6 (A) Unless ..." (原文 "(A) Non-defective Products.
# Unless ..."), 或把上级号拼上 ("4.E. Except ...", 原文 "E. PAYMENT. Except ..."), 号与正文在原文里不相连
CLAUSE_NUMBER = re.compile(r"^\s*\d+(?:\.\d+)*(?:\.[A-Za-z](?![A-Za-z]))?(?:\s*\([0-9A-Za-z]{1,4}\))*\.?\s+")
# 文本层里单独一行的印刷页码 (如 CUAD-03 的 "Page -8-"), 可夹在条款中间, 照抄条款时不算正文
PAGE_MARK = re.compile(r"^\s*page\s*-?\s*\d+\s*-?\s*$", re.IGNORECASE | re.MULTILINE)
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
# termination 真值取的三类摘录 (CUAD 没有 "因违约终止" 一类, 召回只按这几类算)
TERMINATION_CATEGORIES = (
    "Termination For Convenience",
    "Notice Period To Terminate Renewal",
    "Post-Termination Services",
)
# 硬性 (不满足即失败) 与软性 (只告警) 的门槛 (需求 R249 验收 3, 4)
HARD = {"validated": 9, "fabrication_free": 0.95}
SOFT = {
    "counterparty": 9,
    "sign_date": 8,
    "page_hit": 0.85,
    "termination_recall": 0.8,
    "control_transfer": 5,
    "rev03": 5,
}


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


def fabricated(term: ExtractedTerm | ContractTerm, pages: list[str]) -> bool:
    """反编造检查: 条款文本的锚点不从它声明的那一页开始 (页码越界, 文本没有字母数字也算). 锚点取全文的, 或去掉开头
    条款号 (CLAUSE_NUMBER) 后的; 页文本取原样的, 或去掉单独一行的印刷页码 (PAGE_MARK) 后的 (照抄时连页码一起抄的
    也认); 有一种从该页开始即可."""
    keys = {anchor(term.text), anchor(CLAUSE_NUMBER.sub("", term.text, count=1))}
    texts = (pages, [PAGE_MARK.sub("", page) for page in pages])
    return not any(starts_on(text, term.page, key) for text in texts for key in keys)


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
    review: Literal["开发者起草", "用户抽查", "CSV 自动"]  # 最后一种为扩展集由 csv_truth 生成的真值
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


class ExtendedContract(BaseModel):
    """扩展集的一份合同: 七类合同中 3-15 页的 94 份去掉金标准与拆成几个文件的, 以固定种子 4595826 抽出 30 份."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    alias: str  # CUAD-Xnn
    pdf: str  # CUAD_v1/full_contract_pdf 下的相对路径
    csv_filename: str
    page_count: int


def load_extended() -> list[ExtendedContract]:
    return TypeAdapter(list[ExtendedContract]).validate_json(EXTENDED.read_bytes())


def answer_parties(row: dict[str, str]) -> list[str]:
    """Parties-Answer 列出的各方 (分号分隔, 去掉括号里的定义名)."""
    names = [re.sub(r"\s*\(.*$", "", p).strip() for p in row["Parties-Answer"].split(";")]
    return [n for n in names if normalize_party(n)]


def filer_party(row: dict[str, str], file_name: str) -> str:
    """扩展集的被审计单位: Parties-Answer 中与申报公司 (文件名开头) 最像的一方. 申报公司不一定是卖方, 扩展集只看
    趋势, 不人工定."""
    filer = normalize_party(re.split(r"[_,]", file_name)[0])
    return max(answer_parties(row), key=lambda p: difflib.SequenceMatcher(None, normalize_party(p), filer).ratio())


def csv_truth(entry: ExtendedContract, row: dict[str, str], pages: list[str]) -> GoldContract:
    """扩展集一份合同的真值, 全由 CSV 生成: 被审计单位为 filer_party, 对方为其余各方 (别名同, 页为别名定位到的页),
    签订日为 Agreement Date 与 Effective Date 中能解析的 (都不能时不计分), 其页为两类摘录定位到的页; 金额, 条款与
    REV-03 结论不比."""
    client = filer_party(row, Path(entry.pdf).name)
    others = [p for p in answer_parties(row) if not same_party(p, client)] or [client]
    dates = sorted({d for c in ("Agreement Date", "Effective Date") if (d := parse_cuad_date(row[f"{c}-Answer"]))})
    date_pages = {n for c in ("Agreement Date", "Effective Date") for e in excerpts(row, c) for n in locate(pages, e)}
    return GoldContract(
        alias=entry.alias,
        review="CSV 自动",
        csv_filename=entry.csv_filename,
        pdf=entry.pdf,
        category=Path(entry.pdf).parent.name,
        page_count=len(pages),
        client_name=client,
        counterparty=others[0],
        counterparty_aliases=others,
        counterparty_pages=sorted({n for a in others for n in locate(pages, a)}),
        sign_date=dates or None,
        sign_date_pages=sorted(date_pages),
        sign_date_note=None,
        counterparty_abn=None,
        amount_cents=None,
        terms=None,
        rev03=None,
        rev03_note=None,
    )


def crosses(pages: list[str], page: int, key: str) -> bool:
    """从第 page 页开始的锚点是否延续到下一页 (跨页的条款页码允许 ±1)."""
    return 1 <= page <= len(pages) and key not in alnum(pages[page - 1])


def termination_truth(row: dict[str, str], pages: list[str]) -> list[tuple[str, list[int]]]:
    """termination 的真值 (需求 R247 验收 3, 评测时由 CSV 生成): TERMINATION_CATEGORIES 各条摘录的首段与它定位到的页
    (找不到为空列表, 只按锚点算召回). 同一段在几类里重复出现的只记一次."""
    found: dict[str, list[int]] = {}
    for category in TERMINATION_CATEGORIES:
        for excerpt in excerpts(row, category):
            parts = split_omitted(excerpt)
            if parts and anchor(parts[0]):
                found.setdefault(parts[0], locate(pages, excerpt))
    return list(found.items())


def covers(term: ContractTerm, key: str, starts: Collection[int]) -> bool:
    """模型的一条条款是否对上一条真值 (召回, cuad.md 第 3 节): 真值锚点 key 在条款文本里, 或条款起始页在 starts 中
    (真值所在的页)."""
    return key in alnum(term.text) or term.page in starts


def score_contract(
    gold: GoldContract, contract: Contract | None, pages: list[str], termination: list[tuple[str, list[int]]]
) -> dict[str, Any]:
    """一份 DOC-04 输出对金标准的评分 (cuad.md 第 3 节). contract 为通过四级校验的抽取, 没通过为 None (只记
    validated 为假, 其余不计). found: 货物合同四类条款各有没有对上金标准 (covers). 页码命中: 当事方与签订日的证据
    指针在接受页集合内; 文本含真值锚点 (同类的金标准条款, termination 为 CSV 真值) 的每条条款, 页码等于所含某条真值
    的页 (跨页的允许 ±1; 同一段文字在合同里出现两次时, 对上其中一处即可). termination 为 termination_truth 的结果."""
    if contract is None:
        return {"alias": gold.alias, "validated": False}
    hits: list[bool] = []
    for pointer in contract.evidence:
        accepted = {"counterparty": gold.counterparty_pages, "sign_date": gold.sign_date_pages}.get(pointer.locator)
        if accepted is not None:
            hits.append(pointer.page in accepted)
    # 各类的真值: (锚点, 所在页); 金标准条款一页, termination 真值为定位到的页 (找不到的不评页码)
    truths = {t: [(c.anchor, [c.page]) for c in clauses] for t, clauses in (gold.terms or {}).items()}
    truths[TermType.termination] = [(anchor(segment), located) for segment, located in termination]
    for term in contract.terms:
        quoted = [(key, starts) for key, starts in truths.get(term.term_type, []) if starts and key in alnum(term.text)]
        if quoted:
            hits.append(any(abs(term.page - n) <= crosses(pages, n, key) for key, starts in quoted for n in starts))

    def covered(term_type: TermType, key: str, starts: list[int]) -> bool:
        return any(covers(t, key, starts) for t in contract.terms if t.term_type is term_type)

    found = {t.value: any(covered(t, key, starts) for key, starts in truths[t]) for t in gold.terms or {}}
    recalled = sum(covered(TermType.termination, key, starts) for key, starts in truths[TermType.termination])
    sign_date = None if gold.sign_date is None else contract.sign_date in gold.sign_date
    return {
        "alias": gold.alias,
        "validated": True,
        "counterparty": any(
            same_party(contract.counterparty, n) for n in [gold.counterparty, *gold.counterparty_aliases]
        ),
        "sign_date": sign_date,
        "abn_null": contract.counterparty_abn is None,
        "amount": contract.amount_cents == gold.amount_cents,
        "fabricated": [t.id for t in contract.terms if fabricated(t, pages)],
        "terms": len(contract.terms),
        "page_hits": [sum(hits), len(hits)],
        "termination": [recalled, len(termination)],
        "found": found,
    }


def ratio(part: int, whole: int) -> float:
    return part / whole if whole else 1.0


def aggregate(scores: list[dict[str, Any]], rev03: dict[str, tuple[str | None, str]]) -> dict[str, Any]:
    """各份评分的汇总与检查 (需求 R249 验收 3, 4). rev03 为货物合同 REV-03 的 (模型的 proposed_action, 金标准),
    模型没得出结果的为 None. hard 为硬性检查的不符之处 (须为空), soft 为软性检查低于门槛的 (只告警)."""
    passed = [s for s in scores if s["validated"]]
    terms = sum(s["terms"] for s in passed)
    fabricated = sum(len(s["fabricated"]) for s in passed)
    page = [sum(s["page_hits"][0] for s in passed), sum(s["page_hits"][1] for s in passed)]
    termination = [sum(s["termination"][0] for s in passed), sum(s["termination"][1] for s in passed)]
    dated = [s for s in passed if s["sign_date"] is not None]
    goods = [s for s in passed if s["found"]]
    out: dict[str, Any] = {
        "validated": len(passed),
        "abn_not_null": [s["alias"] for s in passed if not s["abn_null"]],
        "fabrication_free": ratio(terms - fabricated, terms),
        "counterparty": sum(s["counterparty"] for s in passed),
        "sign_date": [sum(s["sign_date"] for s in dated), len(dated)],
        "page_hit": ratio(*page),
        "termination_recall": ratio(*termination),
        "control_transfer": sum(s["found"].get(TermType.control_transfer.value, False) for s in goods),
        "rev03": sum(got == want for got, want in rev03.values()),
        "amount": sum(s["amount"] for s in passed),
    }
    hard = []
    if out["validated"] < HARD["validated"]:
        hard.append(f"通过四级校验 {out['validated']}/{len(scores)}, 门槛 {HARD['validated']}")
    if out["abn_not_null"]:
        hard.append(f"counterparty_abn 不为 null: {', '.join(out['abn_not_null'])}")
    if out["fabrication_free"] < HARD["fabrication_free"]:
        hard.append(f"反编造检查通过率 {out['fabrication_free']:.3f}, 门槛 {HARD['fabrication_free']}")
    soft = []
    for name, threshold in SOFT.items():
        value = out[name][0] if name == "sign_date" else out[name]
        if value < threshold:
            soft.append(f"{name} {out[name]}, 门槛 {threshold}")
    return out | {"hard": hard, "soft": soft}


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
