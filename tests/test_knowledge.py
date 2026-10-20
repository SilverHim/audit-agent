import json
import re
import shutil
from bisect import bisect_left
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from functools import cache, cached_property
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal

import pdfplumber
import pytest
import yaml
from jinja2 import Environment, FileSystemLoader
from pydantic import ValidationError

from backend.contracts.llm_outputs import ContractExtraction
from backend.contracts.models import (
    AuditObjective,
    ClientProfile,
    EvidencePointer,
    EvidenceType,
    Model,
    Phase,
    Procedure,
    Retention,
    RiskLevel,
    TermType,
)
from backend.contracts.records import DatasetTraits
from backend.contracts.state import initial_state
from backend.core.config import get_settings
from backend.orchestration.runner import fixed_procedures
from backend.orchestration.scheduler import build_schedule
from backend.services import procedure_library
from backend.services.procedure_library import ProcedureLibrary, highest_risk, ref_problem
from tests.external import optional, supplements_dir
from tests.fakes import ENGAGEMENT, NUMBAT_CONTRACTS, NUMBAT_REPLIES
from tests.fakes import NUMBAT_OBJECTIVES as OBJECTIVES
from tests.fixture_sets import FIXTURE_PARAMS, FIXTURE_SETS, NUMBAT, FixtureSet

ROOT = get_settings().KNOWLEDGE_ROOT
P2 = {"REV-08", "REV-03", "REV-02", "CSH-04", "DOC-06", "CSH-05", "CSH-02"}


# 基线 18 条: Demo 的 17 条加 GEN-09 (A7 独立复核). 新程序一律挂功能名 (模板 feature), 不进基线启用集
BASE_18 = {
    "ACC-01",
    "ACC-04",
    "CSH-02",
    "CSH-04",
    "CSH-05",
    "DOC-04",
    "DOC-06",
    "FIN-01",
    "FIN-04",
    "GEN-09",
    "PLN-01",
    "PLN-03",
    "PLN-07",
    "REV-02",
    "REV-03",
    "REV-08",
    "RPT-01",
    "RPT-03",
}


def count_guards(root: Path) -> tuple[set[str], set[str]]:
    """两条程序数守卫的差集, 都为空才通过: (BASE_18 中全部模板 (含未启用) 没有的, 基线启用集与 BASE_18 的对称差)."""
    library = ProcedureLibrary(root)
    return BASE_18 - {t.id for t in library.all(include_disabled=True)}, {t.id for t in library.all()} ^ BASE_18


def test_all_procedures_load() -> None:
    """全部模板包含基线 18 条, 基线启用集恰为这 18 条 (新程序加进来时不必改这里, 但须挂功能名)."""
    assert count_guards(ROOT) == (set(), set())


def test_count_guards_on_extra_templates(tmp_path: Path) -> None:
    """临时 knowledge 目录多一个 feature 为 journals 的 YAML 时两条都过; 再多一个 feature 为空的 YAML 时第二条失败."""
    root = tmp_path / "knowledge"
    shutil.copytree(ROOT, root)
    template = yaml.safe_load((root / "procedures" / "REV-02.yaml").read_text(encoding="utf-8"))
    for pid, feature in (("X-J", "journals"), ("X-B", None)):
        extra = {k: v for k, v in template.items() if k not in ("triggers", "depends_on", "feature")}
        extra |= {"id": pid, **({"feature": feature} if feature else {})}
        (root / "procedures" / f"{pid}.yaml").write_text(yaml.safe_dump(extra, allow_unicode=True), encoding="utf-8")
        assert count_guards(root) == (set(), set() if feature else {"X-B"}), pid


@pytest.mark.parametrize("feature", ["", "feature: journals\n"])
def test_fixed_templates_are_checked_on_load(tmp_path: Path, feature: str) -> None:
    """R81 验收 1, 2: 现有 knowledge 下没有 fixed 模板; 非 P2 模板写 fixed, fixed 模板写 triggers, fixed 模板或其
    依赖闭包中的 P2 程序的参数按风险等级取值时, ProcedureLibrary 构造即抛 ValueError, 消息含程序编号. 挂了未启用的功能
    (journals 不在基线中) 的模板同样校验."""
    assert ProcedureLibrary(ROOT).fixed() == [] and not any(t.fixed for t in ProcedureLibrary(ROOT).all(True))
    root = tmp_path / "knowledge"
    shutil.copytree(ROOT, root)
    base = "name: 自测\ncycle: Journals\nowner: A6\nengine: rule\nautonomy_level: L3\nfixed: true\n" + feature
    trigger = "triggers:\n  - {account: Revenue, assertions: [Occurrence], level: [high]}\n"
    cases = {
        "X-P3": ("id: X-P3\nphase: P3_Completion\n" + base, "X-P3 不是 P2 程序"),
        "X-RISK": (
            "id: X-RISK\nphase: P2_Executing\n" + base + "params:\n  days: {by_risk: {high: 10}}\n",
            "X-RISK.days",
        ),
        "X-TRIG": ("id: X-TRIG\nphase: P2_Executing\n" + base + trigger, "X-TRIG 标了 fixed"),
        "X-DEPS": (
            "id: X-DEPS\nphase: P2_Executing\n" + base + "depends_on: [REV-02]\n",
            "X-DEPS 依赖的 REV-02.window_days",
        ),
    }
    for name, (text, message) in cases.items():
        path = root / "procedures" / f"{name}.yaml"
        path.write_text(text, encoding="utf-8")
        with pytest.raises(ValueError, match=message):
            ProcedureLibrary(root)
        path.unlink()
    (root / "procedures" / "X-OK.yaml").write_text("id: X-OK\nphase: P2_Executing\n" + base, encoding="utf-8")
    enabled = ProcedureLibrary(root, frozenset({"critic", "journals"}))
    assert [t.id for t in enabled.fixed()] == ["X-OK"]
    assert [t.id for t in ProcedureLibrary(root).fixed()] == ([] if feature else ["X-OK"])


def test_standards_referenced_are_registered() -> None:
    lib = ProcedureLibrary(ROOT)
    missing = {s for t in lib.all() for s in t.standards if lib.fragment(s) is None}
    assert not missing


REFERENCE = Path(__file__).parents[1] / "docs" / "参考资料"  # 准则与法规原文 PDF


@dataclass(frozen=True)
class Source:
    """准则片段 YAML (knowledge/standards/{名}.yaml) 对应的原文.

    volumes: (PDF 相对 docs/参考资料 的路径, 抽取页范围) 的元组, 多卷依次拼接. 页范围从 1 起, 含两端; 为 None 时
    读全卷. 不论限页与否, 本文件的片段都出现后再多读一页即停 (限页使逐字测试不读整部公司法).
    cover: 第一卷封面编译日期的写法: "Compilation date" (日月年, 如 2 December 2025; Date 大小写与冒号后的空格数不限)
    或 "Compiled as at" (月年, 如 July 2025, 记为 2025-07).
    compiled: 还没有 YAML 的行 (原文先登记, 片段由各组写) 预期的编译日期; 有 YAML 时以其 compiled 为准,
    两者都给时须相同.
    """

    volumes: tuple[tuple[str, tuple[int, int] | None], ...]
    cover: Literal["Compilation date", "Compiled as at"] = "Compilation date"
    compiled: str | None = None


CA_VOLUME = "04_法律_CorporationsAct/CorporationsAct2001_Compilation_C148_2026-08-27_Vol{}.pdf"
APES110_PDF = "06_职业道德_APES/APES_110_Code_of_Ethics_Compiled_July2025.pdf"
# knowledge/standards 下每个 YAML 一行; 其余 ASA, AASB 由各组首建该文件时加行
SOURCES: dict[str, Source] = {
    "AASB110": Source((("03_会计准则_AASB/AASB_110.pdf", (5, 8)),)),  # 第 3 段至第 10 段
    "AASB15": Source((("03_会计准则_AASB/AASB_15.pdf", (12, 21)),)),  # 第 31 段至第 88 段
    # 现行版只读两段页: 第 16-17 页 (第 32, 33 段; MD-2 将加的第 36 段也在第 17 页), 第 31-32 页 (A44); 新版不用
    "ASA240": Source((("01_审计准则_ASA/ASA_240.pdf", (16, 17)), ("01_审计准则_ASA/ASA_240.pdf", (31, 32)))),
    "ASA320": Source((("01_审计准则_ASA/ASA_320.pdf", None),)),
    "ASA450": Source((("01_审计准则_ASA/ASA_450.pdf", None),)),
    "ASA505": Source((("01_审计准则_ASA/ASA_505.pdf", None),)),
    "ASA550": Source((("01_审计准则_ASA/ASA_550.pdf", (10, 16)),)),  # 第 10 段至 Aus 27.2
    "ASA560": Source((("01_审计准则_ASA/ASA_560.pdf", (9, 11)),)),  # 第 6 段至第 9 段
    "ASA570": Source((("01_审计准则_ASA/ASA_570.pdf", (10, 20)),)),  # 第 10 段至 A19
    "ASA700": Source((("01_审计准则_ASA/ASA_700.pdf", None),)),
    "ASA705": Source((("01_审计准则_ASA/ASA_705.pdf", None),)),
    "CA2001": Source(((CA_VOLUME.format(1), (505, 515)), (CA_VOLUME.format(2), (80, 260))), compiled="2026-08-27"),
    "APES110": Source(((APES110_PDF, (40, 210)),), cover="Compiled as at", compiled="2025-07"),
}
# 页眉页脚行: "ASA 505 – compiled - 10 - AUDITING STANDARD", "ASA 502 ─ compiled - 10 - AUDITING STANDARD",
# "ASA 810 - 14 - AUDITING STANDARD", "ASQM 1 - compiled - 22 - AUDITING STANDARD", "AASB 15-compiled 21 STANDARD"
# (附录与结论依据部分为 "AASB 15-compiled 33 APPENDIX B", "... ILLUSTRATIVE EXAMPLES"),
# "Authorised Version F2023C00402 registered 03/05/2023", 公司法页眉 "Section 307C", 页首 "Auditing Standard ASA 505"
# (ASA 502 为 "Compiled Auditing Standard ASA 502", ASQM 为 "Auditing Standard ASQM 1", 见 PAGE_TITLE).
# 字号小于正文的页眉页脚 (ASA 的准则标题, 公司法的章节页眉与页脚, APES 110 的页眉与页码) 由 page_layout 按字号去掉
PAGE_TITLE = re.compile(r"(?:Compiled )?Auditing Standard (?:ASA|ASQM) \d+")
RUNNING_LINE = re.compile(
    r"(?:ASA|ASQM|AASB) \d+\s*[-–─]\s*(?:compiled\s*[-–─]?\s*)?\d+\s*[-–─]?\s*[A-Z][A-Z ]*"
    rf"|Authorised Version \S+ registered \S+|Section \d+[A-Z]*|{PAGE_TITLE.pattern}"
)
SMALL = 0.85  # 字号小于正文的这个比例: 上标脚注号, 页底脚注行, 小字的页眉页脚, 小字的交叉引用
BASELINE_TOLERANCE = 1.0  # 小字与正文字的底边相差不超过这么多 (pt) 算在同一基线上
# 正文基线上的小字只留两种 (ASA 9pt 的段末标点, 行首的段号与子项标记), 按词判断; 其余去掉: 交叉引用 "(Ref: Para. A8–"
# 及其跨行的后半截 "A9)", 贴在词后的脚注号, 生效日期注 "[Note: For operative dates ...", 表格下的小字注
SMALL_PUNCTUATION = re.compile(r"[.,;:]+")
SMALL_LEADING_MARK = re.compile(r"(?:Aus )?A?\d+(?:\.\d+)*\.?|\([0-9A-Za-z]{1,5}\)")
WORD_GAP = 1.0  # 同一行相邻两字的间隔大于这么多 (pt) 即分词
# 行首记号: 编号段 "7." (AASB "31"), "Aus 13.2", "A44.", "Aus A15.1", APES "R410.15", "410.14 A1", "AUST R410.14.2",
# 公司法条号 "307C", 子项与款 "(a)" "(iii)" "(2A)". 须与其后正文隔开大于词距的距离 (悬挂缩进), 正文续行以数字开头时
# (如公司法 "18 months) determined") 只隔一个词距 (约 2.5-3.3 pt)
MARK = re.compile(
    r"(?:AUST )?(?:R?\d+(?:\.\d+)+(?: A\d+)?|Aus A?\d+(?:\.\d+)*(?:\([0-9a-z]+\))*|A\d+\.|\d+[A-Z]*\.?"
    r"|\([0-9A-Za-z]{1,5}\))"
)
# ASA 在编号段的子项中插入的澳洲子项 "Aus 28.1(c)": 记号在页边 (同 Aus 段), 却属于所在的段
AUS_SUBITEM = re.compile(r"Aus \d+(?:\.\d+)+\([0-9a-z]+\)")
MARK_GAP = 4.5
PUSHED = 10.0  # 段号太宽时其后正文被推右的最大距离 (pt), 见 settle_marks
INDENT_TOLERANCE = 2.0  # 比较行首 x 的容差 (pt); 公司法的款号右对齐, 同级款号的 x 相差可达 8 pt, 故比的是记号后正文的 x


@dataclass(frozen=True)
class Line:
    """抽取出的一行正文 (见 page_layout).

    x 为行首字的 x; mark 为行首记号 (见 MARK, 没有为 None); body 为记号后正文的 x (没有记号时同 x); font 为该行最常见的
    (字体名, 字号); heading 为条首 (公司法 12pt 粗体的条号与条标题, 字号大于正文的粗体记号行). 记号与 body 在连成原文时
    (layout_text) 按正文的栏位核定, 见 settle_marks.
    """

    text: str
    x: float
    mark: str | None
    body: float
    font: tuple[str, float]
    heading: bool = False


# 其前的空白归一时去掉的标点: 脚注上标贴在词后又接标点时 ("ASA 7055; and"), 上标去掉后版面留下的空格不进比对,
# 片段写原文的正常写法 ("ASA 705; and"); 片段与原文两边同样处理
CLOSING = ";,.:)"


def normalise(text: str) -> str:
    """去掉 (Ref: Para. ...) 交叉引用 (与正文同字号的; 小字的由 page_layout 去掉), 行尾连字符直接接下一行
    (bill-and-hold), 空白归一为单个空格, 标点 CLOSING 之前的空白去掉."""
    text = re.sub(r"\s*\(Ref: ?Paras?\.[^)]*\)", "", text)
    text = re.sub(r"-\n", "-", text)
    return re.sub(rf" (?=[{re.escape(CLOSING)}])", "", " ".join(text.split()))


def char_key(c: dict[str, Any]) -> tuple[float, float, str]:
    return (round(c["x0"], 2), round(c["top"], 2), c["text"])


