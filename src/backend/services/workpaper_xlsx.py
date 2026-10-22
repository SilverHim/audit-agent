"""按工作语言把底稿内容快照渲染成 Excel. 不查项目、程序库或财务资料, 旧版本可独立重现."""

import math
import unicodedata
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.workbook.properties import CalcProperties
from openpyxl.worksheet.worksheet import Worksheet

from backend.contracts.models import (
    LocalizedText,
    WorkingLanguage,
    Workpaper,
    WorkpaperSection,
    WorkpaperTable,
    WorkpaperValue,
)
from backend.services.currency import MINOR_DIGITS
from backend.services.workpaper_files import relative_filename
from backend.services.workpaper_labels import PENDING, SCOPES

THIN = Side(style="thin", color="A6A6A6")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
SECTION_FILL = PatternFill("solid", fgColor="E7EEF7")
HEAD_FILL = PatternFill("solid", fgColor="F2F2F2")
PENDING_FILL = PatternFill("solid", fgColor="FFF2CC")
WRAP = Alignment(wrap_text=True, vertical="top")


def text(value: WorkpaperValue, language: WorkingLanguage) -> str:
    if isinstance(value, LocalizedText):
        return value.zh if language == "zh" else value.en
    if value is None:
        return "—"
    if isinstance(value, bool):
        return ("是" if value else "否") if language == "zh" else ("Yes" if value else "No")
    return str(value)


def filename(workpaper: Workpaper, language: WorkingLanguage) -> str:
    if workpaper.content is None:
        raise ValueError(f"底稿 {workpaper.ref} 的这一版本没有内容快照")
    header = workpaper.content.header
    return relative_filename(workpaper.ref, header.title, header.procedure_id, language)


def money_format(currency: str) -> str:
    digits = MINOR_DIGITS.get(currency, 2)
    numeric = "#,##0" + ("." + "0" * digits if digits else "")
    return f'"{currency}" {numeric};"{currency}" ({numeric});"-"'


def line_count(value: object, width: float) -> int:
    """依中英文字符宽度与显式换行估行高, 使长推理与原文自动换行."""
    return sum(
        max(
            1,
            math.ceil(sum(2 if unicodedata.east_asian_width(ch) in {"W", "F"} else 1.1 for ch in line) / max(width, 1)),
        )
        for line in str(value or "").split("\n")
    )


def put(
    ws: Worksheet,
    row: int,
    col: int,
    value: str | int | float | bool | None,
    *,
    bold: bool = False,
    fill: PatternFill | None = None,
) -> None:
    cell = ws.cell(row, col, value)
    # 客户名称、引用原文与人工意见始终是文本; Excel 公式只由下方合计行生成.
    if isinstance(value, str):
        cell.data_type = "s"
    cell.font = Font(name="Calibri", size=11, bold=bold)
    cell.alignment = WRAP
    cell.border = BOX
    if fill:
        cell.fill = fill
    if value in {"✓", "✗"}:
        cell.font = Font(name="Calibri", size=11, bold=True, color="2E7D32" if value == "✓" else "C00000")


def span(
    ws: Worksheet, row: int, first: int, last: int, value: str, *, bold: bool = False, fill: PatternFill | None = None
) -> None:
    for col in range(first, last + 1):
        put(ws, row, col, None, fill=fill)
    if last > first:
        ws.merge_cells(start_row=row, start_column=first, end_row=row, end_column=last)
    put(ws, row, first, value, bold=bold, fill=fill)
    width = sum(ws.column_dimensions[get_column_letter(col)].width or 8 for col in range(first, last + 1))
    ws.row_dimensions[row].height = max(20, 15 * line_count(value, width))


