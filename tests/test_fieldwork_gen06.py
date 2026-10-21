"""需求 R179: A6 GEN-06 期后事项研判的判断对象与上下文 (功能 going_concern).

状态里的 GEN-07 例外取 A5 在 Xero 演示上执行 GEN-07 的结果 (规则引擎为 XERO_RULES 的预置, 即连库跑一次的 9 条), 其余为
Xero 演示的假存储 (make_xero_deps); GEN-06 的 E01, E02 行由表按规则口径派生 (xero_gen06_rows, 连库一致见
tests/test_rule_sql_gen06.py). 判断引擎为预置结果的 FakeJudgmentEngine (缺省 9 个对象 L3 accept); A7 为真实的
ReviewAgent, 复核模型为脚本 (ScriptedLLM) 或不配置. 渲染出的提示词用真实判断引擎 (llm_call 留痕). Xero 的固定输出归
需求 R180, 真实复核模型与复核 fixture 的验证归 AC-51.
"""

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date
from typing import Any

import pytest

from backend.agents.base import AgentDeps, RunContext
from backend.agents.evidence import EvidenceAgent
from backend.agents.fieldwork import FieldworkAgent
from backend.agents.going_concern import (
    Located,
    MatchKeys,
    documents_by_id,
    reference_tokens,
    related_invoices,
    subsequent_window,
)
from backend.agents.registry import build_agents
from backend.contracts.knowledge import ProcedureTemplate
from backend.contracts.models import (
    AgentId,
    AutonomyLevel,
    CriticVerdict,
    Decision,
    EvidencePointer,
    EvidenceType,
    ExceptionDisposition,
    MisstatementDraft,
    Phase,
    ProcedureRun,
    ProposedAction,
    ReviewStatus,
    RuleOutcome,
    RunStatus,
    TargetType,
    TaskKind,
    TrailKind,
)
from backend.contracts.records import BankTransaction, JournalEntry, SalesInvoice
from backend.contracts.services import JudgmentContext, JudgmentOutcome, RuleResult
from backend.contracts.state import EngagementState, StatePatch
from backend.orchestration.merge import check_patch
from backend.orchestration.nodes import apply_patch
from backend.services.features import BASELINE
from tests.external import needs
from tests.fakes import (
    FakeFinancialStore,
    FakeJudgmentEngine,
    FakeRegistry,
    FakeRuleEngine,
    FakeTrail,
    Outcome,
    ScriptedLLM,
    failed,
    gen06_rows,
    judgment,
    make_ctx,
    make_deps,
    make_run,
)
from tests.test_fieldwork_judging import pending_task
from tests.test_review_agent import step, verdict
from tests.test_xero_fixtures import xero_context
from tests.xero_fakes import (
    GEN07_WINDOW_END,
    ORC1026,
    XERO_RULES,
    make_xero_deps,
    manifest_documents,
    xero_financial_store,
    xero_state,
    xero_traits,
)

FEATURES = frozenset({"critic", "going_concern"})
L3 = AutonomyLevel.L3
# 判断对象 (GEN-07 例外所指的行, 按 ID) 与各自的例外, 依次
SUBJECTS = [
    "BT-090-0092",
    "BT-090-0097",
    "BT-090-0099",
    "BT-090-0107",
    "BT-090-0130",
    "BT-090-0131",
    "XJ-626-2",
    "XJ-676-2",
    "XJ-720-2",
]
FINDINGS = [
    f"X-GEN-07-1-{r}" for r in ("S01-1", "S01-2", "S01-3", "S01-4", "S01-5", "S01-6", "S03-1", "S03-2", "S02-1")
]
EXCEPTION_OF = dict(zip(SUBJECTS, FINDINGS, strict=True))
RIDGEWAY = "SI-e4a0afbd-aea0-450b-ae23-0ce921e84a77"  # ORC01025, Ridgeway University, 10-06 入账
ORC1001 = "SI-f571c38b-5be1-41e1-ad5a-ff6184284beb"  # DIISR 08-05 的发票
T3_MOVED = ("XJ-547", "XJ-548", "XJ-549")  # 注入 T3 改到 10-06 的三笔 (548 生效, 547/549 为作废对)


def subject_pointer(subject_id: str) -> EvidencePointer:
    """指向判断对象那一行的证据指针 (流水 BT-..., 分录行 XJ-...)."""
    bank = subject_id.startswith("BT-")
    return EvidencePointer.model_validate(
        {
            "type": "bank" if bank else "voucher",
            "source_type": "bank_transaction" if bank else "journal_entry",
            "source_id": subject_id,
            "locator": "amount_cents" if bank else "debit_cents",
            "reliability": "external_copy" if bank else "internal_original",
        }
    )


def concluded(**changes: Any) -> Outcome:
    """判断成功: 缺省 L3 accept, 解释对象的全部规则例外, 证据指向对象那一行 (A7 第一轮有证据可给)."""

    def outcome(ctx: JudgmentContext, run: ProcedureRun) -> JudgmentOutcome:
        fields = {"autonomy_level": L3, "evidence": [subject_pointer(ctx.subject_id)], **changes}
        return JudgmentOutcome(judgment=judgment(ctx, run, **fields), failure=None, attempts=1, trail_ids=["TR-1"])

    return outcome


class PresetJudgments:
    """判断引擎: judge 取预置结果 (FakeJudgmentEngine); A7 用到的其余部分 (call_llm, standards, validator) 取真实
    引擎."""

    def __init__(self, preset: FakeJudgmentEngine, real: Any) -> None:
        self.preset = preset
        self.real = real

    async def judge(
        self, eid: str, template: ProcedureTemplate, run: ProcedureRun, ctx: JudgmentContext, performed_by: str
    ) -> JudgmentOutcome:
        return await self.preset.judge(eid, template, run, ctx, performed_by)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.real, name)


def critic_script(verdicts: dict[str, str], missing: tuple[str, ...] = ()) -> ScriptedLLM:
    """A7 的两轮回复: 第一轮的推理链引用对象那一行与 AASB110-P10, 第二轮为 verdicts 给的结论 (缺省 consistent);
    missing 中的对象没有回复 (复核调用出错, 记 not_applicable)."""
    standard = {"type": "standard", "source_type": "standard", "source_id": "AASB110-P10", "locator": "para 10"}
    replies: dict[str, list[str]] = {}
    for subject_id in SUBJECTS:
        if subject_id in missing:
            continue
        independent = {
            "conclusion": f"{subject_id} is a transaction of the period after the reporting date.",
            "reasoning_chain": [
                step("fact", f"{subject_id} is dated after the period end.", [subject_pointer(subject_id)]),
                step("standard", "Non-adjusting events are not adjusted.", [standard]),
                step("inference", "No adjustment is required.", from_steps=[0, 1]),
            ],
        }
        value = verdicts.get(subject_id, "consistent")
        reason = "different_conclusion" if value == "inconsistent" else None
        replies[f"{subject_id}-r1"] = [json.dumps(independent)]
        replies[f"{subject_id}-r2"] = [verdict(value, reason)]
    return ScriptedLLM(replies)


