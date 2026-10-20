"""复核任务的涉及金额与复核档位 (R3): 纯函数 task_amount, review_tier, 以及复核任务列表接口.

演示数据核对用 Numbat (真实 Agent 与 mock 输出跑到复核阶段): Xero 没有发运单, 合同与回函, 跑不出复核任务.
"按交易行取金额" 与负数金额用 Xero Demo Company 快照的发票, 分录与银行流水 (本机没有快照时跳过).
"""

from typing import Any

import pytest

from backend.adapters.xero_dataset import build_tables
from backend.contracts.models import (
    AuditException,
    AutonomyLevel,
    Direction,
    EvidencePointer,
    EvidenceType,
    Judgment,
    Misstatement,
    MisstatementType,
    MisstatementUpdate,
    ProcedureRun,
    ProposedAction,
    ReviewTask,
    ReviewTier,
    RunStatus,
    RunTrigger,
    TargetType,
    TaskKind,
)
from backend.services.review_queue import judgment_amount, review_tier, row_amount, subject_row, task_amount
from tests.fakes import ENGAGEMENT, NOW, NUMBAT_CUTOFF_DRAFT, api_client, wait_idle, xero_snapshot
from tests.test_e2e_mock import start_demo
from tests.test_flow import REVIEWS

PM = 9_000_000  # Numbat 的执行重要性, 单位分
TRIVIAL = 600_000  # Numbat 的明显微小临界值


def pointer(source_type: Any, source_id: str) -> EvidencePointer:
    return EvidencePointer(type=EvidenceType.invoice, source_type=source_type, source_id=source_id, locator="amount")


def judgment(confidence: float = 0.95, **changes: Any) -> Judgment:
    base = Judgment(
        id="J-REV-02-1-S",
        procedure_id="REV-02",
        run_id="RUN-REV-02-1",
        subject_id="S",
        conclusion="",
        confidence=confidence,
        confidence_parts={},
        reasoning_chain=[],
        evidence=[],
        counter_argument="",
        proposed_action=ProposedAction.accept,
        autonomy_level=AutonomyLevel.L3,
        performed_by="U-1",
        created_at=NOW,
    )
    return base.model_copy(update=changes)


def exception(amount: int | None, xid: str = "X-1") -> AuditException:
    return AuditException(
        id=xid,
        procedure_id="REV-08",
        run_id="RUN-REV-08-1",
        rule_id="R01",
        severity="high",
        source_ids=[],
        amount_cents=amount,
        evidence=[],
    )


def task(kind: TaskKind = TaskKind.normal) -> ReviewTask:
    return ReviewTask(
        id="T",
        kind=kind,
        procedure_id="REV-02",
        target_type=TargetType.Judgment,
        target_id="J",
        target_version=1,
        run_id="RUN-REV-02-1",
        created_at=NOW,
    )


def tier(
    target: Judgment | AuditException | ProcedureRun, amount: int | None, kind: TaskKind = TaskKind.normal
) -> ReviewTier:
    return review_tier(task(kind), target, amount, PM, TRIVIAL)


# ── 涉及金额 ──


def test_amount_prefers_draft_then_largest_exception_by_magnitude() -> None:
    with_draft = judgment(misstatement_draft=NUMBAT_CUTOFF_DRAFT, exception_ids=["X-1"])
    assert task_amount(with_draft, {"X-1": exception(1)}, subject_amount=5) == NUMBAT_CUTOFF_DRAFT.amount_cents
    linked = judgment(exception_ids=["X-1", "X-2", "X-3", "X-9"])
    exceptions = {
        "X-1": exception(3_000_000, "X-1"),
        "X-2": exception(-5_000_000, "X-2"),
        "X-3": exception(None, "X-3"),
    }
    assert task_amount(linked, exceptions, subject_amount=1) == -5_000_000  # 按绝对值取最大, 保留负号; 缺的跳过


def test_amount_of_exception_target_subject_row_or_none() -> None:
    assert task_amount(exception(-120_000), {}) == -120_000
    assert task_amount(judgment(), {}, subject_amount=9_500_000) == 9_500_000
    assert task_amount(judgment(), {}) is None


def test_judgment_amount_takes_the_updated_misstatement_first() -> None:
    """需求 R168 验收 6: judgment_amount (复核档位与自主度判断级降级同一口径) 先取错报更新 (FIN-03 分类, FIN-09 验证)
    所指错报的金额, 再依次为草稿, 解释的例外, 交易行; 所指错报不在给定的错报中时跳过这一步. task_amount 对判断即用它."""
    update = MisstatementUpdate(misstatement_id="M-D-1", base_version=1, kind="classification", objective_id="O")
    found = Misstatement(
        id="M-D-1",
        judgment_id="J",
        exception_id=None,
        review_record_id="D-1",
        type=MisstatementType.factual,
        account="Revenue",
        assertion="Occurrence",
        amount_cents=-426_120,
        direction=Direction.overstated,
    )
    j = judgment(misstatement_update=update, misstatement_draft=NUMBAT_CUTOFF_DRAFT, exception_ids=["X-1"])
    exceptions = {"X-1": exception(1)}
    assert judgment_amount(j, exceptions, subject_amount=5, misstatements={found.id: found}) == -426_120
    assert task_amount(j, exceptions, 5, {found.id: found}) == -426_120
    assert judgment_amount(j, exceptions, subject_amount=5) == NUMBAT_CUTOFF_DRAFT.amount_cents  # 没给错报: 跳过


# ── 复核档位 ──


