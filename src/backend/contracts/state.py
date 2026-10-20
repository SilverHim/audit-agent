"""编排状态与 reducer, 定义见 开发接口文档.md 第 3 节."""

import copy
from collections.abc import Mapping
from typing import Annotated, Any, TypedDict, TypeVar, cast

from backend.contracts.errors import VersionConflict
from backend.contracts.models import (
    AcceptanceScorecard,
    AgentId,
    AnalyticsResult,
    AuditException,
    AuditObjective,
    AuditPlan,
    AuditReportVersion,
    ComplianceCheck,
    Confirmation,
    CorrectionNotice,
    DocumentRef,
    Engagement,
    EngagementTerms,
    GateDecision,
    GovernanceCommunication,
    IndependenceDeclaration,
    Judgment,
    MaterialityCandidates,
    Misstatement,
    OpinionType,
    OwnershipGraph,
    Phase,
    Procedure,
    ProcedureRun,
    RerunRequest,
    ReviewRecord,
    ReviewTask,
    RiskItem,
    ScheduleRow,
    UncorrectedSchedule,
    Versioned,
    WrittenRepresentationLetter,
)

StatePatch = dict[str, Any]


class StagedResult(TypedDict):
    """fieldwork 实例执行一条程序一次的结果, 暂存在 staged_results, 由 fan_in 校验后写入 Store 并并入状态."""

    procedure_id: str  # 执行的程序
    run_id: str  # 本次执行记录的 ID
    writer: AgentId  # 片段的写入者: 执行程序的 Agent; 执行失败时为 A1
    patch: StatePatch  # 要写入的片段, 含本次执行记录的终态


V = TypeVar("V", bound=Versioned)
R = TypeVar("R", DocumentRef, ReviewRecord, GateDecision)


def upsert_by_id(left: list[V], right: list[V] | None) -> list[V]:
    """新 ID 追加; version 更高的替换; version 相同的忽略; version 更低抛 VersionConflict."""
    merged = {item.id: item for item in left}
    order = [item.id for item in left]
    for item in right or []:
        current = merged.get(item.id)
        if current is None:
            order.append(item.id)
            merged[item.id] = item
        elif item.version > current.version:
            merged[item.id] = item
        elif item.version < current.version:
            raise VersionConflict(f"{item.id}: v{item.version} 低于已有 v{current.version}")
    return [merged[i] for i in order]


def replace_by_id(left: list[R], right: list[R] | None) -> list[R]:
    """按 id 追加, 同 id 后到者替换."""
    merged = {item.id: item for item in left}
    order = [item.id for item in left]
    for item in right or []:
        if item.id not in merged:
            order.append(item.id)
        merged[item.id] = item
    return [merged[i] for i in order]


def append_unique(left: list[R], right: list[R] | None) -> list[R]:
    """按 ID 追加, 已存在的忽略."""
    seen = {item.id for item in left}
    out = list(left)
    for item in right or []:
        if item.id not in seen:
            seen.add(item.id)
            out.append(item)
    return out


def staged(left: list[StagedResult], right: list[StagedResult] | None) -> list[StagedResult]:
    """追加; 收到 None 时清空, 只由 fan_in 发出."""
    if right is None:
        return []
    return left + right


