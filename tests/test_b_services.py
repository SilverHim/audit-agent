"""Fast failure-path checks for Member B's rule service and mock adapters."""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from backend.adapters.confirmation_mock import MockConfirmationChannel
from backend.adapters.registry_mock import MockRegistrySource
from backend.contracts.errors import RuleError
from backend.contracts.knowledge import ProcedureTemplate
from backend.contracts.models import ProcedureRun
from backend.contracts.ports import ConfirmationRequest
from backend.services.judgment_engine import ROW_EVIDENCE
from backend.services.rule_engine import SqlRuleEngine

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "rows",
    [
        [{"source_id": "x"}],
        [{"source_type": "unknown", "source_id": "x"}],
        [{"source_type": "sales_invoice", "source_id": None}],
    ],
)
async def test_rule_invalid_provenance_is_rule_error(rows):
    template = ProcedureTemplate.model_validate(
        dict(
            id="REV-08",
            name="Match",
            phase="P2_Executing",
            cycle="Revenue",
            owner="A6",
            engine="rule",
            autonomy_level="L3",
            rules=[dict(id="R01", name="Rule", severity="high", sql="SELECT 1 ORDER BY 1")],
        )
    )
    run = ProcedureRun(
        id="RUN-REV-08-1",
        procedure_id="REV-08",
        run_no=1,
        trigger="initial",
        params_snapshot={},
        started_at=datetime.now(UTC),
        status="running",
    )
    financial = AsyncMock()
    financial.query.return_value = rows
    with pytest.raises(RuleError, match="REV-08/R01"):
        await SqlRuleEngine(financial).run("ENG", template, run, {})
    financial.query.side_effect = TimeoutError("timeout")
    with pytest.raises(RuleError, match="timeout"):
        await SqlRuleEngine(financial).run("ENG", template, run, {})


@pytest.mark.parametrize("source_type", sorted(ROW_EVIDENCE))
async def test_rule_rows_point_like_the_output_validator(source_type: str) -> None:
    """规则 SQL 出的每种行 (含应付账单 supplier_bill, 需求 R178), 指针的类型与可靠性同 OutputValidator 解析行指针的
    ROW_EVIDENCE: 规则例外与判断里引用同一行时证据分类一致."""
    template = ProcedureTemplate.model_validate(
        dict(
            id="GEN-04",
            name="Rows",
            phase="P2_Executing",
            cycle="Pervasive",
            owner="A6",
            engine="rule",
            autonomy_level="L3",
            rules=[dict(id="R01", name="Rule", severity="medium", sql="SELECT 1 ORDER BY 1")],
        )
    )
    run = ProcedureRun(
        id="RUN-GEN-04-1",
        procedure_id="GEN-04",
        run_no=1,
        trigger="initial",
        params_snapshot={},
        started_at=datetime.now(UTC),
        status="running",
    )
    financial = AsyncMock()
    financial.query.return_value = [{"source_type": source_type, "source_id": "ROW-1", "amount_cents": 100}]
    result = await SqlRuleEngine(financial).run("ENG", template, run, {})
    [pointer] = result.exceptions[0].evidence
    assert (pointer.type, pointer.reliability, pointer.source_id) == (*ROW_EVIDENCE[source_type], "ROW-1")


async def test_registry_and_durable_confirmation(tmp_path):
    registry = MockRegistrySource(ROOT / "data/demo")
    assert (await registry.asic_company("E", "900 042 015")).name == "Numbat Electronic Components Pty Ltd"
    assert await registry.asic_company("E", "000000000") is None
    request = ConfirmationRequest(
        bank_account_id="BA-01",
        bank_name="Harbourline Bank Limited",
        bsb="999-101",
        account_no="12345601",
        as_of="2026-06-30",
    )
    channel = MockConfirmationChannel(ROOT / "data/demo", tmp_path)
    first = await channel.submit("ENG", request, "key")
    # Previously received bytes remain available even if source fixtures are removed.
    restarted = MockConfirmationChannel(tmp_path / "missing-dataset", tmp_path)
    assert await restarted.submit("ENG", request, "key") == first
    with pytest.raises(ValueError, match="Idempotency"):
        await restarted.submit("ENG", request.model_copy(update={"account_no": "23456702"}), "key")
    absent = await channel.submit("ENG", request.model_copy(update={"account_no": "00000000"}), "absent")
    assert absent.reply_bytes is None and absent.replied_at is None
