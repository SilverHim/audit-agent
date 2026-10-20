"""A4 的 PLN-15: 总体审计计划 (需求 R204; 契约与提示词见需求 R203, 接口文档第 2.3, 3, 5, 6 节).

计划骨架 (期间, 适用框架与约定书, 重要性, 特别风险目标, 计划的程序与参数) 按程序包与状态确定性组装 (skeleton), 叙述
(总体策略, 各段, 各程序的性质时间范围, 时间表, 项目质量复核建议, 专家评估) 由大模型按 PLN-15.md.j2 写 (AuditPlanDraft),
两者合成 PLAN-{revision} 的草稿. 第 2 修订起是 PLN-15 驳回后的重拟: 回应最近一次针对上一修订的驳回, 上一修订出新版本
置 superseded. RiskAssessorAgent 按程序编号分派到 draft_plans (执行) 与 plan_patch (解读).
"""

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

from pydantic_core import to_jsonable_python

from backend.agents.base import AgentDeps, RunContext, finish_run
from backend.contracts.llm_outputs import AuditPlanDraft
from backend.contracts.models import (
    AgentId,
    AuditPlan,
    ClientProfile,
    GateDecision,
    GateId,
    GateVerdict,
    MaterialityChoice,
    Phase,
    PlanItem,
    PlanSection,
    PlanStatus,
    RiskLevel,
)
from backend.contracts.records import DatasetTraits
from backend.contracts.services import ValidationContext
from backend.contracts.state import EngagementState, StatePatch
from backend.services.procedure_library import highest_risk

PROCEDURE_ID = "PLN-15"


def current_plan(plans: Sequence[AuditPlan]) -> AuditPlan | None:
    """当前计划: status 不是 superseded 的修订中 revision 最大的 (第 0 节); 没有为 None."""
    live = [p for p in plans if p.status is not PlanStatus.superseded]
    return max(live, key=lambda p: (p.revision, p.version)) if live else None


def period_months(start: date, end: date) -> int:
    """审计期间的整月数: 期末次日与期初相差的月数 (2026-07-01 至 09-30 为 3)."""
    after = end + timedelta(days=1)
    return (after.year - start.year) * 12 + after.month - start.month


def materiality_choice(state: EngagementState) -> MaterialityChoice:
    """最后一条 PLN-01/03 通过决定选定的基准与比例; 没有 (还没过重要性闸门) 抛 ValueError."""
    approvals = [
        d.materiality
        for d in state["gate_decisions"]
        if d.gate is GateId.PLN_01_03 and d.decision is GateVerdict.approve and d.materiality is not None
    ]
    if not approvals:
        raise ValueError(f"{PROCEDURE_ID} 需要 PLN-01/03 通过时选定的重要性")
    return approvals[-1]


def rejection(state: EngagementState, previous: AuditPlan) -> GateDecision:
    """重拟所回应的决定: 最近一次针对上一修订 (target_id) 的 PLN-15 驳回; 找不到抛 LookupError."""
    rejects = [
        d
        for d in state["gate_decisions"]
        if d.gate is GateId.PLN_15 and d.decision is GateVerdict.reject and d.target_id == previous.id
    ]
    if not rejects:
        raise LookupError(f"{PROCEDURE_ID} 第 {previous.revision + 1} 修订找不到 PLN-15 对 {previous.id} 的驳回决定")
    return rejects[-1]


