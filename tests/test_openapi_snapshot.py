"""需求 R307: 入库的 src/frontend/openapi.json 与后端导出 (backend.export_openapi.openapi_text, 即
bun run generate-client 写出的那份) 逐字相同. 防后端接口 (路由, 模型, 字段, 取值, docstring) 改了而没重新生成,
入库的 openapi.json 与 src/client/ 悄悄落后 (默认测试与前端检查都照过, 前端拿着旧类型调新接口). 不起服务, 不读
前端 node_modules."""

import difflib
import json
from pathlib import Path

import pytest

from backend.export_openapi import openapi_text

COMMITTED = Path(__file__).resolve().parents[1] / "src" / "frontend" / "openapi.json"
REGENERATE = "cd src/frontend && bun run generate-client"  # 重写 openapi.json 与 src/client/, 一并提交


def snapshot_problem(committed: str, exported: str) -> str | None:
    """两份文本不同时的失败信息: 重新生成的命令, 加前几行差异 (unified diff, 最多 12 行); 相同时为 None."""
    if committed == exported:
        return None
    diff = difflib.unified_diff(
        committed.splitlines(), exported.splitlines(), "src/frontend/openapi.json", "后端导出", lineterm="", n=0
    )
    shown = "\n".join(list(diff)[:12])
    return f"入库的 openapi.json 与后端导出不一致, 后端接口改了没重新生成: 运行 {REGENERATE}\n{shown}"


def test_committed_openapi_matches_backend() -> None:
    problem = snapshot_problem(COMMITTED.read_text(encoding="utf-8"), openapi_text())
    if problem is not None:
        pytest.fail(problem, pytrace=False)


def test_a_new_field_without_regenerating_fails() -> None:
    """自测: 后端给某个模型加一个字段而没重新生成时 (这里在入库文档的一份副本上加, 同导出的写法), 失败信息给出重新
    生成的命令与差异行."""
    committed = COMMITTED.read_text(encoding="utf-8")
    schema = json.loads(committed)
    schema["components"]["schemas"]["ClientDataset"]["properties"]["probe"] = {"type": "string", "title": "Probe"}
    added = json.dumps(schema, ensure_ascii=False, indent=2) + "\n"
    problem = snapshot_problem(committed, added)
    assert problem is not None and "bun run generate-client" in problem
    assert '"probe"' in problem
