"""智能导入的批次 (需求 R277): A3 逐项识别生成确认单, 确认单各项的问题与勾选. 写入与锁在 DataService.

- 表格文件 (CSV, XLSX, XLSM, XLS): 每个工作表一项. A3 识别 (agents/sheets, R276), 方案由代码确定性地套到全表
  (table_plan.apply_plan, 沿用 data_import 的类型转换), 得将写入的行数与解析失败; 大模型不输出数据行. 没有一个工作表
  是账表的表格文件 (如报表草稿) 整个作一份文件处理.
- 其他文件: A3 分类与抽取 (DocumentIntake.stage), 结果记在确认单上, 不落到登记与合同上; 文件登记为暂存
  (status staged), 确认前不被程序使用. 审计师在确认单上改了类型的文件按改定的类型重新识别与抽取 (chosen_of).
- 问题 (BatchIssueCode) 有任一项即标红; 审计师没手动勾选或取消的项, 勾选随问题: 没有问题即勾.
"""

import asyncio
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import PurePath

from backend.agents.base import AgentDeps
from backend.agents.ingest import Chosen, DocumentIntake, Requested, Staged, client_profile
from backend.agents.sheets import SheetIntake
from backend.contracts.data import (
    BatchFile,
    BatchIssue,
    BatchIssueCode,
    BatchItem,
    ParseFailure,
    SheetReading,
    TableKind,
)
from backend.contracts.models import DocumentRef, Engagement
from backend.services.data_import import (
    EXTENSIONS,
    MAX_FAILURES,
    MappingError,
    RawRows,
    UnreadableFile,
    cell_text,
    mask_sample,
    raw_rows,
    sheet_from_rows,
    unique_columns,
)
from backend.services.table_plan import apply_plan

TABLE_EXTENSIONS = EXTENSIONS  # 按工作表识别的文件: CSV, XLSX, XLSM, XLS
# 公司名比较时去掉的公司类型词 (Demo Company (AU) 与画像的 Demo Company (AU) Pty Ltd 算同一家)
COMPANY_SUFFIXES = frozenset({"pty", "ltd", "limited", "proprietary", "inc", "incorporated", "plc", "llc"})
# 表格项的识别问题: 改了方案后按新方案重算 (把握低与方案无关, 审计师调整过方案后不再标)
PLAN_ISSUES = frozenset(
    {
        BatchIssueCode.not_a_table,
        BatchIssueCode.plan_invalid,
        BatchIssueCode.parse_failed,
        BatchIssueCode.replaces_data,
        BatchIssueCode.duplicate_table,
    }
)


SAMPLE_SCAN = 200  # 取列的样例时看表头之后的前几个数据行
SAMPLE_CHARS = 80  # 样例截断到的字数


@dataclass(frozen=True)
class Context:
    """判断问题时对照的项目信息."""

    engagement: Engagement  # 项目: 审计期间
    company: str  # 客户画像的法定名称
    existing: Mapping[TableKind, int]  # 各表现有的行数


def company_words(name: str) -> list[str]:
    """公司名比较用的词: 小写字母数字串, 去掉公司类型词."""
    return [w for w in re.findall(r"[a-z0-9]+", name.lower()) if w not in COMPANY_SUFFIXES]


def same_company(read: str, expected: str) -> bool:
    """读到的公司名与画像的法定名称是否同一家: 去掉标点, 大小写与公司类型词 (Pty Ltd 等) 后相同."""
    return company_words(read) == company_words(expected)


def period_issue(reading: SheetReading, engagement: Engagement) -> BatchIssue | None:
    """读到的期间与项目不符时的问题: 只有截至日 (余额表) 的须等于审计期末; 有起止的须覆盖整个审计期间. 没读到期间
    时不判."""
    start, end = reading.period_start, reading.period_end
    if end is None and start is None:
        return None
    if start is None:
        fits = end == engagement.period_end
    else:
        fits = start <= engagement.period_start and (end is None or end >= engagement.period_end)
    if fits:
        return None
    return BatchIssue(
        code=BatchIssueCode.period_mismatch,
        params={
            "period_start": iso(start),
            "period_end": iso(end),
            "expected_start": engagement.period_start.isoformat(),
            "expected_end": engagement.period_end.isoformat(),
        },
    )


