"""A7 独立复核 (R9, GEN-09): 触发与跳过规则 (功能 collab 打开时的 internal_copy, unsupported 与 critic_trigger 见
R149), 两轮输入, 结果写回判断, 置信度, 复核失败的处理.

判断的证据用 Xero Demo Company 快照的发票与分录行 (本机没有快照时跳过); 复核模型用脚本化的假模型,
记下收到的请求, 核对第一轮没有结论与推理链. 文件按页给证据那条用 Numbat 合同 (Xero 没有 PDF).
争议仲裁见 test_dispute.py.
"""

import json
from typing import Any

import pytest
from pydantic_core import to_jsonable_python

from backend.agents.base import RunContext
from backend.agents.review import CRITIC_SYSTEM, ReviewAgent, trigger
from backend.contracts.models import (
    AgentId,
    AutonomyLevel,
    CriticTrigger,
    CriticVerdict,
    DeclaredParty,
    DisputeResolution,
    DocumentRef,
    DocumentType,
    EvidencePointer,
    EvidenceType,
    Judgment,
    Phase,
    Procedure,
    ProposedAction,
    ReasoningStep,
    Reliability,
    StepKind,
    StepStatus,
    TaskKind,
    TrailEntry,
    TrailKind,
)
from backend.contracts.records import JournalEntry, SalesInvoice
from backend.contracts.services import JudgmentOutcome
from backend.contracts.state import initial_state
from backend.services.features import BASELINE
from backend.services.judgment_engine import critic_chain_ok
from backend.services.sampling import sampled
from tests.fakes import (
    ENGAGEMENT,
    NOW,
    NUMBAT_CONTRACTS,
    NUMBAT_PROFILE,
    FakeEngagementStore,
    FakeFinancialStore,
    FakeRegistry,
    FakeTrail,
    ScriptedLLM,
    make_deps,
    make_procedure,
    make_run,
    numbat_rev08_exception,
    xero_engagement,
    xero_financial_store,
    xero_snapshot,
)

CONCLUSION = "SECRET-A6-CONCLUSION: the invoice belongs to the current period."  # 第一轮不能出现
CHAIN_TEXT = "SECRET-A6-CHAIN: the invoice was posted before the period end."  # 第一轮不能出现
COUNTER = "SECRET-A6-COUNTER: a later credit note would reverse it."  # 第一轮不能出现
REVIEW_MODEL = "claude-fable-5-1"
COLLAB = BASELINE | {"collab"}  # 打开智能体协作 (R149 起的触发规则补齐)


def invoice_pointer(invoice: SalesInvoice) -> EvidencePointer:
    """发票行指针, 可靠性同校验器按来源改写后的 (internal_original)."""
    return EvidencePointer(
        type=EvidenceType.invoice,
        source_type="sales_invoice",
        source_id=invoice.id,
        locator="amount_cents",
        reliability=Reliability.internal_original,
    )


def entry_pointer(entry: JournalEntry) -> EvidencePointer:
    """分录行指针, 可靠性同校验器按来源改写后的 (internal_original)."""
    return EvidencePointer(
        type=EvidenceType.voucher,
        source_type="journal_entry",
        source_id=entry.id,
        locator="credit_cents",
        reliability=Reliability.internal_original,
    )


def standard_pointer(fragment_id: str) -> dict[str, Any]:
    return {"type": "standard", "source_type": "standard", "source_id": fragment_id, "locator": "para 31"}


def step(kind: str, text: str, evidence: list[Any] = (), from_steps: list[int] = ()) -> dict[str, Any]:  # type: ignore[assignment]
    pointers = [p.model_dump(mode="json") if isinstance(p, EvidencePointer) else p for p in evidence]
    return {"kind": kind, "text": text, "evidence": pointers, "from_steps": list(from_steps)}


class Xero:
    """一张 Xero 发票及其收入分录行, 装进假财务存储; 判断与 A7 的运行上下文 (REV-02 的一次执行)."""

    def __init__(
        self,
        level: AutonomyLevel = AutonomyLevel.L3,
        review: ScriptedLLM | None = None,
        features: frozenset[str] = BASELINE,
    ) -> None:
        snapshot = xero_snapshot()
        financial = xero_financial_store(snapshot)
        self.invoice = SalesInvoice.model_validate(
            max(
                (
                    r
                    for (source, _), r in financial.records.items()
                    if source == "sales_invoice" and r["journal_entry_id"]
                ),
                key=lambda r: r["amount_cents"],
            )
        )
        self.entry = JournalEntry.model_validate(financial.records[("journal_entry", self.invoice.journal_entry_id)])
        self.trail = FakeTrail()
        self.review = review
        self.features = features
        self.deps = make_deps(
            FakeRegistry(),
            self.trail,
            financial_store=financial,
            llm=ScriptedLLM({}),
            review_llm=review,
            features=features,
        )
        _, engagement = xero_engagement(snapshot)
        template = self.deps.library.load("REV-02")
        procedure = Procedure(id="REV-02", phase=Phase.P2_Executing, template_commit="abc123", autonomy_level=level)
        self.ctx = RunContext(engagement, procedure, template, make_run("REV-02"), initial_state(engagement, [], 1))
        self.standard = template.standards[0]

    def judgment(self, **changes: Any) -> Judgment:
        base = Judgment(
            id=f"J-REV-02-1-{self.invoice.id}",
            procedure_id="REV-02",
            run_id="RUN-REV-02-1",
            subject_id=self.invoice.id,
            conclusion=CONCLUSION,
            confidence=0.8625,
            confidence_parts={"evidence": 0.7, "rule": 1.0, "llm": 0.9},
            reasoning_chain=[
                ReasoningStep(kind=StepKind.fact, text=CHAIN_TEXT, evidence=[invoice_pointer(self.invoice)]),
                ReasoningStep(kind=StepKind.fact, text="The revenue line.", evidence=[entry_pointer(self.entry)]),
            ],
            evidence=[invoice_pointer(self.invoice), entry_pointer(self.entry)],
            counter_argument=COUNTER,
            proposed_action=ProposedAction.accept,
            autonomy_level=AutonomyLevel.L3,
            performed_by="U-1",
            created_at=NOW,
        )
        return base.model_copy(update=changes)

    def agent(self, review: ScriptedLLM) -> ReviewAgent:
        """换上复核模型 review 的 A7, 财务存储与留痕不变."""
        self.review = review
        self.deps = make_deps(
            FakeRegistry(),
            self.trail,
            financial_store=self.deps.financial_store,
            llm=ScriptedLLM({}),
            review_llm=review,
            features=self.features,
        )  # type: ignore[arg-type]
        return ReviewAgent(self.deps)

    def independent(self, chain: list[dict[str, Any]] | None = None) -> str:
        """第一轮的回复: 只引用给出的发票, 分录与 REV-02 的准则片段."""
        return json.dumps(
            {
                "conclusion": f"Invoice {self.invoice.invoice_no} was raised and posted to revenue in the period.",
                "reasoning_chain": chain
                if chain is not None
                else [
                    step(
                        "fact",
                        f"Invoice {self.invoice.invoice_no} is dated {self.invoice.invoice_date}.",
                        [invoice_pointer(self.invoice)],
                    ),
                    step("fact", "The revenue line is credited on the same day.", [entry_pointer(self.entry)]),
                    step(
                        "standard", "Revenue is recognised when control transfers.", [standard_pointer(self.standard)]
                    ),
                    step("inference", "The invoice is revenue of the period.", from_steps=[0, 1, 2]),
                ],
            }
        )


