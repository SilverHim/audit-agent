"""分录行的共用写法: 凭证号的自然序, 分录行与规则行转 JSON 形式 (供提示词 tojson).

只依赖 JournalEntry, A6 (fieldwork, going_concern) 与 A8 (workpaper) 都在顶层导入.
"""

import re
from collections.abc import Sequence
from typing import Any

from pydantic import TypeAdapter

from backend.contracts.records import JournalEntry

JSON_ROWS = TypeAdapter(list[dict[str, Any]])  # 规则返回的行转 JSON 形式 (日期为字符串), 供提示词 tojson


def entry_order(entry_no: str) -> tuple[tuple[tuple[str, int], ...], str]:
    """凭证号的自然序: 按 "非数字段, 数字段" 依次比较, 数字段按数值 (XJ-9 在 XJ-10 之前); 自然序相等时按原文."""
    parts = tuple((text, int(number) if number else -1) for text, number in re.findall(r"(\D*)(\d*)", entry_no))
    return tuple(p for p in parts if p != ("", -1)), entry_no


def journal_rows(lines: Sequence[JournalEntry]) -> list[dict[str, Any]]:
    """分录行转带 source_type 的 JSON 形式, 顺序不变."""
    return [{"source_type": "journal_entry", **line.model_dump(mode="json")} for line in lines]
