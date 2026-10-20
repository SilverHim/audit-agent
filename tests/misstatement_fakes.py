"""错报闭环 (需求 R158 起) 的测试装配, 与 xero_fakes 并列: Xero 演示上 A8 的依赖, 状态与执行上下文, 各轮错报, 更正
通知与第 2 轮的来源判断. 普通构造函数, 不是 fixture; 错报链的测试文件都从这里取, 互相之间不导入这些装配.

错报与来源判断出自 xero_fakes (xero_identified_misstatements, XERO_ROUND2), 第 n 轮登记出自补造的
manual/fin08_round{n}.json (xero_fakes.round_file). 模块常量不读快照与补造, 函数在用例内调用 (收集阶段不读数据).
第 2 轮的 SECOND, round2_communicated, round2_sources 现由 FIN-09 第 2 轮的用例用, 也是给 R167 两轮闭环预留的.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from backend.adapters.llm import MockLLMClient
from backend.agents.base import AgentDeps, RunContext
from backend.contracts.models import (
    Assertion,
    AutonomyLevel,
    CorrectionNotice,
    Direction,
    Judgment,
    Misstatement,
    MisstatementDraft,
    MisstatementStatus,
    MisstatementType,
    Phase,
    ProposedAction,
    RerunRequest,
    TrailEntry,
    TrailKind,
)
from backend.contracts.services import RuleResult
from backend.contracts.state import EngagementState
from backend.orchestration.nodes import build_run
from tests.fakes import ENGAGEMENT, NOW, FakeRuleEngine, FakeTrail, make_ctx, xero_document
from tests.fixture_sets import XERO
from tests.xero_fakes import (
    XERO_ROUND2,
    make_xero_deps,
    round_file,
    with_adjustments,
    xero_engagement,
    xero_identified_misstatements,
    xero_profile,
    xero_state,
    xero_traits,
    xero_v01_rows,
)

FIN03, FIN09 = "FIN-03", "FIN-09"
REPLY = "confirmations/12345679.pdf"  # BA-091 的回函 (CSH-04 存档, 不在外勤阶段的上传里)
XJ499, CSH02 = "M-D-XJ499", "M-D-CSH02-091"  # 第 1 轮的两条错报 (办公设备高估 750.00, 应计利息低估 48.53)
FIRST, SECOND = ["AJE-X01", "AJE-X05"], ["AJE-X02", "AJE-X03", "AJE-X04"]  # 两轮更正通知载入的分录
POSTED_AT = datetime(2026, 10, 20, 3, 30, tzinfo=UTC)  # 第 1 轮更正通知: AJE-X01, X05 录入 (03:00Z) 之后


# ── A8 的依赖, 状态与执行上下文 ──


def fin03_deps(trail: FakeTrail, llm: Any = None, **kwargs: Any) -> AgentDeps:
    """Xero 假实现, 主模型缺省读 Xero fixture 正本 (复核模型同; FIN-03 为 L2, A7 按跳过规则不调); 财务存储另登记
    BA-091 的回函. kwargs 原样转交 make_xero_deps (如 features, library)."""
    client = llm if llm is not None else MockLLMClient(XERO.fixtures)
    deps = make_xero_deps(trail=trail, llm=client, review_llm=client, **kwargs)
    deps.financial_store.document_refs.append(xero_document(REPLY))  # type: ignore[attr-defined]
    return deps


def fin09_deps(
    trail: FakeTrail,
    llm: Any = None,
    rows: Sequence[dict[str, Any]] | None = None,
    entries: Sequence[str] = FIRST,
    **kwargs: Any,
) -> AgentDeps:
    """fin03_deps (kwargs 原样转交, 如 features) 加 FIN-09 的数据: 财务存储另记 entries 的更正分录; 规则引擎的 V01
    为这些分录的行 (xero_v01_rows), rows 给了时换成它."""
    v01 = list(rows) if rows is not None else xero_v01_rows(entries)
    rules = FakeRuleEngine({FIN09: lambda run: RuleResult(exceptions=[], rows={"V01": v01})})
    deps = fin03_deps(trail, llm, rules=rules, **kwargs)
    with_adjustments(deps.financial_store, entries)  # type: ignore[arg-type]
    return deps


def fin03_state(**changes: Any) -> EngagementState:
    """FIN-03 前的状态: xero_state 加两条错报与来源判断 (另有 REV-03 对 CON-X01 的判断); changes 覆盖任意键."""
    preset = xero_identified_misstatements()
    state = xero_state() | {"judgments": preset["judgments"], "misstatements": preset["misstatements"]}
    return state | changes  # type: ignore[return-value]


def fin03_context(deps: AgentDeps, state: EngagementState, procedure_id: str = FIN03) -> RunContext:
    """A8 的完成阶段程序 (缺省 FIN-03) 的一次执行: 程序为完成阶段的第 1 版 (L2), 执行记录同 GraphNodes.perform 用
    build_run 建 (run_no 按状态中的执行记录, 没有时为第 1 次)."""
    engagement = xero_engagement()
    ctx = make_ctx(deps, procedure_id, engagement=engagement)
    procedure = ctx.procedure.model_copy(update={"phase": Phase.P3_Completion})
    run = build_run(
        deps.library, ctx.template, procedure, engagement, state, NOW, traits=xero_traits(), profile=xero_profile()
    )
    return RunContext(engagement, procedure, ctx.template, run, state | {"procedure_pack": [procedure]})  # type: ignore[arg-type]


def llm_calls(trail: FakeTrail) -> list[TrailEntry]:
    return [e for e in trail.entries if e.kind is TrailKind.llm_call]


SUPERSEDED = RerunRequest(  # 过期重跑取代了 XJ-499 来源判断的那次执行
    id="RR-1",
    procedure_ids=["GEN-02"],
    superseded_run_ids=["RUN-GEN-02-1"],
    data_version=2,
    requested_by="U-1",
    requested_at=NOW,
)


# ── 各轮错报 ──

# Xero fixture 正本给两条错报的分类载荷 (R158 验收 2): 类型与舞弊迹象取模型, 来源与审计目标取规则
FACTUAL: dict[str, Any] = {
    "type": MisstatementType.factual,
    "fraud_indicative": False,
    "fraud_reason": None,
    "origin": "current_period",
    "objective_id": None,
}


def classified(m: Misstatement, judgment_id: str | None = None, **changes: Any) -> Misstatement:
    """已分类的下一版: 作分类的判断缺省为 FIN-03 第 1 次对它的判断, changes 为分类载荷写的字段 (R159 的预期值)."""
    judgment_id = judgment_id or f"J-FIN-03-1-{m.id}"
    return m.model_copy(update={"version": m.version + 1, "classification_judgment_id": judgment_id, **changes})


def round1_misstatements() -> list[Misstatement]:
    """第 1 轮前: 两条错报已分类 (v2, 同 R159 确认后), identified."""
    return [classified(m, **FACTUAL) for m in fin03_state()["misstatements"]]


def round2_misstatement(code: str, suffix: str, account: str, amount_cents: int) -> Misstatement:
    """第 2 轮的错报 (FIN-15 第 1 次对科目 code 的判断认定, Xero演示数据集设计第 2.8 节), 已分类 (v2)."""
    m = Misstatement(
        id=f"M-D-{suffix}",
        judgment_id=f"J-FIN-15-1-{code}",
        exception_id=None,
        review_record_id=f"D-{suffix}",
        type=MisstatementType.factual,
        account=account,
        assertion=Assertion.Completeness,
        amount_cents=amount_cents,
        direction=Direction.understated,
        account_code=code,
    )
    return classified(m, f"J-FIN-03-2-{m.id}", **FACTUAL)


ROUND2 = [round2_misstatement(*row) for row in XERO_ROUND2]


def communicated_fields(n: int) -> dict[str, Any]:
    """第 n 轮登记 (都同意) 写进错报的状态与沟通, 回复四项."""
    data = round_file(n)
    return {
        "status": MisstatementStatus.communicated,
        "communicated_at": datetime.fromisoformat(data["communicated_at"]),
        "communicated_to": data["recipient"],
        "responded_at": datetime.fromisoformat(data["responded_at"]),
        "management_response": data["response"],
    }


def round1_communicated() -> list[Misstatement]:
    """第 1 轮登记之后的两条 (v3 communicated, 等更正); 不再列进沟通材料."""
    return [m.model_copy(update={"version": 3, **communicated_fields(1)}) for m in round1_misstatements()]


def round2_communicated() -> list[Misstatement]:
    """第 2 轮登记之后的三条 (v3 communicated, 等更正)."""
    return [m.model_copy(update={"version": 3, **communicated_fields(2)}) for m in ROUND2]


def fin15_judgment(code: str, conclusion: str, draft: MisstatementDraft, run_no: int = 1) -> Judgment:
    """FIN-15 第 run_no 次执行对科目 code 建议认定错报 (草稿 draft) 的判断: 第 1 版, L2, 由项目负责人作出, 没有证据.
    test_p3_flow 的桩 FIN-15 与第 2 轮错报的来源判断 (round2_sources) 共用."""
    return Judgment(
        id=f"J-FIN-15-{run_no}-{code}",
        procedure_id="FIN-15",
        run_id=f"RUN-FIN-15-{run_no}",
        subject_id=code,
        conclusion=conclusion,
        confidence=0.8,
        confidence_parts={},
        reasoning_chain=[],
        evidence=[],
        counter_argument="",
        proposed_action=ProposedAction.misstatement,
        misstatement_draft=draft,
        autonomy_level=AutonomyLevel.L2,
        performed_by=ENGAGEMENT.owner_id,
        created_at=NOW,
    )


def round2_sources() -> list[Judgment]:
    """第 2 轮三条薪酬错报的来源判断 (FIN-15 第 1 次对科目的判断, 草稿取错报的同名字段)."""
    fields = set(MisstatementDraft.model_fields)
    return [
        fin15_judgment(code, f"{m.account} 期末少计", MisstatementDraft(**m.model_dump(include=fields)))
        for (code, *_), m in zip(XERO_ROUND2, ROUND2, strict=True)
    ]


# ── 更正通知 ──


def notice(oid: str = "D-CN1", **fields: Any) -> CorrectionNotice:
    """更正通知: 缺省为第 1 轮两条都已更正 (AJE-X01, AJE-X05), 由 U-1 提交; fields 覆盖任意字段."""
    base: dict[str, Any] = {
        "id": oid,
        "kind": "adjustments_posted",
        "misstatement_ids": [XJ499, CSH02],
        "adjustment_entry_nos": FIRST,
        "decided_by": "U-1",
        "decided_at": POSTED_AT,
    }
    return CorrectionNotice.model_validate(base | fields)
