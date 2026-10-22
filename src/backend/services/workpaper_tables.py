"""底稿各类内容共用的表格与双语区块构造."""

import json
from collections.abc import Sequence
from typing import Any

from backend.contracts.models import (
    LocalizedText,
    Workpaper,
    WorkpaperColumn,
    WorkpaperSection,
    WorkpaperStatus,
    WorkpaperTable,
    WorkpaperValue,
)
from backend.services.workpaper_labels import SECTIONS, field_label


def column(key: str, *, money: bool = False, total: bool = False, width: float = 20) -> WorkpaperColumn:
    return WorkpaperColumn(
        key=key, label=field_label(key), format="money" if money else "text", total=total, width=width
    )


def section(
    key: str, *, paragraphs: Sequence[str | LocalizedText] = (), tables: Sequence[WorkpaperTable] = ()
) -> WorkpaperSection:
    return WorkpaperSection(id=key, title=SECTIONS[key], paragraphs=list(paragraphs), tables=list(tables))


def value(raw: Any) -> WorkpaperValue:
    """单个原始字段保留原文; 嵌套识别信息用稳定 JSON, 不写进 Excel 公式."""
    if raw is None or isinstance(raw, str | int | float | bool):
        return raw
    return json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str)


def unsigned(workpaper: Workpaper) -> dict[str, object]:
    """内容变了时签署的处理 (要并进新版本的字段): 已签署的改为 needs_rereview, 签署移到 last_signoff; 未签署的不变."""
    if workpaper.signoff is None:
        return {}
    return {"status": WorkpaperStatus.needs_rereview, "signoff": None, "last_signoff": workpaper.signoff}