def verdict(value: str = "consistent", reason_type: str | None = None) -> str:
    return json.dumps({"critic_verdict": value, "critic_reason_type": reason_type, "critic_reason": "Same finding."})


def scripted(x_subject: str, r1: list[str], r2: list[str]) -> ScriptedLLM:
    return ScriptedLLM({f"{x_subject}-r1": r1, f"{x_subject}-r2": r2}, model=REVIEW_MODEL)


async def reviewed(
    level: AutonomyLevel = AutonomyLevel.L3, r2: str | None = None, **changes: Any
) -> tuple[Xero, Judgment]:
    """跑一次复核: 先建上下文取得发票, 再按发票给复核模型的两轮回复."""
    x = Xero(level)
    x.review = scripted(x.invoice.id, [x.independent()], [r2 or verdict()])
    x.deps = make_deps(
        FakeRegistry(), x.trail, financial_store=x.deps.financial_store, llm=ScriptedLLM({}), review_llm=x.review
    )  # type: ignore[arg-type]
    return x, await ReviewAgent(x.deps).review(x.ctx, x.judgment(**changes))


# ── 触发与跳过规则 ──


@pytest.mark.external
async def test_l2_judgment_is_skipped_with_reason() -> None:
    x, j = await reviewed(level=AutonomyLevel.L2)
    assert (j.critic_verdict, j.critic_reason) == (
        CriticVerdict.skipped,
        "L2: 审计师逐条复核, 不经独立复核 (GEN-09 跳过规则)",
    )
    assert x.review is not None and x.review.requests == [] and j.confidence == 0.8625


@pytest.mark.external
async def test_l3_closing_judgment_is_reviewed() -> None:
    x, j = await reviewed(proposed_action=ProposedAction.close)
    assert j.critic_verdict is CriticVerdict.consistent
    assert x.review is not None and [r.fixture_key for r in x.review.requests] == [
        f"{x.invoice.id}-r1",
        f"{x.invoice.id}-r2",
    ]


@pytest.mark.external
async def test_assumption_step_triggers_review_without_main_rule() -> None:
    """建议认定错报 (不会自动关闭) 时主规则不命中; 推理链含 assumption 仍要复核, 没有时 (且未抽中) 跳过."""
    assumption = ReasoningStep(kind=StepKind.assumption, text="The goods were delivered.")
    x = Xero()
    plain = x.judgment(proposed_action=ProposedAction.misstatement)
    assert not sampled(plain.id, 0.05)  # 这张发票的判断 ID 不在 5% 抽样里
    assert trigger(AutonomyLevel.L3, plain, 0.05) is None
    with_assumption = plain.model_copy(update={"reasoning_chain": [*plain.reasoning_chain, assumption]})
    assert trigger(AutonomyLevel.L3, with_assumption, 0.05) == "assumption"
    _, j = await reviewed(proposed_action=ProposedAction.misstatement)
    assert (j.critic_verdict, j.critic_reason) == (CriticVerdict.skipped, "未命中独立复核的触发规则")


# ── 触发规则补齐 (R149, 功能 collab): 判定顺序与 critic_trigger ──

COPY = DocumentRef(
    id="DOC-IC-1",
    type=DocumentType.invoice,
    file_name="invoice-copy.pdf",
    sha256="0" * 64,
    page_count=1,
    reliability=Reliability.internal_copy,
    status="extracted",
    uploaded_at=NOW,
)  # 测试构造的发票复印件 (内部复印件), 一页
COPY_PAGE = EvidencePointer(
    type=EvidenceType.invoice, source_type="document", source_id=COPY.id, locator="amount", page=1
)


def copy_only(x: Xero, keep_row: bool = False, **changes: Any) -> Judgment:
    """建议认定错报 (主规则不中), 无 assumption 的判断; 指针全换成复印件第 1 页 (keep_row 时另留发票行指针)."""
    rows = [invoice_pointer(x.invoice)] if keep_row else []
    chain = [
        ReasoningStep(kind=StepKind.fact, text=CHAIN_TEXT, evidence=[COPY_PAGE, *rows]),
        ReasoningStep(kind=StepKind.inference, text="So it is revenue.", from_steps=[0]),
    ]
    base = {"proposed_action": ProposedAction.misstatement, "evidence": [COPY_PAGE, *rows], "reasoning_chain": chain}
    return x.judgment(**(base | changes))


