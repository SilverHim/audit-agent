"""Xero 演示的 P2 七条程序的 Agent 层集成 (需求 R113, 设计稿 v2.1 第 7.3 节 "只开现有 17 条时"): 真实 Agent, 判断
引擎与 Xero 的 fixture (llm_xero 正本), 假存储预置演示数据导入之后的数据, 按层直接调用, 片段经 reducer 并入状态
(写法同 R49 的 test_p2_agents_demo, 分层与各程序的目标取它的). 再用现有决定的效果函数把 BA-091 的判断转为错报
M-D-CSH02-091 (设计稿编号 M-5, 按现有规则为 M-{决定 ID}), 由 FIN-04 汇总.

只在 Agent 层: 不经演示数据导入, 主图与闸门. 复核决定只过 check_review_record 中与图无关的各项 (check_review_values)
与纯效果函数, 经 GraphDecisionSubmit 提交, 留痕总数与报告由 R119 的离线全流程验收. 整段 P2 只跑一次 (模块级夹具),
与 test_xero_fixtures 的两串执行各管各的.
"""

import asyncio
import hashlib
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from backend.adapters.llm import MockLLMClient
from backend.agents.base import AgentDeps, RunContext
from backend.agents.evidence import confirmation_key
from backend.agents.registry import build_agents
from backend.contracts.errors import DecisionRejected
from backend.contracts.models import (
    AuditException,
    AutonomyLevel,
    ConfirmationResult,
    CriticVerdict,
    Decision,
    DocumentType,
    DowngradeCondition,
    ExceptionDisposition,
    Judgment,
    Misstatement,
    MisstatementDraft,
    MisstatementStatus,
    OpinionType,
    Phase,
    Procedure,
    ReceiptChannel,
    Reliability,
    ReviewRecord,
    ReviewStatus,
    RunStatus,
    TaskKind,
    TaskStatus,
    TrailKind,
    UncorrectedItem,
    UncorrectedSchedule,
    Versioned,
)
from backend.contracts.records import BankAccount
from backend.contracts.state import EngagementState, StatePatch
from backend.orchestration.decisions import (
    check_account_code,
    check_review_values,
    confirm_effects,
    misstatement_effects,
)
from backend.orchestration.nodes import apply_patch, assembled_procedure, build_run
from tests.fakes import NOW, FakeConfirmationChannel, FakeEvidenceStore, FakeRuleEngine, FakeTrail
from tests.fixture_sets import XERO
from tests.test_p2_agents_demo import LAYERS, OBJECTIVES, confirm_judgments
from tests.test_xero_fixtures import ORC, REV03_CONTRACTS
from tests.xero_fakes import (
    ORC1026,
    XERO17_FEATURES,
    XERO_RULES,
    make_xero_deps,
    manifest_documents,
    xero_confirmations,
    xero_engagement,
    xero_profile,
    xero_state,
    xero_traits,
)

ACCOUNTS = ["BA-090", "BA-091"]  # Wattlebrook Bank 的两个账户 (账号 12345678, 12345679)
JUDGMENT_IDS = [  # 第 7.3 节的 10 条判断
    *(f"J-REV-03-1-{c}" for c in REV03_CONTRACTS),
    *(f"J-REV-02-1-{i}" for i in ORC.values()),
    "J-CSH-02-1-BA-091",
]
# 审计师对 BA-091 判断的决定所附草稿: 科目按设计稿写到科目代码 (决定可细化 A6 草稿的科目, 错报取决定里的)
DRAFT = MisstatementDraft(
    type="factual",
    account="Interest income (270)",
    assertion="Completeness",
    amount_cents=4853,
    direction="understated",
)


@dataclass
class P2:
    """P2 按层跑完后的状态, 各程序的片段与依赖."""

    state: EngagementState  # 并入各层片段后的状态
    patches: dict[str, StatePatch]  # 各程序执行一次的片段
    deps: AgentDeps  # Xero 假存储, 真实判断引擎
    trail: FakeTrail  # 留痕
    channel: FakeConfirmationChannel  # 假函证平台 (回补造的回函 PDF)


def run_context(deps: AgentDeps, state: EngagementState, procedure_id: str) -> RunContext:
    """状态程序包中某条程序的下一次执行: 执行记录同 GraphNodes.perform 用 build_run 建 (参数按风险等级, 状态与数据集
    声明解析)."""
    engagement = xero_engagement()
    procedure = next(p for p in state["procedure_pack"] if p.id == procedure_id)
    template = deps.library.load(procedure_id)
    run = build_run(
        deps.library, template, procedure, engagement, state, NOW, traits=xero_traits(), profile=xero_profile()
    )
    return RunContext(engagement, procedure, template, run, state)


