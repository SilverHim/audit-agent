"""回答的引用: 从回答文本里找出上下文中出现过的编号, 生成 citations.

编号池只来自本轮工具结果, 保留在上下文中的历史问答的引用, 以及压缩摘要带下来的编号池; 审计师自己输入的编号
不算引用 (没有查过, 不能当来源). 模型输出纯文本, 不另给引用结构 (原 R7 修订).
"""

import re
from collections.abc import Iterable, Mapping

from backend.contracts.conversation import ConciergeCitation

ID_CHARS = "A-Za-z0-9_-"  # 编号由这些字符组成; 匹配时前后不能紧挨着它们 (中文, 标点, 空白可以)


def citation_pool(*groups: Iterable[ConciergeCitation]) -> dict[str, ConciergeCitation]:
    """合并几组引用为编号池 (编号 -> 引用); 同一编号先出现的为准."""
    pool: dict[str, ConciergeCitation] = {}
    for group in groups:
        for citation in group:
            pool.setdefault(citation.id, citation)
    return pool


def match_citations(answer: str, pool: Mapping[str, ConciergeCitation]) -> list[ConciergeCitation]:
    """回答中提到的编号池里的编号, 按首次出现的顺序, 按 (kind, id) 去重.

    长编号优先, 且前后不能紧挨编号字符: "REV-02" 不会命中 "J-REV-02-1-SI-1" 里面的那一段, "CON-001" 不会命中
    "CON-001-T1". 编号前后是中文, 标点或空白都算边界.
    """
    if not pool or not answer:
        return []
    alternatives = "|".join(re.escape(i) for i in sorted(pool, key=len, reverse=True))
    pattern = re.compile(f"(?<![{ID_CHARS}])(?:{alternatives})(?![{ID_CHARS}])")
    found: list[ConciergeCitation] = []
    seen: set[tuple[str, str]] = set()
    for match in pattern.finditer(answer):
        citation = pool[match.group(0)]
        key = (citation.kind, citation.id)
        if key not in seen:
            seen.add(key)
            found.append(citation)
    return found