def copy_review(x: Xero) -> ScriptedLLM:
    """复核模型: 第一轮只引用复印件第 1 页, 第二轮判一致."""
    chain = [
        step("fact", "The copy shows the invoice amount.", [COPY_PAGE]),
        step("inference", "The invoice is revenue of the period.", from_steps=[0]),
    ]
    return scripted(x.invoice.id, [x.independent(chain)], [verdict()])


@pytest.mark.external
async def test_collab_triggers_internal_copy_and_unsupported_in_order() -> None:
    """验收 3: Xero 发票判断 (REV-02, L3, 建议 misstatement, 指针为发票行与分录行, internal_original), 判断 ID
    不在 5% 抽样里. collab 打开: 照现状跳过, 复核模型收不到请求; 指针全换成复印件 DOC-IC-1 第 1 页
    → internal_copy, 收到 -r1, -r2; 另留一个发票行指针 → 不中; 第 1 步 unsupported → unsupported.
    命中时记下 critic_trigger, 未命中为空."""
    x = Xero(features=COLLAB)
    x.deps.financial_store.document_refs.append(COPY)  # type: ignore[attr-defined]
    plain = x.judgment(proposed_action=ProposedAction.misstatement)
    assert not sampled(plain.id, 0.05)
    keys = [f"{x.invoice.id}-r1", f"{x.invoice.id}-r2"]

    async def run(judgment: Judgment) -> tuple[Judgment, list[str]]:
        agent = x.agent(copy_review(x))
        result = await agent.review(x.ctx, judgment)
        assert x.review is not None
        return result, [r.fixture_key for r in x.review.requests]

    j, calls = await run(plain)
    assert (j.critic_verdict, j.critic_reason, j.critic_trigger, calls) == (
        CriticVerdict.skipped,
        "未命中独立复核的触发规则",
        None,
        [],
    )
    j, calls = await run(copy_only(x))
    assert (j.critic_trigger, calls, j.critic_verdict) == (CriticTrigger.internal_copy, keys, CriticVerdict.consistent)
    j, calls = await run(copy_only(x, keep_row=True))
    assert (j.critic_verdict, j.critic_trigger, calls) == (CriticVerdict.skipped, None, [])
    chain = plain.reasoning_chain
    unsupported = plain.model_copy(
        update={"reasoning_chain": [chain[0].model_copy(update={"status": StepStatus.unsupported}), *chain[1:]]}
    )
    j, calls = await run(unsupported)
    assert (j.critic_trigger, calls) == (CriticTrigger.unsupported, keys)


def copy_with_assumption(x: Xero) -> Judgment:
    """复印件判断的推理链再加一步 assumption: internal_copy 与 assumption 同时命中."""
    base = copy_only(x)
    step = ReasoningStep(kind=StepKind.assumption, text="The goods were delivered.")
    return base.model_copy(update={"reasoning_chain": [*base.reasoning_chain, step]})


# 没登记的文件页: 解析不了, 模型却填了内部原件
GHOST = EvidencePointer(
    type=EvidenceType.invoice,
    source_type="document",
    source_id="DOC-NOT-REGISTERED",
    locator="amount",
    page=1,
    reliability=Reliability.internal_original,
)


def lying_row(x: Xero) -> Judgment:
    """复印件之外另有发票行指针, 模型把它的可靠性填成 internal_copy (来源是 internal_original)."""
    lying = invoice_pointer(x.invoice).model_copy(update={"reliability": Reliability.internal_copy})
    return copy_only(x, evidence=[COPY_PAGE, lying])


@pytest.mark.external
@pytest.mark.parametrize(
    ("make", "expected"),
    [
        (copy_with_assumption, CriticTrigger.internal_copy),
        (lambda x: copy_only(x, evidence=[COPY_PAGE, GHOST]), CriticTrigger.internal_copy),
        (lying_row, None),
        (lambda x: copy_only(x, keep_row=True, evidence=[COPY_PAGE]), None),
    ],
    ids=["with_assumption", "unresolved_not_counted", "model_reliability_ignored", "chain_pointer_counts"],
)
async def test_collab_internal_copy_reads_given_evidence(make: Any, expected: CriticTrigger | None) -> None:
    """R149 验收 2 (第 1 轮审核 R149-01, 02), review 层, collab 打开: 复印件与 assumption 同在时按判定顺序记
    internal_copy (防 review 只在旧三条都不中时才取证据); 解析不了的指针不算 (防按模型填的值算); 发票行按来源为
    原件, 模型填的 internal_copy 不采信 (防模型填了就用); 结论只引用复印件而推理链另引用发票行时不中 (防只看结论的
    指针)."""
    x = Xero(features=COLLAB)
    x.deps.financial_store.document_refs.append(COPY)  # type: ignore[attr-defined]
    j = await x.agent(copy_review(x)).review(x.ctx, make(x))
    assert j.critic_trigger is expected


@pytest.mark.external
async def test_collab_review_hits_sample_with_gen09_rate() -> None:
    """R149 验收 2 的 sample (第 1 轮审核 R149-05): 判断 ID 在 GEN-09 的 sample_rate (0.05) 下抽中, 没有其他触发,
    collab 打开: critic_trigger 为 sample, 复核模型收到两轮请求 (防 review 不读 GEN-09 的 sample_rate)."""
    hit = next(i for i in (f"J-REV-02-1-X{n}" for n in range(2000)) if sampled(i, 0.05))
    x = Xero(features=COLLAB)
    review = scripted(x.invoice.id, [x.independent()], [verdict()])
    j = await x.agent(review).review(x.ctx, x.judgment(id=hit, proposed_action=ProposedAction.misstatement))
    assert j.critic_trigger is CriticTrigger.sample
    assert [r.fixture_key for r in review.requests] == [f"{x.invoice.id}-r1", f"{x.invoice.id}-r2"]