async def gen07_state() -> EngagementState:
    """Xero 项目执行过 GEN-07 (9 条例外并入状态, 程序包含 GEN-07) 之后的状态."""
    deps = make_xero_deps(rules=FakeRuleEngine(XERO_RULES), features=FEATURES)
    ctx = xero_context(deps, xero_state(), "GEN-07", [])
    return apply_patch(ctx.state, await EvidenceAgent(deps).run(ctx))  # type: ignore[arg-type]


def gen06_context(deps: AgentDeps, state: EngagementState, rerun: list[str] | None = None) -> RunContext:
    """GEN-06 的下一次执行: 程序包为状态里的 (GEN-07) 加 GEN-06, rerun 给出时为 rerun_subject_ids."""
    ctx = xero_context(deps, state, "GEN-06", [])
    procedure = ctx.procedure.model_copy(update={"rerun_subject_ids": rerun or []})
    pack = [p for p in state["procedure_pack"] if p.id != "GEN-06"] + [procedure]
    return replace(ctx, procedure=procedure, state=ctx.state | {"procedure_pack": pack})  # type: ignore[arg-type]


def gen06_deps(
    outcomes: dict[str, Outcome] | None = None,
    review: ScriptedLLM | None = None,
    rows: Callable[[ProcedureRun], RuleResult] | None = None,
    financial_store: FakeFinancialStore | None = None,
) -> tuple[AgentDeps, FakeJudgmentEngine, FakeRuleEngine]:
    """功能 going_concern 打开的 Xero 依赖: 规则引擎 GEN-06 返回 rows (缺省 XERO_RULES 的派生行); 判断引擎按 outcomes
    (缺省 9 个对象都 L3 accept); 复核模型为 review, 不给时不配置 (A7 记 skipped)."""
    preset = FakeJudgmentEngine(outcomes if outcomes is not None else {s: concluded() for s in SUBJECTS})
    rules = FakeRuleEngine({"GEN-06": rows if rows is not None else XERO_RULES["GEN-06"]})
    deps = make_xero_deps(rules=rules, features=FEATURES, financial_store=financial_store, review_llm=review)
    deps = replace(deps, judgments=PresetJudgments(preset, deps.judgments), review_llm=review)  # type: ignore[arg-type]
    return deps, preset, rules


async def run_gen06(
    deps: AgentDeps, state: EngagementState | None = None, rerun: list[str] | None = None
) -> tuple[StatePatch, RunContext]:
    """在 GEN-07 之后的状态 (或给定状态) 上执行一次 GEN-06, 经 A6 的合并校验."""
    ctx = gen06_context(deps, state if state is not None else await gen07_state(), rerun)
    patch = await FieldworkAgent(deps).run(ctx)
    check_patch(
        patch,
        writer=AgentId.A6,
        procedure_id="GEN-06",
        run_id=ctx.run.id,
        state=ctx.state,
        outputs=ctx.template.outputs,
    )
    return patch, ctx


def contexts(engine: FakeJudgmentEngine) -> dict[str, JudgmentContext]:
    return {jctx.subject_id: jctx for _, jctx, _ in engine.calls}


def ids(rows: list[dict[str, Any]]) -> list[str]:
    return [str(r["id"]) for r in rows]


def documents(data: dict[str, Any]) -> list[str]:
    """data.documents 的文件, 以补造目录 manifest 的文件名表示."""
    paths = {i: d["path"].rsplit("/", 1)[-1] for i, d in manifest_documents().items()}
    return [paths[d["document"]["id"]] for d in data["documents"]]


def nets(rows: list[dict[str, Any]]) -> list[tuple[str, str, int]]:
    """分录行的 (行 ID, 科目, 借减贷)."""
    return [(r["id"], r["account_code"], r["debit_cents"] - r["credit_cents"]) for r in rows]


# ── 程序定义, 功能开关与参数 ──


def test_gen06_template_and_dispatch() -> None:
    """验收 1, 10: GEN-06.yaml 的各项; 只在功能 going_concern 打开时启用, 与 GEN-07 一起进 P2 固定集, 映射到
    FieldworkAgent; 关闭时 (基线) 不启用, 不装配, 注册表不映射."""
    deps = make_deps(FakeRegistry(), FakeTrail(), features=FEATURES)
    t = deps.library.load("GEN-06")
    assert (t.phase, t.cycle, t.owner, t.engine, t.autonomy_level, t.fixed, t.feature) == (
        Phase.P2_Executing,
        "Pervasive",
        AgentId.A6,
        "hybrid",
        "L3",
        True,
        "going_concern",
    )
    assert (t.triggers, t.depends_on, t.outputs) == ([], ["GEN-07"], ["judgments", "exceptions", "review_tasks"])
    assert t.params == {
        "period_start": {"ref": "period_start"},
        "period_end": {"ref": "period_end"},
        "subsequent_days": 31,
        "data_as_of": {"ref": "dataset.data_as_of"},
        "performance_materiality_cents": {"ref": "performance_materiality_cents"},
        "clearly_trivial_cents": {"ref": "clearly_trivial_cents"},
        "counterparty_prefixes": {"ref": "dataset.counterparty_prefixes"},
        "entity_suffixes": ["pty ltd", "pty limited", "pty", "ltd", "limited"],
        "reference_token_min_length": 4,
        "reversal_reference_pattern": {"ref": "dataset.reversal_reference_pattern"},
        "reversal_entry_no_template": {"ref": "dataset.reversal_entry_no_template"},
    }
    gen07 = deps.library.load("GEN-07").params
    assert {k: t.params[k] for k in ("reversal_reference_pattern", "reversal_entry_no_template")} == {
        k: gen07[k] for k in ("reversal_reference_pattern", "reversal_entry_no_template")
    }
    assert t.standards == [
        "ASA560-P06",
        "ASA560-P08",
        "AASB110-P03",
        "AASB110-P08",
        "AASB110-P09",
        "AASB110-P10",
        "AASB15-P88",
    ]
    assert all(deps.library.fragment(s) is not None for s in t.standards)
    assert t.evidence_required == [EvidenceType.bank, EvidenceType.voucher]
    assert [(r.id, r.emit) for r in t.rules] == [("E01", "rows"), ("E02", "rows")]
    assert t.llm is not None and (t.llm.prompt, t.llm.output_model) == ("GEN-06.md.j2", "JudgmentDraft")
    assert "GEN-06" in FieldworkAgent.procedures and isinstance(build_agents(deps)["GEN-06"], FieldworkAgent)
    assert {"GEN-06", "GEN-07"} <= {x.id for x in deps.library.fixed()}
    baseline = make_deps(FakeRegistry(), FakeTrail())
    assert not baseline.library.enabled("GEN-06") and "GEN-06" not in build_agents(baseline)
    assert {"GEN-06", "GEN-07"}.isdisjoint(x.id for x in baseline.library.fixed())
    assert not make_deps(FakeRegistry(), FakeTrail(), features=BASELINE).library.enabled("GEN-06")


