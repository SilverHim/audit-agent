"""Execute bound, read-only rules and retain deterministic evidence provenance."""

from collections.abc import Mapping
from typing import Any

from pydantic_core import to_jsonable_python

from backend.contracts.errors import RuleError
from backend.contracts.knowledge import ProcedureTemplate
from backend.contracts.models import AuditException, EvidencePointer, ProcedureRun
from backend.contracts.ports import FinancialDataStore
from backend.contracts.services import RuleResult


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
            "delivery_note": "delivery",
            "bank_transaction": "bank",
            "bank_account": "bank",
        }
        for rule in template.rules:
            try:
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