@pytest.mark.external
async def test_collab_l2_skips_before_any_trigger() -> None:
    """验收 3: 程序运行时自主度 L2 时, 复印件, unsupported 与主规则都不复核, critic_trigger 为空."""
    x = Xero(AutonomyLevel.L2, features=COLLAB)
    x.deps.financial_store.document_refs.append(COPY)  # type: ignore[attr-defined]
    chain = x.judgment().reasoning_chain
    cases = [
        copy_only(x),
        x.judgment(reasoning_chain=[chain[0].model_copy(update={"status": StepStatus.unsupported}), *chain[1:]]),
        x.judgment(proposed_action=ProposedAction.close),
    ]
    for judgment in cases:
        j = await x.agent(copy_review(x)).review(x.ctx, judgment)
        assert (j.critic_verdict, j.critic_trigger) == (CriticVerdict.skipped, None)
        assert j.critic_reason == "L2: 审计师逐条复核, 不经独立复核 (GEN-09 跳过规则)"
        assert x.review is not None and x.review.requests == []


@pytest.mark.external
async def test_collab_off_keeps_previous_rules_and_no_trigger_recorded() -> None:
    """验收 5: collab 关闭时复印件与 unsupported 两条不判 (照现状跳过), 命中主规则时 critic_trigger 也为空."""
    x = Xero()
    x.deps.financial_store.document_refs.append(COPY)  # type: ignore[attr-defined]
    chain = x.judgment().reasoning_chain
    for judgment in (
        copy_only(x),
        x.judgment(
            proposed_action=ProposedAction.misstatement,
            reasoning_chain=[chain[0].model_copy(update={"status": StepStatus.unsupported}), *chain[1:]],
        ),
    ):
        j = await x.agent(copy_review(x)).review(x.ctx, judgment)
        assert (j.critic_verdict, j.critic_trigger) == (CriticVerdict.skipped, None)
    _, j = await reviewed(proposed_action=ProposedAction.close)
    assert (j.critic_verdict, j.critic_trigger) == (CriticVerdict.consistent, None)


@pytest.mark.external
async def test_collab_records_main_and_keeps_trigger_when_review_incomplete() -> None:
    """collab 打开: 主规则 (L3 建议 close) 记 main; 两轮复核没做成 (not_applicable) 时 critic_trigger 照留."""
    x = Xero(features=COLLAB)
    j = await x.agent(scripted(x.invoice.id, [x.independent()], [verdict()])).review(
        x.ctx, x.judgment(proposed_action=ProposedAction.close)
    )
    assert (j.critic_verdict, j.critic_trigger) == (CriticVerdict.consistent, CriticTrigger.main)
    j = await x.agent(scripted(x.invoice.id, ["not json"] * 3, [verdict()])).review(x.ctx, x.judgment())
    assert (j.critic_verdict, j.critic_trigger) == (CriticVerdict.not_applicable, CriticTrigger.main)


def first_round(x: Xero) -> Any:
    """最近一次独立复核第一轮的 llm_call 留痕."""
    return [e for e in x.trail.entries if e.kind is TrailKind.llm_call and e.payload["fixture_key"].endswith("-r1")][-1]


@pytest.mark.external
async def test_collab_critic_draft_amount_may_come_from_explained_exceptions() -> None:
    """R150 验收 2: 复核方建议错报, 草稿金额只等于该判断解释的例外的金额 (不是给出的发票行, 分录行的金额,
    也不是两行之差): 传入例外时写回草稿; 不传时草稿不写回, critic_proposed_action 照写, 问题记进第一轮留痕的
    notes. collab 关闭时两项都不写回, 也不核对草稿, 留痕不记 "草稿不采用" (第 1 轮审核 R150-04)."""
    x = Xero(features=COLLAB)
    amount = 1234567
    explained = numbat_rev08_exception(make_run("REV-02")).model_copy(update={"amount_cents": -amount})
    draft = {"type": "factual", "account": "Revenue", "assertion": "Cutoff", "amount_cents": amount}
    first = json.loads(x.independent()) | {
        "proposed_action": "misstatement",
        "misstatement_draft": draft | {"direction": "overstated"},
    }

    def review() -> ScriptedLLM:
        return scripted(x.invoice.id, [json.dumps(first)], [verdict("inconsistent", "different_conclusion")])

    closing = x.judgment(proposed_action=ProposedAction.close)
    j = await x.agent(review()).review(x.ctx, closing, [explained])
    assert j.critic_proposed_action is ProposedAction.misstatement
    assert j.critic_misstatement_draft is not None and j.critic_misstatement_draft.amount_cents == amount
    j = await x.agent(review()).review(x.ctx, closing)
    assert (j.critic_proposed_action, j.critic_misstatement_draft) == (ProposedAction.misstatement, None)
    [note] = first_round(x).payload["notes"]
    assert note.startswith("独立复核的错报金额 1234567 不等于"), note
    x.features = BASELINE
    j = await x.agent(review()).review(x.ctx, closing)
    assert (j.critic_verdict, j.critic_proposed_action, j.critic_misstatement_draft) == (
        CriticVerdict.inconsistent,
        None,
        None,
    )
    assert first_round(x).payload["notes"] == []


@pytest.mark.external
async def test_consistent_with_invalid_chain_reason_leaves_out_draft_problems() -> None:
    """第 1 轮审核 R150-04: 判一致而独立推理链无效 (not_applicable) 时, critic_reason 只写推理链的问题, 不混进草稿的
    问题 (草稿金额对不上, 记在 notes)."""
    x = Xero(features=COLLAB)
    chain = [{"kind": "inference", "text": "So it is fine.", "evidence": [], "from_steps": [3]}]
    draft = {
        "type": "factual",
        "account": "Revenue",
        "assertion": "Cutoff",
        "amount_cents": 4321,
        "direction": "overstated",
    }
    first = json.loads(x.independent(chain)) | {"proposed_action": "misstatement", "misstatement_draft": draft}
    review = scripted(x.invoice.id, [json.dumps(first)], [verdict("consistent")])
    j = await x.agent(review).review(x.ctx, x.judgment())
    assert j.critic_verdict is CriticVerdict.not_applicable
    assert "推理链" in (j.critic_reason or "") and "草稿" not in (j.critic_reason or "")
    assert [n for n in first_round(x).payload["notes"] if "草稿不采用" in n]