@pytest.mark.external
def test_xero_params_snapshot() -> None:
    """验收 1: Xero 项目 (执行重要性 16216, 明显微小 1622) 第 1 次执行 RUN-GEN-06-1 的参数快照, 按项目期间, 状态的
    重要性与数据集声明解析."""
    deps = make_xero_deps(features=FEATURES)
    ctx = xero_context(deps, xero_state(), "GEN-06", [])
    traits = xero_traits()
    assert (ctx.run.id, ctx.run.params_snapshot) == (
        "RUN-GEN-06-1",
        {
            "period_start": date(2026, 7, 1),
            "period_end": date(2026, 9, 30),
            "subsequent_days": 31,
            "data_as_of": date(2026, 10, 17),
            "performance_materiality_cents": 16216,
            "clearly_trivial_cents": 1622,
            "counterparty_prefixes": traits.counterparty_prefixes,
            "entity_suffixes": ["pty ltd", "pty limited", "pty", "ltd", "limited"],
            "reference_token_min_length": 4,
            "reversal_reference_pattern": traits.reversal_reference_pattern,
            "reversal_entry_no_template": traits.reversal_entry_no_template,
        },
    )


# ── 匹配规则 (纯函数) ──


@pytest.mark.parametrize(
    ("text", "tokens"),
    [
        ("70135 70209", ["70135", "70209"]),
        ("ORC01025", ["orc01025"]),
        ("Sublet RM2031", ["rm2031"]),
        ("MKT-08123", ["08123"]),  # MKT 不含数字
        ("1026C", ["1026c"]),
        ("DEPOSIT ADV", []),  # 不含数字
        ("INV 123", []),  # 123 短于 4
        ("AB12 X1", ["ab12"]),  # 恰为 4 位的算
        ("SM0195-70135 sm0195", ["sm0195", "70135"]),  # 不区分大小写, 去重
        (None, []),
        ("", []),
    ],
)
def test_reference_tokens(text: str | None, tokens: list[str]) -> None:
    """验收 4: 按非字母数字切分, 长度不小于 4 且含数字的词 (小写), 去重后按出现顺序."""
    assert reference_tokens(text, 4) == tokens


@pytest.mark.parametrize(
    ("line", "keys", "hit"),
    [
        ({"source_doc": "SM0195-70135", "description": "SMART Agency"}, MatchKeys(["70135"], None), True),
        ({"source_doc": "SM0195-701350", "description": "x"}, MatchKeys(["70135"], None), False),  # 整词才算
        ({"source_doc": "x", "description": "Invoice 70135"}, MatchKeys(["70135"], None), True),  # description 也看
        ({"source_doc": None, "description": "Payment: SMART Agency"}, MatchKeys([], "smart agency"), True),
        ({"source_doc": "SMART Agency Pty Ltd", "description": None}, MatchKeys([], "smart agency"), True),
        ({"source_doc": "x", "description": "SMART Agencies"}, MatchKeys([], "smart agency"), False),
        ({"source_doc": "x", "description": "The SMART Agency group"}, MatchKeys([], "smart agency"), True),
        ({"source_doc": "ORC01025", "description": "Ridgeway University"}, MatchKeys([], None), False),
    ],
)
def test_match_keys_hits(line: dict[str, Any], keys: MatchKeys, hit: bool) -> None:
    """验收 4: token 与 source_doc, description 切分后的词整词相等即命中; 名称在两列的规范化文本 (去说明前缀) 中整词
    出现即命中; 没有 token 也没有名称时不命中."""
    assert keys.hits(line, ["payment to", "payment from", "payment:"]) is hit


def test_subsequent_window() -> None:
    """data.window: 期末次日至 min(期末 + 天数, 数据截止日); 截止日为空不截; 参数快照读回的字符串日期同样可用."""
    params = {"period_end": date(2026, 9, 30), "subsequent_days": 31, "data_as_of": date(2026, 10, 17)}
    assert subsequent_window(params) == (date(2026, 10, 1), date(2026, 10, 17))
    assert subsequent_window(params | {"data_as_of": None}) == (date(2026, 10, 1), date(2026, 10, 31))
    assert subsequent_window({"period_end": "2026-09-30", "subsequent_days": 31, "data_as_of": "2026-11-30"}) == (
        date(2026, 10, 1),
        date(2026, 10, 31),
    )


def invoice(no: str, customer: str, posted: date, line: str | None = None) -> SalesInvoice:
    return SalesInvoice(
        id=f"SI-{no}",
        invoice_no=no,
        customer=customer,
        customer_abn=None,
        contract_id=None,
        invoice_date=posted,
        posting_date=posted,
        amount_cents=100,
        journal_entry_id=line,
    )


def entry_line(line_id: str, description: str) -> JournalEntry:
    return JournalEntry(
        id=line_id,
        entry_no=line_id.rsplit("-", 1)[0],
        posting_date=date(2026, 10, 16),
        created_at=None,
        created_by=None,
        approved_by=None,
        account_code="610",
        description=description,
        source_doc=None,
    )


PARAMS = {
    "period_end": date(2026, 9, 30),
    "counterparty_prefixes": ["payment to", "payment from", "payment:"],
    "entity_suffixes": ["pty ltd", "pty limited", "pty", "ltd", "limited"],
}
INVOICES = [  # E02 的顺序 (入账日, ID)
    invoice("A1", "Acme Pty Ltd", date(2026, 8, 5)),
    invoice("B1", "Bilby Services", date(2026, 9, 17)),
    invoice("A2", "ACME", date(2026, 10, 1), line="XJ-9-1"),  # 期后, 挂在 XJ-9 上
    invoice("A3", "Acme Limited", date(2026, 10, 14)),  # 期后, 没挂在相关凭证上
]


