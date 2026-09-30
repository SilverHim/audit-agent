"""程序库: 加载程序 YAML 与准则片段, trigger 匹配, 依赖闭包, 参数解析 (A 负责)."""

import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from backend.contracts.knowledge import ProcedureTemplate, StandardFragment
from backend.contracts.models import AuditObjective, Engagement, RiskLevel
from backend.contracts.state import EngagementState

STATE_REFS = {"materiality_cents", "performance_materiality_cents", "clearly_trivial_cents"}
ENGAGEMENT_REFS = {"period_start", "period_end"}


class ProcedureLibrary:
    """程序库: 加载程序 YAML 与准则片段, 装配时匹配 trigger 并补齐依赖."""

    def __init__(self, root: Path) -> None:
        self.root = root  # knowledge 目录
        self._templates = {  # 程序编号 -> 模板
            t.id: t
            for t in (
                ProcedureTemplate.model_validate(yaml.safe_load(p.read_text(encoding="utf-8")))
                for p in sorted((root / "procedures").glob("*.yaml"))
            )
        }
        self._fragments: dict[str, StandardFragment] = {}  # 片段 ID -> 准则片段
        for p in sorted((root / "standards").glob("*.yaml")):
            doc = yaml.safe_load(p.read_text(encoding="utf-8"))
            for f in doc["fragments"]:
                self._fragments[f["id"]] = StandardFragment(standard=doc["standard"], **f)
        self.commit = _git_commit(root)  # knowledge 目录当前的 git commit

    def load(self, procedure_id: str) -> ProcedureTemplate:
        return self._templates[procedure_id]

    def all(self) -> list[ProcedureTemplate]:
        return list(self._templates.values())

    def match(self, objective: AuditObjective) -> list[ProcedureTemplate]:
        return [
            t
            for t in self._templates.values()
            if any(
                tr.account == objective.account
                and objective.assertion in tr.assertions
                and objective.risk_level in tr.level
                for tr in t.triggers
            )
        ]

    def closure(self, ids: Iterable[str]) -> list[ProcedureTemplate]:
        """补齐 depends_on, 含自身, 按拓扑序 (前置在前). 结果可含 P2 以外的前置, 如 DOC-04."""
        ordered: list[str] = []
        visiting: set[str] = set()

        def visit(pid: str) -> None:
            if pid in ordered:
                return
            if pid in visiting:
                raise ValueError(f"程序依赖成环:{pid}")
            visiting.add(pid)
            for dep in self._templates[pid].depends_on:
                visit(dep)
            visiting.discard(pid)
            ordered.append(pid)

        for pid in ids:
            visit(pid)
        return [self._templates[pid] for pid in ordered]

    def resolve_params(
        self, template: ProcedureTemplate, engagement: Engagement, state: EngagementState, risk_level: RiskLevel
    ) -> dict[str, Any]:
        """params 取值: 字面量, {by_risk: {...}} 或 {ref: 名称}."""
        out: dict[str, Any] = {}
        for key, value in template.params.items():
            if isinstance(value, dict) and "by_risk" in value:
                out[key] = value["by_risk"][risk_level.value]
            elif isinstance(value, dict) and "ref" in value:
                ref = value["ref"]
                if ref in ENGAGEMENT_REFS:
                    out[key] = getattr(engagement, ref)
                elif ref in STATE_REFS:
                    out[key] = state[ref]  # type: ignore[literal-required]
                else:
                    raise KeyError(f"{template.id}.{key}: 未知 ref {ref}")
            else:
                out[key] = value
        return out

    def fragment(self, fragment_id: str) -> StandardFragment | None:
        return self._fragments.get(fragment_id)


def _git_commit(path: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=5
        )
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
