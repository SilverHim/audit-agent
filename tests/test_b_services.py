"""Fast failure-path checks for Member B's rule service and mock adapters."""

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from backend.adapters.confirmation_mock import MockConfirmationChannel
from backend.adapters.registry_mock import MockRegistrySource
from backend.contracts.errors import RuleError
from backend.contracts.knowledge import ProcedureTemplate
from backend.contracts.models import AuditException, EvidencePointer, ProcedureRun
from backend.contracts.ports import ConfirmationRequest
from backend.contracts.services import RuleResult
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


# ── 附加证据指针 (需求 R223) ──


async def run_rows(rows: list[dict[str, Any]], procedure_id: str = "FIN-12", rule_id: str = "K01") -> RuleResult:
    """一条规则 SQL 返回 rows 时 SqlRuleEngine 的结果 (第 1 次执行)."""
    template = ProcedureTemplate.model_validate(
        dict(
            id=procedure_id,
            name="Rows",
            phase="P3_Completion",
            cycle="Completion",
            owner="A9",
            engine="rule",
            autonomy_level="L4",
            rules=[dict(id=rule_id, name="Rule", severity="high", sql="SELECT 1 ORDER BY 1")],
        )
    )
    run = ProcedureRun(
        id=f"RUN-{procedure_id}-1",
        procedure_id=procedure_id,
        run_no=1,
        trigger="initial",
        params_snapshot={},
        started_at=datetime.now(UTC),
        status="running",
    )
    financial = AsyncMock()
    financial.query.return_value = rows
    return await SqlRuleEngine(financial).run("ENG", template, run, {})


EXTRAS = [
    {"source_type": "trial_balance", "source_id": "090", "locator": "balance_cents"},
    {"source_type": "bank_account", "source_id": "BA-090"},
]


async def test_extra_pointers_follow_the_main_pointer() -> None:
    """验收 2: 行带 extra_pointers (对象数组, 或其 JSON 文本): 例外 evidence 为主指针加这些指针, 按出现顺序, 种类与
    可靠性按行类型 (银行来源 external_copy), 没给 locator 的为空串; extra_pointers 不进 detail, 也不进主指针的 locator;
    为 NULL 时只有主指针."""
    line = {"source_type": "financial_statement_line", "amount_cents": 5, "line_cents": 7}
    rows = [
        {**line, "source_id": "SOFP-CASH", "extra_pointers": EXTRAS},
        {**line, "source_id": "SOFP-OD", "extra_pointers": json.dumps(EXTRAS)},
        {**line, "source_id": "SOFP-AR", "extra_pointers": None},
    ]
    first, second, third = (await run_rows(rows)).exceptions
    assert [(p.source_type, p.source_id, p.locator, p.type, p.reliability) for p in first.evidence] == [
        ("financial_statement_line", "SOFP-CASH", "amount_cents, line_cents", "voucher", "internal_original"),
        ("trial_balance", "090", "balance_cents", "voucher", "internal_original"),
        ("bank_account", "BA-090", "", "bank", "external_copy"),
    ]
    assert second.evidence[1:] == first.evidence[1:]
    assert [x.detail for x in (first, second, third)] == [{"line_cents": 7}] * 3
    assert [p.source_id for p in third.evidence] == ["SOFP-AR"]
    assert third.evidence[0].locator == "amount_cents, line_cents"


@pytest.mark.parametrize(
    "extra",
    [
        [{"source_type": "ledger", "source_id": "x"}],  # 未知的行类型
        [{"source_type": "trial_balance"}],  # 缺 source_id
        [{"source_type": "trial_balance", "source_id": "090", "locator": 1}],
        {"source_type": "trial_balance", "source_id": "090"},  # 不是数组
        ["090"],
        "not json",
    ],
)
async def test_bad_extra_pointers_are_rule_errors(extra: Any) -> None:
    """验收 2: extra_pointers 不合格 (未知 source_type, 缺 source_id, locator 不是字符串, 不是对象数组, 不是 JSON) 时抛
    RuleError, 消息含程序与规则编号."""
    row = {"source_type": "financial_statement_line", "source_id": "SOFP-CASH", "extra_pointers": extra}
    with pytest.raises(RuleError, match="FIN-12/K01"):
        await run_rows([row])


async def test_rows_without_extra_pointers_are_unchanged() -> None:
    """验收 3: 不带 extra_pointers 的行, 例外与改前逐字段相同 (例: T1 场景 REV-08 R01 的那一行, 与连库 SqlRuleEngine
    得到的相同, 见 tests/test_xero_injections.t1_r01)."""
    row = {
        "source_type": "sales_invoice",
        "source_id": "SI-1",
        "amount_cents": 715000,
        "posting_date": date(2026, 9, 17),
        "signed_date": date(2026, 10, 2),
    }
    [found] = (await run_rows([row], "REV-08", "R01")).exceptions
    assert found == AuditException(
        id="X-REV-08-1-R01-1",
        procedure_id="REV-08",
        run_id="RUN-REV-08-1",
        rule_id="R01",
        severity="high",
        source_ids=["SI-1"],
        amount_cents=715000,
        detail={"posting_date": "2026-09-17", "signed_date": "2026-10-02"},
        evidence=[
            EvidencePointer(
                type="invoice",
                source_type="sales_invoice",
                source_id="SI-1",
                locator="amount_cents, posting_date, signed_date",
                reliability="internal_original",
            )
        ],
    )
