"""需求 R265: 开发接口文档与代码的一致性 (只做包含检查, 不比对措辞), 代码里有而文档没写时失败:

- contracts/models.py 的每个 StrEnum 在第 2.1 节有一项 (contracts/data.py 的在第 2.8 节), 成员都写在该项里;
- EngagementState 的字段都写在第 3 节的 EngagementState 定义里;
- contracts/ports.py 的每个 Protocol 列在第 1 节的接口一览, 在第 4 节有定义 (实现在 services/ 的 RuleEngine,
  AuditTrail 在第 5 节), 公开方法与属性都写在该定义里;
- persistence/models.py 的表名与审计产出各表的种类 (OUTPUT_KINDS, 含闸门材料) 写在第 9 节;
- 应用的每个 (方法, 路径) 写在第 10 节的接口表, 接口层的错误码 (api/errors 的错误码表与 DataRejected 的错误码) 写在
  第 10 节;
- Settings 的每个字段写在第 11 节;
- DecisionRejected 的错误码 (以字面量调用的) 写在第 7 节.
"""

import ast
import inspect
import re
from enum import StrEnum
from functools import cache
from pathlib import Path
from typing import Protocol

import pytest

from backend.api.errors import _STATUS
from backend.contracts import data, models, ports
from backend.contracts.state import EngagementState
from backend.core.config import Settings
from backend.main import create_app
from backend.persistence.models import OUTPUT_KINDS, metadata

REPO = Path(__file__).resolve().parents[1]
DOC = REPO / "docs" / "开发接口文档.md"
BACKEND = REPO / "src" / "backend"
API_PREFIX = "/api/v1"
SERVICE_PORTS = {
    "RuleEngine",
    "AuditTrail",
}  # 接口在 contracts/ports.py, 实现在 services/, 写在第 5 节 (第 1 节接口一览)


@cache
def section(number: str) -> str:
    """文档第 number 节的正文 (如 "3", "2.1"): 从该节标题到下一个同级或更高级的标题之前."""
    text = DOC.read_text(encoding="utf-8")
    level = "#" * (number.count(".") + 2)
    start = re.search(rf"^{level} {re.escape(number)}[ .]", text, re.M)
    assert start is not None, f"文档没有第 {number} 节"
    rest = text[start.end() :]
    stop = re.search(rf"^#{{2,{len(level)}}} ", rest, re.M)
    return rest[: stop.start()] if stop else rest


def block(text: str, head: str) -> str | None:
    """text 中以 head 开头的一行起, 到下一个类定义 (行首 class) 或代码块结束之前; 没有这一行为 None."""
    found = re.search(rf"^{re.escape(head)}", text, re.M)
    if found is None:
        return None
    rest = text[found.end() :]
    stop = re.search(r"^class \w+|^```", rest, re.M)
    return rest[: stop.start()] if stop else rest


def missing(names: list[str], text: str) -> list[str]:
    """names 中没有作为整词出现在 text 里的 (前后不接字母, 数字与下划线)."""
    return [n for n in names if not re.search(rf"(?<!\w){re.escape(n)}(?!\w)", text)]


ENUM_SECTIONS = {models: "2.1", data: "2.8"}  # 定义 StrEnum 的模块 → 文档里写它们的节


def enums() -> list[tuple[type[StrEnum], str]]:
    """ENUM_SECTIONS 各模块里定义的 StrEnum (不含从别处导入的) 与该写在哪一节."""
    return [
        (e, number)
        for module, number in ENUM_SECTIONS.items()
        for _, e in inspect.getmembers(module, inspect.isclass)
        if issubclass(e, StrEnum) and e is not StrEnum and e.__module__ == module.__name__
    ]


@pytest.mark.parametrize(("enum", "number"), enums(), ids=lambda v: v.__name__ if isinstance(v, type) else v)
def test_enum_members_in_section_2(enum: type[StrEnum], number: str) -> None:
    """每个 StrEnum 在第 2.1 (或 2.8) 节有 `class 名称(StrEnum):` 一项, 成员名都写在这一项里."""
    found = block(section(number), f"class {enum.__name__}(StrEnum):")
    assert found is not None, f"第 {number} 节没有 {enum.__name__}"
    assert missing([m.name for m in enum], found) == []