def iso(day: date | None) -> str | None:
    return day.isoformat() if day is not None else None


def column_sample(rows: RawRows, header_row: int) -> tuple[list[str], dict[str, str]]:
    """列对应界面用的列与样例: 表头行 header_row 的各列显示名, 每列取表头之后第一个非空值作样例 (只看前 SAMPLE_SCAN
    个数据行, 截断到 SAMPLE_CHARS 字). 遮蔽同发给大模型的样本: 从第 1 行起到最后一个取样的行, 原样的各行整体经
    mask_sample 遮蔽一次 (表头行之上出现过的人名表头照样生效, 邮箱在任何行都换掉), 再切出表头行与之后的数据行;
    列名也取遮蔽后的文本 (表头行若是数据行, 人名与邮箱同样换掉). 表头行不在表里或是空行时都为空."""
    try:
        sheet = sheet_from_rows("", rows, header_row)
    except MappingError:
        return [], {}
    sampled = sheet.rows[:SAMPLE_SCAN]
    last = sampled[-1][0] if sampled else sheet.header_row
    kept = [(number, cells) for number, cells in rows if number <= last]
    masked = dict(
        zip((n for n, _ in kept), mask_sample([[cell_text(c) for c in cells] for _, cells in kept]), strict=True)
    )
    width = len(sheet.headers)

    def texts(number: int) -> list[str]:
        values = masked[number][:width]
        return values + [""] * (width - len(values))

    columns = unique_columns(texts(sheet.header_row))
    data = [texts(number) for number, _ in sampled]
    sample = {column: next((row[i] for row in data if row[i]), "")[:SAMPLE_CHARS] for i, column in enumerate(columns)}
    return columns, sample


def masked_failures(rows: RawRows, failures: Sequence[ParseFailure]) -> list[ParseFailure]:
    """解析失败处的取值也遮蔽, 口径同 column_sample: 原样的各行从第 1 行起整体经 mask_sample 遮蔽一次, 失败所在行里
    被遮蔽的单元格, 其原文在取值里出现的换成遮蔽后的文本 (取值多是一格的原文; 唯一键重复时是几格用 / 连起来的;
    取自分组标题的记在标题所在行). 行号与列名不变."""
    if not failures:
        return []
    last = max(f.row for f in failures)
    kept = [(number, [cell_text(c) for c in cells]) for number, cells in rows if number <= last]
    masked = dict(zip((n for n, _ in kept), mask_sample([texts for _, texts in kept]), strict=True))
    original = dict(kept)
    result = []
    for failure in failures:
        value = failure.value
        for before, after in zip(original.get(failure.row, []), masked.get(failure.row, []), strict=True):
            if before and before != after:
                value = value.replace(before, after)
        result.append(failure if value == failure.value else failure.model_copy(update={"value": value}))
    return result


def evaluate_table(item: BatchItem, rows: RawRows, ctx: Context) -> BatchItem:
    """表格项按当前方案重算: 列与样例 (column_sample), 将写入的行数, 解析失败 (前 MAX_FAILURES 处), 现有行数与问题
    (重复的表由 finalize 另算). rows 为该工作表原样读出的全部行."""
    assert item.plan is not None and item.sheet is not None
    plan, reading = item.plan, item.reading
    issues: list[BatchIssue] = []
    if reading is not None and reading.confidence == "low" and item.adjusted_by is None:
        issues.append(BatchIssue(code=BatchIssueCode.low_confidence))
    records: int | None = None
    failures: list[ParseFailure] = []
    failure_count = 0
    existing: int | None = None
    if plan.kind is None:
        issues.append(BatchIssue(code=BatchIssueCode.not_a_table))
    else:
        try:
            converted, failures = apply_plan(plan, item.sheet, rows)
        except MappingError as exc:
            issues.append(
                BatchIssue(code=BatchIssueCode.plan_invalid, params={"code": exc.code, "message": exc.message})
            )
        else:
            failure_count = len(failures)
            if failures:
                issues.append(BatchIssue(code=BatchIssueCode.parse_failed, params={"count": failure_count}))
            else:
                records = len(converted)
        existing = ctx.existing.get(plan.kind) or None
        if existing:
            issues.append(
                BatchIssue(code=BatchIssueCode.replaces_data, params={"kind": plan.kind.value, "existing": existing})
            )
    if reading is not None and reading.company is not None and not same_company(reading.company, ctx.company):
        issues.append(
            BatchIssue(
                code=BatchIssueCode.company_mismatch, params={"company": reading.company, "expected": ctx.company}
            )
        )
    if reading is not None and (period := period_issue(reading, ctx.engagement)) is not None:
        issues.append(period)
    columns, sample = column_sample(rows, plan.header_row)
    return item.model_copy(
        update={
            "columns": columns,
            "sample": sample,
            "records": records,
            "failure_count": failure_count,
            "failures": masked_failures(rows, failures[:MAX_FAILURES]),
            "existing_records": existing,
            "issues": issues,
        }
    )


