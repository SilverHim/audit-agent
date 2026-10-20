"""Execute bound, read-only rules and retain deterministic evidence provenance.

规则可声明执行前检查 (RuleSpec.checks, 需求 R130): 按顺序执行, 参数与规则 SQL 相同; 第一个返回行的检查决定该规则
不执行, 记一条 RuleOutcome (status 取该检查, reason 取第一行的 reason 列, 为空时为 DEFAULT_REASON), 不出例外. 没有
检查的规则照旧只执行规则 SQL. 检查出错与规则出错同样抛 RuleError.
"""

from collections.abc import Mapping
from typing import Any

from pydantic_core import to_jsonable_python

from backend.contracts.errors import RuleError
from backend.contracts.knowledge import ProcedureTemplate, RuleSpec
from backend.contracts.models import AuditException, EvidencePointer, ProcedureRun, RuleOutcome
from backend.contracts.ports import FinancialDataStore
from backend.contracts.services import RuleResult

DEFAULT_REASON = "前置检查未通过"  # 命中的检查没有给出理由 (reason 列为空或没有) 时的理由


class SqlRuleEngine:
    def __init__(self, financial_store: FinancialDataStore) -> None:
        self.financial_store = financial_store

    async def run(
        self, eid: str, template: ProcedureTemplate, run: ProcedureRun, params: Mapping[str, Any]
    ) -> RuleResult:
        result = RuleResult(exceptions=[], rows={})
        kinds = {
            "journal_entry": "voucher",
            "trial_balance": "voucher",
            "sales_invoice": "invoice",
            "supplier_bill": "invoice",  # 应付账单 (需求 R178)
            "delivery_note": "delivery",
            "bank_transaction": "bank",
            "bank_account": "bank",
        }
        for rule in template.rules:
            try:
                outcome = await self.precheck(eid, rule, params)
                if outcome is not None:
                    result.outcomes.append(outcome)
                    continue
                rows = await self.financial_store.query(eid, rule.sql, params)
                if rule.emit == "rows":
                    result.rows[rule.id] = rows
                    continue
                for number, row in enumerate(rows, 1):
                    source_type, source_id = row["source_type"], row["source_id"]
                    if not isinstance(source_id, str) or not source_id:
                        raise ValueError("Missing source_id")
                    kind = kinds[source_type]
                    detail = to_jsonable_python(
                        {k: v for k, v in row.items() if k not in {"source_type", "source_id", "amount_cents"}}
                    )
                    pointer = EvidencePointer.model_validate(
                        dict(
                            type=kind,
                            source_type=source_type,
                            source_id=source_id,
                            locator=", ".join(k for k in row if k not in {"source_type", "source_id"}),
                            reliability="external_copy" if kind == "bank" else "internal_original",
                        )
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
                            evidence=[pointer],
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