@pytest.mark.external
def test_trigger_order_and_sample() -> None:
    """判定顺序 main → internal_copy → assumption → unsupported → sample, 命中即停; 准则指针不算证据, 只有准则时
    internal_copy 不中; collab 关闭时只判 main, assumption, sample."""
    x = Xero()
    copy = [COPY_PAGE.model_copy(update={"reliability": Reliability.internal_copy})]
    standard = [EvidencePointer.model_validate(standard_pointer(x.standard))]
    assumption = ReasoningStep(kind=StepKind.assumption, text="The goods were delivered.")
    unsupported = ReasoningStep(kind=StepKind.fact, text="x", status=StepStatus.unsupported)
    j = x.judgment(proposed_action=ProposedAction.misstatement)
    both = j.model_copy(update={"reasoning_chain": [assumption, unsupported]})
    l3 = AutonomyLevel.L3
    assert trigger(l3, j.model_copy(update={"proposed_action": ProposedAction.accept}), 0.05, copy, True) == "main"
    assert trigger(l3, both, 0.05, copy, True) is CriticTrigger.internal_copy
    assert trigger(l3, both, 0.05, [*copy, *standard], True) is CriticTrigger.internal_copy
    assert trigger(l3, both, 0.05, standard, True) is CriticTrigger.assumption
    only_unsupported = j.model_copy(update={"reasoning_chain": [unsupported]})
    assert trigger(l3, only_unsupported, 0.05, standard, True) is CriticTrigger.unsupported
    assert trigger(l3, only_unsupported, 0.05, copy, False) is None
    assert trigger(l3, only_unsupported, 1.0, copy, False) is CriticTrigger.sample
    assert trigger(l3, only_unsupported, 1.0, (), True) is CriticTrigger.unsupported


def test_sampling_is_replayable() -> None:
    """抽样按判断 ID 的 sha256 (services/sampling.sampled, 键为判断 ID): 同一 ID 每次结果相同; 约 5% 的 ID 抽中."""
    ids = [f"J-REV-02-1-X{n}" for n in range(2000)]
    first = [sampled(i, 0.05) for i in ids]
    assert first == [sampled(i, 0.05) for i in ids]
    assert 60 <= sum(first) <= 140
    hit = ids[first.index(True)]
    judgment = Judgment(
        id=hit,
        procedure_id="REV-02",
        run_id="RUN-REV-02-1",
        subject_id="X",
        conclusion="",
        confidence=0.9,
        confidence_parts={},
        reasoning_chain=[],
        evidence=[],
        counter_argument="",
        proposed_action=ProposedAction.misstatement,
        autonomy_level=AutonomyLevel.L3,
        performed_by="U-1",
        created_at=NOW,
    )
    assert trigger(AutonomyLevel.L4, judgment, 0.05) == "sample"


@pytest.mark.external
async def test_no_review_model_means_skipped() -> None:
    x = Xero()
    j = await ReviewAgent(x.deps).review(x.ctx, x.judgment())  # make_deps 给了 llm 没给 review_llm: 复核模型为空
    assert (j.critic_verdict, j.critic_reason) == (CriticVerdict.skipped, "未配置复核模型, 不做独立复核")


# ── 两轮输入与结果 ──


@pytest.mark.external
async def test_first_round_has_evidence_but_no_conclusion_or_chain() -> None:
    x, _ = await reviewed()
    assert x.review is not None
    first, second = x.review.requests
    prompt = first.messages[0].content
    for hidden in (CONCLUSION, CHAIN_TEXT, COUNTER):
        assert hidden not in prompt and hidden not in first.system
    assert x.invoice.invoice_no in prompt and str(x.invoice.amount_cents) in prompt  # 证据内容: Xero 发票行
    assert x.entry.id in prompt and str(x.entry.credit_cents) in prompt  # 证据内容: Xero 分录行
    assert first.system.startswith(CRITIC_SYSTEM.split("{schema}")[0].strip()[:40])
    assert CONCLUSION in second.messages[0].content  # 第二轮才给 A6 的结论
    assert (first.procedure_id, first.run_id) == ("GEN-09", "RUN-REV-02-1")


@pytest.mark.external
async def test_consistent_adds_critic_part_to_confidence_and_records_review_model() -> None:
    x, j = await reviewed()
    # (0.3 × 0.7 + 0.3 × 1.0 + 0.2 × 0.9 + 0.2 × 1.0) / 1.0; 原为三项 / 0.8 = 0.8625
    assert (j.confidence, j.confidence_parts["critic"]) == (0.89, 1.0)
    assert j.critic_conclusion is not None and x.invoice.invoice_no in j.critic_conclusion
    assert [s.status for s in j.critic_reasoning_chain] == [StepStatus.supported] * 4 and critic_chain_ok(
        j.critic_reasoning_chain
    )
    calls = [e for e in x.trail.entries if e.kind is TrailKind.llm_call]
    assert [(e.procedure_id, e.actor, e.model) for e in calls] == [("GEN-09", AgentId.A7, REVIEW_MODEL)] * 2
    assert (j.critic_model, j.critic_trail_ref) == (REVIEW_MODEL, calls[1].id)
    assert j.version == 1 and j.conclusion == CONCLUSION  # 还是 A6 的那一版, 只多了复核结果


@pytest.mark.external
async def test_inconsistent_keeps_confidence_and_becomes_dispute() -> None:
    x, j = await reviewed(r2=verdict("inconsistent", "different_conclusion"))
    assert (j.critic_verdict, j.critic_reason_type, j.confidence) == (
        CriticVerdict.inconsistent,
        "different_conclusion",
        0.8625,
    )
    outcome = JudgmentOutcome(judgment=j, failure=None, attempts=1, trail_ids=[])
    assert x.deps.queue.classify(x.ctx.template, outcome, []) is TaskKind.dispute