def words_of(chars: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """一行可见字按间隔分词 (字已按 x 排好)."""
    words: list[list[dict[str, Any]]] = []
    for c in chars:
        if words and c["x0"] - words[-1][-1]["x1"] <= WORD_GAP:
            words[-1].append(c)
        else:
            words.append([c])
    return words


def line_of(text: str, chars: list[dict[str, Any]]) -> Line:
    """由一行的文字与可见字定出行首 x, 行首记号 (至多三个词, 取最长的) 与正文的 x, 主要字体."""
    words = words_of(chars)
    font = Counter((c["fontname"].split("+")[-1], round(c["size"], 1)) for c in chars).most_common(1)[0][0]
    for k in (3, 2, 1):
        if len(words) <= k:
            continue
        mark = " ".join("".join(c["text"] for c in word) for word in words[:k])
        if MARK.fullmatch(mark) and words[k][0]["x0"] - words[k - 1][-1]["x1"] >= MARK_GAP:
            return Line(text, chars[0]["x0"], mark, words[k][0]["x0"], font)
    return Line(text, chars[0]["x0"], None, chars[0]["x0"], font)


def page_layout(page: Any, title: str | None = None, body: float | None = None) -> list[Line]:
    """一页正文的行. body 为正文字号 (缺省取该页最常见的; 逐字测试取整卷的, 见 body_size).

    先按字号去字符: 小于正文 SMALL 倍的字符中,
    - 不在正文字基线上的去掉: 上标脚注号, 页底脚注行, 小字的页眉页脚;
    - 在正文行里同基线的, 按词只留段末标点与行首的段号, 子项标记 (见 SMALL_LEADING_MARK), 其余去掉.
    再去掉整行都小于正文的行 (公司法 10pt 的章节页眉, 表格与注释), RUNNING_LINE 的页眉页脚行与页首的准则标题行
    (title; ASA 与正文同字号, 页首 PAGE_TITLE 之后字母全为粗斜体的标题行, 标题折成两行时两行都去掉; 标点可以是别的
    字体, 如 ASA 510 标题里 MS Mincho 的连字符)."""
    sizes = Counter(round(c["size"], 1) for c in page.chars if c["text"].strip())
    if not sizes:
        return []
    body = body if body is not None else sizes.most_common(1)[0][0]
    small = body * SMALL
    baselines = sorted(c["bottom"] for c in page.chars if c["size"] >= small and c["text"].strip())

    def on_baseline(bottom: float) -> bool:
        at = bisect_left(baselines, bottom - BASELINE_TOLERANCE)
        return at < len(baselines) and baselines[at] <= bottom + BASELINE_TOLERANCE

    # 正文行里的小字: 按基线把字符分行 (从左到右), 连续的小字按词判断留不留
    rows: dict[int, list[dict[str, Any]]] = {}
    for c in page.chars:
        if c["text"].strip() and (c["size"] >= small or on_baseline(c["bottom"])):
            rows.setdefault(round(c["bottom"]), []).append(c)
    kept_small: set[tuple[float, float, str]] = set()
    for row in rows.values():
        row.sort(key=lambda c: c["x0"])
        i = 0
        while i < len(row):
            if row[i]["size"] >= small:
                i += 1
                continue
            j = i
            while j < len(row) and row[j]["size"] < small:
                j += 1
            for n, word in enumerate(words_of(row[i:j])):
                text = "".join(c["text"] for c in word)
                if SMALL_PUNCTUATION.fullmatch(text) or (i == 0 and n == 0 and SMALL_LEADING_MARK.fullmatch(text)):
                    kept_small.update(char_key(c) for c in word)
            i = j
    kept = page.filter(
        lambda obj: obj.get("object_type") != "char" or obj["size"] >= small or char_key(obj) in kept_small
    )
    lines: list[Line] = []
    running_title = False  # ASA 页首 "Auditing Standard ASA 600" 之后的准则标题 (粗斜体, 可能折成两行)
    for line in kept.extract_text_lines(return_chars=True):
        chars = [c for c in line["chars"] if c["text"].strip()]
        text = line["text"].strip()
        letters = [c for c in chars if c["text"].isalpha()]
        if letters and running_title and all("BoldIt" in c["fontname"] for c in letters):
            continue
        running_title = bool(PAGE_TITLE.fullmatch(text)) and not lines
        if not chars or max(c["size"] for c in chars) < body - 0.5 or RUNNING_LINE.fullmatch(text) or text == title:
            continue
        found = line_of(text, chars)
        if found.mark is not None and found.font[1] > body + 0.5 and "Bold" in found.font[0]:
            found = replace(found, heading=True)
        lines.append(found)
    return lines


def page_lines(page: Any, title: str | None = None, body: float | None = None) -> list[str]:
    """一页正文的行文字 (见 page_layout)."""
    return [line.text for line in page_layout(page, title, body)]


class Text(str):
    """抽取的原文: 各行归一后连成的一串 (同 normalise), 带版面: lines 为各行 (记号已核定, 见 settle_marks), starts 为
    各行起点在串中的位置 (整行在归一时去掉的为 None), extracted 为核定前的各行 (join_texts 用). 由 layout_text 构造;
    逐字与整段边界的核对 (boundary_problems) 要用版面."""

    lines: tuple[Line, ...]
    starts: tuple[int | None, ...]
    extracted: tuple[Line, ...]

    def __new__(
        cls,
        value: str,
        lines: Sequence[Line] = (),
        starts: Sequence[int | None] = (),
        extracted: Sequence[Line] = (),
    ) -> "Text":
        self = super().__new__(cls, value)
        self.lines, self.starts, self.extracted = tuple(lines), tuple(starts), tuple(extracted)
        return self

    @cached_property
    def by_start(self) -> dict[int, int]:
        """行起点 → 行号 (同一起点有几行时取最后一行: 前面的整行在归一时去掉了)."""
        return {start: k for k, start in enumerate(self.starts) if start is not None}

    @cached_property
    def by_body(self) -> dict[int, int]:
        """记号行的正文起点 (记号与其后一个空格之后) → 行号."""
        found: dict[int, int] = {}
        for k, line in enumerate(self.lines):
            start = self.starts[k]
            if line.mark and start is not None and self.startswith(f"{line.mark} ", start):
                found[start + len(line.mark) + 1] = k
        return found


def settle_marks(lines: Sequence[Line]) -> list[Line]:
    """按正文的栏位核定各记号行.

    栏位为续行 (无记号的行) 的行首 x 中出现两次以上的 (ASA 106.3, 141.9, 177.3 等); 续行少时 (只抽一页) 退而取记号行
    的正文 x 中出现两次以上的.
    - 条首: 条标题的 x 随条号宽窄变, 正文 x 记为条号 x 加 2 倍容差 (条内各款在其右, 下一条与 Division 标题在同一 x).
    - 其余记号行: 正文 x 取它左边 PUSHED 以内 (至右边容差) 行数最多的栏位 (段号太宽时正文被推右, 如 ASA 540 的 "A139."
      后正文在 109.1, 续行仍在 106.3; APES "R220.4" 后在 128, 续行在 125.6). 取不到在记号右边的栏位时不是记号: 两端
      对齐的续行在行首引用段号后词距被拉宽 (ASA 600 "as described in paragraph / A19. In a group audit"), 正文 x 不在
      任何栏位上.
    - 段号 (不带括号的记号) 后正文以两个小写字母开头的不是记号: 续行行首引用段号 (APES "R390.12 to R390.17, as
      applicable"). 子项与款的正文可以小写开头 (公司法 "(a) start at").
    """
    continuations = Counter(round(line.x) for line in lines if line.mark is None)
    bodies = Counter(round(line.body) for line in lines if line.mark is not None)
    settled: list[Line] = []
    for line in lines:
        mark = line.mark
        if mark is not None and line.heading:
            line = replace(line, body=line.x + 2 * INDENT_TOLERANCE)
        elif mark is not None:
            rest = " ".join(line.text.split())[len(mark) + 1 :]
            low = max(line.x + INDENT_TOLERANCE, line.body - PUSHED)
            column = next(
                (
                    max(window, key=lambda x: counts[x])
                    for counts in (continuations, bodies)
                    if (window := [x for x in counts if counts[x] >= 2 and low < x <= line.body + INDENT_TOLERANCE])
                ),
                None,
            )
            if column is None or (not mark.startswith("(") and re.match(r"[a-z]{2}", rest)):
                line = replace(line, mark=None, body=line.x)
            else:
                line = replace(line, body=float(column))
        settled.append(line)
    return settled


def layout_text(lines: Sequence[Line]) -> Text:
    """把各行连成归一的一串 (结果同 normalise("\\n".join(各行文字))), 并记下各行的起点; 记号行先经 settle_marks 核定.

    做法: 每行前加行号标签 \\x00{k}\\x01 再按 normalise 的规则去引用, 接连字符; 归一空白时标签不占位, 记为下一个字的
    位置.
    """
    extracted, lines = lines, settle_marks(lines)
    raw = "\n".join(f"\x00{k}\x01{line.text}" for k, line in enumerate(lines))
    raw = re.sub(r"\s*(?:\x00\d+\x01)?\(Ref: ?Paras?\.[^)]*\)", "", raw)
    raw = re.sub(r"-\n", "-", raw)
    out: list[str] = []
    starts: list[int | None] = [None] * len(lines)
    pending: list[int] = []
    length, space = 0, False
    for m in re.finditer(r"\x00(\d+)\x01|\s+|[^\x00\s]+", raw):
        if m[1] is not None:
            pending.append(int(m[1]))
        elif m[0].isspace():
            space = length > 0
        else:
            if space and m[0][0] not in CLOSING:
                out.append(" ")
                length += 1
            space = False
            for k in pending:
                starts[k] = length
            pending.clear()
            out.append(m[0])
            length += len(m[0])
    return Text("".join(out), lines, starts, extracted)


def join_texts(*texts: Text) -> Text:
    """依次连接几段抽取的原文 (中间隔一个空格), 版面一并接上: 各段核定前的行连起来再核定一次, 栏位按全部行定."""
    return layout_text([line for text in texts for line in text.extracted])


@cache
def reference_pdf(path: str) -> Any:
    """docs/参考资料 下的 PDF, 每卷只打开一次, 随进程关闭: 公司法一卷 800 多页, 打开一次约 0.4 秒, 原先每取一段原文都
    重新打开."""
    return pdfplumber.open(REFERENCE / path)


@cache
def reference_page(path: str, index: int, title: str | None, body: float) -> tuple[Line, ...]:
    """该卷第 index 页 (从 0 起) 的正文行 (见 page_layout), 同一页只解析一次 (逐字测试与正例, 反例常取同一页);
    解析后清掉该页的字符缓存, 不占内存."""
    page = reference_pdf(path).pages[index]
    try:
        return tuple(page_layout(page, title, body))
    finally:
        page.close()


@cache
def body_size(path: str, pages: tuple[int, int] | None = None) -> float:
    """docs/参考资料 下 PDF (页范围内, None 为全卷) 正文的字号: 在中段均匀抽 4 页 (避开封面, 目录与附录), 取最常见的
    字号. 按整卷取, 不按单页: 以表格或注释为主的页上最常见的是小字号, 按单页取会把章节页眉与注释当成正文."""
    pdf = reference_pdf(path)
    first, last = pages if pages else (1, len(pdf.pages))
    picked = sorted({first - 1 + round((last - first) * q) for q in (0.3, 0.45, 0.6, 0.75)})
    sizes: Counter[float] = Counter()
    for i in picked:
        page = pdf.pages[i]
        sizes.update(round(c["size"], 1) for c in page.chars if c["text"].strip())
        page.close()
    return sizes.most_common(1)[0][0]


@cache
def pdf_text(path: str, first: int, last: int, title: str | None = None) -> Text:
    """docs/参考资料 下 PDF 第 first 至 last 页 (从 1 起, 含两端) 的正文 (见 page_layout, 字号按整卷), 带版面; 测试里的
    正例与反例用."""
    body = body_size(path)
    return layout_text([line for i in range(first - 1, last) for line in reference_page(path, i, title, body)])


def standard_doc(name: str) -> dict[str, Any]:
    return yaml.safe_load((ROOT / "standards" / f"{name}.yaml").read_text(encoding="utf-8"))


def fragment_pieces(fragment: dict[str, Any]) -> list[str]:
    """片段正文中须逐字出现在原文里的部分: 没有 subitems 时为整段正文; 有时按所列子项的标记 "(x) " 切开, 为引语与
    各子项 (所摘子项在原文中可以不相邻)."""
    text = normalise(fragment.get("text") or "")
    subitems = fragment.get("subitems") or []
    if not subitems:
        return [text] if text else []
    marks = [m.start() for x in subitems if (m := re.search(rf"(?:^| )\({x}\) ", text))]
    bounds = [0, *marks, len(text)]
    return [text[a:b].strip() for a, b in pairwise(bounds) if text[a:b].strip()]


@cache
def standard_text(name: str) -> Text:
    """按 SOURCES 逐页抽该文件原文的正文 (见 page_layout, 字号按各卷页范围) 后归一, 带版面; 多卷依次拼接, 各卷读页范围内
    (None 为全卷). 本文件的片段 (各部分) 都已出现后再多读一页即停, 余下的页与卷不读 (片段之后的下一段可能在下一页)."""
    doc = standard_doc(name)
    wanted = [piece for f in doc["fragments"] for piece in fragment_pieces(f)]
    lines: list[Line] = []
    found = False
    for path, pages in SOURCES[name].volumes:
        body = body_size(path, pages)
        indexes = range(pages[0] - 1, pages[1]) if pages else range(len(reference_pdf(path).pages))
        for i in indexes:
            lines += reference_page(path, i, doc["title"], body)
            if found:
                break
            found = all(w in normalise("\n".join(line.text for line in lines)) for w in wanted)
        if found:
            break
    return layout_text(lines)


# ── 片段 ID 与段码 (接口文档第 8.2 节) ──


def roman(n: int) -> str:
    """1 至 39 的小写罗马数字."""
    tens, ones = divmod(n, 10)
    return "x" * tens + ["", "i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix"][ones]


ROMAN = [roman(n) for n in range(1, 40)]
PARAGRAPH = re.compile(r"(?P<kind>Aus |A|R|s)?(?P<number>\d+(?:\.\d+)?[A-Z]*)(?P<groups>(?:\([0-9A-Za-z]+\))*)")
KINDS = {None: "num", "Aus ": "aus", "A": "app", "R": "req", "s": "law"}


@dataclass(frozen=True)
class Paragraph:
    """段落号的结构: 种类 (num 编号段, aus 澳洲增补段, app 应用材料, req APES 要求段, law 公司法条), 号, 子项各级."""

    kind: str
    number: str
    groups: tuple[str, ...]


def parse_paragraph(paragraph: str) -> Paragraph:
    """解析 YAML 的 paragraph: "33", "6(b)(i)", "Aus 13.2", "A44", "R410.15", "s307C(3)", "s293(3)(c)", "s328A".
    写法不认识时抛 ValueError."""
    found = PARAGRAPH.fullmatch(paragraph)
    if found is None:
        raise ValueError(f"段落号写法不认识: {paragraph}")
    kind = KINDS[found["kind"]]
    number = found["number"]
    if (kind in ("num", "app") and not number.isdigit()) or (kind in ("aus", "req") and "." not in number):
        raise ValueError(f"段落号写法不认识: {paragraph}")
    return Paragraph(kind, number, tuple(re.findall(r"\(([0-9A-Za-z]+)\)", found["groups"])))


def paragraph_code(paragraph: str) -> str:
    """段码: 编号段 P 加至少两位数字 (P05); Aus 段 Aus{n.m}; 应用材料 A{n}; APES 要求段 R{n.m}; 公司法 S{条}, 有款时加
    -{款}, 其下的项接小写字母. 子项接在段码后, 字母小写, 罗马数字前加点 (P06b.i, S293-3c)."""
    p = parse_paragraph(paragraph)
    base = {
        "num": f"P{int(p.number):02d}" if p.kind == "num" else "",
        "aus": f"Aus{p.number}",
        "app": f"A{p.number}",
        "req": f"R{p.number}",
        "law": f"S{p.number}",
    }[p.kind]
    groups = list(p.groups)
    if p.kind == "law" and groups:
        base += f"-{groups.pop(0)}"
    for level, group in enumerate(groups):  # 字母一级, 罗马数字一级
        base += f".{group}" if level >= 1 and group in ROMAN else group.lower()
    return base


def fragment_id_problem(stem: str, fragment: dict[str, Any]) -> str | None:
    """片段 ID 不合规则时的说明, 合则为 None: id 须为 {YAML 文件名}-{段码}; 有 subitems 时 paragraph 写整段号
    (不带子项), id 也不带子项, subitems 为小写字母."""
    paragraph, subitems = str(fragment["paragraph"]), fragment.get("subitems") or []
    try:
        parsed = parse_paragraph(paragraph)
    except ValueError as error:
        return f"{fragment['id']}: {error}"
    if subitems and (parsed.groups or not all(re.fullmatch(r"[a-z]{1,2}", x) for x in subitems)):
        return f"{fragment['id']}: 有 subitems 时 paragraph 写整段号, subitems 为小写字母"
    expected = f"{stem}-{paragraph_code(paragraph)}"
    return None if fragment["id"] == expected else f"{fragment['id']}: 应为 {expected}"


def standard_fragments() -> list[tuple[str, dict[str, Any]]]:
    """knowledge/standards 下全部片段: (文件名, 片段)."""
    return [
        (path.stem, fragment)
        for path in sorted((ROOT / "standards").glob("*.yaml"))
        for fragment in yaml.safe_load(path.read_text(encoding="utf-8"))["fragments"]
    ]


# ── 整段边界 (按版面) ──

SEPARATOR = re.compile(r"\*(?: ?\*)+")  # ASA 要求部分与应用材料之间的 "* * *" (居中; ASA 706 写作 "***")
APPENDIX = re.compile(r"Appendix(?: \d+)?")  # ASA 附录标题 (粗体), 其后不一定是单元外的行, heading_run 认不出
SENTENCE_END = re.compile(r"[.;:,]$")
PERIOD_END = re.compile(r"[.;:][”’\"')]*\s*$")  # 句子在行末结束
FULL_STOP = re.compile(r"\.[”’\"')]*\s*$")  # 行末为句号 (不含分号, 冒号: APES 跨行的 "(Ref:" 以冒号收尾)


def paragraph_marks(p: Paragraph) -> list[str]:
    """该段 (不看子项) 的段首记号: 编号段 ASA "33." 或 AASB "33", "Aus 13.2", "A44.", "R410.15", 公司法条号 "307C"."""
    return {
        "num": [f"{p.number}.", p.number],
        "aus": [f"Aus {p.number}"],
        "app": [f"A{p.number}."],
        "req": [f"R{p.number}"],
        "law": [p.number],
    }[p.kind]


def label(p: Paragraph) -> str:
    return f"{'s' if p.kind == 'law' else ''}{p.number}{''.join(f'({g})' for g in p.groups)}"


def visible(source: Text, k: int) -> bool:
    return source.starts[k] is not None


def parent(source: Text, k: int) -> int | None:
    """第 k 行记号的上级: 其前最近的, 记号后正文比本行的更靠左的记号行 (公司法的条首为条号与条标题). 同级的记号行
    (正文对齐) 与各行的续行都跳过."""
    body = source.lines[k].body
    for j in range(k - 1, -1, -1):
        line = source.lines[j]
        if visible(source, j) and line.mark is not None and line.body < body - INDENT_TOLERANCE:
            return j
    return None


def heading_run(source: Text, j: int, body: float) -> bool:
    """第 j 行起是否为一行至三行的小标题, 其后回到正文 x 为 body 的单元之外 (或 "* * *", 或串尾): 前一行以句末标点
    收尾; 各行没有记号, 字体相同, 首行大写开头, 都不以标点收尾 (公司法款间的斜体小标题, AASB 段间的小标题).
    字体须为斜体或粗体; 与正文同字体的只认一行, 前一行以句号收尾, 其后紧接单元外的记号行 (ASA 700 A56 之后的
    "Reference to a website ..."). 正文里整行为斜体的书名 ("Industry (Supervision) Act 1993).") 以标点收尾,
    粗体要求段里折到单独一行的连词 ("and", "or") 小写开头, 其后仍是本单元正文的, 都不算."""
    lines, starts = source.lines, source.starts
    first = lines[j]
    if first.mark is not None or not first.text[:1].isupper():
        return False
    previous = next((i for i in range(j - 1, -1, -1) if starts[i] is not None), None)
    if previous is None or not PERIOD_END.search(source[starts[previous] : starts[j]]):  # type: ignore[misc]
        return False
    if not any(s in first.font[0] for s in ("Italic", "Bold", "Oblique")):
        after = next((lines[i] for i in range(j + 1, len(lines)) if starts[i] is not None), None)
        return (
            bool(FULL_STOP.search(source[starts[previous] : starts[j]]))  # type: ignore[misc]
            and not SENTENCE_END.search(first.text)
            and after is not None
            and after.mark is not None
            and after.x < body - INDENT_TOLERANCE
        )
    count = 0
    for i in range(j, len(lines)):
        if starts[i] is None:
            continue
        line = lines[i]
        if count == 3 or line.mark is not None or line.font != first.font:
            return line.x < body - INDENT_TOLERANCE or bool(SEPARATOR.fullmatch(line.text))
        if SENTENCE_END.search(line.text):
            return False
        count += 1
    return True


def unit_end(source: Text, k: int) -> int:
    """以第 k 行记号起的单元 (段, 子项, 款, 条) 在串中的结束位置 (不含).

    单元在其后第一个这样的行之前结束: 行首回到记号后正文的 x 之外 (下一段, 同级或上级的下一子项, 段间的小标题),
    "* * *", 粗体的附录标题 (见 APPENDIX), 或小标题 (见 heading_run); 没有时到串尾. 续行, 下级子项与在正文 x 上的小标题
    (公司法条内的斜体小标题) 都在单元之内. 段一级的单元 (没有上级, 见 parent) 另有两种例外, 子项与款不适用:
    - 插入的澳洲子项 "Aus 28.1(c)" 记号在页边, 仍在段内;
    - 段末在子项之后另起一行, 与段号同 x 的结语 (ASA 700 第 17 段 "..., / the auditor shall modify"): 上一行句子未完,
      本行小写开头, 仍在段内. 子项之后这样的行是上级段的结语, 不属该子项 (ASA 502 6(b))."""
    unit = source.lines[k]
    body, previous = unit.body, source.starts[k]
    top = parent(source, k) is None
    for j in range(k + 1, len(source.lines)):
        start = source.starts[j]
        if start is None:
            continue
        line = source.lines[j]
        outside = line.x < body - INDENT_TOLERANCE and not (top and line.mark and AUS_SUBITEM.fullmatch(line.mark))
        if top and outside and line.mark is None and line.x >= unit.x - INDENT_TOLERANCE and line.text[:1].islower():
            outside = bool(PERIOD_END.search(source[previous:start]))
        appendix = bool(APPENDIX.fullmatch(line.text)) and "Bold" in line.font[0]
        if outside or appendix or SEPARATOR.fullmatch(line.text) or heading_run(source, j, body):
            return start - 1
        previous = start
    return len(source)


def intro_end(source: Text, k: int) -> int | None:
    """第 k 行起的段的引语 (第一个子项之前的部分) 的结束位置: 段内以第 k 行为上级的第一个记号行须为 (a), 否则为 None."""
    end = unit_end(source, k)
    for j in range(k + 1, len(source.lines)):
        start = source.starts[j]
        if start is None:
            continue
        if start > end:
            return None
        if source.lines[j].mark is not None and parent(source, j) == k:
            return start - 1 if source.lines[j].mark == "(a)" else None
    return None


def parent_problem(source: Text, k: int, p: Paragraph) -> str | None:
    """子项与公司法的款 (paragraph 带子项), 正文在第 k 行记号之后: 各级上级 (见 parent) 依次须为所写的上级子项与段号
    (公司法为条号). 合则为 None. 例: 6(b)(i) 的上级须是 (b), (b) 的上级须是第 6 段; 标成 6(a)(i) 或 7(b)(i) 都不合."""
    j: int | None = k
    for group in reversed(p.groups[:-1]):
        j = parent(source, j) if j is not None else None
        if j is None or source.lines[j].mark != f"({group})":
            found = source.lines[j].mark if j is not None else "没有"
            return f"上级子项不对: 其上级为 {found}, 不是 ({group}) ({label(p)})"
    j = parent(source, j) if j is not None else None
    if j is None or source.lines[j].mark not in paragraph_marks(p):
        found = source.lines[j].mark if j is not None else "没有"
        return f"上级段号不对: 其上级为 {found}, 不是 {p.number} ({label(p)})"
    return None


def subitem_problems(fragment: dict[str, Any]) -> list[str]:
    """subitems 所列子项须都在正文中 (标记 "(x) "), 且按原文顺序."""
    text = normalise(fragment.get("text") or "")
    subitems = fragment.get("subitems") or []
    found = {x: m.start() for x in subitems if (m := re.search(rf"(?:^| )\({x}\) ", text))}
    problems = [f"subitems 所列子项 ({x}) 不在正文中" for x in subitems if x not in found]
    positions = [found[x] for x in subitems if x in found]
    if positions != sorted(positions):
        problems.append("subitems 须按原文顺序排列")
    return problems


def boundary_problems(source: Text, fragment: dict[str, Any]) -> list[str]:
    """片段正文 (各部分) 须逐字出现在原文中, 且是整段 (或所列子项完整). 按版面判断 (source 须带版面, 见 Text):

    - 段首: 整段 (或有 subitems 时的引语) 紧接在所写段号的记号之后; 子项与款 (paragraph 带子项) 紧接在本级记号之后,
      各级上级依次为所写的子项与段号 (见 parent_problem). 所列的各子项从其记号 "(x)" 起, 上级为该段.
    - 段尾: 整段, 子项与所列子项都到单元结束处为止 (见 unit_end), 不多不少; 有 subitems 时引语到第一个子项 (a) 之前.
    """
    if not isinstance(source, Text) or len(source.lines) == 0:
        raise TypeError("source 须带版面: 用 pdf_text, standard_text, layout_text 或 join_texts 的结果")
    p = parse_paragraph(str(fragment["paragraph"]))
    pieces = fragment_pieces(fragment)
    if not pieces:
        return ["正文为空"]
    listed = bool(fragment.get("subitems"))
    problems = subitem_problems(fragment)
    position, head = 0, None  # 下一部分须在 position 之后; head 为段首记号行
    for n, piece in enumerate(pieces):
        where = "引语" if n == 0 and listed else "正文" if n == 0 else f"子项 {piece[:4]}"
        hits = [m.start() for m in re.finditer(re.escape(piece), source) if m.start() >= position]
        if not hits:
            problems.append(f"{where}不是原文子串")
            continue
        if n == 0:
            marks = [f"({p.groups[-1]})"] if p.groups else paragraph_marks(p)
            starts = [
                (i, k) for i in hits if (k := source.by_body.get(i)) is not None and source.lines[k].mark in marks
            ]
        else:
            mark = piece[: piece.index(")") + 1]
            starts = [
                (i, k) for i in hits if (k := source.by_start.get(i)) is not None and source.lines[k].mark == mark
            ]
        if not starts:
            problems.append(f"{where}不在段号之后")
            continue
        if n == 0 and p.groups:
            parents = {i: parent_problem(source, k, p) for i, k in starts}
            if all(parents.values()):
                problems.append(f"{where}的{parents[starts[0][0]]}")
                continue
            starts = [(i, k) for i, k in starts if parents[i] is None]
        if n > 0:
            if not (inside := [(i, k) for i, k in starts if parent(source, k) == head]):
                problems.append(f"{where}的上级不是该段 (不在该段之内)")
                continue
            starts = inside
        ends = {i: intro_end(source, k) if n == 0 and listed else unit_end(source, k) for i, k in starts}
        whole = [(i, k) for i, k in starts if ends[i] == i + len(piece)]
        if not whole:
            i, k = starts[0]
            head = k if n == 0 else head  # 引语不完整时所列子项仍按该段核对, 不连带报错
            end = ends[i]
            if end is None:
                problems.append(f"{where}之后不是子项 (a) (不是整段)")
            elif i + len(piece) < end:
                problems.append(f"{where}之后还有本段的内容 (不是整段)")
            else:
                problems.append(f"{where}超出了本段 (不是整段)")
            continue
        i, k = whole[0]
        head = k if n == 0 else head
        position = i + len(piece)
    return problems


# 人工核过的部分摘录: 只摘一段中的一组圆点列举项, 列举项没有 "(a)" 记号, subitems 写不出 (以后同类的圆点组摘录也登记
# 在这里, 不另开机制). 片段 ID → (原因, 片段之后紧接的原文的起首). 逐字与段首照常核对; 段尾不按整段, 改为核对片段之后
# 紧接的原文以所记的文字起首 (即摘到该组为止, 没多摘也没少摘该组的列举项).
PARTIAL_EXCERPTS: dict[str, tuple[str, str]] = {
    "ASA570-A3": ("A3 分 Financial, Operating, Other 三组列举, 只摘引语与 Financial 一组", "Operating •"),
}
NOT_WHOLE = "正文之后还有本段的内容 (不是整段)"


def partial_excerpt_problems(source: Text, fragment: dict[str, Any], after: str) -> list[str]:
    """部分摘录 (PARTIAL_EXCERPTS) 的核对: 逐字与段首同 boundary_problems, 只是不到段尾; 片段之后紧接的原文须以 after
    起首. 不符时报出片段之后实际接着的前 40 个字."""
    problems = boundary_problems(source, fragment)
    if not problems:
        return ["是整段, 不是部分摘录"]
    if problems != [NOT_WHOLE]:
        return [p for p in problems if p != NOT_WHOLE]
    [piece] = fragment_pieces(fragment)
    following = source[source.index(piece) + len(piece) :].lstrip()
    if not following.startswith(after):
        return [f"片段之后接的是 {following[:40]!r}, 不是登记的 {after!r}"]
    return []


@pytest.mark.parametrize("fragment_id", [f["id"] for _, f in standard_fragments() if (f.get("text") or "").strip()])
def test_fragment_text_is_verbatim_from_pdf(fragment_id: str) -> None:
    """knowledge/standards 下全部有正文的片段: 正文 (各部分) 逐字出现在 SOURCES 登记的原文中, 且为整段或所列子项完整;
    PARTIAL_EXCERPTS 登记的部分摘录段首照常, 段尾为所记的下文 (见 partial_excerpt_problems)."""
    stem, fragment = next((s, f) for s, f in standard_fragments() if f["id"] == fragment_id)
    source = standard_text(stem)
    if fragment_id in PARTIAL_EXCERPTS:
        assert partial_excerpt_problems(source, fragment, PARTIAL_EXCERPTS[fragment_id][1]) == []
    else:
        assert boundary_problems(source, fragment) == []


def a3_variant(change: str) -> dict[str, Any]:
    """ASA570-A3 的改写副本: drop_last 删去 Financial 组最后一条; with_operating 多摘 Operating 小标题与其第一条
    (取自原文, 逐字仍对)."""
    stem, fragment = next((s, f) for s, f in standard_fragments() if f["id"] == "ASA570-A3")
    lines = fragment["text"].rstrip("\n").split("\n")
    if change == "drop_last":
        return {**fragment, "text": "\n".join(lines[:-1])}
    source = standard_text(stem)
    [piece] = fragment_pieces(fragment)
    following = source[source.index(piece) + len(piece) :].lstrip()
    heading, rest = following.split(" ", 1)  # "Operating", "• 第一条 • 第二条 ..."
    return {**fragment, "text": "\n".join([*lines, heading, rest[: rest.index(" •")]])}


@pytest.mark.parametrize(
    ("change", "reported"),
    [("drop_last", "• Inability to obtain financing"), ("with_operating", "• Loss of key management")],
)
def test_partial_excerpt_must_end_at_its_group(change: str, reported: str) -> None:
    """部分摘录少摘 (删去 Financial 组最后一条) 或多摘 (多带 Operating 小标题与其第一条) 都报出, 消息带上片段之后
    实际接着的原文."""
    fragment = a3_variant(change)
    problems = partial_excerpt_problems(standard_text("ASA570"), fragment, PARTIAL_EXCERPTS["ASA570-A3"][1])
    assert len(problems) == 1, problems
    assert problems[0].startswith(f"片段之后接的是 '{reported}"), problems


def test_fragment_text_has_no_space_before_closing() -> None:
    """片段正文写原文的正常写法: CLOSING 标点 (; , . : )) 之前不留空白. 版面留下的空格 (如脚注上标去掉之后) 只在原文
    一侧由 normalise 吸收, 不写进片段."""
    found = [
        f"{f['id']}: {m.group(0)!r}"
        for _, f in standard_fragments()
        for m in re.finditer(rf"\S{{0,12}}\s+[{re.escape(CLOSING)}]", f.get("text") or "")
    ]
    assert found == []


def test_partial_excerpts_name_existing_fragments() -> None:
    """PARTIAL_EXCERPTS 的每一行都是有正文的片段: 删了片段或改了 ID 而登记表没跟着改时报出 (登记了却其实是整段的,
    由逐字测试的 problems 断言报出)."""
    fragments = {f["id"] for _, f in standard_fragments() if (f.get("text") or "").strip()}
    assert set(PARTIAL_EXCERPTS) <= fragments


def test_aasb15_p35_is_the_whole_paragraph() -> None:
    """AASB15-P35 逐字为第 35 段整段: 段号 35 紧接正文, (c) 项之后紧接 "36 An asset"; 删掉 (c) 项的副本不是整段."""
    stem, fragment = next((s, f) for s, f in standard_fragments() if f["id"] == "AASB15-P35")
    source = standard_text(stem)
    assert boundary_problems(source, fragment) == []
    [whole] = fragment_pieces(fragment)
    assert whole.endswith("an enforceable right to payment for performance completed to date (see paragraph 37).")
    assert source[source.index(whole) + len(whole) :].lstrip().startswith("36 An asset")
    lines = fragment["text"].splitlines()  # 引语一行, (a) 至 (c) 各一行
    assert len(lines) == 4 and [line[:4] for line in lines[1:]] == ["(a) ", "(b) ", "(c) "]
    without_c = fragment["text"][: fragment["text"].index("(c)")].rstrip()
    assert boundary_problems(source, {**fragment, "text": without_c}) == ["正文之后还有本段的内容 (不是整段)"]


def missing_sources(directory: Path) -> str | None:
    """目录下没有在 SOURCES 登记原文的 YAML, 有时返回含文件名的说明."""
    missing = [p.name for p in sorted(directory.glob("*.yaml")) if p.stem not in SOURCES]
    return f"SOURCES 没有登记: {', '.join(missing)}" if missing else None


def test_every_standards_file_has_a_source(tmp_path: Path) -> None:
    """knowledge/standards 下每个 YAML 都在 SOURCES 中登记原文; 缺的报出文件名 (自测: tmp 目录多一个 ASA999.yaml)."""
    assert missing_sources(ROOT / "standards") is None
    shutil.copytree(ROOT / "standards", tmp_path / "standards")
    (tmp_path / "standards" / "ASA999.yaml").write_text("fragments: []\n", encoding="utf-8")
    assert missing_sources(tmp_path / "standards") == "SOURCES 没有登记: ASA999.yaml"


def test_fragment_ids_follow_rule() -> None:
    """全部片段的 id 为 {YAML 文件名}-{段码} (段码由 paragraph 推出, 有 subitems 时按整段). 规则的正例与反例见下."""
    assert [problem for stem, f in standard_fragments() if (problem := fragment_id_problem(stem, f))] == []
    good = [
        ("ASA570", "Aus 13.2", [], "ASA570-Aus13.2"),
        ("ASA240", "A44", [], "ASA240-A44"),
        ("ASA210", "6(b)(i)", [], "ASA210-P06b.i"),
        ("APES110", "R410.15", [], "APES110-R410.15"),
        ("CA2001", "s307C(3)", [], "CA2001-S307C-3"),
        ("CA2001", "s323D(2A)", [], "CA2001-S323D-2A"),
        ("CA2001", "s293(3)(c)", [], "CA2001-S293-3c"),
        ("CA2001", "s328A", [], "CA2001-S328A"),
        ("ASA240", "33", ["a"], "ASA240-P33"),
        ("ASA550", "10", ["a", "b"], "ASA550-P10"),
        ("ASA450", "5", [], "ASA450-P05"),
    ]
    for stem, paragraph, subitems, fid in good:
        assert fragment_id_problem(stem, {"id": fid, "paragraph": paragraph, "subitems": subitems}) is None, fid
    bad = [
        ("CA2001", "s307C(3)", [], "CorpAct-s307C-3"),  # 公司法只用 CA2001 一个文件, 不用 CorpAct 前缀
        ("ASA570", "Aus 13.2", [], "ASA570-P13.2"),  # Aus 段写成 P
        ("ASA450", "5", [], "ASA450-P5"),  # 缺补零
        ("ASA240", "33", ["a"], "ASA240-P33a"),  # 只摘部分子项时 id 不带子项
        ("ASA240", "33(a)", ["a"], "ASA240-P33"),  # 有 subitems 时 paragraph 写整段号
        ("ASA240", "Para 33", [], "ASA240-P33"),  # 写法不认识
    ]
    for stem, paragraph, subitems, fid in bad:
        assert fragment_id_problem(stem, {"id": fid, "paragraph": paragraph, "subitems": subitems}) is not None, fid


# 整段边界的正例: 原文取自 PDF (见各项页码), 写在测试里, 不进 knowledge. 反例为同一片段只摘前半句
T240 = "The Auditor's Responsibilities Relating to Fraud in an Audit of a Financial Report"
ASA240 = ("01_审计准则_ASA/ASA_240.pdf", 16, 17, T240)  # 第 32, 33 段, 第 33(a) 段跨页, 中间夹脚注 9-11 与页眉
CA_VOL2 = CA_VOLUME.format(2)
ASA240_P33 = (
    "Irrespective of the auditor’s assessment of the risks of management override of controls, the auditor shall"
    " design and perform audit procedures to:\n"
    "(a) Test the appropriateness of journal entries recorded in the general ledger and other adjustments made in the"
    " preparation of the financial report. In designing and performing audit procedures for such tests, the auditor"
    " shall:\n"
    "(i) Make enquiries of individuals involved in the financial reporting process about inappropriate or unusual"
    " activity relating to the processing of journal entries and other adjustments;\n"
    "(ii) Select journal entries and other adjustments made at the end of a reporting period; and\n"
    "(iii) Consider the need to test journal entries and other adjustments throughout the period."
)
ASA570_AUS131 = (
    "In evaluating management’s assessment of the entity’s ability to continue as a going concern, the auditor shall"
    " consider the relevant period as defined in paragraph Aus 13.2 of this Auditing Standard, which may be the same"
    " or may differ from that used by management to make its assessment as required by the applicable financial"
    " reporting framework. If management’s assessment of the entity’s ability to continue as a going concern covers"
    " less than the relevant period, the auditor shall request management to extend its assessment period to"
    " correspond to the relevant period used by the auditor."
)
ASA502_P6 = ("01_审计准则_ASA/ASA_502.pdf", 8, 9, None)
ASA502_P6B = (
    "the auditor is unable to obtain sufficient appropriate audit evidence by performing alternative audit procedures,"
)
APES_R1151B = (
    "Behave in a manner consistent with the profession’s responsibility to act in the public interest in all"
    " Professional Activities and business relationships; and"
)
EXAMPLES: dict[str, tuple[tuple[str, int, int, str | None], dict[str, Any]]] = {
    "编号段 ASA 240 第 32 段": (
        ASA240,
        {
            "paragraph": "32",
            "text": "Management is in a unique position to perpetrate fraud because of management’s ability to"
            " manipulate accounting records and prepare a fraudulent financial report by overriding controls that"
            " otherwise appear to be operating effectively. Although the level of risk of management override of"
            " controls will vary from entity to entity, the risk is nevertheless present in all entities. Due to the"
            " unpredictable way in which such override could occur, it is a risk of material misstatement due to"
            " fraud and thus a significant risk.",
        },
    ),
    "只摘子项 ASA 240 第 33 段 (a)": (ASA240, {"paragraph": "33", "subitems": ["a"], "text": ASA240_P33}),
    "Aus 段 ASA 570 Aus 13.2": (
        ("01_审计准则_ASA/ASA_570.pdf", 11, 11, "Going Concern"),
        {
            "paragraph": "Aus 13.2",
            "text": "Relevant period means the period of approximately twelve months from the date of the auditor’s"
            " current report to the expected date of the auditor’s report for:\n"
            "(a) the next annual reporting date in the case of an annual financial report; or\n"
            "(b) the corresponding reporting period for the following year in the case of an interim financial"
            " reporting period.",
        },
    ),
    "Aus 段 ASA 570 Aus 13.1 (段末句号为小字)": (
        ("01_审计准则_ASA/ASA_570.pdf", 11, 11, "Going Concern"),
        {"paragraph": "Aus 13.1", "text": ASA570_AUS131},
    ),
    "应用材料 ASA 240 A44": (
        ("01_审计准则_ASA/ASA_240.pdf", 31, 31, T240),
        {
            "paragraph": "A44",
            "text": "When identifying and selecting journal entries and other adjustments for testing and determining"
            " the appropriate method of examining the underlying support for the items selected, the following matters"
            " are of relevance: The identification and assessment of the risks of material misstatement due to fraud"
            " – the presence of fraud risk factors and other information obtained during the auditor’s"
            " identification and assessment of the risks of material misstatement due to fraud may assist the auditor"
            " to identify specific classes of journal entries and other adjustments for testing. Controls that have"
            " been implemented over journal entries and other adjustments – effective controls over the preparation"
            " and posting of journal entries and other adjustments may reduce the extent of substantive testing"
            " necessary, provided that the auditor has tested the operating effectiveness of the controls. The"
            " entity’s financial reporting process and the nature of evidence that can be obtained – for many"
            " entities routine processing of transactions involves a combination of manual and automated controls."
            " Similarly, the processing of journal entries and other adjustments may involve both manual and"
            " automated controls. When information technology is used in the financial reporting process, journal"
            " entries and other adjustments may exist only in electronic form. The characteristics of fraudulent"
            " journal entries or other adjustments – inappropriate journal entries or other adjustments often have"
            " unique identifying characteristics. Such characteristics may include entries (a) made to unrelated,"
            " unusual, or seldom-used accounts, (b) made by individuals who typically do not make journal entries,"
            " (c) recorded at the end of the period or as post-closing entries that have little or no explanation or"
            " description, (d) made either before or during the preparation of the financial report that do not have"
            " account numbers, or (e) containing round numbers or consistent ending numbers. The nature and"
            " complexity of the accounts – inappropriate journal entries or adjustments may be applied to accounts"
            " that (a) contain transactions that are complex or unusual in nature, (b) contain significant estimates"
            " and period-end adjustments, (c) have been prone to misstatements in the past, (d) have not been"
            " reconciled on a timely basis or contain unreconciled differences, (e) contain inter-company"
            " transactions, or (f) are otherwise associated with an identified risk of material misstatement due to"
            " fraud. In audits of entities that have several locations or components, consideration is given to the"
            " need to select journal entries from multiple locations. Journal entries or other adjustments processed"
            " outside the normal course of business – nonstandard journal entries may not be subject to the same"
            " nature and extent of controls as those journal entries used on a recurring basis to record"
            " transactions such as monthly sales, purchases and cash disbursements.",
        },
    ),
    "子项 ASA 210 第 6(b)(i) 段": (
        ("01_审计准则_ASA/ASA_210.pdf", 10, 10, "Agreeing the Terms of Audit Engagements"),
        {
            "paragraph": "6(b)(i)",
            "text": "For the preparation of the financial report in accordance with the applicable financial reporting"
            " framework, including where relevant their fair presentation;",
        },
    ),
    "APES 要求段 R410.15 (跨页)": (
        (APES110_PDF, 204, 205, None),
        {
            "paragraph": "R410.15",
            "text": "When for each of five consecutive years total fees from an Audit Client that is not a Public"
            " Interest Entity represent, or are likely to represent, more than 30% of the total fees received by the"
            " Firm, the Firm shall determine whether either of the following actions might be a safeguard to reduce"
            " the threats created to an Acceptable Level, and if so, apply it:\n"
            "(a) Prior to the audit opinion being issued on the fifth year’s Financial Statements, have a Member, who"
            " is not a Member of the Firm expressing the opinion on the Financial Statements, review the fifth year’s"
            " audit work; or\n"
            "(b) After the audit opinion on the fifth year’s Financial Statements has been issued, and before the"
            " audit opinion is issued on the sixth year’s Financial Statements, have a Member, who is not a Member of"
            " the Firm expressing the opinion on the Financial Statements, or a Professional Body review the fifth"
            " year’s audit work.",
        },
    ),
    "公司法的款 s307C(3) (跨页)": (
        (CA_VOL2, 131, 133, None),  # 第 131 页为条首 (307C 条号与标题), 上级段号的核对要它
        {
            "paragraph": "s307C(3)",
            "text": "If an audit firm or audit company conducts:\n"
            "(a) an audit or review of the financial report for a financial year; or\n"
            "(b) an audit or review of the financial report for a half-year; or\n"
            "(ba) an audit of the sustainability report for the financial year;\n"
            "the lead auditor for the audit must give the directors of the company, registered scheme, registrable"
            " superannuation entity or disclosing entity:\n"
            "(c) a written declaration that, to the best of the lead auditor’s knowledge and belief, there have"
            " been:\n"
            "(i) no contraventions of the auditor independence requirements of this Act in relation to the audit or"
            " review; and\n"
            "(ii) no contraventions of any applicable code of professional conduct in relation to the audit or"
            " review; or\n"
            "(d) a written declaration that, to the best of the lead auditor’s knowledge and belief, the only"
            " contraventions of:\n"
            "(i) the auditor independence requirements of this Act in relation to the audit or review; or\n"
            "(ii) any applicable code of professional conduct in relation to the audit or review;\n"
            "are those contraventions details of which are set out in the declaration.",
        },
    ),
    "公司法的款 s298(1) (下一款为插入款 1AA)": (
        (CA_VOL2, 99, 99, None),
        {
            "paragraph": "s298(1)",
            "text": "The company, registered scheme, registrable superannuation entity or disclosing entity must"
            " prepare a directors’ report for each financial year.",
        },
    ),
    "公司法的插入款 s298(1AA)": (
        (CA_VOL2, 99, 99, None),
        {
            "paragraph": "s298(1AA)",
            "text": "Except in the case of a company limited by guarantee or a company covered under subsection"
            " (1AC), the report must include:\n"
            "(a) the general information required by sections 299 (all entities) and 299A (additional requirements"
            " for listed entities); and\n"
            "(b) the specific information required by sections 300, 300A and 300C; and\n"
            "(c) a copy of the auditor’s declaration under section 307C in relation to the audit for the financial"
            " year.",
        },
    ),
    "公司法的条 s293 (跨页)": (
        (CA_VOL2, 83, 84, None),
        {
            "paragraph": "s293",
            "text": "Small proprietary company—shareholder direction\n"
            "(1) Shareholders with at least 5% of the votes in a small proprietary company may give the company a"
            " direction to:\n"
            "(a) prepare a financial report and directors’ report for a financial year; and\n"
            "(b) send them to all shareholders.\n"
            "(2) The direction must be:\n"
            "(a) signed by the shareholders giving the direction; and\n"
            "(b) made no later than 12 months after the end of the financial year concerned.\n"
            "(3) The direction may specify all or any of the following:\n"
            "(a) that the financial report does not have to comply with some or all of the accounting standards;\n"
            "(b) that a directors’ report or a part of that report need not be prepared;\n"
            "(c) that the financial report is to be audited.",
        },
    ),
    # 以下各例的段尾 (审核第 2 轮所列): 要求部分的最后一段, 长小标题, 逗号收尾的子项, 小写开头的下一段, 页边的段末结语,
    # 斜体小标题, Division 标题, APES 的应用材料, 被推右的段号
    "编号段 ASA 520 第 7 段 (要求部分的最后一段, 其后为 * * *)": (
        ("01_审计准则_ASA/ASA_520.pdf", 9, 10, "Analytical Procedures"),
        {
            "paragraph": "7",
            "text": "If analytical procedures performed in accordance with this Auditing Standard identify fluctuations"
            " or relationships that are inconsistent with other relevant information or that differ from expected"
            " values by a significant amount, the auditor shall investigate such differences by:\n"
            "(a) Enquiring of management and obtaining appropriate audit evidence relevant to management’s responses;"
            " and\n"
            "(b) Performing other audit procedures as necessary in the circumstances.",
        },
    ),
    "编号段 ASA 210 第 2 段 (已删除的段, 其后为页边的小标题)": (
        ("01_审计准则_ASA/ASA_210.pdf", 9, 9, "Agreeing the Terms of Audit Engagements"),
        {"paragraph": "2", "text": "[Deleted by the AUASB. Refer Aus 0.3]"},
    ),
    "编号段 ASA 560 第 9 段 (其后小标题 129 字)": (
        ("01_审计准则_ASA/ASA_560.pdf", 10, 10, None),
        {
            "paragraph": "9",
            "text": "The auditor shall request management and, where appropriate, those charged with governance, to"
            " provide a written representation in accordance with ASA 580 that all events occurring subsequent to"
            " the date of the financial report and for which the applicable financial reporting framework requires"
            " adjustment or disclosure have been adjusted or disclosed.",
        },
    ),
    "子项 ASA 560 第 10(b) 段 (以逗号收尾)": (
        ("01_审计准则_ASA/ASA_560.pdf", 10, 10, None),
        {"paragraph": "10(b)", "text": "Determine whether the financial report needs amendment; and if so,"},
    ),
    "应用材料 ASA 720 A9 (A10 小写开头)": (
        ("01_审计准则_ASA/ASA_720.pdf", 16, 16, None),
        {
            "paragraph": "A9",
            "text": "In some cases, the applicable financial reporting framework may require specific disclosures but"
            " permit them to be located outside of the financial report. As such disclosures are required by the"
            " applicable financial reporting framework, they form part of the financial report. Accordingly, they do"
            " not constitute other information for the purpose of this Auditing Standard.",
        },
    ),
    "编号段 ASA 700 第 17 段 (子项之后的结语在页边, 跨页)": (
        ("01_审计准则_ASA/ASA_700.pdf", 13, 14, None),
        {
            "paragraph": "17",
            "text": "If the auditor:\n"
            "(a) concludes that, based on the audit evidence obtained, the financial report as a whole is not free"
            " from material misstatement; or\n"
            "(b) is unable to obtain sufficient appropriate audit evidence to conclude that the financial report as"
            " a whole is free from material misstatement,\n"
            "the auditor shall modify the opinion in the auditor’s report in accordance with ASA 705.",
        },
    ),
    "应用材料 ASA 540 A139 (段号宽, 正文被推右)": (
        ("01_审计准则_ASA/ASA_540.pdf", 54, 54, None),
        {
            "paragraph": "A139",
            "text": "In determining whether, based on the audit procedures performed and evidence obtained,"
            " management’s point estimate and related disclosures are reasonable, or are misstated:\n"
            "• When the audit evidence supports a range, the size of the range may be wide and, in some"
            " circumstances, may be multiples of materiality for the financial report as a whole (see also paragraph"
            " A125). Although a wide range may be appropriate in the circumstances, it may indicate that it is"
            " important for the auditor to reconsider whether sufficient appropriate audit evidence has been obtained"
            " regarding the reasonableness of the amounts within the range.\n"
            "• The audit evidence may support a point estimate that differs from management’s point estimate. In"
            " such circumstances, the difference between the auditor’s point estimate and management’s point"
            " estimate constitutes a misstatement.\n"
            "• The audit evidence may support a range that does not include management’s point estimate. In such"
            " circumstances, the misstatement is the difference between management’s point estimate and the nearest"
            " point of the auditor’s range.",
        },
    ),
    "APES 要求段 R410.13 (其后为小标题与 410.14 A1)": (
        (APES110_PDF, 203, 203, None),
        {
            "paragraph": "R410.13",
            "text": "When a significant part of the fees due from an Audit Client remains unpaid for a long time, the"
            " Firm shall determine:\n"
            "(a) Whether the overdue fees might be equivalent to a loan to the client, in which case the requirements"
            " and application material set out in Section 511 are applicable; and\n"
            "(b) Whether it is appropriate for the Firm to be re-appointed or continue the Audit Engagement.",
        },
    ),
    "公司法的款 s323D(1) (条首前为 Division 标题, 其后为斜体小标题)": (
        (CA_VOL2, 163, 163, None),
        {
            "paragraph": "s323D(1)",
            "text": "The first financial year for a company, registered scheme or disclosing entity starts on the day"
            " on which it is registered or incorporated. It lasts for 12 months or the period (not longer than 18"
            " months) determined by the directors.",
        },
    ),
    "公司法的款 s323DAAA(1) (末行为斜体书名)": (
        (CA_VOL2, 164, 164, None),
        {
            "paragraph": "s323DAAA(1)",
            "text": "The financial year for a registrable superannuation entity is the entity’s year of income (within"
            " the meaning of the Superannuation Industry (Supervision) Act 1993).",
        },
    ),
    "公司法的款 s323D(3) (前一款为插入款, 款号更靠左)": (
        (CA_VOL2, 163, 164, None),
        {
            "paragraph": "s323D(3)",
            "text": "A company, registered scheme or disclosing entity that has to prepare consolidated financial"
            " statements must do whatever is necessary to ensure that the financial years of the consolidated"
            " entities are synchronised with its own financial years. It must achieve this synchronisation by the"
            " end of 12 months after the situation that calls for consolidation arises.",
        },
    ),
    "公司法的款 s321(1) (条标题以 ASIC 开头)": (
        (CA_VOL2, 158, 158, None),
        {
            "paragraph": "s321(1)",
            "text": "ASIC may give a company, registered scheme, registrable superannuation entity or disclosing entity"
            " a direction to lodge with ASIC a copy of reports prepared or obtained by it under Division 1 or 2.",
        },
    ),
    "公司法的款 s324DD(5) (条末, 其后为 Division 标题)": (
        (CA_VOL2, 239, 242, None),
        {
            "paragraph": "s324DD(5)",
            "text": "A person does not commit an offence because of a contravention of subsection (3) in relation to an"
            " individual acting as lead or review auditor on behalf of an audit company at a particular time if the"
            " person has reasonable grounds to believe that the audit company had in place at that time a quality"
            " control system that provided reasonable assurance (taking into account the size and nature of the"
            " audit practice of the audit company) that the audit company and its employees complied with the"
            " requirements of this Division.",
        },
    ),
    "公司法的款 s325(1) (条首前为 Division 与 Subdivision 标题)": (
        (CA_VOL2, 241, 242, None),
        {
            "paragraph": "s325(1)",
            "text": "The directors of a proprietary company may appoint an auditor for the company if an auditor has"
            " not been appointed by the company in general meeting.",
        },
    ),
    "子项之后有上级段的结语 ASA 502 第 6 段 (b)": (ASA502_P6, {"paragraph": "6(b)", "text": ASA502_P6B}),
    "粗体要求段折到单独一行的连词 APES R115.1 (b)": (
        (APES110_PDF, 45, 45, None),
        {"paragraph": "R115.1(b)", "text": APES_R1151B},
    ),
    "分隔写作 *** ASA 706 第 12 段": (
        ("01_审计准则_ASA/ASA_706.pdf", 8, 9, None),
        {
            "paragraph": "12",
            "text": "If the auditor expects to include an Emphasis of Matter or an Other Matter paragraph in the"
            " auditor’s report, the auditor shall communicate with those charged with governance regarding this"
            " expectation and the wording of this paragraph.",
        },
    ),
}


def first_half(text: str) -> str:
    """只摘前半句: 第一句 (到第一个句号, 分号或冒号) 的前一半词."""
    words = re.split(r"(?<=[.;:])\s", normalise(text))[0].split()
    return " ".join(words[: len(words) // 2])


@pytest.mark.parametrize("name", list(EXAMPLES))
def test_whole_paragraph_boundaries(name: str) -> None:
    """按段码种类判断整段边界: 正例 (整段或所列子项完整) 通过; 只摘前半句的反例报出不是整段; 去掉开头一个词的反例
    报出不在段号之后."""
    (path, first, last, title), fragment = EXAMPLES[name]
    source = pdf_text(path, first, last, title)
    assert boundary_problems(source, fragment) == []
    problems = boundary_problems(source, {**fragment, "text": first_half(fragment["text"])})
    assert any("不是整段" in p for p in problems), problems
    tail = normalise(fragment["text"]).split(" ", 1)[1]
    problems = boundary_problems(source, {**fragment, "text": tail})
    assert problems and "不在段号之后" in problems[0], problems


def test_extraction_drops_footnotes_and_running_heads() -> None:
    """抽取去掉上标脚注号, 页底脚注行与页眉页脚: ASA 240 第 33(a) 段跨页, 中间夹脚注 9-11 与页眉, 原文照样逐字通过;
    改一个词即不是原文子串."""
    source = pdf_text(*ASA240)
    assert "See ASA 330, paragraph 5" not in source and "Auditing Standard ASA 240" not in source
    assert T240 not in source and "Authorised Version" not in source
    assert "have been implemented). Responses to the Assessed Risks" in source  # 上标 9 已去掉
    fragment = {"paragraph": "33", "subitems": ["a"], "text": ASA240_P33}
    assert boundary_problems(source, fragment) == []
    changed = {**fragment, "text": ASA240_P33.replace("Select journal entries", "Choose journal entries")}
    assert boundary_problems(source, changed) == ["子项 (a) 不是原文子串"]


@pytest.mark.parametrize(
    ("path", "page", "dropped", "kept"),
    [
        ("01_审计准则_ASA/ASA_240.pdf", 11, "[Note: For operative dates", "after 1 January 2010."),  # 小字生效日期注
        ("03_会计准则_AASB/AASB_1057.pdf", 52, "prepare.44", "prepare."),  # 贴在词后的小字脚注号
        (
            "01_审计准则_ASA/ASA_315.pdf",
            15,
            "A121‒A122)",
            "in paragraph 22(b). The entity’s process",
        ),  # 两栏表的引用
        ("01_审计准则_ASA/ASA_101.pdf", 7, "ASA 101 - 7 - AUDITING STANDARD", "Early adoption"),  # 不带 compiled 的页脚
        (  # 页脚用 "─"
            "01_审计准则_ASA/ASA_502.pdf",
            10,
            "compiled - 10 - AUDITING STANDARD",
            "Application and Other Explanatory Material",
        ),
        ("01_审计准则_ASA/ASA_502.pdf", 10, "Compiled Auditing Standard ASA 502", "Litigation and Claims A1."),  # 页首
        ("02_质量管理_ASQM/ASQM_1.pdf", 22, "ASQM 1 - compiled", "33. The firm shall establish"),  # ASQM 页脚
        (  # ASQM 页首与折成两行的标题
            "02_质量管理_ASQM/ASQM_1.pdf",
            23,
            "Other Assurance or Related Services Engagements",
            "(a) The reasons for the assessments",
        ),
        ("03_会计准则_AASB/AASB_15.pdf", 33, "APPENDIX B", "B34 When another party"),  # AASB 附录的页脚
        (  # 页首标题的连字符为 MS Mincho: 只看字母是否粗斜体, 不靠 YAML 的 title
            "01_审计准则_ASA/ASA_510.pdf",
            10,
            "Initial Audit Engagements",
            "(ii) Evaluating whether audit procedures",
        ),
        (CA_VOLUME.format(2), 169, "Chapter 2M", "Division"),  # 以表格为主的页: 正文字号按整卷取, 10pt 页眉去掉
    ],
)
def test_extraction_drops_small_print_and_footers(path: str, page: int, dropped: str, kept: str) -> None:
    """正文基线上的小字只留段末标点与行首记号; 页眉页脚的各种写法; 正文字号按整卷 (审核第 2 轮 R80-11, 第 3 轮
    R80-15)."""
    source = pdf_text(path, page, page)
    assert dropped not in source and kept in source


def test_extraction_keeps_small_leading_marks_and_drops_wrapped_title() -> None:
    """行首小字的段号留下 (ASA 701 第 9 段的 "9." 为 9pt); ASA 600 页首的准则标题折成两行, 第二行 "Auditors)" 也去掉."""
    asa701 = pdf_text("01_审计准则_ASA/ASA_701.pdf", 9, 9)
    assert "9. The auditor shall determine" in asa701
    assert marks_of(asa701, "9. The auditor shall determine") == ["9."]
    asa600 = pdf_text("01_审计准则_ASA/ASA_600.pdf", 23, 23)
    assert "Auditors)" not in asa600 and "When such centralised activities" in asa600


def layout(*rows: tuple[str, float, str | None, float, str]) -> Text:
    """构造的版面: (文字, x, 记号, 正文 x, 字体名); 字号一律 11."""
    return layout_text([Line(text, x, mark, body, (font, 11.0)) for text, x, mark, body, font in rows])


REGULAR, ITALIC = "TimesNewRomanPSMT", "TimesNewRomanPS-ItalicMT"


def test_heading_rules_on_constructed_layout() -> None:
    """段内整行斜体: 前一行句子未完 (书名接在句中) 时不是小标题, 仍在段内; 前一行句末收尾, 其后回到页边时是小标题.
    小写开头的页边结语只在上一行句子未完时留在段内."""
    rows = [
        ("1. The entity shall apply the", 70.9, "1.", 106.3, REGULAR),
        ("requirements of the", 106.3, None, 106.3, REGULAR),
        ("Corporations Act 2001", 106.3, None, 106.3, ITALIC),
        ("2. Next paragraph.", 70.9, "2.", 106.3, REGULAR),
        ("2. Next paragraph.", 70.9, "2.", 106.3, REGULAR),
    ]
    inline = layout(*rows)
    text = "The entity shall apply the requirements of the Corporations Act 2001"
    assert boundary_problems(inline, {"paragraph": "1", "text": text}) == []
    heading = layout(rows[0], ("requirements of the Act.", 106.3, None, 106.3, REGULAR), rows[2], rows[3], rows[4])
    assert (
        boundary_problems(heading, {"paragraph": "1", "text": "The entity shall apply the requirements of the Act."})
        == []
    )
    closing = [
        ("3. If the auditor:", 70.9, "3.", 106.3, REGULAR),
        ("(a) concludes so,", 106.3, "(a)", 141.9, REGULAR),
        ("(b) is unable,", 106.3, "(b)", 141.9, REGULAR),
        ("the auditor shall modify.", 70.9, None, 70.9, REGULAR),
        ("Heading", 70.9, None, 70.9, REGULAR),
        ("4. Next.", 70.9, "4.", 106.3, REGULAR),
        ("4. Next.", 70.9, "4.", 106.3, REGULAR),
    ]
    whole = "If the auditor: (a) concludes so, (b) is unable, the auditor shall modify."
    assert boundary_problems(layout(*closing), {"paragraph": "3", "text": whole}) == []
    assert boundary_problems(layout(*closing), {"paragraph": "3(b)", "text": "is unable,"}) == []
    # 整行斜体而以句号收尾 (书名在句末) 的不是小标题
    titled = layout(
        ("5. The entity applies AASB 108.", 70.9, "5.", 106.3, REGULAR),
        ("Accounting Policies.", 106.3, None, 106.3, ITALIC),
        ("6. Next.", 70.9, "6.", 106.3, REGULAR),
        ("6. Next.", 70.9, "6.", 106.3, REGULAR),
    )
    text = "The entity applies AASB 108. Accounting Policies."
    assert boundary_problems(titled, {"paragraph": "5", "text": text}) == []


def test_unit_end_exceptions_only_for_paragraphs() -> None:
    """页边结语与插入的澳洲子项只在段一级的单元内 (审核第 3 轮 R80-13, R80-18): 子项之后与子项记号同 x 的小写结语
    属上级段, 不并进最后一个子项; 结语前一行以句号收尾时, 结语不属该段."""
    asa502 = pdf_text(*ASA502_P6)
    closing = "the auditor shall modify the opinion"
    assert boundary_problems(asa502, {"paragraph": "6(b)", "text": f"{ASA502_P6B} {closing}"}) == [
        "正文超出了本段 (不是整段)"
    ]
    rows = [
        ("3. If the auditor:", 70.9, "3.", 106.3, REGULAR),
        ("(a) concludes so,", 106.3, "(a)", 141.9, REGULAR),
        ("(b) is unable,", 106.3, "(b)", 141.9, REGULAR),
        ("the auditor shall modify.", 106.3, None, 106.3, REGULAR),
        ("4. Next.", 70.9, "4.", 106.3, REGULAR),
        ("4. Next.", 70.9, "4.", 106.3, REGULAR),
    ]
    source = layout(*rows)
    assert boundary_problems(source, {"paragraph": "3(b)", "text": "is unable,"}) == []
    whole = "If the auditor: (a) concludes so, (b) is unable, the auditor shall modify."
    assert boundary_problems(source, {"paragraph": "3", "text": whole}) == []
    ended = layout(
        ("9. The auditor shall act.", 70.9, "9.", 106.3, REGULAR),
        ("this line is not part of paragraph 9.", 70.9, None, 70.9, REGULAR),
        ("10. Next.", 70.9, "10.", 106.3, REGULAR),
        ("10. Next.", 70.9, "10.", 106.3, REGULAR),
    )
    assert boundary_problems(ended, {"paragraph": "9", "text": "The auditor shall act."}) == []
    aus = layout(
        ("28. The report shall:", 70.9, "28.", 106.3, REGULAR),
        ("(c) [Deleted by the AUASB. Refer Aus 28.1(c)]", 106.3, "(c)", 141.9, REGULAR),
        ("Aus 28.1(c) Name the framework.", 56.7, "Aus 28.1(c)", 141.9, REGULAR),
        ("29. Next.", 70.9, "29.", 106.3, REGULAR),
        ("29. Next.", 70.9, "29.", 106.3, REGULAR),
    )
    deleted = "[Deleted by the AUASB. Refer Aus 28.1(c)]"
    assert boundary_problems(aus, {"paragraph": "28(c)", "text": deleted}) == []
    assert (
        boundary_problems(
            aus, {"paragraph": "28", "text": f"The report shall: (c) {deleted} Aus 28.1(c) Name the framework."}
        )
        == []
    )


def test_conjunction_line_and_separator_end_units() -> None:
    """粗体要求段折到单独一行的连词不是小标题, 仍在子项内; "***" 是分隔 (审核第 3 轮 R80-14)."""
    apes = pdf_text(APES110_PDF, 45, 45)
    cut = APES_R1151B.removesuffix(" and")
    assert boundary_problems(apes, {"paragraph": "R115.1(b)", "text": cut}) == ["正文之后还有本段的内容 (不是整段)"]
    rows = [
        ("1. Text before.", 70.9, "1.", 106.3, REGULAR),
        ("***", 300.0, None, 300.0, REGULAR),
        ("A1. Next.", 70.9, "A1.", 106.3, REGULAR),
    ]
    assert boundary_problems(layout(*rows), {"paragraph": "1", "text": "Text before."}) == []


@pytest.mark.parametrize(
    ("path", "first", "last", "start", "tail", "after"),
    [
        (  # 与正文同字体的小标题, 其后是下一段
            "01_审计准则_ASA/ASA_700.pdf",
            31,
            32,
            "Paragraph 41 permits",
            "forms part of our auditor’s report.",
            "Reference to a website of an appropriate authority A57.",
        ),
        (  # 粗体的附录标题, 其后是附录的标题
            "01_审计准则_ASA/ASA_710.pdf",
            15,
            16,
            "The predecessor auditor may be unable",
            "the 20X1 financial report taken as a whole.",
            "Appendix 1 Illustrations",
        ),
    ],
)
def test_unit_ends_before_plain_heading_and_appendix(
    path: str, first: int, last: int, start: str, tail: str, after: str
) -> None:
    """段尾的小标题为正文字体或附录标题时, 段在其前结束 (审核第 3 轮 R80-14)."""
    source = pdf_text(path, first, last)
    [k] = [
        k
        for k, line in enumerate(source.lines)
        if line.mark and (i := source.starts[k]) is not None and source.startswith(start, i + len(line.mark) + 1)
    ]
    end = unit_end(source, k)
    assert source[:end].endswith(tail) and source[end + 1 :].startswith(after)


def test_plain_heading_needs_full_stop_and_appendix_needs_bold() -> None:
    """与正文同字体的小标题须前一行以句号收尾: 以冒号收尾时 (APES 跨行的 "(Ref:") 该行仍在段内. 与正文同字体的
    "Appendix 1" 不是附录标题 (审核第 3 轮 R80-14)."""
    colon = layout(
        ("1. The auditor shall consider:", 70.9, "1.", 106.3, REGULAR),
        ("Factors listed in the table", 106.3, None, 106.3, REGULAR),
        ("2. Next.", 70.9, "2.", 106.3, REGULAR),
        ("2. Next.", 70.9, "2.", 106.3, REGULAR),
    )
    text = "The auditor shall consider: Factors listed in the table"
    assert boundary_problems(colon, {"paragraph": "1", "text": text}) == []
    appendix = layout(
        ("3. The illustration is in", 70.9, "3.", 106.3, REGULAR),
        ("Appendix 1", 106.3, None, 106.3, REGULAR),
        ("of this Auditing Standard.", 106.3, None, 106.3, REGULAR),
        ("4. Next.", 70.9, "4.", 106.3, REGULAR),
        ("4. Next.", 70.9, "4.", 106.3, REGULAR),
    )
    text = "The illustration is in Appendix 1 of this Auditing Standard."
    assert boundary_problems(appendix, {"paragraph": "3", "text": text}) == []


def test_join_texts_settles_extracted_lines() -> None:
    """join_texts 拼接各段核定前的行, 不把已核定的行再核定一次: 结果同整体抽取 (审核第 3 轮 R80-18)."""
    rows = [
        Line("1. First paragraph", 70.9, "1.", 106.3, (REGULAR, 11.0)),
        Line("continues here.", 106.3, None, 106.3, (REGULAR, 11.0)),
        Line("2. Second paragraph", 70.9, "2.", 106.3, (REGULAR, 11.0)),
        Line("continues here.", 106.3, None, 106.3, (REGULAR, 11.0)),
    ]
    joined = join_texts(layout_text(rows[:1]), layout_text(rows[1:]))
    whole = layout_text(rows)
    assert (str(joined), joined.lines, joined.starts) == (str(whole), whole.lines, whole.starts)
    assert layout_text(rows[:1]).lines[0].mark is None  # 只有一行时取不到栏位, 单独核定不认记号


def test_listed_subitems_must_be_under_the_paragraph() -> None:
    """所列子项的上级须是该段: 别段的同号子项不算 (审核第 3 轮 R80-17)."""
    rows = [
        ("5. Intro:", 70.9, "5.", 106.3, REGULAR),
        ("(a) first item;", 106.3, "(a)", 141.9, REGULAR),
        ("6. Other:", 70.9, "6.", 106.3, REGULAR),
        ("(a) other item;", 106.3, "(a)", 141.9, REGULAR),
        ("(b) second item.", 106.3, "(b)", 141.9, REGULAR),
        ("7. Next.", 70.9, "7.", 106.3, REGULAR),
        ("7. Next.", 70.9, "7.", 106.3, REGULAR),
    ]
    fragment = {"paragraph": "5", "subitems": ["a", "b"], "text": "Intro:\n(a) first item;\n(b) second item."}
    assert boundary_problems(layout(*rows), fragment) == ["子项 (b) 的上级不是该段 (不在该段之内)"]


def test_by_body_needs_the_mark_at_line_start() -> None:
    """记号行的正文起点只在串中该行起点确为记号时登记 (审核第 3 轮 R80-17): 行文字与记号不符的行不登记."""
    source = layout(
        ("1. First.", 70.9, "1.", 106.3, REGULAR),
        ("and 2. Second.", 70.9, "2.", 106.3, REGULAR),
        ("3. Third.", 70.9, "3.", 106.3, REGULAR),
        ("3. Third.", 70.9, "3.", 106.3, REGULAR),
    )
    assert source.lines[1].mark == "2."
    assert sorted(source.by_body.values()) == [0, 2, 3]


def test_intro_must_be_followed_by_subitem_a() -> None:
    """有 subitems 时引语之后须紧接子项 (a): 段内第一个子项是 (b) 时报出 (构造的版面)."""
    rows = [
        ("7. Intro:", 70.9, "7.", 106.3, REGULAR),
        ("(b) Item b", 106.3, "(b)", 141.9, REGULAR),
        ("continues", 141.9, None, 141.9, REGULAR),
        ("here.", 141.9, None, 141.9, REGULAR),
        ("8. Next.", 70.9, "8.", 106.3, REGULAR),
        ("8. Next.", 70.9, "8.", 106.3, REGULAR),
    ]
    problems = boundary_problems(
        layout(*rows), {"paragraph": "7", "subitems": ["b"], "text": "Intro:\n(b) Item b continues here."}
    )
    assert problems == ["引语之后不是子项 (a) (不是整段)"]


def marks_of(source: Text, *prefixes: str) -> list[str | None]:
    """以各前缀开头的第一行的行首记号."""
    return [next(line.mark for line in source.lines if line.text.startswith(prefix)) for prefix in prefixes]


def test_marks_by_layout() -> None:
    """行首记号按悬挂缩进认 (与其后正文隔开大于词距): 各种段码的记号; 正文续行以段号或数字开头时不是记号."""
    asa = pdf_text("01_审计准则_ASA/ASA_570.pdf", 11, 11, "Going Concern")
    assert marks_of(asa, "12. The", "Aus 13.1 In", "Aus 13.2 Relevant", "(a) the next", "14. In") == [
        "12.",
        "Aus 13.1",
        "Aus 13.2",
        "(a)",
        "14.",
    ]
    assert marks_of(asa, "Aus 13.2 of this") == [None]  # 正文里引用段号的续行
    law = pdf_text(CA_VOL2, 163, 163)
    assert marks_of(law, "323D Meaning", "(1) The first", "(2A) A subsequent", "(a) start at") == [
        "323D",
        "(1)",
        "(2A)",
        "(a)",
    ]
    assert marks_of(law, "18 months) determined") == [None]  # 续行以数字开头, 只隔一个词距
    apes = pdf_text(APES110_PDF, 203, 204)
    assert marks_of(apes, "R410.13 When", "410.14 A1 When", "AUST R410.14.2 When", "AUST 410.14.1 A1") == [
        "R410.13",
        "410.14 A1",
        "AUST R410.14.2",
        "AUST 410.14.1 A1",
    ]
    assert marks_of(pdf_text("03_会计准则_AASB/AASB_15.pdf", 14, 14), "48 The", "(a) variable") == ["48", "(a)"]
    assert marks_of(pdf_text("01_审计准则_ASA/ASA_700.pdf", 15, 15), "Aus 28.1(c) Includes") == ["Aus 28.1(c)"]
    # 续行行首引用段号: 两端对齐拉宽了词距, 但正文 x 不在栏位上; 或段号后小写开头
    assert marks_of(pdf_text("01_审计准则_ASA/ASA_600.pdf", 23, 23), "A19. In a group audit") == [None]
    assert marks_of(pdf_text(APES110_PDF, 167, 167), "R390.12 to R390.17") == [None]
    assert marks_of(pdf_text(APES110_PDF, 164, 164), "R390.14 sets out") == [None]  # 正文 x 落在栏位上, 靠小写排除
    # 段号宽, 正文被推右: 正文 x 取续行的栏位
    a139 = pdf_text("01_审计准则_ASA/ASA_540.pdf", 54, 54)
    assert next(line.body for line in a139.lines if line.mark == "A139.") == 106.0
    # 正文在 128, 左边 126 与 128 都是栏位 (APES 第 60-70 页续行 169 行与 2 行), 取行数最多的 126 (构造的版面)
    rows = [("R220.4 When", 70.9, "R220.4", 128.0, REGULAR)]
    rows += [("continued", 125.6, None, 125.6, REGULAR)] * 3 + [("table", 128.0, None, 128.0, REGULAR)] * 2
    assert layout(*rows).lines[0].body == 126.0


def word(text: str, x0: float, width: float = 5.0) -> list[dict[str, Any]]:
    """构造一个词的字 (等宽), 供 line_of 用."""
    font = {"fontname": "TimesNewRomanPSMT", "size": 11.0}
    return [{"text": c, "x0": x0 + i * width, "x1": x0 + (i + 1) * width, **font} for i, c in enumerate(text)]


def test_line_marks_need_more_than_word_spacing() -> None:
    """行首记号与正文的间隔须大于词距 (MARK_GAP): 隔一个词距的是正文续行 (如 "18 months) determined")."""
    hanging = [*word("(1)", 158.7), *word("The", 182.0)]  # 间隔 8.3 pt
    assert line_of("(1) The", hanging).mark == "(1)"
    spaced = [*word("18", 177.2), *word("months)", 190.0)]  # 间隔 2.8 pt
    assert line_of("18 months)", spaced).mark is None


@pytest.mark.parametrize(
    ("name", "paragraph", "cut"),
    [
        ("Aus 段 ASA 570 Aus 13.1 (段末句号为小字)", "Aus 13.1", "as defined in paragraph"),  # 其后为 "Aus 13.2 of"
        (None, "s295A(1)", "a declaration under subsection"),  # 其后为 "(2) of this section"
    ],
)
def test_body_references_are_not_paragraph_ends(name: str | None, paragraph: str, cut: str) -> None:
    """只摘到正文里引用下一段号之前的半句, 不当成整段: 段号引用之后是小写, 且半句不以句末标点收尾."""
    if name is None:
        source = pdf_text(CA_VOL2, 89, 90, None)
        start = source.index("If the company, disclosing entity or registered scheme is listed")
    else:
        (path, first, last, title), fragment = EXAMPLES[name]
        source = pdf_text(path, first, last, title)
        start = source.index(normalise(fragment["text"]))
    half = source[start : source.index(cut, start) + len(cut)]
    problems = boundary_problems(source, {"paragraph": paragraph, "text": half})
    assert problems and all("不是整段" in p for p in problems), problems


@pytest.mark.parametrize(
    ("name", "wrong"),
    [
        ("子项 ASA 210 第 6(b)(i) 段", "6(a)(i)"),  # 上级子项标错
        ("子项 ASA 210 第 6(b)(i) 段", "5(b)(i)"),  # 第 5 段之后另起了第 6 段
        ("子项 ASA 210 第 6(b)(i) 段", "7(b)(i)"),  # 第 7 段在其后
        ("公司法的款 s307C(3) (跨页)", "s308(3)"),
        ("公司法的插入款 s298(1AA)", "s297(1AA)"),
        ("公司法的款 s323D(1) (条首前为 Division 标题, 其后为斜体小标题)", "s323C(1)"),  # 条首前没有句末标点
        ("公司法的款 s321(1) (条标题以 ASIC 开头)", "s320(1)"),
        ("公司法的款 s325(1) (条首前为 Division 与 Subdivision 标题)", "s324DD(1)"),
        ("公司法的款 s323DAAA(1) (末行为斜体书名)", "s323D(1)"),  # 条号宽, 条标题被推右
        ("子项 ASA 560 第 10(b) 段 (以逗号收尾)", "11(b)"),
    ],
)
def test_parent_numbers_are_checked(name: str, wrong: str) -> None:
    """子项与款的上级段号, 上级子项要对: 正文照抄而 paragraph 标错上级时报出."""
    (path, first, last, title), fragment = EXAMPLES[name]
    source = pdf_text(path, first, last, title)
    problems = boundary_problems(source, {**fragment, "paragraph": wrong})
    assert problems and "上级" in problems[0], problems


def test_parent_numbers_with_contents_pages() -> None:
    """ASA 550 第 1 至 4 页 (封面日期与目录) 接第 13 页: 上级按版面取紧挨的上级段, 目录里的段号 "23" 与日期不当成段首;
    22(a) 标成 23(a) 报上级段号不对 (审核第 2 轮 R80-09: 旧写法把目录里的段号当成段首)."""
    path, title = "01_审计准则_ASA/ASA_550.pdf", "Related Parties"
    source = join_texts(pdf_text(path, 1, 4, title), pdf_text(path, 13, 13, title))
    start = source.index("Promptly communicate the relevant information to the other members of the engagement team")
    text = source[start : source.index("(b)", start) - 1]
    assert boundary_problems(source, {"paragraph": "22(a)", "text": text}) == []
    problems = boundary_problems(source, {"paragraph": "23(a)", "text": text})
    assert problems and "上级段号不对" in problems[0], problems


def test_listed_subitems_must_be_in_text() -> None:
    """subitems 列了而正文中没有的子项报出; 所列子项须按原文顺序."""
    source = pdf_text(*ASA240)
    fragment = {"paragraph": "33", "subitems": ["a", "c"], "text": ASA240_P33}
    assert "subitems 所列子项 (c) 不在正文中" in boundary_problems(source, fragment)
    two = {"paragraph": "293", "subitems": ["b", "a"], "text": "x:\n(a) y; and\n(b) z."}
    assert subitem_problems(two) == ["subitems 须按原文顺序排列"]


def cover_date(text: str, cover: str) -> str | None:
    """封面上的编译日期: "Compilation date: 2 December 2025" (Date 大小写, 冒号后空格数不限) 记为 2025-12-02,
    "Compiled as at July 2025" 记为 2025-07; 找不到为 None."""
    if cover == "Compiled as at":
        found = re.search(r"Compiled as at\s+(\w+ \d{4})", text)
        return datetime.strptime(found[1], "%B %Y").strftime("%Y-%m") if found else None
    found = re.search(r"Compilation [Dd]ate:\s+(\d{1,2} \w+ \d{4})", text)
    return datetime.strptime(found[1], "%d %B %Y").date().isoformat() if found else None


@pytest.mark.parametrize("name", list(SOURCES))
def test_compiled_date_matches_pdf(name: str) -> None:
    """SOURCES 每行: 第一卷封面的编译日期等于 YAML 的 compiled (还没有 YAML 时等于登记的 compiled)."""
    source = SOURCES[name]
    found = cover_date(reference_pdf(source.volumes[0][0]).pages[0].extract_text() or "", source.cover)
    path = ROOT / "standards" / f"{name}.yaml"
    expected = [v for v in (standard_doc(name)["compiled"] if path.exists() else None, source.compiled) if v]
    assert expected and all(found == v for v in expected), (found, expected)


def test_cover_date_formats() -> None:
    """ASA 210 封面 "Compilation Date:    2 December 2025" (大写 D, 多个空格) 解析为 2025-12-02; APES 110 为月年."""
    assert cover_date("Compilation Date:    2 December 2025", "Compilation date") == "2025-12-02"
    assert cover_date("Compilation date: 27 August 2026", "Compilation date") == "2026-08-27"
    assert cover_date("Compiled as at July 2025", "Compiled as at") == "2025-07"
    with pdfplumber.open(REFERENCE / "01_审计准则_ASA" / "ASA_210.pdf") as pdf:
        assert cover_date(pdf.pages[0].extract_text() or "", "Compilation date") == "2025-12-02"


def test_normalise_joins_hyphen_and_drops_refs() -> None:
    assert normalise("bill-and-\nhold (Ref: Para. A3-A6)\n  next") == "bill-and-hold next"
    # 标点前的空白去掉: 脚注上标去掉后留下的空格 (ASA 570 第 23(a) 段 "ASA 7055; and"), 与原文的正常写法相同
    assert normalise("ASA 705 ; and (a , b ) x .") == normalise("ASA 705; and (a, b) x.") == "ASA 705; and (a, b) x."


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
    with pdfplumber.open(REFERENCE / SOURCES["ASA700"].volumes[0][0]) as pdf:
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
        lib.resolve_params(lib.load("REV-08"), ENGAGEMENT, state, None, traits=DatasetTraits(), profile=None)
    assert lib.resolve_params(lib.load("CSH-04"), ENGAGEMENT, state, None, traits=DatasetTraits(), profile=None) == {
        "as_of": date(2026, 6, 30)
    }


def test_resolve_params_by_risk_without_that_level() -> None:
    """REV-02 只给了 medium 以上的取值, 低风险时报错而不是 KeyError."""
    lib = ProcedureLibrary(ROOT)

    with pytest.raises(ValueError, match="low"):
        lib.resolve_params(
            lib.load("REV-02"),
            ENGAGEMENT,
            initial_state(ENGAGEMENT, [], 1),
            RiskLevel.low,
            traits=DatasetTraits(),
            profile=None,
        )


def test_resolve_params_state_ref_not_set() -> None:
    """重要性还没经闸门确定时执行 FIN-04: 报错, 不以 None 作参数."""
    lib = ProcedureLibrary(ROOT)

    with pytest.raises(ValueError, match="materiality_cents"):
        lib.resolve_params(
            lib.load("FIN-04"), ENGAGEMENT, initial_state(ENGAGEMENT, [], 1), None, traits=DatasetTraits(), profile=None
        )


def test_resolve_params_literals_are_copies() -> None:
    """改解析结果里的列表与字典, 模板不受影响."""
    lib = ProcedureLibrary(ROOT)

    out = lib.resolve_params(
        lib.load("PLN-01"), ENGAGEMENT, initial_state(ENGAGEMENT, [], 1), None, traits=DatasetTraits(), profile=None
    )
    out["candidates"][0]["pct_low"] = 0
    out["candidates"].append({"basis": "x"})

    candidates = lib.load("PLN-01").params["candidates"]
    assert len(candidates) == 3
    assert candidates[0]["pct_low"] == 0.005


# ── R95: 数据集声明与 {ref: dataset.<键>} ──

# Xero演示数据集设计.md 第 5.2 节的 JSON 原文 (过长的一行拆成两段相接)
DESIGN_TRAITS = (
    r"""{"dataset": "xero/20261017-q1",
 "data_as_of": "2026-10-17",
 "prior_period_start": "2025-07-01",
 "entry_timestamps_reliable": false,
 "entry_timestamps_note": "Created timestamps are regenerated when the Xero demo company is reset; """
    r"""15 journals carry created times after 19 October 2026 and up to 26 November 2026.",
 "reversal_reference_pattern": "Reversal of ID (\\d+)",
 "reversal_entry_no_template": "XJ-{1}",
 "counterparty_prefixes": ["payment to", "payment from", "payment:"],
 "audit_adjustment_entry_prefix": "AJE-",
 "payroll_accounts": {"wages_expense": ["477"], "super_expense": ["478"], "super_payable": ["826"],
                      "wages_payable": ["804"], "leave_provision": ["805"]},
 "payroll_register": "payroll/employees.csv"}"""
)
# 测试模板: 三个参数各引一处声明 (日期, 嵌套的科目列表, 有缺省值的前缀)
REF_TEMPLATE = (
    "id: X-REF\nname: 自测\nphase: P1_Planning\ncycle: Planning\nowner: A4\nengine: rule\nautonomy_level: L3\n"
    "params:\n  a: {ref: dataset.prior_period_start}\n  b: {ref: dataset.payroll_accounts.wages_expense}\n"
    "  c: {ref: dataset.audit_adjustment_entry_prefix}\n"
)


def design_traits() -> DatasetTraits:
    return DatasetTraits.model_validate_json(DESIGN_TRAITS)


def ref_library(tmp_path: Path, template: str = REF_TEMPLATE) -> ProcedureLibrary:
    """现有 knowledge 加一个测试模板 (文件名取其 id) 的程序库."""
    root = tmp_path / "knowledge"
    shutil.copytree(ROOT, root)
    pid = yaml.safe_load(template)["id"]
    (root / "procedures" / f"{pid}.yaml").write_text(template, encoding="utf-8")
    return ProcedureLibrary(root)


def test_dataset_traits_defaults() -> None:
    """没有声明文件的数据集 (Numbat) 的缺省值, 设计稿第 5.2 节."""
    assert DatasetTraits().model_dump() == {
        "dataset": None,
        "data_as_of": None,
        "prior_period_start": None,
        "entry_timestamps_reliable": True,
        "entry_timestamps_note": None,
        "reversal_reference_pattern": None,
        "reversal_entry_no_template": None,
        "counterparty_prefixes": ["payment to", "payment from", "payment:"],
        "audit_adjustment_entry_prefix": "AJE-",
        "payroll_accounts": None,
        "payroll_register": None,
    }


@pytest.mark.external
def test_dataset_traits_from_design() -> None:
    traits = design_traits()
    assert (traits.prior_period_start, traits.data_as_of, traits.entry_timestamps_reliable) == (
        date(2025, 7, 1),
        date(2026, 10, 17),
        False,
    )
    assert (traits.reversal_reference_pattern, traits.reversal_entry_no_template) == (r"Reversal of ID (\d+)", "XJ-{1}")
    assert traits.payroll_accounts is not None
    assert (traits.payroll_accounts.wages_expense, traits.payroll_accounts.leave_provision) == (["477"], ["805"])
    assert DatasetTraits.model_validate(json.loads(DESIGN_TRAITS)) == traits
    # 补造目录的声明同第 5.2 节 (没带补造目录的检出不比); 只比补造与设计, 不核对快照哈希
    supplements = optional(supplements_dir, "20261017-q1", False)
    if supplements is not None:
        declared = json.loads((supplements / "dataset_traits.json").read_text(encoding="utf-8"))
        assert declared == json.loads(DESIGN_TRAITS)


@pytest.mark.parametrize(
    "change",
    [
        {"colour": "red"},  # 未知键
        {"reversal_reference_pattern": "Reversal of ID (\\d+"},  # 不能编译
        {"reversal_reference_pattern": "Reversal of ID \\d+"},  # 没有捕获组
        {"reversal_reference_pattern": "(Reversal) of ID (\\d+)"},  # 两个捕获组
        {"reversal_entry_no_template": "XJ-{n}"},  # 模板不含 {1}
        {"reversal_entry_no_template": None},  # 只给格式
        {"reversal_reference_pattern": None},  # 只给模板
    ],
)
def test_dataset_traits_rejects_bad_declarations(change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        DatasetTraits.model_validate({**json.loads(DESIGN_TRAITS), **change})


def test_dataset_refs_resolve(tmp_path: Path) -> None:
    """dataset. 的 ref 按声明的字段路径取值, 支持一层嵌套; 值为空时以 None 执行 (与状态 ref 不同); 结果深拷贝."""
    lib = ref_library(tmp_path)
    template, state = lib.load("X-REF"), initial_state(ENGAGEMENT, [], 1)
    traits = design_traits()

    out = lib.resolve_params(template, ENGAGEMENT, state, None, traits=traits, profile=None)
    assert out == {"a": date(2025, 7, 1), "b": ["477"], "c": "AJE-"}
    assert lib.resolve_params(template, ENGAGEMENT, state, None, traits=DatasetTraits(), profile=None) == {
        "a": None,
        "b": None,
        "c": "AJE-",
    }
    out["b"].append("478")
    assert traits.payroll_accounts is not None and traits.payroll_accounts.wages_expense == ["477"]


def test_dataset_ref_to_a_nested_model_is_a_dict(tmp_path: Path) -> None:
    template = REF_TEMPLATE.split("params:")[0] + "params:\n  accounts: {ref: dataset.payroll_accounts}\n"
    lib = ref_library(tmp_path, template)
    out = lib.resolve_params(
        lib.load("X-REF"), ENGAGEMENT, initial_state(ENGAGEMENT, [], 1), None, traits=design_traits(), profile=None
    )
    assert out == {
        "accounts": {
            k: [v]
            for k, v in zip(
                ["wages_expense", "super_expense", "super_payable", "wages_payable", "leave_provision"],
                ["477", "478", "826", "804", "805"],
                strict=True,
            )
        }
    }


@pytest.mark.parametrize(
    "ref",
    [
        "dataset.no_such_key",
        "dataset",
        "dataset.payroll_accounts.no_such_key",
        "dataset.payroll_accounts.wages_expense.x",
        "dataset.audit_adjustment_entry_prefix.x",
        "profile.x",  # 画像没有这个字段
        "nosuch",
    ],
)
def test_refs_are_checked_on_load(tmp_path: Path, ref: str) -> None:
    """模板的 ref 写错时 ProcedureLibrary 构造即抛 ValueError, 消息含 "程序编号.参数名" 与该 ref."""
    template = REF_TEMPLATE.split("params:")[0] + f"params:\n  bad: {{ref: {ref}}}\n"
    with pytest.raises(ValueError, match="程序参数的 ref 写错") as e:
        ref_library(tmp_path, template)
    assert "X-REF.bad" in str(e.value) and ref in str(e.value)


def test_refs_of_disabled_templates_are_checked_on_load(tmp_path: Path) -> None:
    """功能未启用的模板 (going_concern 不在基线里) 写错 ref 也在构造时抛错, 不因功能关着而漏检."""
    head = REF_TEMPLATE.split("params:")[0] + "feature: going_concern\n"
    lib = ref_library(tmp_path / "good", head + "params:\n  a: {ref: dataset.prior_period_start}\n")
    assert not lib.enabled("X-REF") and "X-REF" in {t.id for t in lib.all(include_disabled=True)}

    with pytest.raises(ValueError, match="程序参数的 ref 写错") as e:
        ref_library(tmp_path / "bad", head + "params:\n  bad: {ref: dataset.no_such_key}\n")
    assert "X-REF.bad" in str(e.value) and "dataset.no_such_key" in str(e.value)


class Item(Model):
    code: str


class ContainerTraits(Model):
    """测试用的命名空间模型: 容器字段与可为空的嵌套模型."""

    items: list[Item] = []
    mapping: dict[str, Item] = {}
    maybe: Item | None = None


def test_ref_paths_do_not_enter_containers(monkeypatch: pytest.MonkeyPatch) -> None:
    """ref 路径只往下进入嵌套模型 (含可为空的); 列表, 字典容器里的模型按字段名取不到, 加载时就报未知 ref,
    不留到解析时 (namespace_value 对列表 getattr 会抛 AttributeError)."""
    monkeypatch.setitem(procedure_library.REF_NAMESPACES, "probe", ContainerTraits)

    assert ref_problem("probe.items.code") == "未知 ref probe.items.code"
    assert ref_problem("probe.mapping.code") == "未知 ref probe.mapping.code"
    assert ref_problem("probe.items") is None and ref_problem("probe.mapping") is None  # 容器可以整个取
    assert ref_problem("probe.maybe.code") is None
    assert ref_problem("probe.maybe.nope") == "未知 ref probe.maybe.nope"


@pytest.mark.parametrize("ref", ["dataset.no_such_key", "nosuch", "profile.x", "dataset.payroll_accounts.x"])
def test_resolve_params_rejects_bad_refs_of_direct_templates(tmp_path: Path, ref: str) -> None:
    """不经程序库加载, 直接构造的模板写错 ref: resolve_params 抛 KeyError, 消息含 "程序编号.参数名" 与该 ref."""
    lib = ref_library(tmp_path)
    template = lib.load("X-REF").model_copy(update={"params": {"bad": {"ref": ref}}})
    with pytest.raises(KeyError) as e:
        lib.resolve_params(
            template, ENGAGEMENT, initial_state(ENGAGEMENT, [], 1), None, traits=design_traits(), profile=None
        )
    assert "X-REF.bad" in str(e.value) and ref in str(e.value)


# 引了 dataset. 的模板参数 (程序编号 → 参数名 → ref): 声明不同时只有这些参数随之不同
DATASET_REFS = {
    "PLN-07": {"prior_period_start": "prior_period_start"},  # 上期起始日 (需求 R102)
    "CSH-02": {  # D03, D04 剔除作废对 (需求 R105)
        "reversal_reference_pattern": "reversal_reference_pattern",
        "reversal_entry_no_template": "reversal_entry_no_template",
    },
}


def test_existing_templates_pass_ref_checks_and_ignore_traits() -> None:
    """现有模板都过加载校验 (含未启用的); 只有 DATASET_REFS 里的参数引 dataset., 声明不同时只有它们随之不同 (取各自
    声明的值), 其余解析结果相同."""
    lib = ProcedureLibrary(ROOT)
    state = initial_state(ENGAGEMENT, [], 1)
    state["materiality_cents"], state["performance_materiality_cents"], state["clearly_trivial_cents"] = 3, 2, 1
    traits = design_traits()
    for template in lib.all(include_disabled=True):
        refs = {k: v["ref"] for k, v in template.params.items() if isinstance(v, dict) and "ref" in v}
        expected = DATASET_REFS.get(template.id, {})
        assert {k: r.removeprefix("dataset.") for k, r in refs.items() if r.startswith("dataset.")} == expected
        risk = RiskLevel.significant if any("by_risk" in str(v) for v in template.params.values()) else None
        default = lib.resolve_params(template, ENGAGEMENT, state, risk, traits=DatasetTraits(), profile=None)
        declared = lib.resolve_params(template, ENGAGEMENT, state, risk, traits=traits, profile=None)
        for key, field in expected.items():
            assert (default.pop(key), declared.pop(key)) == (getattr(DatasetTraits(), field), getattr(traits, field))
        assert declared == default, template.id


# ── R127: profile. 参数引用 (本项目采用版本的客户画像) ──

PROFILE_TEMPLATE = REF_TEMPLATE.split("params:")[0] + (
    "params:\n  limit: {ref: profile.approval_limit_cents}\n  fee: {ref: profile.fees.audit_fee_cents}\n"
    "  audited: {ref: profile.prior_period_audited, optional: true}\n"
    "  prefix: {ref: dataset.audit_adjustment_entry_prefix}\n"
)


def xero_profile() -> ClientProfile:
    """补造的 Xero 演示画像 CLIENT-XERO-3C1332BB-Q1 v1 (审批限额 500000, 审计费 480000, 上期未审)."""
    return ClientProfile.model_validate_json((supplements_dir("20261017-q1") / "client_profile.json").read_bytes())


def resolve_profile(lib: ProcedureLibrary, profile: ClientProfile | None) -> dict[str, Any]:
    return lib.resolve_params(
        lib.load("X-REF"), ENGAGEMENT, initial_state(ENGAGEMENT, [], 1), None, traits=DatasetTraits(), profile=profile
    )


@pytest.mark.external
def test_profile_refs_resolve(tmp_path: Path) -> None:
    """profile. 按画像的字段路径取值, 支持一层嵌套 (fees.audit_fee_cents); 为 None 时抛 ValueError, 写了 optional 的
    以 None 执行; 没有画像时引用 profile. 也抛错; dataset. 不受影响."""
    lib = ref_library(tmp_path, PROFILE_TEMPLATE)
    profile = xero_profile()
    assert resolve_profile(lib, profile) == {"limit": 500000, "fee": 480000, "audited": False, "prefix": "AJE-"}
    unset = profile.model_copy(update={"approval_limit_cents": None})
    with pytest.raises(ValueError, match=r"X-REF\.limit: approval_limit_cents 尚未确定"):
        resolve_profile(lib, unset)
    with pytest.raises(ValueError, match=r"X-REF\.fee: fees\.audit_fee_cents 尚未确定"):
        resolve_profile(lib, profile.model_copy(update={"fees": None}))  # 路径中途为 None
    with pytest.raises(ValueError, match="没有本项目采用的客户画像"):
        resolve_profile(lib, None)

    optional = REF_TEMPLATE.split("params:")[0] + (
        "params:\n  limit: {ref: profile.approval_limit_cents, optional: true}\n"
        "  audited: {ref: profile.prior_period_audited, optional: false}\n"
    )
    lib = ref_library(tmp_path / "optional", optional)
    assert resolve_profile(lib, unset) == {"limit": None, "audited": False}
    with pytest.raises(ValueError, match="prior_period_audited 尚未确定"):  # optional: false 同不写
        resolve_profile(lib, unset.model_copy(update={"prior_period_audited": None}))


@pytest.mark.external
def test_profile_ref_results_are_copies(tmp_path: Path) -> None:
    """profile. 的取值是新建的 JSON 兼容值: 列表里的模型也是字典, 改返回值不会改到画像."""
    template = REF_TEMPLATE.split("params:")[0] + "params:\n  people: {ref: profile.key_personnel}\n"
    lib = ref_library(tmp_path, template)
    profile = xero_profile()
    out = resolve_profile(lib, profile)
    assert out["people"] == [p.model_dump(mode="json") for p in profile.key_personnel]
    assert [p["name"] for p in out["people"]] == ["Alex Morgan"] and type(out["people"][0]) is dict
    out["people"][0]["name"] = "Changed"
    out["people"].clear()
    assert len(profile.key_personnel) == 1 and profile.key_personnel[0].name == "Alex Morgan"


@pytest.mark.external
def test_profile_ref_snapshot_reads_back_equal(tmp_path: Path) -> None:
    """参数快照经检查点与 Store (JSON) 读回后与首次执行相等: 列表中的模型, 嵌套模型中的列表, 日期与枚举都已转成
    JSON 兼容的值 (R127 第 1 轮审核 R127-03)."""
    from backend.contracts.models import ProcedureRun, RunStatus, RunTrigger
    from backend.orchestration.checkpoint import checkpoint_serde

    template = REF_TEMPLATE.split("params:")[0] + (
        "params:\n  people: {ref: profile.key_personnel}\n  gov: {ref: profile.governance}\n"
        "  keep: {ref: profile.retention}\n  framework: {ref: profile.applicable_framework}\n"
        "  parties: {ref: profile.related_parties}\n"
    )
    lib = ref_library(tmp_path, template)
    profile = xero_profile().model_copy(
        update={"retention": Retention(created=date(2025, 7, 1), review_by=date(2032, 7, 1), basis="APP 11.2")}
    )
    params = resolve_profile(lib, profile)
    assert params["keep"] == {"created": "2025-07-01", "review_by": "2032-07-01", "basis": "APP 11.2"}
    assert params["framework"] == "tier2_gpfs" and type(params["framework"]) is str
    assert type(params["gov"]["members"][0]) is dict and type(params["parties"][0]) is dict
    assert json.loads(json.dumps(params)) == params
    run = ProcedureRun(
        id="RUN-X-REF-1",
        procedure_id="X-REF",
        run_no=1,
        trigger=RunTrigger.initial,
        status=RunStatus.running,
        params_snapshot=params,
        started_at=ENGAGEMENT.created_at,
    )
    assert ProcedureRun.model_validate_json(run.model_dump_json()) == run  # Store 存 JSON 后读回
    serde = checkpoint_serde()
    assert serde.loads_typed(serde.dumps_typed({"r": run}))["r"] == run  # 检查点读回


@pytest.mark.parametrize(
    "param",
    [
        "{ref: profile.no_such_field}",
        "{ref: profile.fees.no_such_field}",
        "{ref: profile.key_personnel.name}",  # 列表里的模型按字段名取不到
        "{ref: profile.approval_limit_cents, optional: sometimes}",  # optional 须为布尔
        "{ref: profile.approval_limit_cents, default: 1}",  # optional 以外的多余键
        "{ref: dataset.prior_period_start, optional: true}",  # optional 只用于 profile.
        "{ref: materiality_cents, optional: true}",
    ],
)
def test_profile_param_problems_are_checked_on_load(tmp_path: Path, param: str) -> None:
    """profile. 路径写错, 多余的键, optional 写法不对: ProcedureLibrary 构造即抛 ValueError, 消息含
    "程序编号.参数名"."""
    template = REF_TEMPLATE.split("params:")[0] + f"params:\n  bad: {param}\n"
    with pytest.raises(ValueError, match="程序参数的 ref 写错") as e:
        ref_library(tmp_path, template)
    assert "X-REF.bad" in str(e.value)


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


# ── R54: fixture 文档 ID 对齐 manifest (R77: 按数据集登记表 tests/fixture_sets.py 参数化) ──


def manifest_documents() -> dict[str, dict[str, Any]]:
    """data/demo/manifest.json 登记的 PDF: 文档 ID -> 条目 (path 相对 data/demo, type, page_count, sha256)."""
    return NUMBAT.documents()


def document_pointers(node: Any) -> list[dict[str, Any]]:
    """fixture JSON 中 source_type 为 document 的全部证据指针, 含推理链各步里的."""
    if isinstance(node, dict):
        own = [node] if node.get("source_type") == "document" else []
        return own + [p for value in node.values() for p in document_pointers(value)]
    if isinstance(node, list):
        return [p for item in node for p in document_pointers(item)]
    return []


def check_document_pointers(fs: FixtureSet) -> tuple[dict[str, int], list[tuple[str, str, str, int]]]:
    """该数据集测试 fixture 中的 document 指针: 返回 (各 fixture 的指针数, 不合格的指针). 合格: source_id 在 manifest
    中, 页码在 1 到该 PDF 页数之间; 登记了期望 PDF 函数且它给出对应时还须指向其中一份."""
    documents = fs.documents()
    checked: dict[str, int] = {}
    wrong = []
    for path in sorted(fs.fixtures.rglob("*.json")):
        pointers = document_pointers(json.loads(path.read_text(encoding="utf-8")))
        if not pointers:
            continue
        procedure_id, key = path.parent.name, path.stem
        checked[f"{procedure_id}/{key}"] = len(pointers)
        expected = fs.expected_document(procedure_id, key) if fs.expected_document is not None else None
        for p in pointers:
            entry = documents.get(p["source_id"])
            if (
                entry is None
                or (expected is not None and entry["path"] not in expected)
                or not isinstance(p.get("page"), int)
                or not 1 <= p["page"] <= entry["page_count"]
            ):
                wrong.append((f"{procedure_id}/{key}", p["source_id"], p["locator"], p.get("page")))
    return checked, wrong


# 有 document 指针的 fixture 及其指针数, 按登记名; 没有列出的数据集只核对指针合格
EXPECTED_POINTERS = {
    "numbat": {  # DOC-04, DOC-06, REV-03, REV-02, CSH-02 五个程序的全部 fixture 与 GEN-09 的第一轮
        "CSH-02/BA-01": 4,
        "DOC-04/CON-001": 4,
        "DOC-04/CON-002": 4,
        "DOC-04/CON-003": 3,
        "DOC-06/BA-01": 5,
        "DOC-06/BA-02": 5,
        "GEN-09/BA-01-r1": 2,
        "REV-02/SI-2026-0612": 2,
        "REV-02/SI-2026-0618": 2,
        "REV-03/CON-001": 2,
        "REV-03/CON-002": 2,
        "REV-03/CON-003": 6,
    }
}


@pytest.mark.parametrize("fs", FIXTURE_PARAMS)
def test_fixture_document_ids_in_manifest(fs: FixtureSet) -> None:
    """验收 1: 每个 document 指针的 source_id 都在该数据集的 manifest.documents 中, 指向该 fixture 对应的 PDF
    (Numbat: 合同为 contracts/CON-00x.pdf, 回函为 confirmations/{账号}.pdf), 页码不超过该 PDF 的页数."""
    checked, wrong = check_document_pointers(fs)
    assert wrong == []
    assert EXPECTED_POINTERS.keys() <= {f.name for f in FIXTURE_SETS}  # 登记行改名时计数核对不会静默消失
    if fs.name in EXPECTED_POINTERS:
        assert checked == EXPECTED_POINTERS[fs.name]


def test_document_check_without_expected_function(tmp_path: Path) -> None:
    """期望 PDF 函数为空的登记行只核对指针在 manifest 中且页码不超过页数; 不写页码的指针报出, 不抛异常."""
    some_id, entry = next(iter(NUMBAT.documents().items()))
    fixtures = tmp_path / "fixtures"

    def pointer(source_id: str, page: int | None) -> dict[str, Any]:
        found = {"source_type": "document", "source_id": source_id, "locator": "x", "page": page}
        return found if page is not None else {k: v for k, v in found.items() if k != "page"}  # 不写 page 键

    for key, ptr in (
        ("OK", pointer(some_id, entry["page_count"])),
        ("UNKNOWN", pointer("DOC-000000000000", 1)),
        ("PAGE", pointer(some_id, entry["page_count"] + 1)),
        ("NOPAGE", pointer(some_id, None)),
    ):
        (fixtures / "REV-03").mkdir(parents=True, exist_ok=True)
        (fixtures / "REV-03" / f"{key}.json").write_text(json.dumps({"evidence": [ptr]}), encoding="utf-8")
    fs = replace(NUMBAT, name="tmp", fixtures=fixtures, runtime=tmp_path / "runtime", expected_document=None)
    checked, wrong = check_document_pointers(fs)
    assert checked == {"REV-03/NOPAGE": 1, "REV-03/OK": 1, "REV-03/PAGE": 1, "REV-03/UNKNOWN": 1}
    assert sorted(w[0] for w in wrong) == ["REV-03/NOPAGE", "REV-03/PAGE", "REV-03/UNKNOWN"]


def test_fake_documents_from_manifest() -> None:
    """验收 2: 假存储登记的 3 份合同与 2 份回函即 manifest 的 5 份 PDF: 文档 ID, 页数, 哈希取 manifest,
    文件名为 PDF 路径末段, 类型一致."""
    fakes = {d.file_name: d for d in [*NUMBAT_CONTRACTS, *NUMBAT_REPLIES]}
    expected = {Path(d["path"]).name: d for d in manifest_documents().values()}

    assert (
        sorted(fakes)
        == sorted(expected)
        == [
            "12345601.pdf",
            "23456702.pdf",
            "CON-001.pdf",
            "CON-002.pdf",
            "CON-003.pdf",
        ]
    )
    for name, d in expected.items():
        ref = fakes[name]
        assert (ref.id, ref.page_count, ref.sha256, ref.type) == (
            d["document_id"],
            d["page_count"],
            d["sha256"],
            d["type"],
        ), name


def check_runtime(fs: FixtureSet) -> None:
    """运行副本与测试 fixture 文件相同, 内容相同 (JSON 比较); 运行副本根不存在时报 "运行副本未生成", 不跳过.
    登记了上传处理的固定输出 (uploads) 时, 其中 uploads_in_runtime 列出的程序 (演示也用的分类, 回函副本, 账表识别与
    ASIC 摘录抽取) 也须在运行副本中; 其余 (如 agreement, 只供 "人工指定类型后抽取" 的测试) 不进运行目录."""
    fs.ready()
    if not fs.runtime.is_dir():
        raise AssertionError(f"{fs.name}: 运行副本未生成 ({fs.runtime})")
    test_files = {p.relative_to(fs.fixtures): p for p in fs.fixtures.rglob("*.json")}
    if fs.uploads is not None:
        test_files |= {
            p.relative_to(fs.uploads): p for p in fs.uploads.rglob("*.json") if p.parent.name in fs.uploads_in_runtime
        }
    runtime_files = {p.relative_to(fs.runtime): p for p in fs.runtime.rglob("*.json")}
    assert sorted(test_files) == sorted(runtime_files)
    for name, path in test_files.items():
        assert json.loads(path.read_text(encoding="utf-8")) == json.loads(
            runtime_files[name].read_text(encoding="utf-8")
        ), name


@pytest.mark.parametrize("fs", FIXTURE_PARAMS)
def test_runtime_fixtures_match_test_fixtures(fs: FixtureSet) -> None:
    """运行副本 (Numbat 为 data/demo/llm, B 的生成器由测试 fixture 复制并映射文档 ID) 与测试 fixture 内容相同:
    文档 ID 已对齐, 映射不再改动内容; 改了测试 fixture 而未重新生成时在这里报出."""
    check_runtime(fs)


def test_runtime_check_fails_without_runtime_copy(tmp_path: Path) -> None:
    """登记行的运行副本根不存在时失败 (运行副本未生成), 不跳过; 多一个或改了一个文件时也失败."""
    with pytest.raises(AssertionError, match="运行副本未生成"):
        check_runtime(replace(NUMBAT, runtime=tmp_path / "missing"))
    runtime = tmp_path / "runtime"
    shutil.copytree(NUMBAT.runtime, runtime)
    check_runtime(replace(NUMBAT, runtime=runtime))
    (runtime / "REV-03" / "CON-999.json").write_text("{}", encoding="utf-8")
    with pytest.raises(AssertionError):
        check_runtime(replace(NUMBAT, runtime=runtime))