def test_engagement_state_fields_in_section_3() -> None:
    found = block(section("3"), "class EngagementState(TypedDict")
    assert found is not None and missing(list(EngagementState.__annotations__), found) == []


def protocols() -> list[type]:
    """contracts/ports.py 里定义的 Protocol."""
    return [
        p
        for _, p in inspect.getmembers(ports, inspect.isclass)
        if p.__module__ == ports.__name__ and Protocol in p.__mro__[1:] and getattr(p, "_is_protocol", False)
    ]


def test_ports_listed_in_section_1() -> None:
    assert missing([p.__name__ for p in protocols()], section("1")) == []


@pytest.mark.parametrize("protocol", protocols(), ids=lambda p: p.__name__)
def test_port_methods_in_section_4(protocol: type) -> None:
    """contracts/ports.py 的每个 Protocol 在第 4 节有 `class 名称(Protocol):` 定义 (SERVICE_PORTS 在第 5 节), 公开方法
    与属性的名字都写在该定义里."""
    number = "5" if protocol.__name__ in SERVICE_PORTS else "4"
    found = block(section(number), f"class {protocol.__name__}(Protocol):")
    assert found is not None, f"第 {number} 节没有 {protocol.__name__}"
    names = [n for n in (*vars(protocol), *getattr(protocol, "__annotations__", {})) if not n.startswith("_")]
    assert missing(names, found) == []


def test_protocols_are_found() -> None:
    """自测: 取到了 ports 的全部 Protocol 与 models 的全部 StrEnum (不是空的, 不会让上面两例空跑)."""
    names = {p.__name__ for p in protocols()}
    assert {"FinancialDataStore", "AuditTrail", "RuleEngine", "LLMClient", "XeroConnections"} <= names
    assert {"Phase", "TaskKind", "TrailKind", "TableKind"} <= {e.__name__ for e, _ in enums()}


def test_tables_and_output_kinds_in_section_9() -> None:
    """第 9 节写出全部表名 (persistence/models.py 的元数据), 审计产出各表与其中的种类 (OUTPUT_KINDS, 含闸门材料)."""
    text = section("9")
    kinds = [kind for listed in OUTPUT_KINDS.values() for kind in listed]
    assert missing(sorted(metadata.tables), text) == []
    assert missing([*OUTPUT_KINDS, *kinds], text) == []


def routes() -> list[tuple[str, str]]:
    """应用的全部 (方法, 路径), 路径去掉 /api/v1 前缀."""
    found = []
    for path, operations in create_app().openapi()["paths"].items():
        for method in operations:
            found.append((method.upper(), path.removeprefix(API_PREFIX)))
    return sorted(found)


def test_routes_in_section_10() -> None:
    """第 10 节的接口表写出每个 (方法, 路径), 写法如 `GET /engagements/{eid}` (路径后可接查询串)."""
    text = section("10")
    assert len(routes()) > 50
    absent = [f"{m} {p}" for m, p in routes() if not re.search(re.escape(f"`{m} {p}") + r"[`?\s]", text)]
    assert absent == []


def test_settings_in_section_11() -> None:
    assert missing(list(Settings.model_fields), section("11")) == []


def raised_codes(exception: str) -> list[str]:
    """src/backend 里以字面量调用 exception("CODE", ...) 的错误码, 去重排序."""
    codes: set[str] = set()
    for path in BACKEND.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == exception
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                codes.add(node.args[0].value)
    return sorted(codes)


def test_decision_error_codes_in_section_7() -> None:
    """DecisionRejected 的错误码 (字面量调用处) 都写在第 7 节."""
    codes = raised_codes("DecisionRejected")
    assert len(codes) > 10 and missing(codes, section("7")) == []


def test_http_error_codes_in_section_10() -> None:
    """接口层错误码表 (api/errors._STATUS) 与 DataRejected 的错误码都写在第 10 节."""
    assert missing(sorted({*_STATUS, *raised_codes("DataRejected")}), section("10")) == []
