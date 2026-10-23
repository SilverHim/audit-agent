"""审计助理的定界操作: 提案只保存范围, 显式授权后由决定入口执行."""

from datetime import datetime
from typing import Literal

from pydantic import Field

from backend.contracts.models import Model

AssistantAction = Literal[
    "evidence_unavailable", "defer", "resume", "confirm_reviews", "recognize_misstatements", "rerun_stale"
]
AssistantTargetKind = Literal["evidence_request", "review_task", "data_request", "procedure"]


class AssistantTarget(Model):
    kind: AssistantTargetKind
    id: str
    version: int | None = None
    fingerprint: str
    label: str
    conclusion: str | None = None
    suggestion: str | None = None
    procedure_id: str | None = None


class AssistantPlan(Model):
    id: str
    action: AssistantAction
    targets: list[AssistantTarget] = Field(min_length=1, max_length=50)
    reason: str | None = None
    instruction: str
    proposed_for: str
    created_at: datetime
    completed_at: datetime | None = None
    status: Literal["proposed", "completed", "cancelled"] = "proposed"


class AssistantPlanIn(Model):
    action: AssistantAction
    target_kind: AssistantTargetKind = "evidence_request"
    target_ids: list[str] = Field(default_factory=list, max_length=50)
    reason: str | None = Field(default=None, max_length=2000)


class AssistantExecuteIn(Model):
    reason: str | None = Field(default=None, max_length=2000)


class AssistantReceipt(Model):
    plan_id: str
    action: AssistantAction
    target_ids: list[str]
    authorized_by: str
    executed_by: Literal["audit_assistant"] = "audit_assistant"
    completed_at: datetime
    reason: str | None = None
    status: Literal["completed"] = "completed"