def test_related_invoices_of_a_bank_subject() -> None:
    """验收 4: 流水对象的相关发票为 journal_entry_id 落在相关凭证行中的发票 (期后的也算), 加规范化客户名 (去前缀与公司
    后缀) 等于对方名称的本期发票 (入账日不晚于期末); 期后而没挂在相关凭证上的不算. 顺序同 E02."""
    found = Located(
        BankTransaction(
            id="BT-1",
            bank_account_id="BA-1",
            txn_date=date(2026, 10, 15),
            amount_cents=100,
            balance_after_cents=0,
            description="Payment from ACME PTY LTD",
            reference=None,
        ),
        [],
        MatchKeys([], "acme"),
        ["XJ-9"],
    )
    chosen = related_invoices(found, [entry_line("XJ-9-1", "Acme")], INVOICES, PARAMS)
    assert [i.id for i in chosen] == ["SI-A1", "SI-A2"]
    assert [i.id for i in related_invoices(found, [], INVOICES, PARAMS)] == ["SI-A1"]  # 没有相关凭证: 只按名称
    nameless = replace(found, keys=MatchKeys([], None))
    assert [i.id for i in related_invoices(nameless, [entry_line("XJ-9-1", "x")], INVOICES, PARAMS)] == ["SI-A2"]


def test_related_invoices_of_a_journal_subject() -> None:
    """验收 4: 分录对象的相关发票为规范化客户名在本凭证行 description 或 source_doc 中整词出现的本期发票; 期后的不算,
    名称只是词的一部分的不算."""
    voucher = [entry_line("XJ-1-1", "Acme - credit note"), entry_line("XJ-1-2", "Bilby Servicesx")]
    found = Located(voucher[0], voucher, MatchKeys([], None), [])
    assert [i.id for i in related_invoices(found, [], INVOICES, PARAMS)] == ["SI-A1"]


async def test_documents_are_looked_up_by_every_id_and_merged() -> None:
    """验收 3: documents 对每个关联对象 ID 逐个取 (同一 ID 只取一次), 按文件 ID 去重并排序."""
    calls: list[str] = []
    registered = {"BT-1": ["DOC-b", "DOC-a"], "XJ-9": ["DOC-a"], "SI-A2": ["DOC-c"]}

    async def documents_of(eid: str, related_id: str) -> list[dict[str, Any]]:
        calls.append(related_id)
        return [{"document": {"id": d}, "pages": []} for d in registered.get(related_id, [])]

    found = await documents_by_id("ENG-1", ["BT-1", "XJ-9", "XJ-9", "SI-A2"], documents_of)
    assert ([d["document"]["id"] for d in found], calls) == (["DOC-a", "DOC-b", "DOC-c"], ["BT-1", "XJ-9", "SI-A2"])


# ── Xero 演示: 对象, 上下文与去向 ──


@dataclass(frozen=True)
class Flow:
    patch: StatePatch  # GEN-06 的状态片段
    ctx: RunContext  # GEN-06 这次执行的上下文
    contexts: dict[str, JudgmentContext]  # 送给判断引擎的上下文, 按对象
    rules: FakeRuleEngine


@pytest.fixture(scope="module")
def flow() -> Flow:
    """9 个对象都 L3 accept, 复核模型未配置 (A7 skipped) 的一次 GEN-06."""
    deps, engine, rules = gen06_deps()
    patch, ctx = asyncio.run(run_gen06(deps))
    return Flow(patch, ctx, contexts(engine), rules)


@pytest.mark.external
def test_xero_subjects_findings_and_task(flow: Flow) -> None:
    """验收 2: 对象为 9 条 GEN-07 例外所指的行 (按 ID), fixture_key 同对象, 规则例外为各自的例外 (当前版本 v1); 任务说明
    为 RUN-GEN-06-1 的参数快照 (JSON 形式, 不另加窗口) 与空风险 (固定集); 规则引擎收到的就是参数快照, 调一次."""
    assert list(flow.contexts) == SUBJECTS
    state = {x.id: x for x in flow.ctx.state["exceptions"]}
    for subject_id, jctx in flow.contexts.items():
        assert jctx.fixture_key == subject_id and jctx.rule_findings == [state[EXCEPTION_OF[subject_id]]]
        assert jctx.task == {"params": flow.ctx.run.model_dump(mode="json")["params_snapshot"], "risks": []}
        assert jctx.autonomy_level is L3
    assert flow.contexts["XJ-720-2"].task["params"]["data_as_of"] == "2026-10-17"
    assert "window" not in flow.contexts["XJ-720-2"].task["params"]
    assert flow.rules.calls == [("GEN-06", "RUN-GEN-06-1", flow.ctx.run.params_snapshot)]