async def run_p2(features: frozenset[str] = XERO17_FEATURES) -> P2:
    """按层执行 7 条 P2 程序 (同层以本层开始时的状态执行, 再一起并入, 同 fan_in); 进入第 1 层前 REV-03 的 3 条判断
    置 confirmed v2. 状态为进入 P2 时的 (xero_state: 预置目标与风险, 外勤阶段上传的文件, 重要性 32431 / 16216 /
    1622); 程序第 1 版同 assemble_pack (nodes.assembled_procedure: 关联目标同 OBJECTIVES, 功能 autonomy 打开时为
    装配级降级后的级别); 规则引擎为 FakeRuleEngine(XERO_RULES), 主模型与复核模型都读正本."""
    trail = FakeTrail()
    client = MockLLMClient(XERO.fixtures)
    channel = xero_confirmations()
    rules = FakeRuleEngine(XERO_RULES)
    deps = make_xero_deps(
        trail=trail, llm=client, review_llm=client, rules=rules, confirmations=channel, features=features
    )
    agents = build_agents(deps)
    state: EngagementState = xero_state() | {"phase": Phase.P2_Executing}  # type: ignore[assignment]
    objectives = state["audit_objectives"]
    pack = [assembled_procedure(deps, deps.library.load(pid), objectives) for layer in LAYERS for pid in layer]
    assert {p.id: p.objective_ids for p in pack} == OBJECTIVES
    state = state | {"procedure_pack": pack}  # type: ignore[assignment]
    patches: dict[str, StatePatch] = {}
    for n, layer in enumerate(LAYERS):
        if n == 1:
            state = confirm_judgments(state, "REV-03")
        for pid in layer:
            patches[pid] = await agents[pid].run(run_context(deps, state, pid))
        for pid in layer:
            state = apply_patch(state, patches[pid])
    return P2(state, patches, deps, trail, channel)


@pytest.fixture(scope="module")
def p2() -> P2:
    return asyncio.run(run_p2())


@pytest.fixture(scope="module")
def p2_autonomy() -> P2:
    """功能 autonomy 打开时的同一段 P2 (需求 R170)."""
    return asyncio.run(run_p2(XERO17_FEATURES | {"autonomy"}))


@pytest.mark.external
def test_layers_run_once_and_each_call_passes(p2: P2) -> None:
    """验收 1, 3 的调用部分: 7 条程序各一次执行都成功; 大模型调用 REV-03 3 次, REV-02 6 次, DOC-06 2 次, CSH-02 1 次,
    GEN-09 0 次 (REV-03, REV-02 按 L2 跳过, CSH-02 未命中触发规则), 全部第 1 次就通过; 没有判断失败 (没有 failed
    或 unattributed 任务). 层间: REV-03 的判断进入第 1 层前已是 confirmed v2, REV-02 挂合同的两张发票的提示词里
    该合同的 REV-03 结论标 (confirmed)."""
    assert sorted((r.id, r.status) for r in p2.state["procedure_runs"]) == sorted(
        (f"RUN-{pid}-1", RunStatus.succeeded) for layer in LAYERS for pid in layer
    )
    calls = [e for e in p2.trail.entries if e.kind is TrailKind.llm_call]
    assert Counter(e.procedure_id for e in calls) == {"REV-03": 3, "REV-02": 6, "DOC-06": 2, "CSH-02": 1}
    assert {(e.payload["attempt"], e.payload["level_failed"]) for e in calls} == {(1, None)}
    judgments = {j.id: j for j in p2.state["judgments"]}
    for j in judgments.values():
        expected = "未命中独立复核的触发规则" if j.procedure_id == "CSH-02" else "L2"
        assert j.critic_verdict is CriticVerdict.skipped and (j.critic_reason or "").startswith(expected), j.id
    assert {
        (judgments[f"J-REV-03-1-{c}"].version, judgments[f"J-REV-03-1-{c}"].review_status) for c in REV03_CONTRACTS
    } == {(2, ReviewStatus.confirmed)}
    prompts = {
        e.payload["fixture_key"]: e.payload["messages"][0]["content"] for e in calls if e.procedure_id == "REV-02"
    }
    for number, contract in (("ORC1026", "CON-X01"), ("ORC1020", "CON-X02")):
        assert f"J-REV-03-1-{contract} (confirmed)" in prompts[ORC[number]], number


def bank_account(p2: P2, account_id: str) -> BankAccount:
    return BankAccount.model_validate(p2.deps.financial_store.records[("bank_account", account_id)])  # type: ignore[attr-defined]