class EngagementState(TypedDict):
    """LangGraph 主图的状态: 一个审计项目全流程共享的数据, 每步存检查点."""

    engagement_id: str  # 项目编号, 也是图的 thread_id
    phase: Phase  # 当前阶段
    version_snapshot: dict[str, str]  # 程序库 commit, 模型, prompt, 功能集的版本快照
    acceptance_scorecard: AcceptanceScorecard | None  # 承接评分卡, A2 写
    engagement_terms: EngagementTerms | None  # 约定条款与委任核验 (TERMS), A2 ACC-08 写 (功能 acceptance, 需求 R198)
    client_profile_version: int  # 本项目采用的画像版本; ACC-04 按它读画像, ACC-01 路由据此判断评分卡是否过期
    analytics: AnalyticsResult | None  # 初步分析结果, A4 写
    materiality_candidates: MaterialityCandidates | None  # 重要性候选, A4 写
    materiality_cents: int | None  # 整体重要性, PLN-01/03 闸门确定; 只在图状态中
    performance_materiality_cents: int | None  # 执行重要性, 同上
    clearly_trivial_cents: int | None  # 明显微小门槛, 同上
    audit_objectives: list[AuditObjective]  # 审计目标, 读数据集预置
    risk_register: list[RiskItem]  # 风险清单, 读数据集预置, 闸门可改等级
    # 总体审计计划 PLAN-{revision} (需求 R203, 功能 planning): A4 PLN-15 起草, PLN-15 闸门批准; 当前计划为 status 不是
    # superseded 的修订中 revision 最大的 (第 0 节)
    audit_plans: Annotated[list[AuditPlan], upsert_by_id]
    procedure_pack: list[Procedure]  # 程序包: 全部程序实例, 每次写入传完整列表
    schedule_table: list[ScheduleRow]  # P2 调度表
    documents: Annotated[list[DocumentRef], replace_by_id]  # 证据文件, 同 id 后到者替换
    staged_results: Annotated[list[StagedResult], staged]  # fieldwork 实例的返回, fan_in 校验后并入并清空
    procedure_runs: Annotated[list[ProcedureRun], upsert_by_id]  # 执行记录, 按 id 与版本合并
    exceptions: Annotated[list[AuditException], upsert_by_id]  # 例外, 按 id 与版本合并
    judgments: Annotated[list[Judgment], upsert_by_id]  # 判断, 按 id 与版本合并
    review_tasks: Annotated[list[ReviewTask], upsert_by_id]  # 复核任务, 按 id 与版本合并
    confirmations: Annotated[list[Confirmation], upsert_by_id]  # 函证, 按 id 与版本合并
    ownership_graph: OwnershipGraph | None  # 股权与任职关系图 (OWNERSHIP), A5 GEN-08 写 (需求 R136)
    report_versions: Annotated[list[AuditReportVersion], upsert_by_id]  # 报告版本, 按 id 与版本合并
    review_records: Annotated[list[ReviewRecord], append_unique]  # 复核决定, 只追加
    gate_decisions: Annotated[list[GateDecision], append_unique]  # 闸门决定, 只追加
    # 错报, 按 id 与版本合并 (需求 R156: 分类, 沟通, 更正与验证各出新版本; 原为只追加, 旧检查点照常读回)
    misstatements: Annotated[list[Misstatement], upsert_by_id]
    rerun_requests: Annotated[list[RerunRequest], append_unique]  # 过期程序的重跑请求, 只追加 (DecisionSubmit 写)
    # 更正通知 (需求 R162), 只追加: DecisionSubmit 写 Store, 等待更正的节点读回后并入
    correction_notices: Annotated[list[CorrectionNotice], append_unique]
    uncorrected_schedule: UncorrectedSchedule | None  # 未更正错报汇总表, A9 写
    # 签字前提的四类审计产出 (需求 R215): 声明书 REPLETTER (A5 FIN-07), 合规检查 CHECK-{程序} (A9 FIN-10, FIN-18),
    # 治理层沟通 GC-{程序}-{run_no} (A10 RPT-07), 独立性声明 S307C (A10 RPT-08 起草, RPT-01 签字时由 A1 置 signed)
    representation_letter: WrittenRepresentationLetter | None
    compliance_checks: Annotated[list[ComplianceCheck], upsert_by_id]  # 按 id 与版本合并
    governance_communications: Annotated[list[GovernanceCommunication], upsert_by_id]  # 按 id 与版本合并
    independence_declaration: IndependenceDeclaration | None
    opinion_route: OpinionType | None  # FIN-01 确认的意见类型; 只在图状态中


# 项目启动时由 initial_state 的参数给出的键, 检查点里一定有, 不登记缺省值
REQUIRED_KEYS = ("engagement_id", "phase", "version_snapshot", "client_profile_version", "procedure_pack")

# 其余各键的缺省值: 列表为 [], 单对象与 int | None 为 None. 新增状态字段须在此登记 (第 3 节字段登记清单):
# 旧检查点没有该键时, 节点, 路由, 查询, 决定提交与恢复补做经 fill_defaults 读到缺省值
STATE_DEFAULTS: dict[str, list[Any] | None] = {
    "acceptance_scorecard": None,
    "engagement_terms": None,
    "analytics": None,
    "materiality_candidates": None,
    "materiality_cents": None,
    "performance_materiality_cents": None,
    "clearly_trivial_cents": None,
    "audit_objectives": [],
    "risk_register": [],
    "audit_plans": [],
    "schedule_table": [],
    "documents": [],
    "staged_results": [],
    "procedure_runs": [],
    "exceptions": [],
    "judgments": [],
    "review_tasks": [],
    "confirmations": [],
    "ownership_graph": None,
    "report_versions": [],
    "review_records": [],
    "gate_decisions": [],
    "misstatements": [],
    "rerun_requests": [],
    "correction_notices": [],
    "uncorrected_schedule": None,
    "representation_letter": None,
    "compliance_checks": [],
    "governance_communications": [],
    "independence_declaration": None,
    "opinion_route": None,
}


def fill_defaults(values: Mapping[str, Any]) -> EngagementState:
    """补上缺的登记键 (列表各取一个新的空列表), 返回新字典, 不改入参.

    检查点里缺键的情形: 加字段之前存的检查点, 或没有 reducer 的通道从未写过. 带 reducer 的通道
    LangGraph 读回时自行补空列表, 照样经这里. 未启动的项目 (检查点为空) 只补登记的键, 不补 REQUIRED_KEYS.
    """
    filled = dict(values)
    for key, default in STATE_DEFAULTS.items():
        if key not in filled:
            filled[key] = copy.copy(default)
    return cast(EngagementState, filled)


def initial_state(engagement: Engagement, procedures: list[Procedure], client_profile_version: int) -> EngagementState:
    """runner.start 的图输入: 固定集程序在此实例化, 客户画像取项目创建时的版本, 其余字段取 STATE_DEFAULTS."""
    return fill_defaults(
        {
            "engagement_id": engagement.id,
            "phase": engagement.phase,
            "version_snapshot": dict(engagement.version_snapshot),
            "client_profile_version": client_profile_version,
            "procedure_pack": procedures,
        }
    )