@pytest.mark.external
def test_xero_bank_subject_contexts(flow: Flow) -> None:
    """验收 3, 5: 六个流水对象的上下文. 参考号与名称找到的总账凭证 (整笔), 相关发票, 文件 (按 index.csv 的关联对象)
    与涉及科目的本期余额; 窗口 10-01 至 10-17; 流水对象没有本凭证行."""
    data = {s: flow.contexts[s].data for s in SUBJECTS[:6]}
    assert {s: d["subject"]["source_type"] for s, d in data.items()} == dict.fromkeys(data, "bank_transaction")
    assert {s: (d["subject"]["id"], d["voucher_lines"]) for s, d in data.items()} == {s: (s, []) for s in data}
    assert {s: d["window"] for s, d in data.items()} == {
        s: {"from": "2026-10-01", "to": GEN07_WINDOW_END} for s in data
    }

    ridgeway = data["BT-090-0092"]
    assert ridgeway["match_keys"] == {"tokens": ["orc01025"], "name": "ridgeway university"}
    assert nets(ridgeway["related_entries"]) == [
        ("XJ-647-1", "610", 618750),
        ("XJ-647-2", "200", -562500),
        ("XJ-647-3", "820", -56250),
    ]
    assert {(r["posting_date"], r["source_doc"]) for r in ridgeway["related_entries"]} == {("2026-10-06", "ORC01025")}
    [invoice] = ridgeway["related_invoices"]
    assert (invoice["invoice"]["source_type"], invoice["invoice"]["id"], invoice["invoice"]["invoice_no"]) == (
        "sales_invoice",
        RIDGEWAY,
        "ORC01025",
    )
    assert (invoice["invoice"]["amount_cents"], invoice["delivery_notes"]) == (618750, [])
    assert documents(ridgeway) == ["ridgeway_order_CRM08-12.pdf"]
    assert [r["account_code"] for r in ridgeway["trial_balance"]] == ["090", "200", "610", "820"]

    smart = data["BT-090-0097"]
    assert smart["match_keys"] == {"tokens": ["70135", "70209"], "name": "smart agency"}
    assert ids(smart["related_entries"]) == [f"XJ-{no}-{n}" for no in (548, 641) for n in (1, 2, 3)]
    assert [(r["entry_no"], r["posting_date"], r["source_doc"]) for r in smart["related_entries"][::3]] == [
        ("XJ-548", "2026-09-05", "SM0195-70135"),
        ("XJ-641", "2026-10-06", "SM0210-70209"),
    ]
    assert ("XJ-548-3", "800", -200000) in nets(smart["related_entries"])
    assert ("XJ-641-3", "800", -250000) in nets(smart["related_entries"])
    assert smart["related_invoices"] == []
    assert documents(smart) == ["smart_agency_invoice_SM0195-70135.pdf"]
    assert [r["account_code"] for r in smart["trial_balance"]] == ["090", "400", "800", "820"]

    expected = {
        "BT-090-0099": ([], "jakaranda maple systems", ["jakaranda_quote.pdf"]),  # DEPOSIT, ADV 不含数字
        "BT-090-0107": ([], "e bank dep", ["director_support_letter.pdf"]),
        "BT-090-0130": ([], "e bank dep", ["director_support_letter.pdf"]),
        "BT-090-0131": (["rm2031"], "majestic contracting", ["rm2031_sublease.pdf"]),
    }
    for subject_id, (tokens, name, files) in expected.items():
        d = data[subject_id]
        assert d["match_keys"] == {"tokens": tokens, "name": name}, subject_id
        assert (d["related_entries"], d["related_invoices"], documents(d)) == ([], [], files), subject_id
        assert [r["account_code"] for r in d["trial_balance"]] == ["090"], subject_id
    rows = {s: d["trial_balance"] for s, d in data.items()}
    bank = rows["BT-090-0099"][0]
    assert {k: bank[k] for k in ("source_type", "account_name", "balance_cents")} == {
        "source_type": "trial_balance",
        "account_name": "Business Bank Account",
        "balance_cents": -1090044,
    }


@pytest.mark.external
def test_xero_journal_subject_contexts(flow: Flow) -> None:
    """验收 3, 5: 三个分录对象带本凭证全部行, 按 source_doc 拆出的 token 找总账 (不按名称), 都没有别的凭证命中;
    XJ-720-2 的相关发票为凭证行写的 DIISR 本期 (不晚于期末) 的两张, ORC1026 带 09-17 签收的服务完成记录; 10 月的
    ORC1033, ORC1041 不算. 科目余额中 720 期末没有余额 (null)."""
    data = {s: flow.contexts[s].data for s in SUBJECTS[6:]}
    assert {s: d["subject"]["source_type"] for s, d in data.items()} == dict.fromkeys(data, "journal_entry")
    assert nets(data["XJ-626-2"]["voucher_lines"]) == [
        ("XJ-626-1", "800", -693000),
        ("XJ-626-2", "710", 630000),
        ("XJ-626-3", "820", 63000),
    ]
    assert nets(data["XJ-676-2"]["voucher_lines"]) == [
        ("XJ-676-1", "800", -216699),
        ("XJ-676-2", "720", 196999),
        ("XJ-676-3", "820", 19700),
    ]
    assert nets(data["XJ-720-2"]["voucher_lines"]) == [
        ("XJ-720-1", "200", 105000),
        ("XJ-720-2", "610", -115500),
        ("XJ-720-3", "820", 10500),
    ]
    assert {s: d["match_keys"] for s, d in data.items()} == {
        "XJ-626-2": {"tokens": ["08123"], "name": None},
        "XJ-676-2": {"tokens": [], "name": None},
        "XJ-720-2": {"tokens": ["1026c"], "name": None},
    }
    assert {s: d["related_entries"] for s, d in data.items()} == {s: [] for s in data}
    assert {s: documents(d) for s, d in data.items()} == {
        "XJ-626-2": ["fixed_asset_register.pdf"],
        "XJ-676-2": ["fixed_asset_register.pdf"],
        "XJ-720-2": ["diisr_email.pdf"],
    }
    assert data["XJ-626-2"]["related_invoices"] == data["XJ-676-2"]["related_invoices"] == []
    diisr = data["XJ-720-2"]["related_invoices"]
    assert [(i["invoice"]["id"], i["invoice"]["invoice_no"], i["invoice"]["amount_cents"]) for i in diisr] == [
        (ORC1001, "ORC1001", 149600),
        (ORC1026, "ORC1026", 715000),
    ]
    assert [[(n["id"], n["signed_date"]) for n in i["delivery_notes"]] for i in diisr][1] == [
        ("SR-ORC1026", "2026-09-17")
    ]
    assert {i["invoice"]["posting_date"] <= "2026-09-30" for i in diisr} == {True}
    assert [r["account_code"] for r in data["XJ-720-2"]["trial_balance"]] == ["200", "610", "820"]
    computer = {r["account_code"]: r for r in data["XJ-676-2"]["trial_balance"]}["720"]
    assert (computer["account_name"], computer["balance_cents"]) == ("Computer Equipment", None)
    pages = data["XJ-626-2"]["documents"][0]["pages"]
    assert pages and all(isinstance(p, str) for p in pages)  # 带各页文字


@needs("q1_snapshot", "q1_supplements")
async def test_documents_of_related_entries_and_invoices() -> None:
    """验收 3: 文件也按相关凭证号与相关发票 ID 取: 合成两份登记, 一份只关联 XJ-641 (BT-090-0097 的相关凭证), 一份只
    关联 ORC1026 的发票 (XJ-720-2 的相关发票), 分别进这两个对象的 documents (按文件 ID 排序), 不进别的对象."""
    store = xero_financial_store()
    base = store.document_refs[0]
    extra = {"DOC-zz-entry": ["XJ-641"], "DOC-zz-invoice": [ORC1026]}
    store.document_refs += [base.model_copy(update={"id": i, "related_ids": r}) for i, r in extra.items()]
    deps, engine, _ = gen06_deps(financial_store=store)
    await FieldworkAgent(deps).run(gen06_context(deps, await gen07_state()))
    found = {s: [d["document"]["id"] for d in c.data["documents"]] for s, c in contexts(engine).items()}
    assert found["BT-090-0097"][-1] == "DOC-zz-entry" and found["XJ-720-2"][-1] == "DOC-zz-invoice"
    assert [s for s, ids in found.items() if set(ids) & set(extra)] == ["BT-090-0097", "XJ-720-2"]
    assert all(ids == sorted(ids) for ids in found.values())