@pytest.mark.external
def test_csh04_sends_and_archives_both_replies(p2: P2) -> None:
    """验收 2: CSH-04 建 C-BA-090, C-BA-091 第 1 版, 幂等键为 confirmation_key(eid, 该银行账户行), 回函方 Wattlebrook
    Bank Limited, 渠道 direct_email; 假平台收到的两次请求截至 2026-09-30, 用同一键; 2 行 external_call; 两份回函按
    账号存档 (confirmation, external_original, 内容为补造的回函 PDF), 文件 ID 即补造 manifest 的 ID."""
    eid = xero_engagement().id
    keys = {a: confirmation_key(eid, bank_account(p2, a)) for a in ACCOUNTS}
    assert [
        (c.id, c.version, c.idempotency_key, c.confirming_party, c.receipt_channel)
        for c in p2.patches["CSH-04"]["confirmations"]
    ] == [(f"C-{a}", 1, keys[a], "Wattlebrook Bank Limited", ReceiptChannel.direct_email) for a in ACCOUNTS]
    assert [(key, req.bank_account_id, req.as_of) for key, req in p2.channel.submits] == [
        (keys[a], a, date(2026, 9, 30)) for a in ACCOUNTS
    ]
    assert len([e for e in p2.trail.entries if e.kind is TrailKind.external_call]) == 2
    numbers = [bank_account(p2, a).account_no for a in ACCOUNTS]
    store = p2.deps.evidence_store
    assert isinstance(store, FakeEvidenceStore)
    assert store.save_args == [
        (
            f"{n}.pdf",
            hashlib.sha256(p2.channel.bodies[n]).hexdigest(),
            DocumentType.confirmation,
            Reliability.external_original,
        )
        for n in numbers
    ]
    manifest = {Path(d["path"]).stem: i for i, d in manifest_documents().items() if d["registration"] == "confirmation"}
    sent = {c.id: c.reply_document_id for c in p2.patches["CSH-04"]["confirmations"]}
    assert sent == {f"C-{a}": manifest[n] for a, n in zip(ACCOUNTS, numbers, strict=True)}


@pytest.mark.external
def test_results_match_the_design(p2: P2) -> None:
    """验收 3: 例外 2 条 (X-REV-08-1-R03-1, X-CSH-02-1-D04-1, 都 open); 判断 10 条, REV-02 与 CSH-02 的 7 条 pending;
    pending normal 任务 10 条, 各指向一条判断; C-BA-090, C-BA-091 agreed, 后者应计利息 4853; 两份回函 extracted."""
    assert sorted((x.id, x.disposition) for x in p2.state["exceptions"]) == [
        ("X-CSH-02-1-D04-1", ExceptionDisposition.open),
        ("X-REV-08-1-R03-1", ExceptionDisposition.open),
    ]
    judgments = {j.id: j for j in p2.state["judgments"]}
    assert sorted(judgments) == sorted(JUDGMENT_IDS)
    assert {j.review_status for j in judgments.values() if j.procedure_id != "REV-03"} == {ReviewStatus.pending}
    tasks = p2.state["review_tasks"]
    assert {(t.kind, t.status) for t in tasks} == {(TaskKind.normal, TaskStatus.pending)}
    assert sorted(t.target_id for t in tasks) == sorted(JUDGMENT_IDS)
    confirmations = {c.id: c for c in p2.state["confirmations"]}
    assert {i: (c.result, c.accrued_interest_cents) for i, c in confirmations.items()} == {
        "C-BA-090": (ConfirmationResult.agreed, 0),
        "C-BA-091": (ConfirmationResult.agreed, 4853),
    }
    documents = {d.id: d.status for d in p2.state["documents"]}
    assert {documents[c.reply_document_id or ""] for c in confirmations.values()} == {"extracted"}


def effects_patch(effects: list[Versioned]) -> StatePatch:
    """决定的效果按对象类型并入状态的字段 (同 await_events 从 Store 读回的字段)."""
    fields = {Judgment: "judgments", AuditException: "exceptions", Misstatement: "misstatements"}
    patch: dict[str, list[Any]] = {}
    for effect in effects:
        patch.setdefault(fields[type(effect)], []).append(effect)
    return patch  # type: ignore[return-value]