def plan_items(
    deps: AgentDeps, ctx: RunContext, traits: DatasetTraits, profile: ClientProfile | None
) -> list[PlanItem]:
    """计划的程序: 程序包中的 P2 程序, 按程序编号排序. 参数同建执行记录时 (nodes.build_run): 模板参数按关联审计目标的
    最高风险等级解析, 叠加 Procedure.params; 转成 JSON 形式 (日期为 ISO 文本), 经 Store 与检查点读回不变."""
    items: list[PlanItem] = []
    state, library = ctx.state, deps.library
    for procedure in sorted((p for p in state["procedure_pack"] if p.phase is Phase.P2_Executing), key=lambda p: p.id):
        template = library.load(procedure.id)
        risk: RiskLevel | None = highest_risk(state["audit_objectives"], procedure.objective_ids)
        resolved = library.resolve_params(template, ctx.engagement, state, risk, traits=traits, profile=profile)
        items.append(
            PlanItem(
                procedure_id=procedure.id,
                name=template.name,
                template_commit=library.commit,
                objective_ids=list(procedure.objective_ids),
                params=to_jsonable_python({**resolved, **procedure.params}),
            )
        )
    return items


def skeleton(ctx: RunContext, revision: int, items: list[PlanItem], decision: GateDecision | None) -> dict[str, Any]:
    """计划骨架的字段 (第 2.3 节): 期间取项目, 适用框架与约定书取 TERMS (没有为空; 约定书取出具日指针所在的文件),
    重要性取闸门选定的与状态中的三项 (还没确定抛 ValueError), 特别风险为风险等级 significant 的审计目标 (状态中的
    顺序). decision 为重拟所回应的驳回 (第 1 修订为空)."""
    state, engagement = ctx.state, ctx.engagement
    amounts = (state["materiality_cents"], state["performance_materiality_cents"], state["clearly_trivial_cents"])
    if any(a is None for a in amounts):
        raise ValueError(f"{PROCEDURE_ID} 需要 PLN-01/03 确定的重要性三项")
    materiality, performance, trivial = cast(tuple[int, int, int], amounts)
    terms = state["engagement_terms"]
    letter = terms.letter_evidence or terms.framework_evidence if terms is not None else []
    return {
        "id": f"PLAN-{revision}",
        "revision": revision,
        "status": PlanStatus.draft,
        "run_id": ctx.run.id,
        "period_start": engagement.period_start,
        "period_end": engagement.period_end,
        "applicable_framework": terms.framework if terms is not None else None,
        "engagement_letter_document_id": letter[0].source_id if letter else None,
        "materiality_choice": materiality_choice(state),
        "materiality_cents": materiality,
        "performance_materiality_cents": performance,
        "clearly_trivial_cents": trivial,
        "significant_risk_objective_ids": [
            o.id for o in state["audit_objectives"] if o.risk_level is RiskLevel.significant
        ],
        "items": items,
        "addresses_decision_id": decision.id if decision is not None else None,
        "revision_reason": decision.reason if decision is not None else None,
    }


async def trial_balance(deps: AgentDeps, eid: str) -> list[dict[str, Any]]:
    """提示词的本期试算表: 每个科目的代码, 名称, 标准分类与余额 (分, 借方为正), 按科目代码排序."""
    accounts = {a.code: a for a in await deps.financial_store.accounts(eid)}
    rows = await deps.financial_store.trial_balance(eid, "current")
    return [
        {
            "code": r.account_code,
            "name": accounts[r.account_code].name if r.account_code in accounts else r.account_code,
            "category": accounts[r.account_code].category if r.account_code in accounts else None,
            "balance_cents": r.balance_cents,
        }
        for r in sorted(rows, key=lambda r: r.account_code)
    ]