@pytest.mark.external
def test_skipped_critic_confirms_and_attributes(flow: Flow) -> None:
    """验收 7: 复核模型未配置 (A7 skipped), L3 accept 不建任务: 9 条判断 J-GEN-06-1-{对象} v1 confirmed, 各解释自己的
    GEN-07 例外; 9 条例外 v2 attributed, attributed_by 为对应判断; 执行成功."""
    judgments = {j.subject_id: j for j in flow.patch["judgments"]}
    assert list(judgments) == SUBJECTS
    for subject_id, j in judgments.items():
        assert (j.id, j.version, j.review_status) == (f"J-GEN-06-1-{subject_id}", 1, ReviewStatus.confirmed)
        assert (j.exception_ids, j.critic_verdict) == ([EXCEPTION_OF[subject_id]], CriticVerdict.skipped)
    assert [(x.id, x.version, x.disposition, x.attributed_by) for x in flow.patch["exceptions"]] == [
        (EXCEPTION_OF[s], 2, ExceptionDisposition.attributed, f"J-GEN-06-1-{s}") for s in SUBJECTS
    ]
    assert flow.patch["review_tasks"] == []
    [run] = flow.patch["procedure_runs"]
    assert (run.id, run.status) == ("RUN-GEN-06-1", RunStatus.succeeded)


@needs("q1_snapshot", "q1_supplements")
@pytest.mark.parametrize("inconsistent", [(), ("XJ-720-2",)])
async def test_scripted_critic_decides_the_route(inconsistent: tuple[str, ...]) -> None:
    """验收 7: 脚本复核对 9 条都判 consistent 时同 skipped (不建任务, 例外归因); 对 XJ-720-2 判 inconsistent 时建
    T-J-GEN-06-1-XJ-720-2-v1 (dispute), 该判断 pending, X-GEN-07-1-S02-1 不出新版本, 其余 8 条照常."""
    review = critic_script(dict.fromkeys(inconsistent, "inconsistent"))
    deps, _, _ = gen06_deps(review=review)
    patch, _ = await run_gen06(deps)
    judgments = {j.subject_id: j for j in patch["judgments"]}
    disputed = set(inconsistent)
    assert {s: j.critic_verdict for s, j in judgments.items()} == {
        s: CriticVerdict.inconsistent if s in disputed else CriticVerdict.consistent for s in SUBJECTS
    }
    assert {s: j.review_status for s, j in judgments.items()} == {
        s: ReviewStatus.pending if s in disputed else ReviewStatus.confirmed for s in SUBJECTS
    }
    assert [x.id for x in patch["exceptions"]] == [EXCEPTION_OF[s] for s in SUBJECTS if s not in disputed]
    assert [(t.id, t.kind, t.target_type, t.target_version) for t in patch["review_tasks"]] == [
        (f"T-J-GEN-06-1-{s}-v1", TaskKind.dispute, TargetType.Judgment, 1) for s in inconsistent
    ]
    assert len(review.requests) == 2 * len(SUBJECTS)  # 每个对象两轮


@needs("q1_snapshot", "q1_supplements")
async def test_critic_error_gets_a_normal_task() -> None:
    """验收 7: XJ-720-2 的复核调用出错 (脚本没有它的回复) 记 not_applicable, 建 normal 任务; 其余 8 条 consistent 照常
    自动关闭."""
    deps, _, _ = gen06_deps(review=critic_script({}, missing=("XJ-720-2",)))
    patch, _ = await run_gen06(deps)
    j = next(j for j in patch["judgments"] if j.subject_id == "XJ-720-2")
    assert (j.critic_verdict, j.review_status) == (CriticVerdict.not_applicable, ReviewStatus.pending)
    assert [(t.id, t.kind) for t in patch["review_tasks"]] == [("T-J-GEN-06-1-XJ-720-2-v1", TaskKind.normal)]
    assert len(patch["exceptions"]) == 8 and EXCEPTION_OF["XJ-720-2"] not in {x.id for x in patch["exceptions"]}


@needs("q1_snapshot", "q1_supplements")
async def test_misstatement_gets_a_normal_task() -> None:
    """验收 7: XJ-720-2 改为建议错报: 判断 pending, 建 T-J-GEN-06-1-XJ-720-2-v1 (normal, 目标为判断 v1, run_id
    RUN-GEN-06-1), X-GEN-07-1-S02-1 不变; 其余 8 条照常."""
    draft = MisstatementDraft(
        type="factual", account="Sales (200)", assertion="Occurrence", amount_cents=105000, direction="overstated"
    )
    outcomes = {s: concluded() for s in SUBJECTS} | {
        "XJ-720-2": concluded(proposed_action=ProposedAction.misstatement, misstatement_draft=draft)
    }
    deps, _, _ = gen06_deps(outcomes)
    patch, _ = await run_gen06(deps)
    j = next(j for j in patch["judgments"] if j.subject_id == "XJ-720-2")
    assert (j.id, j.review_status, j.proposed_action) == (
        "J-GEN-06-1-XJ-720-2",
        ReviewStatus.pending,
        ProposedAction.misstatement,
    )
    [task] = patch["review_tasks"]
    assert (task.id, task.kind, task.target_type, task.target_id, task.target_version, task.run_id) == (
        "T-J-GEN-06-1-XJ-720-2-v1",
        TaskKind.normal,
        TargetType.Judgment,
        "J-GEN-06-1-XJ-720-2",
        1,
        "RUN-GEN-06-1",
    )
    assert [x.id for x in patch["exceptions"]] == [EXCEPTION_OF[s] for s in SUBJECTS if s != "XJ-720-2"]