def decide(p2: P2, judgment_id: str, record_id: str, decision: Decision, **fields: Any) -> tuple[Any, ReviewRecord]:
    """审计师对该判断的 normal 任务 (第 1 版, 待决) 作出决定, 先过 check_review_values (与图无关的校验) 再返回
    (任务, 决定). 任务待决与目标的当前版本按状态核对 (Agent 层没有 Store 与图)."""
    [task] = [t for t in p2.state["review_tasks"] if t.target_id == judgment_id]
    [target] = [j for j in p2.state["judgments"] if j.id == judgment_id]
    assert (task.id, task.status, task.target_version, target.version) == (
        f"T-{judgment_id}-v1",
        TaskStatus.pending,
        1,
        1,
    )
    record = ReviewRecord(
        id=record_id,
        task_id=task.id,
        target_version=task.target_version,
        decision=decision,
        reviewed_by="U-1",
        decided_at=NOW,
        **fields,
    )
    check_review_values(p2.deps, record, task, target)
    return task, record


@pytest.mark.external
async def test_misstatement_path_reaches_fin04(p2: P2) -> None:
    """验收 4, 5: 对 T-J-CSH-02-1-BA-091-v1 的 misstatement 决定 D-CSH02-091 通过与图无关的校验 (允许 misstatement,
    字段齐, 绑定的目标版本与任务一致), misstatement_effects 得判断 v2 confirmed, D04 v2 归因于它, 错报
    M-D-CSH02-091 (4853 低估, 未更正); 对 ORC1026 的判断 confirm 后 R03-1 v2 归因于它. 两组效果并入状态后 A9 执行
    FIN-04: 参数快照为重要性 32431 与明显微小 1622, SCHEDULE v1 一行 4853, 合计 4853, 候选无保留意见."""
    judgments = {j.id: j for j in p2.state["judgments"]}
    exceptions = {x.id: x for x in p2.state["exceptions"]}
    cash = judgments["J-CSH-02-1-BA-091"]
    task, record = decide(
        p2,
        cash.id,
        "D-CSH02-091",
        Decision.misstatement,
        reason="Accrued interest of 48.53 to 30 September 2026 is not recorded; accumulate as uncorrected.",
        misstatement_draft=DRAFT,
    )
    missed = misstatement_effects(record, task, cash, [exceptions[i] for i in cash.exception_ids], set(judgments))
    assert [(type(e).__name__, e.id, e.version) for e in missed] == [
        ("Judgment", "J-CSH-02-1-BA-091", 2),
        ("AuditException", "X-CSH-02-1-D04-1", 2),
        ("Misstatement", "M-D-CSH02-091", 1),
    ]
    confirmed, d04, misstatement = missed
    assert isinstance(confirmed, Judgment) and confirmed.review_status is ReviewStatus.confirmed
    assert isinstance(d04, AuditException)
    assert (d04.disposition, d04.attributed_by) == (ExceptionDisposition.attributed, cash.id)
    assert misstatement == Misstatement(
        id="M-D-CSH02-091",
        judgment_id=cash.id,
        exception_id=None,
        review_record_id="D-CSH02-091",
        status=MisstatementStatus.uncorrected,  # 功能 misstatement 关闭 (需求 R156 起缺省为 identified)
        **DRAFT.model_dump(),
    )
    assert misstatement.status == "uncorrected"
    receipt = judgments[f"J-REV-02-1-{ORC1026}"]
    decide(p2, receipt.id, "D-REV02-1026", Decision.confirm)
    kept = confirm_effects(receipt, [exceptions[i] for i in receipt.exception_ids])
    [r03] = [e for e in kept if isinstance(e, AuditException)]
    assert (r03.id, r03.version, r03.disposition, r03.attributed_by) == (
        "X-REV-08-1-R03-1",
        2,
        ExceptionDisposition.attributed,
        receipt.id,
    )
    fin04 = Procedure(
        id="FIN-04",
        phase=Phase.P3_Completion,
        template_commit=p2.deps.library.commit,
        autonomy_level=p2.deps.library.load("FIN-04").autonomy_level,
    )
    state = apply_patch(apply_patch(p2.state, effects_patch(missed)), effects_patch(kept))
    state = state | {"procedure_pack": [*state["procedure_pack"], fin04]}  # type: ignore[assignment]
    ctx = run_context(p2.deps, state, "FIN-04")
    assert ctx.run.params_snapshot == {
        "materiality_cents": 32431,
        "performance_materiality_cents": 16216,
        "clearly_trivial_cents": 1622,
    }
    patch = await build_agents(p2.deps)["FIN-04"].run(ctx)
    assert patch["uncorrected_schedule"] == UncorrectedSchedule(
        id="SCHEDULE",
        version=1,
        items=[UncorrectedItem(misstatement_id="M-D-CSH02-091", amount_cents=4853, direction="understated")],
        excluded_trivial_ids=[],
        total_cents=4853,
        materiality_cents=32431,
        candidate_opinion=OpinionType.unqualified,
    )
    [run] = patch["procedure_runs"]
    assert (run.id, run.status) == ("RUN-FIN-04-1", RunStatus.succeeded)