def table(ws: Worksheet, start: int, data: WorkpaperTable, language: WorkingLanguage, currency: str) -> int:
    if not data.columns:
        return start
    heights = []
    for n, col in enumerate(data.columns, 1):
        label = text(col.label, language)
        put(ws, start, n, label, bold=True, fill=HEAD_FILL)
        heights.append(line_count(label, ws.column_dimensions[get_column_letter(n)].width))
    ws.row_dimensions[start].height = max(22, 15 * max(heights))
    first, at = start + 1, start + 1
    for item in data.rows:
        heights = []
        for n, col in enumerate(data.columns, 1):
            raw = item.get(col.key)
            displayed: str | int | float | bool | None
            if isinstance(raw, int | float) and not isinstance(raw, bool):
                displayed = raw / 100 if col.format == "money" else raw
            else:
                displayed = text(raw, language)
            put(ws, at, n, displayed)
            cell = ws.cell(at, n)
            if data.link_column == col.key and isinstance(raw, str) and raw in data.links:
                cell.hyperlink = text(data.links[raw], language)
                cell.font = Font(name="Calibri", size=11, color="0563C1", underline="single")
            if col.format == "money":
                cell.number_format = money_format(currency)
                cell.alignment = Alignment(horizontal="right", vertical="top", wrap_text=True)
            elif col.format == "percent":
                cell.number_format = "0.00%"
            heights.append(line_count(displayed, ws.column_dimensions[get_column_letter(n)].width))
        ws.row_dimensions[at].height = max(20, 15 * max(heights))
        at += 1
    if any(col.total for col in data.columns) and data.rows:
        put(ws, at, 1, "合计" if language == "zh" else "Total", bold=True, fill=HEAD_FILL)
        for n, col in enumerate(data.columns, 1):
            if col.total:
                letter = get_column_letter(n)
                cell = ws.cell(at, n, f"=SUM({letter}{first}:{letter}{at - 1})")
                cell.font = Font(bold=True)
                cell.border = BOX
                cell.fill = HEAD_FILL
                cell.number_format = money_format(currency) if col.format == "money" else "#,##0"
        ws.row_dimensions[at].height = 22
        at += 1
    return at


def flatten(parts: list[WorkpaperSection]) -> list[WorkpaperSection]:
    """按展示顺序展开对象小节, 以实际最宽表设置列宽."""
    return [part for root in parts for part in [root, *flatten(root.children)]]


