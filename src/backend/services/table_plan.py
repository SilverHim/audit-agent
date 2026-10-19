"""规整方案的套用 (需求 R275): 按 TablePlan 把原样读出的工作表规整成某种账表的标准行. 纯函数, 不连库, 不调大模型.

套用的步骤:
1. 取表头行 (方案的 header_row, 之前的标题行不读), 之后跳过空行 (data_import.sheet_from_rows).
2. 校验方案与工作表对得上 (check_plan), 对不上抛 MappingError, 不逐行报.
3. 逐个数据行: 匹配 skip 任一条的跳过; 分组标题行记下标题里的值 (向下填充), 不出行; 其余各字段按来源取值并按字段
   类型转换 (data_import.convert_cell); 试算表分期时每期出一行.
4. 没对应的可选字段为空, 编号按 GENERATED_IDS 生成, 行模型校验, 同一唯一键重复算失败 (dedupe 时内容相同的只留第一行).

失败处 (ParseFailure) 按行号顺序, 一行多处都记, 同一处 (行, 列, 值) 只记一次; 行号是原文件行号, 列是来源的列名;
调用方取前 MAX_FAILURES 处.
账表导入的按列对应 (convert) 是只有列来源的方案, 与规整方案同一条路径.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, cast

from pydantic import BaseModel, ValidationError

from backend.contracts.data import DateFormat, FieldSource, GroupTitle, ParseFailure, TableKind, TablePlan
from backend.services.data_import import (
    GENERATED_IDS,
    KEYS,
    ONE_OF,
    ROW_MODELS,
    SCHEMAS,
    Cell,
    CellError,
    MappingError,
    RawRows,
    Sheet,
    cell_text,
    convert_cell,
    is_blank,
    normalize,
    parse_amount,
    sheet_from_rows,
)


def mapping_plan(
    kind: TableKind, mapping: Mapping[str, str | None], date_format: DateFormat, header_row: int = 1
) -> TablePlan:
    """按列对应 (字段 → 列, 为空表示不对应) 的方案: 只有列来源, 金额单位按列的原始表头."""
    fields = {name: FieldSource(column=column) for name, column in mapping.items() if column is not None}
    return TablePlan(kind=kind, header_row=max(header_row, 1), fields=fields, date_format=date_format)


def convert(
    kind: TableKind, sheet: Sheet, mapping: Mapping[str, str | None], date_format: DateFormat
) -> tuple[list[BaseModel], list[ParseFailure]]:
    """账表导入的提交: 按列对应把已读好的工作表 (表头为第一个非空行) 转成行模型, 返回 (行, 失败处); 有失败时行不可用.
    对应不可用抛 MappingError (见 check_plan); 对应里有不属于该表的字段时, 值为空 (不对应) 也报."""
    check_fields(kind, mapping)
    return apply_sheet(mapping_plan(kind, mapping, date_format, sheet.header_row), sheet)


def check_fields(kind: TableKind, names: Iterable[str]) -> None:
    """字段都属于该表, 否则抛 MappingError (INVALID_VALUE)."""
    unknown = sorted(set(names) - {f.name for f in SCHEMAS[kind].fields})
    if unknown:
        raise MappingError("INVALID_VALUE", f"{kind} 没有字段 {', '.join(unknown)}")


def apply_plan(plan: TablePlan, name: str, rows: RawRows) -> tuple[list[BaseModel], list[ParseFailure]]:
    """把方案套到原样读出的工作表 (data_import.raw_rows 的结果), 返回 (行, 失败处). 表头行不在表里或是空行, 方案与
    工作表对不上时抛 MappingError."""
    return apply_sheet(plan, sheet_from_rows(name, rows, plan.header_row))


def check_plan(plan: TablePlan, sheet: Sheet) -> None:
    """方案能否套到这个工作表: 是账表; 字段都属于该表; 按期分列只用于试算表且不另对应期间与余额; 借贷两列与拆正负只用于
    金额字段; 取值映射的原文规范化后不重复; 用到的列 (字段, 期间, 跳过规则, 分组标题) 都在表头里; 必须字段与 ONE_OF
    组都有来源. 不过抛 MappingError: 前几项与缺列为 INVALID_VALUE, 必须字段没有来源为 MISSING_FIELD."""
    if plan.kind is None:
        raise MappingError("INVALID_VALUE", "方案为不是账表, 不能套用")
    kind = plan.kind
    schema = {f.name: f for f in SCHEMAS[kind].fields}
    check_fields(kind, plan.fields)
    if plan.periods and kind is not TableKind.trial_balance:
        raise MappingError("INVALID_VALUE", f"只有试算表能按期分列, 不能用于 {kind}")
    doubled = sorted({"period", "balance_cents"} & set(plan.fields)) if plan.periods else []
    if doubled:
        raise MappingError("INVALID_VALUE", f"按期分列时不能另对应 {', '.join(doubled)}")
    sources = [*plan.fields.items(), *(("balance_cents", p.balance) for p in plan.periods)]
    not_amount = sorted(
        {name for name, s in sources if (s.debit is not None or s.part is not None) and schema[name].type != "amount"}
    )
    if not_amount:
        raise MappingError("INVALID_VALUE", f"借贷两列与拆正负只用于金额字段: {', '.join(not_amount)}")
    repeated = sorted(name for name, s in sources if len({normalize(k) for k in s.values}) != len(s.values))
    if repeated:
        raise MappingError("INVALID_VALUE", f"取值映射的原文规范化后重复: {', '.join(repeated)}")
    used = [c for _, s in sources for c in (s.column, s.debit, s.credit) if c is not None]
    used += [r.column for r in plan.skip] + ([plan.group_title.column] if plan.group_title else [])
    missing_columns = sorted({c for c in used if c not in sheet.columns})
    if missing_columns:
        raise MappingError("INVALID_VALUE", f"工作表没有列 {', '.join(missing_columns)}")
    covered = set(plan.fields) | ({"period", "balance_cents"} if plan.periods else set())
    required = [f.name for f in SCHEMAS[kind].fields if f.required and f.name not in covered]
    if required:
        raise MappingError("MISSING_FIELD", f"必须对应的字段没有对应: {', '.join(required)}")
    group = ONE_OF.get(kind)
    if group and not covered & set(group):
        raise MappingError("MISSING_FIELD", f"至少要对应其中一个字段: {', '.join(group)}")


class SourceError(CellError):
    """一个字段取不了值: 出错的列与原值文本."""

    def __init__(self, column: str, value: str) -> None:
        super().__init__(f"{column}: {value}")
        self.column = column
        self.value = value


class Applier:
    """一次套用: 方案, 工作表, 逐行的分组标题值与已出的行 (编号计数, 唯一键)."""

    def __init__(self, plan: TablePlan, sheet: Sheet) -> None:
        self.plan = plan
        self.kind = cast(TableKind, plan.kind)
        self.schema = {f.name: f for f in SCHEMAS[self.kind].fields}
        self.position = {column: i for i, column in enumerate(sheet.columns)}
        self.headers = dict(zip(sheet.columns, sheet.headers, strict=True))
        self.skip = [(self.position[r.column], re.compile(r.pattern)) for r in plan.skip]
        title = plan.group_title
        self.title = (self.position[title.column], re.compile(title.pattern)) if title else None
        self.groups: dict[str, str | None] = {}  # 当前分组标题里的值
        self.group_row = 0  # 当前分组标题所在行号
        self.rows: list[BaseModel] = []
        self.failures: list[ParseFailure] = []
        self.failed: set[tuple[int, str, str]] = set()  # 已记的失败处 (行, 列, 值)
        self.seen: dict[tuple[Any, ...], BaseModel] = {}  # 唯一键 → 已出的行
        self.counters: dict[str, int] = {}  # 编号生成的计数

    def run(self, rows: Sequence[tuple[int, list[Cell]]]) -> None:
        for number, cells in rows:
            if any(pattern.search(cell_text(cells[i])) for i, pattern in self.skip):
                continue
            match = self.title_match(cells)
            if match is not None:
                self.groups, self.group_row = match.groupdict(), number
                continue
            self.data_row(number, cells)

    def title_match(self, cells: list[Cell]) -> re.Match[str] | None:
        """分组标题行的匹配: 只有标题列有值且其文本匹配 pattern; 不是标题行 (或方案没有分组标题) 为空."""
        if self.title is None:
            return None
        position, pattern = self.title
        if any(not is_blank(c) for i, c in enumerate(cells) if i != position):
            return None
        return pattern.search(cell_text(cells[position]))

    def fail(self, row: int, column: str, value: str) -> None:
        """记一处失败; 同一处 (行, 列, 值) 只记一次: 试算表分期时各期的同一处, 分组标题里的坏值在其下各数据行."""
        if (row, column, value) not in self.failed:
            self.failed.add((row, column, value))
            self.failures.append(ParseFailure(row=row, column=column, value=value))

    def data_row(self, number: int, cells: list[Cell]) -> None:
        values: dict[str, Any] = {}
        bad = False
        for name, source in self.plan.fields.items():
            try:
                values[name] = self.value(name, source, cells)
            except SourceError as e:
                self.fail(self.error_row(source, number), e.column, e.value)
                bad = True
        if bad:
            return
        if not self.plan.periods:
            self.finish(number, cells, values, dict(self.plan.fields))
            return
        for period in self.plan.periods:
            try:
                balance = self.value("balance_cents", period.balance, cells)
            except SourceError as e:
                self.fail(number, e.column, e.value)
                continue
            sources = {**self.plan.fields, "balance_cents": period.balance}
            self.finish(number, cells, {**values, "period": period.period, "balance_cents": balance}, sources)

    def error_row(self, source: FieldSource, number: int) -> int:
        """失败处的行号: 取自分组标题的为标题所在行 (第一个标题之前的数据行取不到标题值, 为该数据行), 其余为该数据行."""
        return self.group_row if source.group is not None and self.group_row else number

    def cents(self, column: str) -> bool:
        """该列的金额按分读: 方案写了单位按单位, 否则看原始表头是否以 cents 结尾."""
        if self.plan.amount_unit is not None:
            return self.plan.amount_unit == "cents"
        return normalize(self.headers[column]).endswith("cents")

    def amount(self, column: str, cells: list[Cell]) -> int | None:
        cell = cells[self.position[column]]
        try:
            return parse_amount(cell, cents=self.cents(column))
        except CellError as exc:
            raise SourceError(column, cell_text(cell)) from exc

    def value(self, name: str, source: FieldSource, cells: list[Cell]) -> Any:
        """一个字段按来源取值并转换; 取不了抛 SourceError."""
        field = self.schema[name]
        if source.debit is not None and source.credit is not None:
            return (self.amount(source.debit, cells) or 0) - (self.amount(source.credit, cells) or 0)
        if source.column is not None:
            column = source.column
            cell: Cell = cells[self.position[column]]
            if source.part is not None:
                amount = self.amount(column, cells)
                if amount is None:
                    return None
                return max(amount, 0) if source.part == "positive" else max(-amount, 0)
            cents = field.type == "amount" and self.cents(column)
        else:
            column = cast(GroupTitle, self.plan.group_title).column
            cell = self.groups.get(cast(str, source.group))
            cents = self.plan.amount_unit == "cents"
        try:
            if source.values:
                cell = self.mapped(source, cell)
            return convert_cell(self.kind, field, cell, cents, self.plan.date_format)
        except CellError as exc:
            raise SourceError(column, cell_text(cell)) from exc

    @staticmethod
    def mapped(source: FieldSource, cell: Cell) -> str | None:
        """取值映射: 空值为空; 原文规范化后在映射表里取系统取值, 不在表里抛 CellError."""
        text = cell_text(cell)
        if not text:
            return None
        table = {normalize(k): v for k, v in source.values.items()}
        if normalize(text) not in table:
            raise CellError("不在取值映射里")
        return table[normalize(text)]

    def finish(
        self, number: int, cells: list[Cell], values: dict[str, Any], sources: Mapping[str, FieldSource]
    ) -> None:
        """补缺省, 生成编号, 校验行模型, 查唯一键, 出一行."""
        kind = self.kind
        if kind is not TableKind.approval_log:  # 没对应的可选字段为空 (行模型里可空而没有缺省值的也得给)
            for name in self.schema:
                values.setdefault(name, None)
        if kind is TableKind.journal_entries:
            values["debit_cents"] = values.get("debit_cents") or 0
            values["credit_cents"] = values.get("credit_cents") or 0
            values["is_adjustment"] = bool(values.get("is_adjustment"))
        generate = GENERATED_IDS.get(kind)
        if generate is not None and not values.get("id"):
            values["id"] = generate(values, self.counters)
        if kind is TableKind.approval_log:  # 审批日志只含对应了的字段, 补进总账时只改这些列
            values = {k: v for k, v in values.items() if k == "entry_no" or k in sources}
        try:
            row = ROW_MODELS[kind].model_validate(values)
        except ValidationError as exc:
            for error in exc.errors():
                name = str(error["loc"][0]) if error["loc"] else ""
                source = sources.get(name)
                column, value = self.origin(source, cells) if source is not None else (name, "")
                where = self.error_row(source, number) if source is not None else number
                self.fail(where, column, value)
            return
        key = tuple(getattr(row, k) for k in KEYS[kind])
        if key in self.seen:
            if self.plan.dedupe and self.seen[key] == row:
                return
            name = KEYS[kind][-1]
            source = sources.get(name)
            column = self.origin(source, cells)[0] if source is not None else name
            self.fail(number, column, "/".join(map(str, key)))
            return
        self.seen[key] = row
        self.rows.append(row)

    def origin(self, source: FieldSource, cells: list[Cell]) -> tuple[str, str]:
        """字段来源的列名与原值文本: 一列取该列; 借贷两列取借方列与两列的文本; 分组标题取标题列与标题里的值."""
        if source.column is not None:
            return source.column, cell_text(cells[self.position[source.column]])
        if source.debit is not None and source.credit is not None:
            texts = [cell_text(cells[self.position[c]]) for c in (source.debit, source.credit)]
            return source.debit, "/".join(texts)
        title = cast(GroupTitle, self.plan.group_title)
        return title.column, cell_text(self.groups.get(cast(str, source.group)))


def apply_sheet(plan: TablePlan, sheet: Sheet) -> tuple[list[BaseModel], list[ParseFailure]]:
    """把方案套到已取好表头的工作表 (见 apply_plan; 方案的 header_row 不再看), 返回 (行, 失败处). 失败处按行号稳定排序:
    取自分组标题的失败记在标题所在行, 可能晚于同一数据行里列来源的失败才记下."""
    check_plan(plan, sheet)
    applier = Applier(plan, sheet)
    applier.run(sheet.rows)
    return applier.rows, sorted(applier.failures, key=lambda f: f.row)
