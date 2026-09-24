"""服务之间传递的数据对象，定义见 开发接口文档.md 第 5 节。"""

from typing import Any, Literal

from backend.contracts.models import AuditException, Judgment, Model


class RuleResult(Model):
    exceptions: list[AuditException]
    rows: dict[str, list[dict[str, Any]]]


class JudgmentContext(Model):
    subject_id: str
    task: dict[str, Any]
    data: dict[str, Any]
    rule_findings: list[AuditException] = []
    fixture_key: str


class JudgmentOutcome(Model):
    judgment: Judgment | None
    failure: Literal["failed", "unattributed"] | None
    attempts: int
    trail_ids: list[str]


class ValidationReport(Model):
    level_failed: Literal["syntax", "structure", "reference", "semantic"] | None
    retryable: bool
    unsupported_steps: list[int]
    errors: list[str]
