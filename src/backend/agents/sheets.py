"""A3 识别上传的账表 (需求 R276, 识别程序 DOC-SHEET).

每个工作表把开头的样本行 (至多 SAMPLE_ROWS 行, 含标题行; 人名列与邮箱经 data_import.mask_sample 遮蔽) 交给大模型,
得到识别结果: 规整方案 (R275 的 TablePlan), 标题里读到的公司名与期间, 把握与理由. 方案由 services/table_plan 确定性地
套到全表, 大模型不输出数据行. 大模型不可用 (mock 下没有 fixture, 未配密钥, 调用失败, 重试后输出仍不合格式) 时退回按列名
匹配 (同账表导入的建议: data_import.preview), 把握为低, 理由以 "按列名匹配, 请核对" 开头.

只识别, 不写财务表, 不改文件登记; 确认单与写入在 R277. 识别模板不在程序库里 (不是审计程序), 只借 call_llm 的渲染,
校验, 重试与留痕, 同 DOC-CLASSIFY.
"""

import asyncio
import json
import logging
from pathlib import PurePath
from typing import Any, ClassVar, cast

from backend.agents.base import AgentDeps
from backend.agents.ingest import upload_run
from backend.contracts.data import SheetReading, TableKind, TablePlan
from backend.contracts.knowledge import LLMSpec, ProcedureTemplate
from backend.contracts.llm_outputs import SheetRecognition
from backend.contracts.models import AgentId, AutonomyLevel, DocumentRef, Engine, Phase
from backend.contracts.services import ValidationContext
from backend.services.data_import import (
    KEYS,
    ONE_OF,
    SCHEMAS,
    RawRows,
    cell_text,
    mask_sample,
    preview,
    raw_rows,
    sheet_from_rows,
    sheet_names,
)
from backend.services.table_plan import mapping_plan

logger = logging.getLogger(__name__)

SHEET = ProcedureTemplate(
    id="DOC-SHEET",
    name="账表识别",
    phase=Phase.P1_Planning,
    cycle="Data",
    owner=AgentId.A3,
    engine=Engine.llm,
    autonomy_level=AutonomyLevel.L2,
    # 输出上限走缺省: 原来显式的 4096 等于旧缺省, 推理类模型的推理 token 计入上限, 会同 GEN-02 一样截断 (需求 R312)
    llm=LLMSpec(prompt="DOC-SHEET.md.j2", output_model="SheetRecognition"),
)
SAMPLE_ROWS = 30  # 发给大模型的样本行数上限, 含标题行与空行 (issue #3 补充)
FALLBACK = "按列名匹配, 请核对"  # 退回时理由的开头 (issue #3)
# 提示词里的账表说明: 种类, 字段 (名称, 类型, 是否必须), 至少对应其一的字段组, 唯一键
TABLES = [
    {
        "kind": kind.value,
        "fields": [f.model_dump() for f in SCHEMAS[kind].fields],
        "one_of": list(ONE_OF.get(kind, ())),
        "key": list(KEYS[kind]),
    }
    for kind in TableKind
]


def fixture_key(file_name: str, sheet: str, sheet_count: int) -> str:
    """mock 模式取固定输出的键: 文件名主干; 工作簿有多个工作表时加 "#工作表名"."""
    stem = PurePath(file_name).stem
    return stem if sheet_count == 1 else f"{stem}#{sheet}"


def sample_rows(rows: RawRows, limit: int = SAMPLE_ROWS) -> list[tuple[int, list[str]]]:
    """样本行: 原样的前 limit 行 (含标题行与空行), 每行 (行号, 各单元格文本), 去掉行末的空单元格; 人名列与邮箱经
    data_import.mask_sample 遮蔽."""
    head = rows[:limit]
    texts = mask_sample([[cell_text(c) for c in cells] for _, cells in head])
    sample = []
    for (number, _), cells in zip(head, texts, strict=True):
        while cells and not cells[-1]:
            cells.pop()
        sample.append((number, cells))
    return sample


