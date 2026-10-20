"""导出 OpenAPI 文档供前端生成客户端: uv run python -m backend.export_openapi <输出路径>. 不启动服务, 不连数据库.
入库的 src/frontend/openapi.json 即它的输出, 默认测试 tests/test_openapi_snapshot 守卫两者一致 (需求 R307)."""

import json
import sys
from pathlib import Path

from backend.main import app


def openapi_text() -> str:
    """导出的文本: app.openapi() 的 JSON, 不转义非 ASCII, 缩进 2, 末尾一个换行."""
    return json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n"


if __name__ == "__main__":
    Path(sys.argv[1]).write_text(openapi_text(), encoding="utf-8")