@pytest.mark.external
async def test_chain_citing_evidence_not_given_is_unsupported() -> None:
    """第一轮推理链引用了没给出的行: 该步不受支持, 推理链无效 (仲裁时不提供 a7_upheld); 结论照存."""
    x = Xero()
    stranger = EvidencePointer(
        type=EvidenceType.voucher, source_type="journal_entry", source_id="NOT-GIVEN", locator="x"
    )
    chain = [step("fact", "A different line.", [stranger]), step("inference", "So it is revenue.", from_steps=[0])]
    x.review = scripted(x.invoice.id, [x.independent(chain)], [verdict("inconsistent", "insufficient_evidence")])
    deps = make_deps(
        FakeRegistry(), x.trail, financial_store=x.deps.financial_store, llm=ScriptedLLM({}), review_llm=x.review
    )  # type: ignore[arg-type]
    j = await ReviewAgent(deps).review(x.ctx, x.judgment())
    assert [s.status for s in j.critic_reasoning_chain] == [StepStatus.unsupported, StepStatus.supported]
    assert j.critic_verdict is CriticVerdict.inconsistent and not critic_chain_ok(j.critic_reasoning_chain)


@pytest.mark.external
async def test_fragment_outside_the_reviewed_program_is_not_given() -> None:
    """需求 R301: A6 的判断引了已登记而不在被复核程序 (REV-02) standards 里的 ASA450-P05 (A6 校验时该步 unsupported,
    指针原样留在判断里). given_evidence 同 A6 校验按 REV-02 的 standards 核对: 第一轮提示词不列它 (Evidence 段与
    Standards 段都没有), 也不进可引证据; 复核方仍引它的一步不受支持, 判一致也因此记 not_applicable 转人工. 判断引的
    REV-02 自己的片段照样作为证据给出, 复核方引它照常. 防第一轮把校验会拒绝的片段列为 "Cite only these pointers"
    里的证据."""
    x = Xero()
    own = EvidencePointer.model_validate(standard_pointer(x.standard))
    outside = EvidencePointer.model_validate(standard_pointer("ASA450-P05") | {"locator": "para 5"})
    base = x.judgment()
    judgment = x.judgment(
        reasoning_chain=[
            *base.reasoning_chain,
            ReasoningStep(kind=StepKind.standard, text="AASB 15.", evidence=[own]),
            ReasoningStep(kind=StepKind.standard, text="ASA 450.", evidence=[outside]),
        ],
        evidence=[*base.evidence, own, outside],
    )
    chain = [
        step("fact", "The invoice is dated in the period.", [invoice_pointer(x.invoice)]),
        step("standard", "Revenue is recognised when control transfers.", [standard_pointer(x.standard)]),
        step("standard", "Misstatements are accumulated.", [outside]),
        step("inference", "The invoice is revenue of the period.", from_steps=[0, 1, 2]),
    ]
    review = scripted(x.invoice.id, [x.independent(chain)], [verdict()])
    agent = x.agent(review)
    given = await agent.given_evidence(x.ctx.engagement.id, judgment)
    assert [(p.source_type, p.source_id) for p in given][2:] == [("standard", x.standard)]  # 本程序的片段照给
    assert [p.source_type for p in given[:2]] == ["sales_invoice", "journal_entry"]
    j = await agent.review(x.ctx, judgment)
    prompt = review.requests[0].messages[0].content
    evidence = prompt.split("## Evidence", 1)[1].split("Standards (cite only these IDs)", 1)[0]
    assert x.invoice.invoice_no in evidence and f'"source_id": "{x.standard}"' in evidence
    assert "ASA450-P05" not in prompt
    assert [s.status for s in j.critic_reasoning_chain] == [
        StepStatus.supported,
        StepStatus.supported,
        StepStatus.unsupported,
        StepStatus.supported,
    ]
    assert j.critic_verdict is CriticVerdict.not_applicable
    # 第一轮校验也按 REV-02 核对 (兜底: 可引证据已不含它, 这里看错误写明的是片段不在被复核程序的 standards 内)
    assert first_round(x).payload["errors"] == [
        "第 2 步指针 standard ASA450-P05: 准则片段 ASA450-P05 不在 REV-02 的 standards 内"
    ]


# ── 复核没做成: not_applicable, 转人工 ──


@pytest.mark.external
@pytest.mark.parametrize("failure", ["bad_json", "error"])
async def test_failed_review_is_not_applicable_and_goes_to_human(failure: str) -> None:
    x = Xero()
    review = scripted(x.invoice.id, ["not json"] * 3, [verdict()]) if failure == "bad_json" else ScriptedLLM({})
    deps = make_deps(
        FakeRegistry(), x.trail, financial_store=x.deps.financial_store, llm=ScriptedLLM({}), review_llm=review
    )  # type: ignore[arg-type]
    j = await ReviewAgent(deps).review(x.ctx, x.judgment())
    assert j.critic_verdict is CriticVerdict.not_applicable and j.critic_reason is not None
    assert j.critic_reason.startswith("独立复核没有完成")
    outcome = JudgmentOutcome(judgment=j, failure=None, attempts=1, trail_ids=[])
    assert deps.queue.classify(x.ctx.template, outcome, []) is TaskKind.normal  # 本来会自动关闭的, 改由人复核
    assert j.confidence == 0.8625


# ── 无效的独立复核不加分; 第一轮只给能解析的证据 ──

NOT_GIVEN = EvidencePointer(type=EvidenceType.voucher, source_type="journal_entry", source_id="NOT-GIVEN", locator="x")


