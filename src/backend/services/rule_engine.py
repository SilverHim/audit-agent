"""Execute bound, read-only rules and retain deterministic evidence provenance.

规则可声明执行前检查 (RuleSpec.checks, 需求 R130): 按顺序执行, 参数与规则 SQL 相同; 第一个返回行的检查决定该规则
不执行, 记一条 RuleOutcome (status 取该检查, reason 取第一行的 reason 列, 为空时为 DEFAULT_REASON), 不出例外. 没有
检查的规则照旧只执行规则 SQL. 检查出错与规则出错同样抛 RuleError.

例外行可带 extra_pointers 列 (需求 R223): 在主指针 (source_type, source_id) 之后附加的证据指针, 如分录行的例外另指
相关科目的试算表行. 不进 detail, 也不进主指针的 locator.

执行了的规则各记一份被测总体 (RuleResult.populations, 需求 R324, 按 YAML 顺序): emit rows 的规则以返回行为总体;
emit exception 的规则写了 population 时另执行其 SQL (参数同规则 SQL), 没写的记为未记录 (count 为空). 总体 SQL 出错
与规则出错同样抛 RuleError.
"""

import json
from collections.abc import Mapping, Sequence
from typing import Any, get_args

from pydantic_core import to_jsonable_python

from backend.contracts.errors import RuleError
from backend.contracts.knowledge import ProcedureTemplate, RuleSpec
from backend.contracts.models import (
    AuditException,
    EvidencePointer,
    PopulationItem,
    ProcedureRun,
    RuleOutcome,
    RulePopulation,
    SourceType,
)
from backend.contracts.ports import FinancialDataStore
from backend.contracts.services import RuleResult
from backend.services.judgment_engine import ROW_EVIDENCE
from backend.services.populations import total_cents

DEFAULT_REASON = "前置检查未通过"  # 命中的检查没有给出理由 (reason 列为空或没有) 时的理由
ROW_TYPES = frozenset(get_args(SourceType))  # 认识的行类型, 被测总体的对象类型只取这些 (rule_population)
EXTRA_POINTERS = "extra_pointers"  # 附加指针列: JSON 数组 (或其文本), 每项 {source_type, source_id, locator 可选}


def row_pointer(source_type: Any, source_id: Any, locator: str) -> EvidencePointer:
    """一行的证据指针: 种类与可靠性按 judgment_engine.ROW_EVIDENCE (接口文档第 5 节 RuleEngine 约定的映射表); 行类型
    不认识或 source_id 为空抛 ValueError."""
    if source_type not in ROW_EVIDENCE:
        raise ValueError(f"Unknown source_type {source_type!r}")
    if not isinstance(source_id, str) or not source_id:
        raise ValueError("Missing source_id")
    kind, reliability = ROW_EVIDENCE[source_type]
    return EvidencePointer.model_validate(
        dict(
            type=kind,
            source_type=source_type,
            source_id=source_id,
            locator=locator,
            reliability=reliability,
        )
    )


def extra_pointers(value: Any) -> list[EvidencePointer]:
    """extra_pointers 列的指针, 按出现顺序; 列为空 (NULL) 时没有. JSON 文本先解析; 不是数组, 某项不是对象, 行类型
    不认识, 缺 source_id 或 locator 不是字符串时抛 ValueError. 没给 locator 时为空串 (指向整行)."""
    if value is None:
        return []
    items = json.loads(value) if isinstance(value, str) else value
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise ValueError(f"{EXTRA_POINTERS} 须为对象数组")
    pointers = []
    for item in items:
        locator = item.get("locator", "")
        if not isinstance(locator, str):
            raise ValueError(f"{EXTRA_POINTERS} 的 locator 须为字符串")
        pointers.append(row_pointer(item.get("source_type"), item.get("source_id"), locator))
    return pointers