def finalize(items: Sequence[BatchItem]) -> list[BatchItem]:
    """整张确认单的收尾: 同一张表出现两次的表格项各记一条 duplicate_table (先去掉旧的); 审计师没手动勾选或取消的项
    按有没有问题定勾选."""
    tables = [i for i in items if i.item_kind == "table" and i.plan is not None and i.plan.kind is not None]
    by_kind: dict[TableKind, list[str]] = {}
    for item in tables:
        assert item.plan is not None and item.plan.kind is not None
        by_kind.setdefault(item.plan.kind, []).append(item.id)
    result = []
    for item in items:
        issues = [i for i in item.issues if i.code is not BatchIssueCode.duplicate_table]
        kind = item.plan.kind if item.item_kind == "table" and item.plan is not None else None
        others = [i for i in by_kind.get(kind, []) if i != item.id] if kind is not None else []
        if kind is not None and others:
            issues.append(BatchIssue(code=BatchIssueCode.duplicate_table, params={"kind": kind.value, "items": others}))
        selected = item.selected if item.chosen_by is not None else not issues
        result.append(item.model_copy(update={"issues": issues, "selected": selected}))
    return result


def item_number(item_id: str) -> int:
    """确认单一项的序号 ({批次 ID}-{n} 的 n)."""
    return int(item_id.rsplit("-", 1)[1])


def chosen_of(previous: Sequence[BatchItem]) -> Chosen | None:
    """一个文件重新识别前确认单上的项里审计师改定的类型 (调整过类型的文件项), 没有为空."""
    for item in previous:
        if item.item_kind == "document" and item.adjusted_by and item.document_type and item.type_reason:
            return Chosen(item.document_type, item.type_reason, item.adjusted_by)
    return None


def carried(previous: Sequence[BatchItem], new: Sequence[BatchItem]) -> list[BatchItem]:
    """重新识别的项沿用审计师手动的勾选 (同序号的旧项有 chosen_by 时)."""
    chosen = {i.id: i for i in previous if i.chosen_by is not None}
    return [
        i.model_copy(update={"selected": chosen[i.id].selected, "chosen_by": chosen[i.id].chosen_by})
        if i.id in chosen
        else i
        for i in new
    ]


def document_item(item_id: str, document: DocumentRef, staged: Staged | None) -> BatchItem:
    """文件项: staged 为 A3 的分类与抽取; 为空表示同一文件之前已登记 (不再处理, 记 already_registered)."""
    if staged is None:
        return BatchItem(
            id=item_id,
            file_name=document.file_name,
            document_id=document.id,
            item_kind="document",
            document_type=document.type,
            issues=[
                BatchIssue(
                    code=BatchIssueCode.already_registered,
                    params={"document_id": document.id, "type": document.type.value, "status": document.status},
                )
            ],
        )
    issues = []
    if staged.failed_step in ("read", "classify"):
        issues.append(BatchIssue(code=BatchIssueCode.unclassified, params={"step": staged.failed_step}))
    elif staged.failed_step == "extract":
        issues.append(BatchIssue(code=BatchIssueCode.extraction_failed))
    return BatchItem(
        id=item_id,
        file_name=document.file_name,
        document_id=document.id,
        item_kind="document",
        document_type=staged.document_type,
        type_reason=staged.reason,
        job=staged.job,
        contract=staged.contract,
        reply=staged.reply,
        registry=staged.registry,
        issues=issues,
    )


