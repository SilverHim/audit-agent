"""按已存内容导出文件与压缩包, 文件名和索引相同, 不复制原始资料文件."""

from io import BytesIO
from urllib.parse import quote
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from backend.contracts.models import WorkingLanguage, Workpaper
from backend.services.workpaper_xlsx import filename, render

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class Utf8Info(ZipInfo):
    """英文条目也明确标 UTF-8, 避免解压工具使用本地编码猜测."""

    def _encodeFilenameFlags(self) -> tuple[bytes, int]:
        return self.filename.encode("utf-8"), self.flag_bits | 0x800


def disposition(name: str, fallback: str) -> str:
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"


def archive(papers: list[Workpaper], language: WorkingLanguage) -> bytes:
    stream = BytesIO()
    with ZipFile(stream, "w", compression=ZIP_DEFLATED) as bundle:
        for paper in papers:
            if paper.content is None:
                continue
            info = Utf8Info(filename(paper, language))
            info.compress_type = ZIP_DEFLATED
            bundle.writestr(info, render(paper, language))
    return stream.getvalue()