def test_tier_by_target_amount_and_confidence() -> None:
    run = ProcedureRun(
        id="RUN-REV-02-1",
        procedure_id="REV-02",
        run_no=1,
        trigger=RunTrigger.initial,
        params_snapshot={},
        started_at=NOW,
        status=RunStatus.succeeded,
    )
    assert tier(run, None) is ReviewTier.check_evidence
    assert tier(exception(50_000_000), 50_000_000) is ReviewTier.check_evidence  # 目标不是判断, 金额不看
    assert tier(judgment(0.99), PM + 1) is ReviewTier.full_review
    assert tier(judgment(0.99), -(PM + 1)) is ReviewTier.full_review  # 负数按绝对值
    assert tier(judgment(0.99), PM) is ReviewTier.read_reasoning  # 等于执行重要性不算超过
    assert tier(judgment(0.69), None) is ReviewTier.check_evidence
    assert tier(judgment(0.70), None) is ReviewTier.read_reasoning
    assert tier(judgment(0.90), None) is ReviewTier.read_reasoning


def test_batch_only_for_high_confidence_small_normal_non_l1() -> None:
    assert tier(judgment(0.95), None) is ReviewTier.batch_eligible
    assert tier(judgment(0.95), -TRIVIAL) is ReviewTier.batch_eligible
    assert tier(judgment(0.95), TRIVIAL + 1) is ReviewTier.read_reasoning
    assert tier(judgment(0.95, autonomy_level=AutonomyLevel.L1), None) is ReviewTier.read_reasoning
    assert tier(judgment(0.95), None, TaskKind.failed) is ReviewTier.read_reasoning


def test_tier_before_materiality_is_set() -> None:
    """PLN-01/03 之前没有阈值: 不判超过执行重要性; 有金额时不能确定 "金额小", 不批量."""
    assert review_tier(task(), judgment(0.95), 10**12, None, None) is ReviewTier.read_reasoning
    assert review_tier(task(), judgment(0.95), None, None, None) is ReviewTier.batch_eligible


# ── 交易行取金额: Xero 快照 ──


@pytest.mark.external
def test_xero_transaction_rows_give_subject_amount() -> None:
    tables = build_tables(xero_snapshot())
    invoice = max(tables["sales_invoices"], key=lambda i: i.amount_cents)
    spend = min(tables["bank_transactions"], key=lambda t: t.amount_cents)  # 付款为负
    line = next(e for e in tables["journal_entries"] if e.credit_cents)

    on_invoice = judgment(0.95, subject_id=invoice.id, evidence=[pointer("sales_invoice", invoice.id)])
    found = subject_row(on_invoice)
    assert found is not None and found.source_id == invoice.id
    assert row_amount("sales_invoice", invoice.model_dump()) == invoice.amount_cents
    assert review_tier(task(), on_invoice, invoice.amount_cents, invoice.amount_cents - 1, 0) is ReviewTier.full_review

    assert spend.amount_cents < 0
    assert row_amount("bank_transaction", spend.model_dump()) == spend.amount_cents
    assert review_tier(task(), judgment(0.95), spend.amount_cents, -spend.amount_cents - 1, 0) is ReviewTier.full_review
    assert row_amount("journal_entry", line.model_dump()) == line.debit_cents - line.credit_cents < 0

    # 对象是合同或银行账户时不按行取: 证据里没有与对象同 ID 的交易行
    on_account = judgment(
        subject_id="090", evidence=[pointer("bank_account", "090"), pointer("sales_invoice", invoice.id)]
    )
    assert subject_row(on_account) is None


# ── 接口: Numbat 演示数据跑到复核阶段 ──


async def test_numbat_review_queue_tiers() -> None:
    """REV-02 依赖 REV-03: 先读到 REV-03 三条与 BA-01, 确认 REV-03 后 REV-02 才派发, 再读到两张发票."""
    s = await start_demo()
    await wait_idle(s.runner, ENGAGEMENT.id)
    url = f"/api/v1/engagements/{ENGAGEMENT.id}/review-tasks?status=pending"
    async with api_client(s.runner) as client:
        first = (await client.get(url)).json()
        for decision in REVIEWS[:3]:  # REV-03 三条 confirm
            await s.submit.submit(ENGAGEMENT.id, decision)
            await wait_idle(s.runner, ENGAGEMENT.id)
        second = (await client.get(url)).json()

    def tiers(views: list[dict[str, Any]]) -> dict[str, tuple[float, int | None, str]]:
        return {v["task"]["id"]: (v["target"]["confidence"], v["amount_cents"], v["tier"]) for v in views}

    assert tiers(first) == {
        # 合同条款评价: 对象是合同, 没有金额
        "T-J-REV-03-1-CON-001-v1": (0.84, None, "read_reasoning"),
        "T-J-REV-03-1-CON-002-v1": (0.84, None, "read_reasoning"),
        "T-J-REV-03-1-CON-003-v1": (0.82, None, "read_reasoning"),
        # 函证差异: 两条例外都是 -50,000 澳元; 独立复核一致后置信度 0.65 (原 0.5625), 仍低于 0.70
        "T-J-CSH-02-1-BA-01-v1": (0.65, -5_000_000, "check_evidence"),
    }
    assert [v["task"]["created_at"] for v in first] == sorted(v["task"]["created_at"] for v in first)
    assert tiers(second) == {
        "T-J-CSH-02-1-BA-01-v1": (0.65, -5_000_000, "check_evidence"),
        # 截止性: 0612 有错报草稿 142,000 澳元, 0618 没有草稿与例外, 取发票金额 95,000 澳元; 都超过执行重要性 90,000
        "T-J-REV-02-1-SI-2026-0612-v1": (0.875, 14_200_000, "full_review"),
        "T-J-REV-02-1-SI-2026-0618-v1": (0.875, 9_500_000, "full_review"),
    }
