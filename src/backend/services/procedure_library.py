"""程序库: 加载程序 YAML 与准则片段, trigger 匹配, 依赖闭包, 参数解析 (A 负责)."""

import copy
import hashlib
import os
import subprocess
import types
from collections.abc import Iterable
from pathlib import Path
from typing import Any, TypeGuard, Union, get_args, get_origin

import yaml
from pydantic import BaseModel, ValidationError

from backend.contracts.knowledge import ProcedureTemplate, StandardFragment
from backend.contracts.models import (
    AuditObjective,
    DocumentType,
    Engagement,
    OpinionType,
    Phase,
    ReportSection,
    RiskLevel,
)
from backend.contracts.records import DatasetTraits
from backend.contracts.state import EngagementState
from backend.services.data_quality import REQUIRED_TABLES
from backend.services.features import BASELINE, FEATURES, check_features

# 参数 {ref: 名称} 可用的名称. 不带前缀的: 图状态的重要性 (为 None 时不执行) 与项目期间
STATE_REFS = {"materiality_cents", "performance_materiality_cents", "clearly_trivial_cents"}
ENGAGEMENT_REFS = {"period_start", "period_end"}
# 带命名空间的: 前缀 → 取值的模型, 写成 {前缀}.{字段}, 字段是嵌套模型 (或可为空的嵌套模型) 时可再往下取 (如
# {前缀}.{字段}.{子字段}); 列表, 字典等容器字段只能整个取. dataset. 的值为 None 时以 None 执行; 新命名空间登记时一并定
# 它的 None 策略与解析时传入的对象 (resolve_params 的 sources), 如 profile. 为 None 抛 ValueError (R127)
REF_NAMESPACES: dict[str, type[BaseModel]] = {"dataset": DatasetTraits}
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
    """程序库: 加载程序 YAML 与准则片段, 装配时匹配 trigger 并补齐依赖.

    features: 启用的功能 (services/features.py), 缺省为基线. 模板的 feature 为空或在 features 中即启用; all, match,
    closure 只看启用的模板 (固定集与装配因此不含未启用的程序), load 不限. 启用的模板对未启用模板的依赖一律丢弃
    (启用视图中的 depends_on 不含它们, 不分 P2, P3). 功能集或模板的 feature 有未登记的名字时抛 ValueError.
    """

    def __init__(self, root: Path, features: Iterable[str] = BASELINE) -> None:
        self.root = root  # knowledge 目录
        self.features = check_features(features)  # 启用的功能
        templates = [load_template(p) for p in sorted((root / "procedures").glob("*.yaml"))]
        unknown = [f"{t.id}: {t.feature}" for t in templates if t.feature is not None and t.feature not in FEATURES]
        if unknown:
            raise ValueError(f"程序模板的功能未登记: {', '.join(unknown)}")
        wrong = fixed_problems(templates)
        if wrong:
            raise ValueError(f"固定集程序写法不对: {'; '.join(wrong)}")
        # P2 以外的程序在所在节点内等人, 失败任务只收 rerun (nodes.read_rerun); 开 manual_result 会被 DecisionSubmit
        # 收下而节点读回时报错, 等待卡住, 所以加载时就拒绝
        manual = [t.id for t in templates if t.manual_result_allowed and t.phase is not Phase.P2_Executing]
        if manual:
            raise ValueError(f"P2 以外的程序不能开 manual_result_allowed (失败只能 rerun): {', '.join(manual)}")
        refs = [
            f"{t.id}.{key}: {problem}"
            for t in templates
            for key, value in t.params.items()
            if isinstance(value, dict) and "ref" in value and (problem := ref_problem(value["ref"])) is not None
        ]
        if refs:
            raise ValueError(f"程序参数的 ref 写错: {'; '.join(refs)}")
        wrong = inputs_problems(templates)
        if wrong:
            raise ValueError(f"程序模板的 inputs 不对: {'; '.join(wrong)}")
        enabled = {t.id for t in templates if t.feature is None or t.feature in self.features}
        disabled = {t.id for t in templates} - enabled
        self._templates = {  # 程序编号 -> 启用的模板 (启用视图: depends_on 去掉未启用的程序)
            t.id: t.model_copy(update={"depends_on": [d for d in t.depends_on if d not in disabled]})
            if disabled & set(t.depends_on)
            else t
            for t in templates
            if t.id in enabled
        }
        self._all = {t.id: self._templates.get(t.id, t) for t in templates}  # 程序编号 -> 模板, 含未启用的
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
        """取模板, 含未启用的 (旧项目的程序包与留痕可能引用它们); 启用的取启用视图. 没有该程序抛 KeyError."""
        return self._all[procedure_id]

    def all(self, include_disabled: bool = False) -> list[ProcedureTemplate]:
        """启用的模板, 按文件名顺序; include_disabled 时含未启用的 (如 build_agents 检查缺模板)."""
        return list((self._all if include_disabled else self._templates).values())

    def enabled(self, procedure_id: str) -> bool:
        """该程序的模板是否启用."""
        return procedure_id in self._templates

    def fixed(self) -> list[ProcedureTemplate]:
        """启用的 P2 固定集模板 (fixed), 按文件名顺序: 装配时不论审计目标一律加入."""
        return [t for t in self._templates.values() if t.fixed]

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
        """补齐 depends_on, 含自身, 按拓扑序 (前置在前). 结果可含 P2 以外的前置, 如 DOC-04.
        只在启用的模板中补: 依赖按启用视图, ids 中有未启用的程序时抛 KeyError."""
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
        *,
        traits: DatasetTraits,
    ) -> dict[str, Any]:
        """解析模板参数. 每个参数三种写法: 字面量, {by_risk: {等级: 值}} 或 {ref: 名称}.

        risk_level: 程序关联审计目标的最高等级 (见 highest_risk), 没有关联目标为 None.
        traits: 项目数据集的声明, dataset. 命名空间的 ref 从它取.
        by_risk 参数在 risk_level 为 None 或没有该等级的取值时抛 ValueError.
        ref 指向的状态值为 None (如重要性还没经闸门确定) 时抛 ValueError, 不以 None 执行; dataset. 的值为 None
        时照样以 None 执行; 名称写错 (见 ref_problem) 抛 KeyError.
        字面量与取自声明的值深拷贝, 改返回值不会改到模板与声明.
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
                problem = ref_problem(ref)
                if problem is not None:
                    raise KeyError(f"{template.id}.{key}: {problem}")
                if ref in ENGAGEMENT_REFS:
                    out[key] = getattr(engagement, ref)
                elif ref in STATE_REFS:
                    resolved = state[ref]  # type: ignore[literal-required]
                    if resolved is None:
                        raise ValueError(f"{template.id}.{key}: {ref} 尚未确定")
                    out[key] = resolved
                else:
                    out[key] = namespace_value({"dataset": traits}, ref)
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


def is_model(annotation: Any) -> TypeGuard[type[BaseModel]]:
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def nested_model(annotation: Any) -> type[BaseModel] | None:
    """ref 路径可往下取的模型: 字段类型本身是模型, 或是含模型的 Union (如 PayrollAccounts | None 中的 PayrollAccounts).
    list[模型], dict[str, 模型] 等容器不拆 (按字段名取不到容器里的元素), 与非模型字段一样为 None."""
    if is_model(annotation):
        return annotation
    if get_origin(annotation) in (Union, types.UnionType):
        return next((a for a in get_args(annotation) if is_model(a)), None)
    return None


def ref_problem(ref: object) -> str | None:
    """ref 名称写错时的说明 (含该 ref), 合格为 None. 不带前缀的须在 STATE_REFS 或 ENGAGEMENT_REFS; 带前缀的命名空间须
    已登记 (REF_NAMESPACES), 之后的字段路径逐层须是模型的字段 (嵌套模型的字段再往下一层, 如
    dataset.payroll_accounts.wages_expense)."""
    if not isinstance(ref, str):
        return f"ref 须为文本: {ref!r}"
    if ref in STATE_REFS or ref in ENGAGEMENT_REFS:
        return None
    namespace, _, path = ref.partition(".")
    model: type[BaseModel] | None = REF_NAMESPACES.get(namespace)
    if model is None:
        return f"未知 ref {ref}"
    if not path:
        return f"ref {ref} 缺字段, 写成 {namespace}.<字段>"
    for name in path.split("."):
        if model is None or name not in model.model_fields:
            return f"未知 ref {ref}"
        model = nested_model(model.model_fields[name].annotation)
    return None


def namespace_value(sources: dict[str, BaseModel], ref: str) -> Any:
    """命名空间 ref 的值 (名称已经 ref_problem 查过): 沿字段路径取, 中途为 None 即为 None; 模型转成字典, 其余深拷贝."""
    namespace, _, path = ref.partition(".")
    value: Any = sources[namespace]
    for name in path.split("."):
        if value is None:
            return None
        value = getattr(value, name)
    return value.model_dump() if isinstance(value, BaseModel) else copy.deepcopy(value)


def load_template(path: Path) -> ProcedureTemplate:
    """读一个程序 YAML. 字段校验不过时抛 ValueError, 消息带程序编号 (YAML 的 id, 没有时取文件名) 与文件名."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    try:
        return ProcedureTemplate.model_validate(raw)
    except ValidationError as e:
        pid = raw.get("id") if isinstance(raw, dict) else None
        raise ValueError(f"程序模板 {pid or path.stem} ({path.name}) 写法不对: {e}") from e


