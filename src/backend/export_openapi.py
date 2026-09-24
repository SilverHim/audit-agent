"""导出 OpenAPI 文档供前端生成客户端：uv run python -m backend.export_openapi <输出路径>。不启动服务、不连数据库。"""

import json
import sys
from pathlib import Path

from backend.main import app

if __name__ == "__main__":
    Path(sys.argv[1]).write_text(json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