@pytest.mark.external
async def test_lifecycle_misstatement_keeps_account_code(p2: P2) -> None:
    """R156 验收 4: 决定 D-CSH02-091 的草稿带科目代码 270 (Xero 科目表 Interest Income) 时过科目代码校验; 功能
    misstatement 打开时 M-D-CSH02-091 v1 为 identified, 带 account_code 270, 来源 J-CSH-02-1-BA-091, 其余字段同
    R19; 关闭时为 uncorrected 且 uncorrected_basis 为空 (上一用例为不带代码的原断言). 代码 999 (科目表没有) 报
    INVALID_VALUE; 提交路径上被拒不写由 test_review_decisions 经 GraphDecisionSubmit 核对."""
    judgments = {j.id: j for j in p2.state["judgments"]}
    cash = judgments["J-CSH-02-1-BA-091"]
    explained = [x for x in p2.state["exceptions"] if x.id in cash.exception_ids]
    draft = DRAFT.model_copy(update={"account_code": "270"})
    task, record = decide(
        p2, cash.id, "D-CSH02-091", Decision.misstatement, reason="复核意见", misstatement_draft=draft
    )
    eid = xero_engagement().id
    await check_account_code(p2.deps.financial_store, eid, record)
    for lifecycle, status in ((True, MisstatementStatus.identified), (False, MisstatementStatus.uncorrected)):
        *_, misstatement = misstatement_effects(record, task, cash, explained, set(judgments), lifecycle)
        assert misstatement == Misstatement(
            id="M-D-CSH02-091",
            judgment_id=cash.id,
            exception_id=None,
            review_record_id="D-CSH02-091",
            status=status,
            **draft.model_dump(),
        )
    unknown = record.model_copy(update={"misstatement_draft": draft.model_copy(update={"account_code": "999"})})
    with pytest.raises(DecisionRejected) as e:
        await check_account_code(p2.deps.financial_store, eid, unknown)
    assert (e.value.code, e.value.message) == ("INVALID_VALUE", "错报草稿的科目代码 999 不在本项目科目表中")


@pytest.mark.external
def test_judgment_levels_with_autonomy(p2_autonomy: P2) -> None:
    """需求 R170 验收 5, 8 (功能 autonomy 打开, 真实判断引擎与 Xero 正本 fixture): 装配后 REV-03, REV-02 为 L1
    [significant_risk], CSH-02 为 L2 [high_risk] (模板 L3). 判断起点为程序的运行时级别, 理由接着装配时的: REV-02 六条
    金额 (发票行金额, ORC1026 取例外 R03-1) 都超过执行重要性 16216, ORC1026 置信度低于 0.7 另记 low_confidence;
    REV-03 三条只有装配时的理由; J-CSH-02-1-BA-091 金额 4853 未超过, 仍为 L2 [high_risk], A7 按 L2 跳过. 任务仍为
    10 条 normal."""
    level, sig, high = AutonomyLevel, DowngradeCondition.significant_risk, DowngradeCondition.high_risk
    over_pm, low = DowngradeCondition.amount_over_pm, DowngradeCondition.low_confidence
    pack = {p.id: (p.autonomy_level, p.downgrade_reasons) for p in p2_autonomy.state["procedure_pack"]}
    assert (pack["REV-03"], pack["REV-02"], pack["CSH-02"]) == (
        (level.L1, [sig]),
        (level.L1, [sig]),
        (level.L2, [high]),
    )
    judgments = {j.id: j for j in p2_autonomy.state["judgments"]}
    assert {jid: (j.autonomy_level, j.downgrade_reasons) for jid, j in judgments.items()} == {
        **{f"J-REV-03-1-{c}": (level.L1, [sig]) for c in REV03_CONTRACTS},
        **{
            f"J-REV-02-1-{i}": (level.L1, [sig, over_pm, low] if i == ORC1026 else [sig, over_pm]) for i in ORC.values()
        },
        "J-CSH-02-1-BA-091": (level.L2, [high]),
    }
    cash = judgments["J-CSH-02-1-BA-091"]
    assert cash.critic_verdict is CriticVerdict.skipped and (cash.critic_reason or "").startswith("L2")
    tasks = p2_autonomy.state["review_tasks"]
    assert {(t.kind, t.status) for t in tasks} == {(TaskKind.normal, TaskStatus.pending)}
    assert sorted(t.target_id for t in tasks) == sorted(JUDGMENT_IDS)
