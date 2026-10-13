"""HTTP 层的请求与响应模型. 领域对象仍用 contracts; 这里只放接口文档第 10 节点名由 C 定义的形状."""

from datetime import date, datetime
from typing import Any

from backend.contracts.models import (
    Annotation,
    AuditException,
    AuditReportVersion,
    Decision,
    GateDecision,
    GateEdit,
    GateId,
    GateVerdict,
    Judgment,
    ManualResult,
    MaterialityChoice,
    Misstatement,
    MisstatementDraft,
    Model,
    OpinionType,
    Phase,
    Procedure,
    ProcedureRun,
    ReviewRecord,
    ReviewTask,
    UncorrectedSchedule,
)


class Token(Model):
    """登录成功返回的访问令牌."""

    access_token: str
    token_type: str = "bearer"


class EngagementCreate(Model):
    """新建项目. 客户名称与 ABN 不在此传, 取数据集里的客户画像."""

    period_start: date
    period_end: date
    dataset: str


class EngagementSummary(Model):
    """项目列表中的一行."""

    id: str
    client_name: str
    phase: Phase
    current_gate: GateId | None
    pending_tasks: int
    updated_at: datetime


class IdBody(Model):
    """202 响应: 刚写入的决定 ID."""

    id: str


class GateDecisionIn(Model):
    """闸门决定请求体: 去掉 id, version, decided_by, decided_at, 由服务端填写."""

    gate: GateId
    target_id: str
    target_version: int
    decision: GateVerdict
    materiality: MaterialityChoice | None = None
    edits: list[GateEdit] = []
    disposition: OpinionType | None = None
    annotations: list[Annotation] = []
    reason: str | None = None


class ReviewRecordIn(Model):
    """复核决定请求体: 去掉 id, version, reviewed_by, decided_at."""

    task_id: str
    target_version: int
    decision: Decision
    reason: str | None = None
    override_conclusion: str | None = None
    misstatement_draft: MisstatementDraft | None = None
    rerun_params: dict[str, Any] | None = None
    manual_result: ManualResult | None = None


class ClientProfileIn(Model):
    """更新客户资料. id 由服务端取项目的 client_id; version 为当前版本 +1."""

    version: int
    legal_name: str
    abn: str
    entity_type: str
    industry_anzsic: str
    listed: bool
    directors: list[str]
    notes: str = ""


class ReviewTaskView(Model):
    """复核队列中的一项: 任务, 程序名, 目标对象, 允许的决定."""

    task: ReviewTask
    procedure_name: str
    target: Judgment | AuditException | ProcedureRun
    allowed_decisions: list[Decision]


class ProcedureDetail(Model):
    """一条程序的执行记录, 例外与判断."""

    procedure: Procedure
    name: str
    depends_on: list[str]
    standards: list[str]
    runs: list[ProcedureRun]
    exceptions: list[AuditException]
    judgments: list[Judgment]


class JudgmentDetail(Model):
    """一条判断的当前版本, 历史版本与相关复核决定."""

    judgment: Judgment
    versions: list[Judgment]
    review_records: list[ReviewRecord]


class MisstatementReport(Model):
    """错报列表, 未更正汇总, 以及总览里的重要性与意见."""

    items: list[Misstatement]
    schedule: UncorrectedSchedule | None
    materiality_cents: int | None
    opinion_route: OpinionType | None


class ReportBundle(Model):
    """报告各修订版, 以及全部 RPT-01 决定 (含批注)."""

    versions: list[AuditReportVersion]
    decisions: list[GateDecision]
