"""导出与索引共用的相对文件名, 不查询当前资料."""

import re

from backend.contracts.models import LocalizedText, WorkingLanguage


def relative_filename(ref: str, title: LocalizedText, procedure_id: str | None, language: WorkingLanguage) -> str:
    raw = title.zh if language == "zh" else title.en
    clean = re.sub(r'[\\/:*?"<>|\r\n]', " ", raw).strip()
    suffix = f" ({procedure_id})" if procedure_id else ""
    return f"{ref} {clean}{suffix}.xlsx"