@pytest.mark.external
@pytest.mark.parametrize(
    "chain",
    [
        pytest.param([], id="empty"),
        pytest.param(
            [step("fact", "A line not given.", [NOT_GIVEN]), step("inference", "So.", from_steps=[0])],
            id="unresolvable",
        ),
        pytest.param([step("inference", "Unsupported.")], id="semantic"),
    ],
)
async def test_consistent_with_invalid_chain_does_not_count(chain: list[dict[str, Any]]) -> None:
    """判为一致而独立推理链无效 (空链, 引用没给的行, inference 不引前序步骤): 不计复核分项, 置信度不变,
    记 not_applicable 转人工; 推理链照存, 供人复核时看."""
    x = Xero()
    j = await x.agent(scripted(x.invoice.id, [x.independent(chain)], [verdict()])).review(x.ctx, x.judgment())
    assert (j.critic_verdict, j.confidence, "critic" in j.confidence_parts) == (
        CriticVerdict.not_applicable,
        0.8625,
        False,
    )
    assert j.critic_reason is not None and j.critic_reason.startswith(
        "独立复核没有完成: 判为一致, 但独立推理链没有通过校验"
    )
    assert not critic_chain_ok(j.critic_reasoning_chain) and j.critic_conclusion is not None
    outcome = JudgmentOutcome(judgment=j, failure=None, attempts=1, trail_ids=[])
    assert x.deps.queue.classify(x.ctx.template, outcome, []) is TaskKind.normal


@pytest.mark.external
async def test_pointer_to_model_call_trail_is_not_given() -> None:
    """A6 校验时解析不了的指针原样留在判断里 (只要还有别的有效指针), 如指向 llm_call 留痕, 其明细里有旧输出的结论:
    第一轮不给它的内容, 推理链引用它也不受支持."""
    x = Xero()
    trail_id = await x.trail.record(
        TrailEntry(
            engagement_id=x.ctx.engagement.id,
            kind=TrailKind.llm_call,
            actor=AgentId.A6,
            procedure_id="REV-02",
            run_id="RUN-REV-02-1",
            summary="An earlier A6 call.",
            payload={"raw": CONCLUSION},
        )
    )
    leaked = EvidencePointer(type=EvidenceType.external, source_type="trail", source_id=trail_id, locator="raw")
    chain = [step("fact", "The earlier call.", [leaked]), step("inference", "So.", from_steps=[0])]
    review = scripted(x.invoice.id, [x.independent(chain)], [verdict("inconsistent", "insufficient_evidence")])
    j = await x.agent(review).review(x.ctx, x.judgment(evidence=[*x.judgment().evidence, leaked]))
    prompt = review.requests[0].messages[0].content
    assert CONCLUSION not in prompt and trail_id not in prompt and x.invoice.invoice_no in prompt
    assert [s.status for s in j.critic_reasoning_chain] == [StepStatus.unsupported, StepStatus.supported]


@pytest.mark.external
async def test_no_resolvable_evidence_is_not_applicable_without_calling_model() -> None:
    x = Xero()
    review = scripted(x.invoice.id, [x.independent()], [verdict()])
    lost = [ReasoningStep(kind=StepKind.fact, text=CHAIN_TEXT, evidence=[NOT_GIVEN])]
    j = await x.agent(review).review(x.ctx, x.judgment(evidence=[NOT_GIVEN], reasoning_chain=lost))
    assert (j.critic_verdict, j.critic_reason) == (
        CriticVerdict.not_applicable,
        "独立复核没有完成: 判断没有能解析的证据指针",
    )
    assert review.requests == [] and j.confidence == 0.8625


PARTY = DeclaredParty(name="Wombat Holdings Pty Ltd", kind="company", relationship="parent entity")
PROFILE_V1 = NUMBAT_PROFILE.model_copy(update={"related_parties": [PARTY]})  # 本项目采用的那一版 (状态中 v1)
CITED_PROFILE = EvidencePointer(
    type=EvidenceType.voucher,
    source_type="client_profile",
    source_id=NUMBAT_PROFILE.id,
    locator="related_parties",
    reliability=Reliability.internal_copy,
)


def profile_case(
    review: ScriptedLLM, profiles: list[Any], features: frozenset[str] = BASELINE
) -> tuple[ReviewAgent, RunContext]:
    """引用客户画像的复核场景 (需求 R128): Numbat 项目的 CSH-02, 项目存储里放 profiles."""
    deps = make_deps(
        FakeRegistry(),
        FakeTrail(),
        engagement_store=FakeEngagementStore(profiles, [ENGAGEMENT]),
        llm=ScriptedLLM({}),
        review_llm=review,
        features=features,
    )
    procedure = make_procedure("CSH-02", phase=Phase.P2_Executing)
    ctx = RunContext(
        ENGAGEMENT, procedure, deps.library.load("CSH-02"), make_run("CSH-02"), initial_state(ENGAGEMENT, [], 1)
    )
    return ReviewAgent(deps), ctx


def profile_judgment(ctx: RunContext, action: ProposedAction = ProposedAction.close) -> Judgment:
    """只在推理链步骤里引用画像 (结论证据为空) 的 BA-01 判断; BA-01 的判断 ID 不在 5% 抽样里."""
    return Judgment(
        id="J-CSH-02-1-BA-01",
        procedure_id="CSH-02",
        run_id=ctx.run.id,
        subject_id="BA-01",
        conclusion=CONCLUSION,
        confidence=0.5625,
        confidence_parts={"evidence": 0.9, "rule": 0.0, "llm": 0.9},
        reasoning_chain=[ReasoningStep(kind=StepKind.fact, text=CHAIN_TEXT, evidence=[CITED_PROFILE])],
        evidence=[],
        counter_argument=COUNTER,
        proposed_action=action,
        autonomy_level=AutonomyLevel.L3,
        performed_by="U-1",
        created_at=NOW,
    )