def inputs_problems(templates: Iterable[ProcedureTemplate]) -> list[str]:
    """模板 inputs 的写法问题 (需求 R272, 消息含程序编号, 含未启用的模板): 不能写就绪必需的表 (由 P1 等数据保证),
    不能写文件类型 other (分类前的类型). 取值须为 TableKind 与 DocumentType 由字段校验保证 (见 load_template).
    P2 以外的程序写了 inputs 须在执行阶段能补跑, 要看图节点, 在 GraphNodes 构造时查 (orchestration.inputs)."""
    problems: list[str] = []
    for t in templates:
        required = [k.value for k in t.inputs.tables if k in REQUIRED_TABLES]
        if required:
            problems.append(f"{t.id}: inputs 不能写就绪必需的表 {', '.join(required)} (由 P1 等数据保证)")
        if DocumentType.other in t.inputs.documents:
            problems.append(f"{t.id}: inputs 不能写文件类型 other")
    return problems


def fixed_problems(templates: Iterable[ProcedureTemplate]) -> list[str]:
    """固定集模板的写法问题 (消息含程序编号): fixed 只用于 P2 程序; fixed 程序不对应审计目标, 不写 triggers; 没有关联
    审计目标, 取不到风险等级, 自身与经依赖一并加入的 P2 程序 (依赖闭包) 的参数都不能写 {by_risk: ...}."""
    by_id = {t.id: t for t in templates}

    def by_risk(t: ProcedureTemplate) -> list[str]:
        return [key for key, value in t.params.items() if isinstance(value, dict) and "by_risk" in value]

    def p2_dependencies(t: ProcedureTemplate) -> list[ProcedureTemplate]:
        """t 的依赖闭包中的 P2 程序 (不含 t), 按首次遇到的顺序; 找不到的依赖由加载的其他校验报出, 这里跳过."""
        found: list[ProcedureTemplate] = []
        pending = list(t.depends_on)
        while pending:
            dep = by_id.get(pending.pop(0))
            if dep is None or dep in found or dep is t:
                continue
            if dep.phase is Phase.P2_Executing:
                found.append(dep)
            pending += dep.depends_on
        return found

    problems: list[str] = []
    for t in by_id.values():
        if not t.fixed:
            continue
        if t.phase is not Phase.P2_Executing:
            problems.append(f"{t.id} 不是 P2 程序, 不能标 fixed")
        if t.triggers:
            problems.append(f"{t.id} 标了 fixed, 不能再写 triggers: 固定集程序不对应审计目标")
        problems += [f"{t.id}.{key} 按风险等级取值, 但固定集程序没有关联审计目标" for key in by_risk(t)]
        problems += [
            f"{t.id} 依赖的 {dep.id}.{key} 按风险等级取值, 只经固定集加入时没有关联审计目标"
            for dep in p2_dependencies(t)
            for key in by_risk(dep)
        ]
    return problems


def git_env() -> dict[str, str]:
    """git 子进程的环境: 去掉 GIT_DIR 等 GIT_* 变量. 在 git 钩子里运行时 (如 pre-push 跑测试) 这些变量指向外层仓库,
    继承下来会让 -C 指定的目录失效, 读错仓库甚至改写外层仓库的配置."""
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _git(path: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=True, timeout=5, env=git_env()
    )
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
