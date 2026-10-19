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