def unreadable_item(item_id: str, file: BatchFile) -> BatchItem:
    """处理出错的文件 (如原件读不了): 一份类型未定的文件项, 记 unclassified (read)."""
    return BatchItem(
        id=item_id,
        file_name=file.name,
        document_id=file.document_id,
        item_kind="document",
        issues=[BatchIssue(code=BatchIssueCode.unclassified, params={"step": "read"})],
    )


def staged_of(item: BatchItem) -> Staged:
    """确认单上的文件项还原成 A3 当时的结果 (DocumentIntake.staged_change 用)."""
    return Staged(
        item.document_type,
        item.type_reason,
        item.job,
        None,
        item.contract,
        item.reply,
        registry=item.registry,
    )


class BatchIntake:
    """A3 处理批次里的文件, 生成确认单的各项 (需求 R277). 不写财务表, 不改文件登记 (暂存的文件分类时写 classify
    留痕, 同上传后的处理)."""

    def __init__(self, deps: AgentDeps) -> None:
        self.deps = deps  # 外部依赖

    async def context(self, eid: str, engagement: Engagement) -> Context:
        """项目期间, 客户画像的法定名称与各表现有行数."""
        profile = await client_profile(self.deps, engagement, engagement.client_profile_version)
        stats = await self.deps.data.stats(eid)
        return Context(engagement, profile.legal_name, {kind: s.records for kind, s in stats.items()})

    async def document(self, eid: str, document_id: str) -> DocumentRef:
        found = next((d for d in await self.deps.financial_store.documents(eid) if d.id == document_id), None)
        if found is None:
            raise LookupError(f"文件 {document_id} 不存在")
        return found

    async def content(self, eid: str, document_id: str) -> bytes:
        path = await self.deps.evidence_store.path(eid, document_id)
        return await asyncio.to_thread(path.read_bytes)

    async def process_file(
        self,
        eid: str,
        file: BatchFile,
        first: int,
        prefix: str,
        engagement: Engagement,
        ctx: Context,
        requested: Requested | None,
        chosen: Chosen | None = None,
    ) -> list[BatchItem]:
        """一个文件的各项, ID 为 {prefix}-{n}, n 从 first 起. 表格文件先按工作表识别, 有账表时每个工作表一项 (不是
        账表的工作表也列出, 记 not_a_table); 没有账表 (或读不了) 时与其他文件一样作一份文件: 暂存的分类与抽取, 之前已
        登记的同一文件记 already_registered. 审计师改定了类型 (chosen) 的直接作文件, 按它重新识别与抽取, 记调整人."""
        document = await self.document(eid, file.document_id)
        if chosen is not None and document.status == "staged":
            staged = await DocumentIntake(self.deps).stage(eid, document, engagement, chosen=chosen)
            return [
                document_item(f"{prefix}-{first}", document, staged).model_copy(update={"adjusted_by": chosen.actor})
            ]
        if PurePath(file.name).suffix.lower() in TABLE_EXTENSIONS:
            data = await self.content(eid, document.id)
            try:
                readings = await SheetIntake(self.deps).recognize(
                    eid, document.model_copy(update={"file_name": file.name}), data, self.deps.run_language(engagement)
                )
            except UnreadableFile:
                readings = []
            if any(r.plan.kind is not None for r in readings):
                items = []
                for n, reading in enumerate(readings, start=first):
                    _, rows = await asyncio.to_thread(raw_rows, file.name, data, reading.sheet)
                    item = BatchItem(
                        id=f"{prefix}-{n}",
                        file_name=file.name,
                        document_id=document.id,
                        item_kind="table",
                        sheet=reading.sheet,
                        reading=reading,
                        plan=reading.plan,
                    )
                    items.append(evaluate_table(item, rows, ctx))
                return items
        item_id = f"{prefix}-{first}"
        if document.status != "staged":
            return [document_item(item_id, document, None)]
        staged = await DocumentIntake(self.deps).stage(eid, document, engagement, requested)
        return [document_item(item_id, document, staged)]

    async def reevaluate(self, eid: str, item: BatchItem, ctx: Context) -> BatchItem:
        """表格项按当前方案重算 (读该文件的原件)."""
        assert item.sheet is not None
        data = await self.content(eid, item.document_id)
        _, rows = await asyncio.to_thread(raw_rows, item.file_name, data, item.sheet)
        return evaluate_table(item, rows, ctx)