def render(workpaper: Workpaper, language: WorkingLanguage) -> bytes:
    """生成一份 xlsx; 稳定的单元格内容取快照, 文件属性中的生成时刻由 openpyxl 记."""
    content = workpaper.content
    if content is None:
        raise ValueError(f"底稿 {workpaper.ref} 的这一版本没有内容快照")
    book = Workbook()
    ws = book.active
    assert isinstance(ws, Worksheet)
    ws.title = workpaper.ref
    parts = flatten(content.sections)
    tables = [data for part in parts for data in part.tables]
    widest = max(tables, key=lambda data: len(data.columns), default=None)
    ncols = max(8, len(widest.columns) if widest else 0)
    for n in range(1, ncols + 1):
        width = widest.columns[n - 1].width if widest and n <= len(widest.columns) else 20
        ws.column_dimensions[get_column_letter(n)].width = width
    header = content.header
    zh = language == "zh"
    half = max(1, ncols // 2)
    span(ws, 1, 1, ncols, f"{workpaper.ref}  {text(header.title, language)}", bold=True)
    ws.cell(1, 1).font = Font(name="Calibri", size=14, bold=True)
    span(ws, 2, 1, half, f"{'客户' if zh else 'Client'}: {header.client_name}")
    period = f"{header.period_start or '—'} {'至' if zh else 'to'} {header.period_end or '—'}"
    span(
        ws,
        2,
        half + 1,
        ncols,
        f"{'审计期间' if zh else 'Period'}: {period}    "
        f"{'项目' if zh else 'Engagement'}: {header.engagement_id}    {header.currency}",
    )
    span(
        ws,
        3,
        1,
        half,
        f"{'对应程序' if zh else 'Procedure'}: {header.procedure_id or '—'} {text(header.title, language)}",
    )
    span(ws, 3, half + 1, ncols, f"{'索引' if zh else 'Index'}: {workpaper.ref}")
    completed = header.completed_at.date().isoformat() if header.completed_at else "—"
    reviewed = header.reviewed_at.date().isoformat() if header.reviewed_at else text(PENDING, language)
    scope = text(SCOPES[header.review_scope], language) if header.review_scope else "—"
    span(
        ws,
        4,
        1,
        half,
        f"{'编制人' if zh else 'Preparer'}: {text(header.preparer, language)}    "
        f"{'完成日期' if zh else 'Completed'}: {completed}",
    )
    span(
        ws,
        4,
        half + 1,
        ncols,
        f"{'复核人' if zh else 'Reviewer'}: {header.reviewer or '—'}    "
        f"{'复核日期' if zh else 'Reviewed'}: {reviewed}    {'复核范围' if zh else 'Extent'}: {scope}",
        fill=PENDING_FILL if not header.reviewed_at else None,
    )
    at = 6
    positions: dict[str, tuple[WorkpaperTable, int]] = {}
    for part in parts:
        span(ws, at, 1, ncols, text(part.title, language), bold=True, fill=SECTION_FILL)
        at += 1
        if part.generated_language and part.generated_language != language:
            generated = "英文" if part.generated_language == "en" else "中文"
            message = (
                f"判断文字为{generated}生成"
                if zh
                else f"Judgment text generated in {'English' if part.generated_language == 'en' else 'Chinese'}"
            )
            span(ws, at, 1, ncols, message, fill=PENDING_FILL)
            at += 1
        for paragraph in part.paragraphs:
            span(ws, at, 1, ncols, text(paragraph, language))
            at += 1
        for data in part.tables:
            positions[data.id] = (data, at + 1)
            at = table(ws, at, data, language, header.currency)
        at += 1
    if "lead_schedule" in positions:
        lead, first = positions["lead_schedule"]
        lead_letters = {col.key: get_column_letter(n) for n, col in enumerate(lead.columns, 1)}
        for row in range(first, first + len(lead.rows)):
            ws[f"{lead_letters['audited_cents']}{row}"] = (
                f"={lead_letters['unaudited_cents']}{row}+{lead_letters['adjustments_cents']}{row}"
            )
            ws[f"{lead_letters['change_cents']}{row}"] = (
                f"={lead_letters['audited_cents']}{row}-{lead_letters['prior_cents']}{row}"
            )
    if "misstatement_totals" in positions and "misstatements" in positions:
        totals, row = positions["misstatement_totals"]
        detail, first = positions["misstatements"]
        cols = {col.key: n for n, col in enumerate(totals.columns, 1)}
        amount_col = next(n for n, col in enumerate(detail.columns, 1) if col.key == "accumulated_cents")
        letter = get_column_letter(amount_col)
        ws.cell(row, cols["total_cents"]).value = (
            f"=SUM({letter}{first}:{letter}{first + len(detail.rows) - 1})" if detail.rows else "=0"
        )
        for ratio, denominator in (("performance_ratio", "performance_cents"), ("overall_ratio", "overall_cents")):
            numerator_cell = f"{get_column_letter(cols['total_cents'])}{row}"
            denominator_cell = f"{get_column_letter(cols[denominator])}{row}"
            ws.cell(row, cols[ratio]).value = f'=IF({denominator_cell}=0,"—",{numerator_cell}/{denominator_cell})'
    ws.freeze_panes = "A6"
    ws.print_title_rows = "1:4"
    assert ws.sheet_properties.pageSetUpPr is not None
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A3 if ncols > 12 else ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
    ws.print_options.horizontalCentered = True
    ws.print_area = f"A1:{get_column_letter(ncols)}{at - 1}"
    assert ws.oddFooter is not None
    ws.oddFooter.left.text = f"{header.engagement_id} | {workpaper.ref} | v{workpaper.version}"
    ws.oddFooter.right.text = "&P / &N"
    book.calculation = CalcProperties(calcId=0, fullCalcOnLoad=True, forceFullCalc=True)
    stream = BytesIO()
    book.save(stream)
    return stream.getvalue()