def sample_text(sample: list[tuple[int, list[str]]]) -> str:
    """样本行的提示词写法: 每行 "行号: 各单元格的 JSON 数组" (空行为 [])."""
    return "\n".join(f"{number}: {json.dumps(cells, ensure_ascii=False)}" for number, cells in sample)


def by_column_names(file_name: str, sheet: str, rows: RawRows, cause: str, trail_ids: list[str]) -> SheetReading:
    """退回按列名匹配: 表头取第一个非空行, 表种类, 列对应与日期写法同账表导入的建议 (data_import.preview); 认不出种类的
    方案为不是账表. 把握为低, 理由以 FALLBACK 开头并写明原因."""
    parsed = sheet_from_rows(sheet, rows)
    suggestion = preview("", file_name, [sheet], parsed)
    kind = suggestion.suggested_kind
    if kind is None:
        plan, found = TablePlan(kind=None), "按列名认不出是哪张账表"
    else:
        plan = mapping_plan(kind, suggestion.suggested_mapping, suggestion.suggested_date_format, parsed.header_row)
        found = f"按列名认作 {kind.value}"
    return SheetReading(
        file_name=file_name,
        sheet=sheet,
        plan=plan,
        confidence="low",
        reason=f"{FALLBACK}: {cause}, {found}",
        method="column_names",
        trail_ids=trail_ids,
    )


class SheetIntake:
    """A3 识别上传文件里的账表 (需求 R276): 每个工作表一项, 先问大模型, 不可用时按列名匹配."""

    agent: ClassVar[AgentId] = AgentId.A3  # 记在大模型留痕上的调用方

    def __init__(self, deps: AgentDeps) -> None:
        self.deps = deps  # 外部依赖

    async def recognize(self, eid: str, document: DocumentRef, data: bytes) -> list[SheetReading]:
        """识别工作簿的每个工作表 (CSV 一个, 名为文件名主干), 按工作表顺序. 文件读不了抛 UnreadableFile.

        document 为该文件的登记 (留痕的 run_id 为 UPLOAD-{文件 ID}); data 为文件内容."""
        names = await asyncio.to_thread(sheet_names, document.file_name, data)
        readings = []
        for name in names:
            _, rows = await asyncio.to_thread(raw_rows, document.file_name, data, name)
            key = fixture_key(document.file_name, name, len(names))
            readings.append(await self.recognize_sheet(eid, document, name, rows, key))
        return readings

    async def recognize_sheet(
        self, eid: str, document: DocumentRef, sheet: str, rows: RawRows, key: str
    ) -> SheetReading:
        """识别一个工作表: rows 为原样读出的全部行 (只取开头的样本行发给大模型), key 为 fixture_key."""
        variables: dict[str, Any] = {
            "file_name": document.file_name,
            "sheet": sheet,
            "row_count": rows[-1][0] if rows else 0,
            "rows": sample_text(sample_rows(rows)),
            "tables": TABLES,
        }
        run = upload_run(SHEET.id, document)
        try:
            result = await self.deps.judgments.call_llm(
                eid, SHEET, run, variables, ValidationContext(standards_of=None), self.agent, key
            )
        except Exception:
            logger.exception("识别账表时调用大模型出错 %s %s %s", eid, document.id, sheet)
            return by_column_names(document.file_name, sheet, rows, "大模型调用失败", [])
        output = cast(SheetRecognition | None, result.report.output)
        if result.report.level_failed is not None or output is None:
            return by_column_names(document.file_name, sheet, rows, "大模型没有给出合格的识别结果", result.trail_ids)
        return SheetReading(
            file_name=document.file_name,
            sheet=sheet,
            plan=output.plan,
            company=output.company,
            period_start=output.period_start,
            period_end=output.period_end,
            confidence=output.confidence,
            reason=output.reason,
            method="llm",
            trail_ids=result.trail_ids,
        )