@pytest.mark.parametrize(
    ("features", "action", "hit"),
    [(BASELINE, ProposedAction.close, None), (COLLAB, ProposedAction.more_evidence, CriticTrigger.internal_copy)],
    ids=["baseline", "collab"],
)
async def test_cited_client_profile_is_given_from_the_adopted_version(
    features: frozenset[str], action: ProposedAction, hit: CriticTrigger | None
) -> None:
    """判断只在推理链步骤里引用客户画像 (client_profile 指针, 需求 R128): 第一轮给出的证据含该指针, 内容为本项目采用
    那一版 (状态中 v1) 该字段的 JSON, 画像之后改出的 v2 不进来; 复核方引用同一指针的步骤 supported. collab 打开时
    触发判定先取第一轮证据, 只靠画像 (internal_copy) 的判断即使不关闭也命中 internal_copy. 防第一轮读最新版画像, 只看
    结论证据而漏了推理链里的画像, collab 分支取证据时漏传画像, 或复核方引用管理层申报的步骤全都站不住."""
    later = DeclaredParty(name="Quoll Trading Pty Ltd", kind="company", relationship="sister company")
    v2 = PROFILE_V1.model_copy(update={"version": 2, "related_parties": [PARTY, later]})
    independent = {
        "conclusion": "Management declared one related party.",
        "reasoning_chain": [
            step("fact", "One party is declared.", [CITED_PROFILE]),
            step("inference", "So.", from_steps=[0]),
        ],
    }
    review = ScriptedLLM({"BA-01-r1": [json.dumps(independent)], "BA-01-r2": [verdict()]})
    agent, ctx = profile_case(review, [PROFILE_V1, v2], features)
    j = await agent.review(ctx, profile_judgment(ctx, action))
    prompt = review.requests[0].messages[0].content
    assert PARTY.name in prompt and later.name not in prompt
    assert json.dumps(to_jsonable_python(PROFILE_V1.related_parties), indent=2, sort_keys=True) in prompt  # 同 tojson
    assert (j.critic_trigger, j.critic_verdict) == (hit, CriticVerdict.consistent)
    assert [s.status for s in j.critic_reasoning_chain] == [StepStatus.supported, StepStatus.supported]


async def test_critic_citing_a_profile_field_not_given_is_unsupported() -> None:
    """第一轮只给判断所引画像字段 (related_parties) 的 JSON; 复核方引用同一画像的另一字段 (directors) 的步骤不受支持
    (evidence_key 对画像比字段, 同文件比页), 判为一致也不计复核分项. 防复核方凭没给过的内容得到支撑."""
    directors = CITED_PROFILE.model_copy(update={"locator": "directors"})
    independent = {
        "conclusion": "The directors are listed.",
        "reasoning_chain": [
            step("fact", "One party is declared.", [CITED_PROFILE]),
            step("fact", "The directors are listed.", [directors]),
            step("inference", "So.", from_steps=[0, 1]),
        ],
    }
    review = ScriptedLLM({"BA-01-r1": [json.dumps(independent)], "BA-01-r2": [verdict()]})
    agent, ctx = profile_case(review, [PROFILE_V1])
    j = await agent.review(ctx, profile_judgment(ctx))
    prompt = review.requests[0].messages[0].content
    assert NUMBAT_PROFILE.directors[0] not in prompt  # 第一轮确实没给 directors
    assert [s.status for s in j.critic_reasoning_chain] == [
        StepStatus.supported,
        StepStatus.unsupported,
        StepStatus.supported,
    ]
    assert j.critic_verdict is CriticVerdict.not_applicable and "critic" not in j.confidence_parts


async def test_collab_off_trigger_miss_does_not_read_the_profile() -> None:
    """collab 关闭时触发判定用不到证据: 引用画像但不命中触发规则的判断记 skipped, 不读项目存储 (存储里没有画像,
    读了就会记 not_applicable). 防触发判定之前就读画像."""
    review = ScriptedLLM({})
    agent, ctx = profile_case(review, [])
    j = await agent.review(ctx, profile_judgment(ctx, ProposedAction.more_evidence))
    assert (j.critic_verdict, j.critic_reason) == (CriticVerdict.skipped, "未命中独立复核的触发规则")
    assert review.requests == []


async def test_document_page_not_given_is_unsupported() -> None:
    """文件按页给: 判断引用合同第 1 页, 第一轮只有这页的文字; 推理链引用同一文件第 2 页不受支持,
    仲裁时不提供 a7_upheld."""
    doc = NUMBAT_CONTRACTS[0]

    def page(n: int) -> EvidencePointer:
        return EvidencePointer(
            type=EvidenceType.contract, source_type="document", source_id=doc.id, locator="clause", page=n
        )

    independent = {
        "conclusion": "Page 2 changes the terms.",
        "reasoning_chain": [step("fact", "Page 2 says so.", [page(2)]), step("inference", "So.", from_steps=[0])],
    }
    review = ScriptedLLM(
        {"BA-01-r1": [json.dumps(independent)], "BA-01-r2": [verdict("inconsistent", "different_conclusion")]}
    )
    deps = make_deps(
        FakeRegistry(), FakeTrail(), financial_store=FakeFinancialStore([doc]), llm=ScriptedLLM({}), review_llm=review
    )
    procedure = make_procedure("CSH-02", phase=Phase.P2_Executing)
    ctx = RunContext(
        ENGAGEMENT, procedure, deps.library.load("CSH-02"), make_run("CSH-02"), initial_state(ENGAGEMENT, [], 1)
    )
    judgment = Judgment(
        id="J-CSH-02-1-BA-01",
        procedure_id="CSH-02",
        run_id=ctx.run.id,
        subject_id="BA-01",
        conclusion=CONCLUSION,
        confidence=0.5625,
        confidence_parts={"evidence": 0.9, "rule": 0.0, "llm": 0.9},
        reasoning_chain=[ReasoningStep(kind=StepKind.fact, text=CHAIN_TEXT, evidence=[page(1)])],
        evidence=[page(1)],
        counter_argument=COUNTER,
        proposed_action=ProposedAction.close,
        autonomy_level=AutonomyLevel.L3,
        performed_by="U-1",
        created_at=NOW,
    )
    j = await ReviewAgent(deps).review(ctx, judgment)
    prompt = review.requests[0].messages[0].content
    assert f"{doc.file_name} page 1" in prompt and f"{doc.file_name} page 2" not in prompt
    assert [s.status for s in j.critic_reasoning_chain] == [StepStatus.unsupported, StepStatus.supported]
    task = deps.queue.make_task(TaskKind.dispute, j, j.procedure_id, j.run_id, NOW)
    assert DisputeResolution.a7_upheld not in deps.queue.resolutions(task, j)