@needs("q1_snapshot", "q1_supplements")
async def test_failed_subject_marks_its_exception_unattributed() -> None:
    """验收 7: BT-090-0131 判断失败 (hybrid 为 unattributed): 其例外 X-GEN-07-1-S01-6 v2 unattributed, 建
    T-X-GEN-07-1-S01-6-v2 (subject_id 为该对象); 允许的决定为 override, misstatement 与 rerun (rerun 由需求 R347 加,
    晚于本条验收的写法); 其余 8 条照常, 执行成功."""
    outcomes = {s: concluded() for s in SUBJECTS} | {"BT-090-0131": failed("unattributed")}
    deps, _, _ = gen06_deps(outcomes)
    patch, _ = await run_gen06(deps)
    assert [j.subject_id for j in patch["judgments"]] == [s for s in SUBJECTS if s != "BT-090-0131"]
    marked = {x.id: x for x in patch["exceptions"]}["X-GEN-07-1-S01-6"]
    assert (marked.version, marked.disposition) == (2, ExceptionDisposition.unattributed)
    [task] = patch["review_tasks"]
    assert (task.id, task.kind, task.subject_id, task.target_id, task.target_version) == (
        "T-X-GEN-07-1-S01-6-v2",
        TaskKind.unattributed,
        "BT-090-0131",
        "X-GEN-07-1-S01-6",
        2,
    )
    assert deps.queue.allowed(task) == {Decision.override, Decision.misstatement, Decision.rerun}
    assert patch["procedure_runs"][0].status is RunStatus.succeeded


@needs("q1_snapshot", "q1_supplements")
async def test_only_open_findings_in_scope_are_subjects() -> None:
    """验收 2: 只取未归因的 GEN-07 例外 (已归因的不算; 上一轮失败置 unattributed 的 v2 照算, 规则例外为当前版本), 再按
    in_scope 筛选: rerun_subject_ids 非空时只判其中的, 已交人处理的对象 (本程序带 subject_id 的待决任务) 不再重判."""
    state = await gen07_state()
    current = {x.id: x for x in state["exceptions"]}
    done = current["X-GEN-07-1-S01-1"].model_copy(
        update={"version": 2, "disposition": ExceptionDisposition.attributed, "attributed_by": "J-X"}
    )
    reopened = current["X-GEN-07-1-S02-1"].model_copy(
        update={"version": 2, "disposition": ExceptionDisposition.unattributed}
    )
    waiting = pending_task(
        id="T-X-GEN-07-1-S01-6-v2",
        procedure_id="GEN-06",
        target_id="X-GEN-07-1-S01-6",
        run_id="RUN-GEN-07-1",
        subject_id="BT-090-0131",
    )
    newer = {x.id: x for x in (done, reopened)}
    exceptions = [newer.get(x.id, x) for x in state["exceptions"]]  # 同 upsert_by_id: 新版本取代旧版本
    state = state | {"exceptions": exceptions, "review_tasks": [waiting]}  # type: ignore[assignment]

    deps, engine, _ = gen06_deps()
    await FieldworkAgent(deps).run(gen06_context(deps, state))
    found = contexts(engine)
    assert list(found) == [s for s in SUBJECTS if s not in ("BT-090-0092", "BT-090-0131")]
    assert found["XJ-720-2"].rule_findings == [reopened]

    deps, engine, _ = gen06_deps()
    await FieldworkAgent(deps).run(gen06_context(deps, state, rerun=["XJ-720-2", "BT-090-0131"]))
    assert list(contexts(engine)) == ["XJ-720-2"]

    deps, engine, rules = gen06_deps()  # 要重判的对象都不在范围内: 不取 E01, E02
    await FieldworkAgent(deps).run(gen06_context(deps, state, rerun=["BT-090-0131"]))
    assert (engine.calls, rules.calls) == ([], [])


@needs("q1_snapshot", "q1_supplements")
async def test_findings_are_grouped_by_row_of_the_prior_program() -> None:
    """验收 2: 只取前置程序 (GEN-07) 的例外: 别的程序指向同一行的未归因例外不进规则例外, 指向别的行的不成对象; 同一行
    上的几条 GEN-07 例外合为一个对象 (按 ID); 行类型取 source_id 为该行的主证据指针 (前面另有别的指针也不受影响)."""
    state = await gen07_state()
    current = {x.id: x for x in state["exceptions"]}
    s02 = current["X-GEN-07-1-S02-1"]
    extra = EvidencePointer.model_validate(
        {"type": "standard", "source_type": "standard", "source_id": "ASA560-P06", "locator": "para 6"}
    )
    first = s02.model_copy(update={"evidence": [extra, *s02.evidence, extra]})  # 主指针前后都另有指针
    second = s02.model_copy(update={"id": "X-GEN-07-1-S02-9"})
    other = s02.model_copy(update={"id": "X-GEN-05-1-R09-9", "procedure_id": "GEN-05", "run_id": "RUN-GEN-05-1"})
    stray = other.model_copy(update={"id": "X-GEN-05-1-R09-8", "source_ids": ["XJ-999-1"]})
    exceptions = [first if x.id == s02.id else x for x in state["exceptions"]]
    state = state | {"exceptions": [*exceptions, second, other, stray]}  # type: ignore[assignment]
    deps, engine, _ = gen06_deps()
    await FieldworkAgent(deps).run(gen06_context(deps, state))
    found = contexts(engine)
    assert list(found) == SUBJECTS
    assert [x.id for x in found["XJ-720-2"].rule_findings] == ["X-GEN-07-1-S02-1", "X-GEN-07-1-S02-9"]
    assert found["XJ-720-2"].data["subject"]["source_type"] == "journal_entry"


@needs("q1_snapshot", "q1_supplements")
async def test_rules_not_executed_reach_the_run() -> None:
    """取对象时规则引擎没有执行的规则 (Subjects.outcomes, 需求 R130) 记进执行记录的终态; 一条行规则没执行时按没有行
    处理 (E02 没执行: 相关发票为空)."""
    skipped = RuleOutcome(rule_id="E02", status="skipped", reason="no invoices")
    e01 = XERO_RULES["GEN-06"](make_run("GEN-06")).rows["E01"]
    deps, engine, _ = gen06_deps(rows=lambda run: RuleResult(exceptions=[], rows={"E01": e01}, outcomes=[skipped]))
    patch, _ = await run_gen06(deps)
    assert patch["procedure_runs"][0].rule_outcomes == [skipped]
    assert {s: c.data["related_invoices"] for s, c in contexts(engine).items()} == {s: [] for s in SUBJECTS}