async def draft_plans(deps: AgentDeps, ctx: RunContext) -> list[AuditPlan]:
    """PLN-15 的执行: 起草下一修订 PLAN-{revision} (revision 为状态中最大修订号 + 1) 的草稿第 1 版; 第 2 修订起回应
    针对上一修订的驳回, 并把上一修订的当前版本置 superseded (记本次执行). 返回要写入的计划 (新修订在首).

    提示词变量见 PLN-15.md.j2 头注释, fixture_key 为 r{revision}. 校验上下文给出模板 (准则片段只能取它的 standards),
    计划的程序与期末. 校验不通过时抛 ValueError (含最后一次的错误), 由 A1 按执行失败处理.
    """
    from backend.agents.ingest import client_profile  # 在此导入: ingest 经 evidence 间接导入的模块多, 免得循环

    state, engagement = ctx.state, ctx.engagement
    revision = max((p.revision for p in state["audit_plans"]), default=0) + 1
    earlier = [p for p in state["audit_plans"] if p.revision == revision - 1]
    previous = max(earlier, key=lambda p: p.version, default=None)  # 上一修订的最新版本
    decision = rejection(state, previous) if previous is not None else None
    profile = await client_profile(deps, engagement, state["client_profile_version"])
    traits = deps.datasets.dataset_traits(engagement.dataset) if engagement.dataset is not None else DatasetTraits()
    items = plan_items(deps, ctx, traits, profile)
    fields = skeleton(ctx, revision, items, decision)
    choice: MaterialityChoice = fields["materiality_choice"]
    variables = {
        "entity": profile.model_dump(mode="json"),
        "period_start": engagement.period_start.isoformat(),
        "period_end": engagement.period_end.isoformat(),
        "period_months": period_months(engagement.period_start, engagement.period_end),
        "terms": state["engagement_terms"].model_dump(mode="json") if state["engagement_terms"] is not None else None,
        "analytics": state["analytics"].model_dump(mode="json") if state["analytics"] is not None else None,
        "materiality": {
            "choice": choice.model_dump(mode="json"),
            "materiality_cents": fields["materiality_cents"],
            "performance_materiality_cents": fields["performance_materiality_cents"],
            "clearly_trivial_cents": fields["clearly_trivial_cents"],
        },
        "objectives": [o.model_dump(mode="json") for o in state["audit_objectives"]],
        "risks": [r.model_dump(mode="json") for r in state["risk_register"]],
        "items": [i.model_dump(mode="json") for i in items],
        "trial_balance": await trial_balance(deps, engagement.id),
        "revision": revision,
        "revision_reason": fields["revision_reason"],
        "standards": deps.judgments.standards(ctx.template),
    }
    context = ValidationContext(
        standards_of=ctx.template.id,
        expected_procedure_ids=[i.procedure_id for i in items],
        period_end=engagement.period_end,
    )
    result = await deps.judgments.call_llm(
        engagement.id, ctx.template, ctx.run, variables, context, AgentId.A4, f"r{revision}"
    )
    report = result.report
    if report.level_failed is not None:
        raise ValueError(
            f"{PROCEDURE_ID} 第 {revision} 修订未通过 {report.level_failed} 校验: {'; '.join(report.errors)}"
        )
    draft = cast(AuditPlanDraft, report.output)
    narrative = [PlanSection(section="overall_strategy", text=draft.overall_strategy)]
    narrative += [PlanSection(section=name, text=text) for name, text in draft.sections.items()]
    plan = AuditPlan.model_validate(
        {
            **fields,
            "narrative": narrative,
            "procedure_notes": draft.procedure_notes,
            "timetable": draft.timetable,
            "eqr_recommendation": draft.eqr_recommendation,
            "expert_assessment": draft.expert_assessment,
            "standards_cited": draft.standards_cited,
        }
    )
    plans = [plan]
    if previous is not None and previous.status is not PlanStatus.superseded:
        retired = {"version": previous.version + 1, "status": PlanStatus.superseded, "run_id": ctx.run.id}
        plans.append(AuditPlan.model_validate({**previous.model_dump(), **retired}))  # 经校验构造 (第 2.3 节)
    return plans


def plan_patch(ctx: RunContext, plans: list[AuditPlan]) -> StatePatch:
    """PLN-15 的片段: 新修订的草稿 (与上一修订置 superseded 的新版本) 与执行记录的成功终态. PLN-15 是闸门程序,
    程序本身仍为 planned, 由 PLN-15 闸门批准时置 completed (Q5, 节点按模板 gate 处理)."""
    return {"audit_plans": plans, "procedure_runs": [finish_run(ctx.run, datetime.now(UTC))]}
