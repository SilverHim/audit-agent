"""Xero 演示的大模型 fixture 守卫 (R78 验收 4 (b) 至 (d)): tests/fixtures/llm_xero/<程序>/<对象>.json.

正本与运行副本 (data/xero_demo/20261017-q1/llm) 相同由 tests/test_knowledge.py 按 FIXTURE_SETS 核对. 这里按集合包含
断言, 不写死文件清单, 目录为空时通过; 各程序段加 fixture 时不必改这里. 自测用 tmp 目录造不合格的 fixture.
"""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from backend.agents.review import GEN09, VERDICT_SPEC
from backend.contracts import llm_outputs
from backend.core.config import get_settings
from backend.services.judgment_engine import output_model
from backend.services.procedure_library import ProcedureLibrary
from tests.fixture_sets import XERO, FixtureSet
from tests.test_knowledge import document_pointers

LIBRARY = ProcedureLibrary(get_settings().KNOWLEDGE_ROOT)


def fixture_model(library: ProcedureLibrary, procedure_id: str, fixture_key: str) -> type[BaseModel]:
    """该 fixture 应符合的输出模型: 模板 llm.output_model; GEN-09 的第二轮 ({对象}-r2) 为 VERDICT_SPEC 的."""
    if procedure_id == GEN09 and fixture_key.endswith("-r2"):
        model = getattr(llm_outputs, VERDICT_SPEC.output_model)
        assert isinstance(model, type) and issubclass(model, BaseModel)
        return model
    return output_model(library.load(procedure_id))


def fixture_problems(fs: FixtureSet, library: ProcedureLibrary = LIBRARY) -> list[str]:
    """不合格的 fixture (只写文件, 程序与指针 ID, 不写内容):
    (b) document 指针的 source_id 须在 manifest.documents 中, 1 ≤ page ≤ page_count, 所指文件类型不是 other;
    (c) 目录名须是程序库中 (含未启用的) 带 llm 段的程序, 文件须能按该程序的输出模型解析.
    fixture 根不存在或为空时没有问题."""
    problems: list[str] = []
    documents = fs.documents()
    programs = {t.id for t in library.all(include_disabled=True) if t.llm is not None}
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
            for pointer in document_pointers(json.loads(raw)):
                entry = documents.get(pointer["source_id"])
                if entry is None:
                    problems.append(f"{name}: {pointer['source_id']} 不在 manifest.documents 中")
                elif not isinstance(pointer.get("page"), int) or not 1 <= pointer["page"] <= entry["page_count"]:
                    problems.append(f"{name}: {pointer['source_id']} 的页码超出 1 至 {entry['page_count']}")
                elif entry["type"] == "other":
                    problems.append(f"{name}: {pointer['source_id']} 指向类型为 other 的文件")
    return problems


def test_xero_fixtures_are_valid() -> None:
    """(b) 至 (d): 正本中每个文件都合格; 按集合包含核对, 目录为空时通过."""
    assert fixture_problems(XERO) == []
    present = {p.parent.name for p in XERO.fixtures.glob("*/*.json")}
    assert present <= {t.id for t in LIBRARY.all(include_disabled=True) if t.llm is not None}


def write(root: Path, rel: str, value: Any) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_fixture_problems_are_reported(tmp_path: Path) -> None:
    """自测: 合格的 CSH-02 fixture 不报; 未知程序目录, 没有 llm 段的程序, 不符合输出模型, 指针不在 manifest,
    页码超出 (含 0 与空), 指向 other 类型的文件都报出, 不抛异常."""
    documents = XERO.documents()
    reply = next(d for d in documents.values() if d["type"] == "confirmation")
    other = next(d for d in documents.values() if d["type"] == "other")
    good = json.loads((get_settings().LLM_FIXTURES_ROOT / "CSH-02" / "BA-01.json").read_text(encoding="utf-8"))

    def with_pointer(document_id: str, page: int | None) -> dict[str, Any]:
        """Numbat 的 CSH-02 fixture, 其中每个 document 指针都改成指向 document_id 的第 page 页."""
        value: dict[str, Any] = json.loads(json.dumps(good))
        for pointer in document_pointers(value):
            pointer.update(source_id=document_id, page=page)
        return value

    root = tmp_path / "llm_xero"
    write(root, "CSH-02/OK.json", with_pointer(reply["document_id"], reply["page_count"]))
    assert fixture_problems(replace(XERO, fixtures=root)) == []
    write(root, "X-UNKNOWN/a.json", good)
    write(root, "ACC-01/a.json", good)
    write(root, "CSH-02/BAD.json", {"conclusion": 1})
    write(root, "CSH-02/MISSING.json", with_pointer("DOC-000000000000", 1))
    write(root, "CSH-02/PAGE.json", with_pointer(reply["document_id"], reply["page_count"] + 1))
    write(root, "CSH-02/PAGE0.json", with_pointer(reply["document_id"], 0))
    write(root, "CSH-02/PAGENULL.json", with_pointer(reply["document_id"], None))  # EvidencePointer 允许 page 为空
    write(root, "CSH-02/OTHER.json", with_pointer(other["document_id"], 1))
    problems = fixture_problems(replace(XERO, fixtures=root))
    assert sorted({p.split(":")[0] for p in problems}) == [  # 每个不合格的指针各报一条, 这里按文件看
        "ACC-01",
        "CSH-02/BAD",
        "CSH-02/MISSING",
        "CSH-02/OTHER",
        "CSH-02/PAGE",
        "CSH-02/PAGE0",
        "CSH-02/PAGENULL",
        "X-UNKNOWN",
    ]


def test_gen09_second_round_uses_verdict_model() -> None:
    """GEN-09 的 {对象}-r2 按 VERDICT_SPEC 的输出模型解析, 其余按模板的."""
    assert fixture_model(LIBRARY, "GEN-09", "BA-01-r2").__name__ == VERDICT_SPEC.output_model
    assert fixture_model(LIBRARY, "GEN-09", "BA-01-r1") is output_model(LIBRARY.load("GEN-09"))
    with pytest.raises(ValueError):
        fixture_model(LIBRARY, "ACC-01", "x")  # 没有 llm 段
