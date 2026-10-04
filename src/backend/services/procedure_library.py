"""程序库: 加载程序 YAML 与准则片段, trigger 匹配, 依赖闭包, 参数解析 (A 负责)."""

import copy
import hashlib
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from backend.contracts.knowledge import ProcedureTemplate, StandardFragment
from backend.contracts.models import AuditObjective, Engagement, OpinionType, ReportSection, RiskLevel
from backend.contracts.state import EngagementState

STATE_REFS = {"materiality_cents", "performance_materiality_cents", "clearly_trivial_cents"}
ENGAGEMENT_REFS = {"period_start", "period_end"}
RISK_ORDER = list(RiskLevel)  # 风险等级由低到高: low, medium, high, significant (枚举定义顺序)


def highest_risk(objectives: Iterable[AuditObjective], objective_ids: Iterable[str]) -> RiskLevel | None:
    """程序针对的审计目标中最高的风险等级, 用来取 by_risk 参数. 程序没有关联审计目标时返回 None.

    objectives: 状态中的全部审计目标; objective_ids: 程序关联的审计目标 ID (Procedure.objective_ids).
    关联的 ID 在 objectives 中找不到时抛 LookupError: 程序包与审计目标对不上, 按缺失的目标取值会算错参数.
    """
    by_id = {o.id: o for o in objectives}
    ids = list(objective_ids)
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise LookupError(f"审计目标不存在: {', '.join(missing)}")
    levels = [by_id[i].risk_level for i in ids]
    return max(levels, key=RISK_ORDER.index) if levels else None


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
        self.commit = _git_commit(root)  # knowledge 目录当前的 git commit, 有未提交改动时加 -dirty
        report = yaml.safe_load((root / "templates" / "report.yaml").read_text(encoding="utf-8"))
        self._report: dict[OpinionType, dict[ReportSection, str]] = {  # 意见类型 -> 报告固定段落 (含占位符)
            OpinionType(opinion): {ReportSection(name): text for name, text in sections.items()}
            for opinion, sections in report.items()
        }

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
        self,
        template: ProcedureTemplate,
        engagement: Engagement,
        state: EngagementState,
        risk_level: RiskLevel | None,
    ) -> dict[str, Any]:
        """解析模板参数. 每个参数三种写法: 字面量, {by_risk: {等级: 值}} 或 {ref: 名称}.

        risk_level: 程序关联审计目标的最高等级 (见 highest_risk), 没有关联目标为 None.
        by_risk 参数在 risk_level 为 None 或没有该等级的取值时抛 ValueError.
        ref 指向的状态值为 None (如重要性还没经闸门确定) 时抛 ValueError, 不以 None 执行; 未知的 ref 名称抛 KeyError.
        字面量深拷贝, 改返回值不会改到模板.
        """
        out: dict[str, Any] = {}
        for key, value in template.params.items():
            if isinstance(value, dict) and "by_risk" in value:
                if risk_level is None:
                    raise ValueError(f"{template.id}.{key}: 按风险等级取值, 但程序没有关联审计目标")
                if risk_level.value not in value["by_risk"]:
                    raise ValueError(f"{template.id}.{key}: 没有 {risk_level.value} 等级的取值")
                out[key] = value["by_risk"][risk_level.value]
            elif isinstance(value, dict) and "ref" in value:
                ref = value["ref"]
                if ref in ENGAGEMENT_REFS:
                    out[key] = getattr(engagement, ref)
                elif ref in STATE_REFS:
                    resolved = state[ref]  # type: ignore[literal-required]
                    if resolved is None:
                        raise ValueError(f"{template.id}.{key}: {ref} 尚未确定")
                    out[key] = resolved
                else:
                    raise KeyError(f"{template.id}.{key}: 未知 ref {ref}")
            else:
                out[key] = copy.deepcopy(value)
        return out

    def fragment(self, fragment_id: str) -> StandardFragment | None:
        return self._fragments.get(fragment_id)

    def report_sections(self, opinion: OpinionType) -> dict[ReportSection, str]:
        """templates/report.yaml 中该意见类型的固定段落 (董事责任, 审计师责任, 其他法律法规要求, 签名块).

        正文含占位符 {entity_name}, {period_end}, {auditor_name}, 由 RPT-03 代入.
        """
        return dict(self._report[opinion])

    def prompts_sha256(self) -> str:
        """prompts/*.j2 按文件名排序后, 各文件内容依次拼接的 sha256. 每次调用重新读文件, 改任一 prompt 即变化."""
        digest = hashlib.sha256()
        for p in sorted((self.root / "prompts").glob("*.j2"), key=lambda p: p.name):
            digest.update(p.read_bytes())
        return digest.hexdigest()


def _git(path: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, check=True, timeout=5)
    return out.stdout.strip()


def _git_commit(path: Path) -> str:
    """path 所在仓库的 HEAD; path 下有未提交的改动 (含未跟踪文件) 时加 -dirty; 不在 git 仓库中或无 git 时为 unknown."""
    try:
        commit = _git(path, "rev-parse", "HEAD")
        # 显式列出未跟踪文件, 不受 status.showUntrackedFiles 配置影响
        dirty = _git(path, "status", "--porcelain", "--untracked-files=all", "--", ".")
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return f"{commit}-dirty" if dirty else commit
