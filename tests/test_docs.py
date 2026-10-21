"""需求 R264: Demo需求文档与程序库一致. 第 2 节程序清单的编号集合等于程序库 (含未启用功能的程序), 逐行核对引擎,
自主度, 闸门, 负责 Agent, 依赖与功能; 文中的条数等于程序库与演示配置下启用的条数; 第 7 节准则片段表等于各程序
standards 引用的片段 ID 集合, 逐行核对引用它的程序. 需求 R266: 规格书第 17.3 节的程序表与条数同样与程序库一致.
程序库或文档改了一边而另一边没跟上时失败."""

import re
from collections import defaultdict
from pathlib import Path

from backend.contracts.knowledge import ProcedureTemplate
from backend.core.config import get_settings
from backend.services.procedure_library import ProcedureLibrary
from tests.xero_fakes import DEMO_FEATURES

REPO = Path(__file__).resolve().parents[1]
DEMO_DOC = REPO / "docs" / "Demo需求文档.md"
SPEC = REPO / "docs" / "ELEC5620_设计规格说明书.md"
LIBRARY = ProcedureLibrary(get_settings().KNOWLEDGE_ROOT)
DEMO_LIBRARY = ProcedureLibrary(get_settings().KNOWLEDGE_ROOT, DEMO_FEATURES)
ENGINES = {"规则": "rule", "LLM": "llm", "混合": "hybrid", "人工": "manual"}  # 文档的引擎写法 → 模板的 engine
NONE = "—"  # 表中的空值


def section(text: str, heading: str) -> str:
    """从标题行 heading (如 "## 2. 程序清单", "### 17.3 ...") 到下一个同级或更高级标题之前的正文."""
    start = text.index(f"\n{heading}\n")
    level = heading.split()[0]
    ends = [text.find(f"\n{'#' * n} ", start + 1) for n in range(2, len(level) + 1)]
    end = min([e for e in ends if e != -1], default=len(text))
    return text[start:end]


def first_table(body: str) -> list[list[str]]:
    """正文中第一张表的数据行 (去掉表头与分隔行), 每行为去掉首尾空白的各列."""
    lines = body.splitlines()
    start = next(n for n, line in enumerate(lines) if line.startswith("|"))
    rows = []
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
    return rows


def items(cell: str) -> list[str]:
    """以顿号分隔的编号列; NONE 为空列表."""
    return [] if cell == NONE else cell.split("、")


def row_of(template: ProcedureTemplate) -> dict[str, object]:
    """模板在程序清单中应有的各列 (引擎, 自主度, 闸门, Agent, 依赖, 功能)."""
    return {
        "engine": template.engine.value,
        "level": template.autonomy_level.value,
        "gate": str(template.gate) if template.gate is not None else None,
        "owner": template.owner.value if template.owner is not None else None,
        "depends_on": sorted(template.depends_on),
        "feature": template.feature,
    }


def documented_programs() -> dict[str, dict[str, object]]:
    """第 2 节程序清单: 程序编号 → 文档写的各列 (同 row_of 的键)."""
    found: dict[str, dict[str, object]] = {}
    for _phase, program, engine_level, agent, depends, feature, _how in first_table(
        section(DEMO_DOC.read_text(encoding="utf-8"), "## 2. 程序清单")
    ):
        pid = program.split()[0]
        assert pid not in found, f"程序清单重复: {pid}"
        parts = [p.strip() for p in engine_level.split("·")]
        gate = parts[2].removeprefix("闸门 ") if len(parts) > 2 else None
        found[pid] = {
            "engine": ENGINES[parts[0]],
            "level": parts[1],
            "gate": gate,
            "owner": agent,
            "depends_on": sorted(items(depends)),
            "feature": None if feature == NONE else feature,
        }
    return found


def test_program_table_matches_the_library() -> None:
    """验收 2: 程序清单的编号集合与程序库加载的 (含未启用功能的) 完全相同, 每行的引擎, 自主度, 闸门, 负责 Agent,
    依赖 (YAML 的 depends_on, 不按功能去掉) 与功能开关名都与模板一致."""
    documented = documented_programs()
    templates = {t.id: t for t in LIBRARY.all(include_disabled=True)}
    assert sorted(documented) == sorted(templates)
    assert {pid: row for pid, row in documented.items() if row != row_of(templates[pid])} == {}


def test_program_counts_match_the_library() -> None:
    """文首与第 2 节写的条数: 程序库共 N 条, 演示配置 (.env.example 的 FEATURES) 启用其中 M 条."""
    text = DEMO_DOC.read_text(encoding="utf-8")
    total, demo = len(LIBRARY.all(include_disabled=True)), len(DEMO_LIBRARY.all())
    assert f"程序库有 {total} 条程序，演示配置启用其中 {demo} 条" in text
    assert f"程序库共 {total} 条" in text and f"共 {demo} 条进演示" in text


def referenced_fragments() -> dict[str, set[str]]:
    """各程序 (含未启用功能的) standards 引用的片段 ID → 引用它的程序."""
    found: dict[str, set[str]] = defaultdict(set)
    for template in LIBRARY.all(include_disabled=True):
        for fragment_id in template.standards:
            found[fragment_id].add(template.id)
    return dict(found)


def documented_fragments() -> dict[str, set[str]]:
    """第 7 节准则片段表: 片段 ID → 程序列写的程序 (一行写多个片段时各片段同列)."""
    found: dict[str, set[str]] = {}
    body = section(DEMO_DOC.read_text(encoding="utf-8"), "## 7. 准则片段")
    for fragment_ids, _content, programs in first_table(body):
        for fragment_id in items(fragment_ids):
            assert fragment_id not in found, f"片段表重复: {fragment_id}"
            found[fragment_id] = set(items(programs))
    return found


def test_standards_table_matches_the_references() -> None:
    """验收 5: 片段表的 ID 集合等于各程序引用的片段 ID 集合, 每个 ID 在 knowledge/standards 中有原文, 程序列等于
    引用它的程序."""
    documented, referenced = documented_fragments(), referenced_fragments()
    assert sorted(documented) == sorted(referenced)
    assert [fid for fid in documented if LIBRARY.fragment(fid) is None] == []
    assert {fid: sorted(p) for fid, p in documented.items() if p != referenced[fid]} == {}


def test_spec_scope_matches_the_library() -> None:
    """R266 验收 1: 规格书第 17.3 节程序表中出现的程序编号集合与程序库 (含未启用功能的) 相同; 写的条数等于程序库
    与演示配置下启用的条数."""
    body = section(SPEC.read_text(encoding="utf-8"), "### 17.3 Stage 2 实现范围")
    listed = {pid for row in first_table(body) for pid in re.findall(r"\b[A-Z]{3}-\d{2}\b", row[1])}
    assert sorted(listed) == sorted(t.id for t in LIBRARY.all(include_disabled=True))
    total, demo = len(LIBRARY.all(include_disabled=True)), len(DEMO_LIBRARY.all())
    assert f"程序库有 {total} 条程序" in body and f"启用 {demo} 条" in body
