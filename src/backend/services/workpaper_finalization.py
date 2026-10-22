"""签字后底稿的唯一写入例外: 定稿和带理由的追加记录. 通用存储的锁定规则不改变."""

from typing import Literal

from backend.contracts.errors import DataRejected
from backend.contracts.models import Workpaper, WorkpaperContent, WorkpaperSection, WorkpaperStatus, WorkpaperTable
from backend.services.workpaper_labels import words
from backend.services.workpaper_tables import column


def amendment_content(paper: Workpaper) -> WorkpaperContent:
    """只增加补充区块, 保存原始内容与签署信息; 已有补充区块按追加清单重建."""
    if paper.content is None:
        raise DataRejected("INVALID_VALUE", "这一版本没有记录内容快照")
    additions = WorkpaperSection(
        id="amendments",
        title=words("归档后补充", "Post-archive amendments"),
        tables=[
            WorkpaperTable(
                id="amendments",
                columns=[
                    column("description", width=85),
                    column("reason", width=85),
                    column("added_by"),
                    column("added_at", width=28),
                    column("reviewer"),
                    column("reviewed_at", width=28),
                ],
                rows=[
                    {
                        "description": a.text,
                        "reason": a.reason,
                        "added_by": a.added_by,
                        "added_at": a.added_at.isoformat(),
                        "reviewer": a.reviewed_by,
                        "reviewed_at": a.reviewed_at.isoformat(),
                    }
                    for a in paper.amendments
                ],
            )
        ],
    )
    return paper.content.model_copy(
        update={"sections": [*[s for s in paper.content.sections if s.id != "amendments"], additions]}
    )


def validate_signed_write(
    previous: Workpaper | None, new: Workpaper, actor: str, mode: Literal["archive", "amendment"]
) -> None:
    """持久层也校验例外边界, 防止普通写入借签字路径覆盖历史内容."""
    if type(new) is not Workpaper or new.status is not WorkpaperStatus.final or new.id != f"WP-{new.ref}":
        raise DataRejected("INVALID_VALUE", "签字后的例外只允许 final 底稿")
    if mode == "archive":
        if actor != "A8" or (previous and previous.status is WorkpaperStatus.final):
            raise DataRejected("INVALID_VALUE", "已经定稿的底稿只能追加补充")
        if new.amendments != (previous.amendments if previous else []):
            raise DataRejected("INVALID_VALUE", "定稿不能新增归档后补充")
        return
    if mode != "amendment" or previous is None or previous.status is not WorkpaperStatus.final:
        raise DataRejected("NOT_EDITABLE", "底稿尚未归档")
    if len(new.amendments) != len(previous.amendments) + 1 or new.amendments[:-1] != previous.amendments:
        raise DataRejected("INVALID_VALUE", "只能在原补充记录之后追加一条")
    addition = new.amendments[-1]
    if not addition.reason.strip() or not addition.text.strip():
        raise DataRejected("MISSING_FIELD", "text, reason")
    if addition.added_by != actor or addition.reviewed_by != actor or addition.reviewed_at != addition.added_at:
        raise DataRejected("INVALID_VALUE", "补充人与确认人须为提交人")
    ignored = {"content", "amendments", "version"}
    if new.model_dump(exclude=ignored) != previous.model_dump(exclude=ignored):
        raise DataRejected("INVALID_VALUE", "归档后不能覆盖原底稿字段")
    if new.content != amendment_content(previous.model_copy(update={"amendments": new.amendments})):
        raise DataRejected("INVALID_VALUE", "归档后不能覆盖原底稿内容")