async def test_no_open_findings_is_a_succeeded_run_without_judgments() -> None:
    """验收 7: 没有 GEN-07 未归因例外时判断为空, 执行成功, 不调规则引擎与判断引擎."""
    rules, engine = FakeRuleEngine({}), FakeJudgmentEngine({})
    deps = replace(make_deps(FakeRegistry(), FakeTrail(), rules=rules, features=FEATURES), judgments=engine)  # type: ignore[arg-type]
    patch = await FieldworkAgent(deps).run(make_ctx(deps, "GEN-06"))
    [run] = patch["procedure_runs"]
    assert (patch["judgments"], patch["exceptions"], patch["review_tasks"]) == ([], [], [])
    assert (run.status, rules.calls, engine.calls) == (RunStatus.succeeded, [], [])


# ── 能区分对错 (T3 的前提) ──


def store_lines(store: FakeFinancialStore) -> tuple[list[JournalEntry], list[SalesInvoice]]:
    """假财务存储里的分录行与销售发票."""
    lines = [JournalEntry.model_validate(r) for (kind, _), r in store.records.items() if kind == "journal_entry"]
    invoices = [SalesInvoice.model_validate(r) for (kind, _), r in store.records.items() if kind == "sales_invoice"]
    return lines, invoices


async def smart_agency_entries(change: Callable[[FakeFinancialStore], None]) -> list[dict[str, Any]]:
    """改过假存储之后 BT-090-0097 的 related_entries: E01, E02 由改后的表按规则口径派生 (同 xero_gen06_rows)."""
    store = xero_financial_store()
    change(store)
    lines, invoices = store_lines(store)
    traits = xero_traits()
    rows = gen06_rows(
        lines,
        invoices,
        date(2026, 7, 1),
        date.fromisoformat(GEN07_WINDOW_END),
        traits.reversal_reference_pattern,
        traits.reversal_entry_no_template,
    )
    deps, engine, _ = gen06_deps(
        {"BT-090-0097": concluded()}, rows=lambda run: RuleResult(exceptions=[], rows=rows), financial_store=store
    )
    await FieldworkAgent(deps).run(gen06_context(deps, await gen07_state(), rerun=["BT-090-0097"]))
    return list(contexts(engine)["BT-090-0097"].data["related_entries"])


def updated(
    store: FakeFinancialStore, source_type: str, match: Callable[[dict[str, Any]], bool], **fields: Any
) -> None:
    """把假存储里满足 match 的 source_type 行改掉 fields."""
    for (kind, key), row in list(store.records.items()):
        if kind == source_type and match(row):
            store.records[(kind, key)] = row | fields


@needs("q1_snapshot", "q1_supplements")
async def test_t3_moved_entries_reach_the_context() -> None:
    """验收 6: 按 T3 把 XJ-547, 548, 549 的入账日改为 10-06 后, BT-090-0097 的 related_entries 中 XJ-548 各行为 10-06
    (基线 09-05, 见上), XJ-547/549 作为作废对仍被剔除, XJ-641 不变."""
    moved = await smart_agency_entries(
        lambda s: updated(s, "journal_entry", lambda r: r["entry_no"] in T3_MOVED, posting_date=date(2026, 10, 6))
    )
    assert [(r["id"], r["posting_date"]) for r in moved] == [
        *((f"XJ-548-{n}", "2026-10-06") for n in (1, 2, 3)),
        *((f"XJ-641-{n}", "2026-10-06") for n in (1, 2, 3)),
    ]


@needs("q1_snapshot", "q1_supplements")
@pytest.mark.parametrize(
    ("fields", "entries"),
    [
        ({"reference": None}, ["XJ-548", "XJ-641"]),  # 没有参考号: 按名称 "smart agency" 照样取到
        ({"description": "SMART Agency Pty Ltd"}, ["XJ-548", "XJ-641"]),  # 去公司后缀后同名
        ({"reference": None, "description": "SMART Agency Pty Ltd"}, ["XJ-548", "XJ-641"]),  # 只靠去后缀的名称
        ({"description": "Unknown payee"}, ["XJ-548", "XJ-641"]),  # 名称不中, 参考号 70135, 70209 照样取到
        ({"reference": None, "description": "Unknown payee"}, []),  # 两样都没有就取不到
        ({"reference": "70135"}, ["XJ-548", "XJ-641"]),  # 只剩一个参考号, XJ-641 由名称取到
        ({"reference": "70135", "description": "Unknown payee"}, ["XJ-548"]),
    ],
)
async def test_reference_and_name_each_find_the_entries(fields: dict[str, Any], entries: list[str]) -> None:
    """验收 6: 参考号与名称各自都能找到凭证: 把 BT-090-0097 的 reference 改为空时按名称 "smart agency" 仍取到 XJ-548 与
    XJ-641; payee 写成 "SMART Agency Pty Ltd" 与 "SMART Agency" 取到同一组凭证; 两样都对不上时为空."""
    found = await smart_agency_entries(
        lambda s: updated(s, "bank_transaction", lambda r: r["id"] == "BT-090-0097", **fields)
    )
    assert sorted({r["entry_no"] for r in found}) == entries


# ── 渲染出的提示词 ──


@needs("q1_snapshot", "q1_supplements")
async def test_rendered_prompt_has_standards_window_and_only_its_own_rows() -> None:
    """验收 8: 真实判断引擎渲染出的提示词 (llm_call 留痕的第 1 次) 含 ASA560-P08, AASB110-P03, AASB110-P10 原文与窗口
    截止日 2026-10-17, 不含另一判断对象的行 (其 ID 不出现); 本对象与它的例外在内."""
    trail = FakeTrail()
    deps = make_xero_deps(
        rules=FakeRuleEngine({"GEN-06": XERO_RULES["GEN-06"]}),
        features=FEATURES,
        trail=trail,
        llm=ScriptedLLM({s: ["{}"] * 3 for s in SUBJECTS}),
    )
    await FieldworkAgent(deps).run(gen06_context(deps, await gen07_state()))
    calls = [e.payload for e in trail.entries if e.kind is TrailKind.llm_call and e.payload["attempt"] == 1]
    prompts = {c["fixture_key"]: c["messages"][0]["content"] for c in calls}
    assert sorted(prompts) == SUBJECTS
    texts = [deps.library.fragment(f) for f in ("ASA560-P08", "AASB110-P03", "AASB110-P10")]
    for subject_id, prompt in prompts.items():
        assert all(f is not None and f.text.strip() in prompt for f in texts), subject_id
        assert GEN07_WINDOW_END in prompt and subject_id in prompt and EXCEPTION_OF[subject_id] in prompt
        others = [s for s in SUBJECTS if s != subject_id]
        assert [s for s in others if s in prompt] == [], subject_id
        assert [x for x in FINDINGS if x != EXCEPTION_OF[subject_id] and x in prompt] == [], subject_id