def rule_population(rule_id: str, rows: Sequence[Mapping[str, Any]]) -> RulePopulation:
    """一条规则的被测总体 (需求 R324): 每行一个被测项目. 各行 source_type 列相同且是认识的行类型 (SourceType) 时为
    对象类型; 不同 (如 GEN-03 C01 合并分录与流水), 不认识或没有该列时对象类型为空, 各行的 source_type 原样留在识别字段
    里 (不报错: emit rows 的规则返回什么列由程序自定). source_id 列为行 ID (没有该列时取 id 列, emit rows 的规则多以 id
    返回行 ID), amount_cents 列为金额, 其余列为识别字段 (JSON 形式, 日期成 ISO 文本). 金额合计见
    populations.total_cents."""
    types = {row.get("source_type") for row in rows}
    only = next(iter(types)) if len(types) == 1 else None
    source_type = only if only in ROW_TYPES else None
    items = []
    for row in rows:
        key = "source_id" if "source_id" in row else "id"
        lifted = {key, "amount_cents", *(("source_type",) if source_type is not None else ())}
        fields = to_jsonable_python({k: v for k, v in row.items() if k not in lifted})
        items.append(PopulationItem(source_id=row.get(key), amount_cents=row.get("amount_cents"), fields=fields))
    return RulePopulation(
        rule_id=rule_id,
        source_type=source_type,
        items=items,
        count=len(items),
        amount_cents=total_cents(item.amount_cents for item in items),
    )


class SqlRuleEngine:
    def __init__(self, financial_store: FinancialDataStore) -> None:
        self.financial_store = financial_store

    async def run(
        self, eid: str, template: ProcedureTemplate, run: ProcedureRun, params: Mapping[str, Any]
    ) -> RuleResult:
        result = RuleResult(exceptions=[], rows={})
        for rule in template.rules:
            try:
                outcome = await self.precheck(eid, rule, params)
                if outcome is not None:
                    result.outcomes.append(outcome)
                    continue
                rows = await self.financial_store.query(eid, rule.sql, params)
                if rule.emit == "rows":
                    result.rows[rule.id] = rows
                    result.populations.append(rule_population(rule.id, rows))
                    continue
                if rule.population is None:
                    result.populations.append(RulePopulation(rule_id=rule.id))  # 未记录
                else:
                    tested = await self.financial_store.query(eid, rule.population.sql, params)
                    result.populations.append(rule_population(rule.id, tested))
                for number, row in enumerate(rows, 1):
                    source_id = row["source_id"]
                    locator = ", ".join(k for k in row if k not in {"source_type", "source_id", EXTRA_POINTERS})
                    pointer = row_pointer(row["source_type"], source_id, locator)
                    extras = extra_pointers(row.get(EXTRA_POINTERS))
                    detail = to_jsonable_python(
                        {
                            k: v
                            for k, v in row.items()
                            if k not in {"source_type", "source_id", "amount_cents", EXTRA_POINTERS}
                        }
                    )
                    result.exceptions.append(
                        AuditException(
                            id=f"X-{template.id}-{run.run_no}-{rule.id}-{number}",
                            version=1,
                            procedure_id=template.id,
                            run_id=run.id,
                            rule_id=rule.id,
                            severity=rule.severity,
                            source_ids=[source_id],
                            amount_cents=row.get("amount_cents"),
                            detail=detail,
                            evidence=[pointer, *extras],
                        )
                    )
            except Exception as exc:
                raise RuleError(f"{template.id}/{rule.id}: {exc}") from exc
        return result

    async def precheck(self, eid: str, rule: RuleSpec, params: Mapping[str, Any]) -> RuleOutcome | None:
        """规则的执行前检查: 按顺序执行, 第一个返回行的决定不执行, 返回其 RuleOutcome; 都不返回行 (或没有检查) 为空."""
        for check in rule.checks:
            rows = await self.financial_store.query(eid, check.sql, params)
            if rows:
                reason = rows[0].get("reason")
                text = str(reason) if reason is not None else ""  # 理由原样, 为空或没有时取缺省
                return RuleOutcome(rule_id=rule.id, status=check.status, reason=text or DEFAULT_REASON)
        return None
