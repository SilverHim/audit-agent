"""程序库: 加载程序 YAML, 准则片段与底稿配置, trigger 匹配, 依赖闭包, 参数解析 (A 负责)."""

import copy
import hashlib
import json
import os
import re
import string
import subprocess
import types
from collections.abc import Callable, Iterable, Mapping
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn, TypeGuard, Union, cast, get_args, get_origin

import yaml
from jinja2 import (
    Environment,
    FileSystemLoader,
    StrictUndefined,
    TemplateSyntaxError,
    Undefined,
    meta,
    nodes,
    pass_context,
)
from jinja2.runtime import Context
from pydantic import BaseModel, ValidationError
from pydantic_core import to_jsonable_python

from backend.contracts import llm_outputs
from backend.contracts.data import DataInput
from backend.contracts.knowledge import (
    ProcedureTemplate,
    ReportTemplate,
    RequiredItem,
    StandardFragment,
    StandardRequirement,
    WorkpaperConfig,
    WorkpaperKind,
)
from backend.contracts.llm_outputs import JudgmentDraft
from backend.contracts.manual_results import RESULT_MODELS, ManagementResponses
from backend.contracts.models import (
    AuditObjective,
    AutonomyLevel,
    ClientProfile,
    DocumentType,
    Engagement,
    Engine,
    LocalizedText,
    OpinionType,
    Phase,
    Procedure,
    ReportSection,
    RiskItem,
    RiskLevel,
    TaskKind,
    WorkingLanguage,
)
from backend.contracts.records import DatasetTraits
from backend.contracts.state import EngagementState
from backend.services.adjusted_tb import verified_entry_nos
from backend.services.currency import format_money
from backend.services.data_quality import REQUIRED_TABLES
from backend.services.features import BASELINE, FEATURES, check_features

FIN08 = "FIN-08"  # 错报沟通: 首次沟通时间取它的登记 (first_misstatement_communicated_at)
# 参数 {ref: 名称} 可用的名称. 不带前缀的: 图状态的重要性 (为 None 时不执行), 项目期间与派生引用 (DERIVED_REFS)
STATE_REFS = {"materiality_cents", "performance_materiality_cents", "clearly_trivial_cents"}
ENGAGEMENT_REFS = {"period_start", "period_end"}
# 带命名空间的: 前缀 → 取值的模型, 写成 {前缀}.{字段}, 字段是嵌套模型 (或可为空的嵌套模型) 时可再往下取 (如
# {前缀}.{字段}.{子字段}); 列表, 字典等容器字段只能整个取. 新命名空间登记时一并定它的 None 策略 (NONE_RUNS), 取值形式
# (JSON_NAMESPACES), 缺取值对象时的说明 (MISSING_SOURCES) 与解析时传入的对象 (resolve_params 的 sources):
# dataset. 为项目数据集的声明, profile. 为本项目采用版本的客户画像 (R127)
REF_NAMESPACES: dict[str, type[BaseModel]] = {"dataset": DatasetTraits, "profile": ClientProfile}
NONE_RUNS = {"dataset"}  # 值为 None 时以 None 执行的命名空间; 其余为 None 时抛 ValueError, 除非参数写了 optional: true
OPTIONAL_NAMESPACES = {"profile"}  # 参数可写 {ref: ..., optional: true} (为 None 时以 None 执行) 的命名空间
JSON_NAMESPACES = {"profile"}  # 取值一律转成 JSON 兼容的值 (模型与其中的列表, 字典都成字典, 日期成文本, 枚举成值)
MISSING_SOURCES = {"profile": "没有本项目采用的客户画像"}  # 命名空间缺取值对象时的说明
# 带错报更新类别的程序允许的自主度 (update_autonomy_problems): L2 及以下 A7 按跳过规则不复核, 不出争议任务
LOW_AUTONOMY = frozenset({AutonomyLevel.L1, AutonomyLevel.L2})
RISK_ORDER = list(RiskLevel)  # 风险等级由低到高: low, medium, high, significant (枚举定义顺序)
# 规则 SQL 里出现的账表名: 按 DataInput 的取值整词扫描 (长的在前), 逗号连接与子查询里的表也认得出
SQL_TABLE = re.compile(
    r"\b(" + "|".join(sorted((i.value for i in DataInput), key=len, reverse=True)) + r")\b", re.IGNORECASE
)
# SQL 的绑定参数 :name, 写法同 SQLAlchemy text() 的认法 (PostgreSQL 的 :: 类型转换不算)
SQL_BIND = re.compile(r"(?<![:\w\\]):(\w+)(?!:)")


def json_default(value: object) -> NoReturn:
    """tojson 遇到不能序列化的对象时调用: 缺变量 (StrictUndefined) 抛 UndefinedError, 其余仍抛 TypeError.

    StrictUndefined 只在取值, 转字符串等操作时报错, json.dumps 只会说它不能序列化, 看不出是缺了变量.
    """
    if isinstance(value, Undefined):
        str(value)  # StrictUndefined 转字符串即抛 UndefinedError, 消息写明缺的是哪个变量
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def days_before(day: date | str, days: int) -> date:
    """过滤器 days_before (需求 R324): 日期往前推 days 天, 如 {{ period_end | days_before(window_days) }}. 日期可为
    ISO 文本 (参数快照经 Store 读回后日期为文本)."""
    return (day if isinstance(day, date) else date.fromisoformat(day)) - timedelta(days=days)


@pass_context
def money(context: Context, cents: int | None, currency: str | None = None) -> str:
    """过滤器 money (需求 R366): 金额 (分) 按记账币种写, 如 {{ x.amount_cents | money }} → "AUD 1,234.56"
    (services.currency.format_money, 带该币种通常的小数位). 币种取参数, 没给取渲染变量 currency (判断引擎按项目的
    记账币种给, JudgmentEngine.call_llm), 都没有为澳元; 金额为空写 null."""
    if cents is None:
        return "null"
    code = currency or context.get("currency")
    return format_money(int(cents), code) if isinstance(code, str) and code else format_money(int(cents))


def prompt_env(root: Path | None = None) -> Environment:
    """知识文件里 Jinja2 模板的环境: 提示词 (root 为模板目录), 与程序 YAML 带参数占位的文字 (不给 root, 见
    render_text). 去掉块标签所在行的换行与行首空白, 缺变量即报错 (含经 tojson 输出的), 不做 HTML 转义; 另有过滤器
    days_before 与 money."""
    env = Environment(
        loader=FileSystemLoader(root) if root is not None else None,
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=StrictUndefined,
        autoescape=False,
    )
    env.policies["json.dumps_kwargs"] = {"sort_keys": True, "default": json_default}  # sort_keys 同 Jinja2 缺省
    env.filters["days_before"] = days_before
    env.filters["money"] = money
    return env


TEXT_ENV = prompt_env()  # 程序 YAML 带参数占位的文字 (范围说明, 总体说明) 的环境


def render_text(text: LocalizedText, params: Mapping[str, Any]) -> LocalizedText:
    """程序 YAML 带参数占位的文字 (scope 与规则总体的 description, 需求 R324) 按一次执行的参数快照渲染, 纯函数.
    占位只能用 params 中的参数, 加载时已校验 (text_problems); 快照缺参数时抛 UndefinedError."""
    return LocalizedText(
        zh=TEXT_ENV.from_string(text.zh).render(params), en=TEXT_ENV.from_string(text.en).render(params)
    )


def first_misstatement_communicated_at(state: EngagementState) -> datetime | None:
    """首次发出错报沟通的时间 (需求 R161): 状态中 FIN-08 各次登记 (manual 任务的 manual_result 决定) 的沟通时间的最早
    值; 未提供沟通时刻时取该登记的决定时刻作为更正跟踪起点. 还没有登记过为 None.
    决定只追加, 每次沟通都登记在那里, 被过期重跑取代的执行的
    登记也还在; 不看错报上的 communicated_at: 重新沟通会覆盖它 (第 1 轮审核 R161-02). 纯函数."""
    fin08 = {t.id for t in state["review_tasks"] if t.procedure_id == FIN08 and t.kind is TaskKind.manual}
    return min(
        (
            response.communicated_at or record.decided_at
            for record in state["review_records"]
            if record.task_id in fin08 and record.manual_result is not None
            for response in ManagementResponses.model_validate(record.manual_result.details).responses
        ),
        default=None,
    )


# 派生引用 (需求 R161): 名字 → 由图状态算出值的纯函数 (只收纯函数: 重放时重算得同一值). 值可为 None 并以 None 执行,
# None 的语义由引用它的程序处理 (接口文档第 8.1 节; 如 GEN-05 的 R06 在 audit_adjustment_start 为空时不执行); 值经
# to_jsonable_python 转成 JSON 兼容的值 (同 JSON_NAMESPACES), 参数快照首次执行与经检查点, Store 读回后一致
DERIVED_REFS: dict[str, Callable[[EngagementState], Any]] = {
    "first_misstatement_communicated_at": first_misstatement_communicated_at,
    # 已验证更正分录的凭证号 (需求 R174, services/adjusted_tb): 规则 SQL 传给 adjusted_trial_balance(:eid, :它)
    "verified_adjustment_entry_nos": verified_entry_nos,
}


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


def run_params(
    library: "ProcedureLibrary",
    template: ProcedureTemplate,
    procedure: Procedure,
    engagement: Engagement,
    state: EngagementState,
    *,
    traits: DatasetTraits,
    profile: ClientProfile | None,
) -> dict[str, Any]:
    """程序某次执行的参数: 模板参数的解析结果 (resolve_params; 风险等级取程序关联审计目标的最高等级, highest_risk),
    再叠加 Procedure.params (rerun 决定改的参数, 同名键以它为准). 建执行记录 (nodes.build_run) 与总体审计计划的程序
    参数 (agents/audit_plan, 需求 R204) 共用, 两处同口径由此保证.
    Procedure.params 深拷贝后叠加: 其中的列表与字典若与快照共用, 改一处会连带改到程序和历次执行的快照."""
    risk = highest_risk(state["audit_objectives"], procedure.objective_ids)
    resolved = library.resolve_params(template, engagement, state, risk, traits=traits, profile=profile)
    return {**resolved, **copy.deepcopy(procedure.params)}


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
        refs = [
            f"{t.id}.{key}: {problem}"
            for t in templates
            for key, value in t.params.items()
            if isinstance(value, dict) and "ref" in value and (problem := ref_param_problem(value)) is not None
        ]
        if refs:
            raise ValueError(f"程序参数的 ref 写错: {'; '.join(refs)}")
        wrong = inputs_problems(templates)
        if wrong:
            raise ValueError(f"程序模板的 inputs 不对: {'; '.join(wrong)}")
        wrong = manual_problems(templates)
        if wrong:
            raise ValueError(f"人工程序写法不对: {'; '.join(wrong)}")
        wrong = manual_result_problems(templates)
        if wrong:
            raise ValueError(f"只有判断程序能开 manual_result_allowed (人工结果取代对象的判断): {', '.join(wrong)}")
        wrong = update_autonomy_problems(templates)
        if wrong:
            raise ValueError(f"带错报更新类别的程序自主度不能高于 L2 (仲裁不收载荷): {', '.join(wrong)}")
        wrong = population_problems(templates)
        if wrong:
            raise ValueError(f"规则被测总体的写法不对: {'; '.join(wrong)}")
        wrong = text_problems(templates)
        if wrong:
            raise ValueError(f"范围说明, 总体说明与检查项名称的写法不对: {'; '.join(wrong)}")
        unread = read_problems(templates)
        if unread:
            raise ValueError(f"程序的 reads 漏了规则 SQL 读的数据 (过期判断要用): {'; '.join(unread)}")
        wrong = document_read_problems(templates)
        if wrong:
            raise ValueError(f"程序读文件登记的写法不对 (过期判断要用): {'; '.join(wrong)}")
        self.workpapers = load_workpapers(root / "workpapers.yaml")  # 底稿配置 (需求 R329)
        wrong = workpaper_problems(templates, self.workpapers)
        if wrong:
            raise ValueError(f"底稿索引号或底稿配置不对: {'; '.join(wrong)}")
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
        self._report = report_templates(root / "templates" / "report.yaml")  # 工作语言 -> 报告的固定部分

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
        profile: ClientProfile | None,
    ) -> dict[str, Any]:
        """解析模板参数. 每个参数三种写法: 字面量, {by_risk: {等级: 值}} 或 {ref: 名称} (profile. 的 ref 可另写
        optional: true).

        risk_level: 程序关联审计目标的最高等级 (见 highest_risk), 没有关联目标为 None.
        traits: 项目数据集的声明, dataset. 命名空间的 ref 从它取.
        profile: 本项目采用版本的客户画像, profile. 命名空间的 ref 从它取; 为 None 时引用 profile. 抛 ValueError.
            profile. 的取值一律是 JSON 兼容的值 (见 JSON_NAMESPACES), 参数快照经检查点与 Store 读回后与首次执行相等.
        by_risk 参数在 risk_level 为 None 或没有该等级的取值时抛 ValueError.
        ref 指向的状态值为 None (如重要性还没经闸门确定) 时抛 ValueError, 不以 None 执行; 派生引用 (DERIVED_REFS)
        与 dataset. 的值为 None
        时照样以 None 执行; profile. 的值为 None 时 (含路径中途为 None) 抛 ValueError ("<字段路径> 尚未确定"), 写了
        optional: true 时以 None 执行; 写法不对 (见 ref_param_problem) 抛 KeyError.
        字面量与取自声明的值深拷贝, 取自画像的值是新建的 JSON 兼容值, 改返回值不会改到模板, 声明与画像.
        """
        out: dict[str, Any] = {}
        sources: dict[str, BaseModel] = {"dataset": traits}  # 命名空间 ref 的取值对象
        if profile is not None:
            sources["profile"] = profile
        for key, value in template.params.items():
            if isinstance(value, dict) and "by_risk" in value:
                if risk_level is None:
                    raise ValueError(f"{template.id}.{key}: 按风险等级取值, 但程序没有关联审计目标")
                if risk_level.value not in value["by_risk"]:
                    raise ValueError(f"{template.id}.{key}: 没有 {risk_level.value} 等级的取值")
                out[key] = value["by_risk"][risk_level.value]
            elif isinstance(value, dict) and "ref" in value:
                ref = value["ref"]
                problem = ref_param_problem(value)
                if problem is not None:
                    raise KeyError(f"{template.id}.{key}: {problem}")
                if ref in ENGAGEMENT_REFS:
                    out[key] = getattr(engagement, ref)
                elif ref in DERIVED_REFS:
                    out[key] = to_jsonable_python(DERIVED_REFS[ref](state))
                elif ref in STATE_REFS:
                    resolved = state[ref]  # type: ignore[literal-required]
                    if resolved is None:
                        raise ValueError(f"{template.id}.{key}: {ref} 尚未确定")
                    out[key] = resolved
                else:
                    namespace, _, path = ref.partition(".")
                    if namespace not in sources:
                        missing = MISSING_SOURCES.get(namespace, f"没有 {namespace}. 的取值对象")
                        raise ValueError(f"{template.id}.{key}: {missing}, 取不到 {ref}")
                    resolved = namespace_value(sources, ref)
                    if resolved is None and namespace not in NONE_RUNS and not value.get("optional"):
                        raise ValueError(f"{template.id}.{key}: {path} 尚未确定")
                    out[key] = resolved
            else:
                out[key] = copy.deepcopy(value)
        return out

    def planning_templates(self) -> tuple[list[AuditObjective], list[RiskItem]]:
        """planning/ 的通用审计目标与风险清单模板 (每次调用重新读文件): 没有数据集预置的项目用, 如新建的客户.
        与没有补造目录的 Xero 快照所用的模板是同一份."""
        directory = self.root / "planning"
        objectives = json.loads((directory / "audit_objectives.json").read_text(encoding="utf-8"))
        risks = json.loads((directory / "risk_register.json").read_text(encoding="utf-8"))
        return [AuditObjective.model_validate(o) for o in objectives], [RiskItem.model_validate(r) for r in risks]

    def workpaper_refs(self) -> list[str]:
        """全部底稿的索引号: 各程序 (含未启用的) 的与系统底稿的, 按档案顺序 (workpaper_order)."""
        refs = [t.workpaper.ref for t in self._all.values() if t.workpaper is not None]
        return sorted([*refs, *(w.ref for w in self.workpapers.system)], key=self.workpaper_order)

    def workpaper_order(self, ref: str) -> tuple[int, int, int, str]:
        """索引号的档案顺序键 (R330, R334 的底稿与档案按它排): 属区的按区的顺序, 区字母本身 (导引表, 错报汇总) 在前,
        序号按该区的写法比较 (数字按数值, 字母先比长度再比字母, 即 A..Z, AA..). 不属任何区的系统底稿按配置 system
        列表的顺序: 列在第一份属区的系统底稿之前的排在全部区之前 (如索引 00), 之后的排在全部区之后 (如准则对照 S).
        不在配置里的索引号排在最后."""
        areas = {a.id: (i, a) for i, a in enumerate(self.workpapers.areas)}
        system = self.workpapers.system
        position = next((i for i, w in enumerate(system) if w.ref == ref and w.area is None), None)
        if position is not None:
            first = next((i for i, w in enumerate(system) if w.area is not None), len(system))
            return (-1 if position < first else len(areas), 0, position, ref)
        area_id, _, suffix = ref.partition("-")
        if area_id not in areas:
            return (len(areas) + 1, 0, 0, ref)
        index, area = areas[area_id]
        if not suffix:
            return (index, 0, 0, "")
        number = int(suffix) if area.numbering == "digits" and suffix.isdigit() else len(suffix)
        return (index, 1, number, suffix)

    def required_items(self, ref: str) -> list[RequiredItem]:
        """该索引号的底稿必须记录的内容 (底稿配置的 required, 按配置顺序): 不限范围的 (全部底稿), kinds 含该程序的
        底稿类别的 (workpaper_kind; 系统底稿没有类别), refs 含它的. 例: A-2 为通用八项加 ASA 320 第 14 段两项; D-3
        (规则程序) 另有被测项目的识别特征. 索引号不存在抛 KeyError."""
        if ref not in self.workpaper_refs():
            raise KeyError(ref)
        template = next((t for t in self._all.values() if t.workpaper is not None and t.workpaper.ref == ref), None)
        kind = workpaper_kind(template) if template is not None else None
        return [
            r
            for r in self.workpapers.required
            if (not r.kinds and not r.refs) or (kind is not None and kind in r.kinds) or ref in r.refs
        ]

    def standard_refs(self, requirement: StandardRequirement) -> list[str]:
        """准则对照一行对应的底稿索引号: refs 加 areas 中各区的全部底稿 (程序底稿与导引表), 去重后按档案顺序."""
        refs = {*requirement.refs, *(r for r in self.workpaper_refs() if r.partition("-")[0] in requirement.areas)}
        return sorted(refs, key=self.workpaper_order)

    def fragment(self, fragment_id: str) -> StandardFragment | None:
        return self._fragments.get(fragment_id)

    def report_sections(self, opinion: OpinionType, language: WorkingLanguage) -> dict[ReportSection, str]:
        """templates/report.yaml 中该语言, 该意见类型的固定段落 (董事责任, 审计师责任, 其他法律法规要求, 签名块).

        正文含占位符 {entity_name}, {period_end}, {auditor_name}, 由 RPT-03 代入.
        """
        return dict(self._report[language].sections[opinion])

    def report_headings(self, language: WorkingLanguage) -> dict[OpinionType, tuple[str, str]]:
        """该语言各意见类型的两段标题: 意见类型 → (意见段, 形成意见的基础段), RPT-03 的提示词与校验共用 (需求 R341)."""
        return {o: (h.opinion, h.basis_for_opinion) for o, h in self._report[language].headings.items()}

    def report_date(self, day: date, language: WorkingLanguage) -> str:
        """报告正文里的日期写法 (需求 R341), 如 en 的 30 June 2026."""
        return self._report[language].date.format(d=day)

    def report_terms(self, language: WorkingLanguage) -> dict[str, str]:
        """该语言下模型写两段时的名称 (键 → 写法, 与固定段落相同, R341 审核 03); 两种语言的键相同, 按 report.yaml
        的顺序."""
        return dict(self._report[language].terms)

    def prompts_sha256(self) -> str:
        """prompts/*.j2 按文件名排序后, 各文件内容依次拼接的 sha256. 每次调用重新读文件, 改任一 prompt 即变化."""
        digest = hashlib.sha256()
        for p in sorted((self.root / "prompts").glob("*.j2"), key=lambda p: p.name):
            digest.update(p.read_bytes())
        return digest.hexdigest()


def report_templates(path: Path) -> dict[WorkingLanguage, ReportTemplate]:
    """读 templates/report.yaml (需求 R341): 每种工作语言一份 ReportTemplate, 结构相同 (段落一一对应). 程序库载入时即
    核对, 不过抛 ValueError (消息含文件名与语言): 缺语言; 每种语言有标题的意见类型与有固定段落的相同, 标题与名称
    (terms) 去掉首尾空白后不为空; 各语言的意见类型, 段落, 标题与名称的键相同; 同一意见类型, 同一段的占位符
    (str.format 的字段名) 各语言相同."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    languages = get_args(WorkingLanguage)
    if sorted(doc) != sorted(languages):
        raise ValueError(f"{path.name}: 须按 {', '.join(languages)} 各给一份, 现为 {', '.join(doc)}")
    templates = {cast(WorkingLanguage, language): ReportTemplate.model_validate(part) for language, part in doc.items()}
    for language, template in templates.items():
        if set(template.headings) != set(template.sections):
            raise ValueError(f"{path.name} {language}: 有标题的意见类型与有固定段落的须相同")
        headings = [t for h in template.headings.values() for t in (h.opinion, h.basis_for_opinion)]
        texts = [*headings, *template.terms.values()]
        if not all(text.strip() for text in texts):
            raise ValueError(f"{path.name} {language}: 标题与名称不能为空")
    shapes = {
        language: (
            {o: {name: placeholders(text) for name, text in t.items()} for o, t in template.sections.items()},
            sorted(template.headings),
            sorted(template.terms),
        )
        for language, template in templates.items()
    }
    if len({repr(shape) for shape in shapes.values()}) != 1:
        raise ValueError(f"{path.name}: 各语言的意见类型, 段落, 占位符, 标题与名称须相同: {shapes}")
    return templates


def placeholders(text: str) -> list[str]:
    """str.format 模板里的字段名, 去重排序, 如 "{auditor_name}\n..." 得 ["auditor_name"]."""
    return sorted({field for _, field, _, _ in string.Formatter().parse(text) if field})


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
    if ref in STATE_REFS or ref in ENGAGEMENT_REFS or ref in DERIVED_REFS:
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


def ref_param_problem(param: dict[str, Any]) -> str | None:
    """{ref: ...} 参数写错时的说明 (含该 ref), 合格为 None: ref 名称见 ref_problem; 除 ref 外只许 optional 一个键,
    其值须为布尔, 且只用于 OPTIONAL_NAMESPACES 的命名空间 (重要性等为 None 时不能以 None 执行)."""
    ref = param.get("ref")
    problem = ref_problem(ref)
    if problem is not None:
        return problem
    extra = sorted(str(k) for k in param if k not in ("ref", "optional"))
    if extra:
        return f"ref {ref} 的参数只能另写 optional, 多了 {', '.join(extra)}"
    if "optional" in param:
        if not isinstance(param["optional"], bool):
            return f"ref {ref} 的 optional 须为 true 或 false"
        if str(ref).partition(".")[0] not in OPTIONAL_NAMESPACES:
            return f"ref {ref} 不能写 optional (只用于 {', '.join(sorted(OPTIONAL_NAMESPACES))}. 的引用)"
    return None


def namespace_value(sources: dict[str, BaseModel], ref: str) -> Any:
    """命名空间 ref 的值 (名称已经 ref_problem 查过): 沿字段路径取, 中途为 None 即为 None. JSON_NAMESPACES 的值转成
    JSON 兼容的新对象; 其余命名空间的模型转成字典, 其他值深拷贝."""
    namespace, _, path = ref.partition(".")
    value: Any = sources[namespace]
    for name in path.split("."):
        if value is None:
            return None
        value = getattr(value, name)
    if namespace in JSON_NAMESPACES:
        return to_jsonable_python(value)
    return value.model_dump() if isinstance(value, BaseModel) else copy.deepcopy(value)


def load_template(path: Path) -> ProcedureTemplate:
    """读一个程序 YAML. 字段校验不过时抛 ValueError, 消息带程序编号 (YAML 的 id, 没有时取文件名) 与文件名."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    try:
        return ProcedureTemplate.model_validate(raw)
    except ValidationError as e:
        pid = raw.get("id") if isinstance(raw, dict) else None
        raise ValueError(f"程序模板 {pid or path.stem} ({path.name}) 写法不对: {e}") from e


def load_workpapers(path: Path) -> WorkpaperConfig:
    """读底稿配置 (knowledge/workpapers.yaml, 需求 R329). 字段校验不过时抛 ValueError, 消息带文件名."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    try:
        return WorkpaperConfig.model_validate(raw)
    except ValidationError as e:
        raise ValueError(f"底稿配置 ({path.name}) 写法不对: {e}") from e


# 程序底稿索引号的序号写法 (WorkpaperArea.numbering) → (正则, 说明)
WORKPAPER_NUMBERING = {"digits": (r"[1-9][0-9]*", "1 起的数字"), "letters": (r"[A-Z]+", "A 起的大写字母")}


def workpaper_kind(template: ProcedureTemplate) -> WorkpaperKind | None:
    """程序的底稿类别 (底稿配置必填项的 kinds): 规则程序 (engine rule) 为 rule, 判断程序 (judging_program) 为
    judgment, 其余 (人工程序, 闸门决定, 抽取与起草) 为空."""
    if template.engine is Engine.rule:
        return "rule"
    return "judgment" if judging_program(template) else None


def workpaper_problems(templates: Iterable[ProcedureTemplate], config: WorkpaperConfig) -> list[str]:
    """底稿索引号与底稿配置的问题 (需求 R329; 含未启用的模板, 消息含程序编号或索引号): 区, 必填项代号与准则对照的
    准则各不重复; 程序 workpaper 与导引表的区须在 areas 中, 索引号为区字母本身或 {区}-{序号}, 序号按该区的
    numbering (错报区 X 为 letters: X-A 起, X-数字留给错报汇总表中的错报编号); 有 categories 的系统底稿 (导引表)
    须属一个区; 不属区的系统底稿 (索引, 准则对照) 的索引号不能归到某区 (区字母本身或以 {区}- 开头, 与 standard_refs
    归区的口径相同); 程序与系统底稿的
    索引号不重复; 必填项与准则对照所引的索引号与区都在."""
    templates = list(templates)
    areas = {a.id: a for a in config.areas}
    problems: list[str] = []
    for what, values in (
        ("区", [a.id for a in config.areas]),
        ("必填项代号", [r.id for r in config.required]),
        ("准则对照的准则", [s.standard for s in config.standards]),
    ):
        problems += [f"{what} {v} 重复" for v in sorted({v for v in values if values.count(v) > 1})]

    def index_problem(owner: str, ref: str, area_id: str) -> str | None:
        area = areas.get(area_id)
        if area is None:
            return f"{owner}: 区 {area_id} 不在底稿配置的 areas 中"
        pattern, hint = WORKPAPER_NUMBERING[area.numbering]
        if re.fullmatch(rf"{re.escape(area_id)}(?:-(?:{pattern}))?", ref) is None:
            return f"{owner}: 索引号 {ref} 应为 {area_id} 或 {area_id}-序号 ({area.name.zh}区的序号为 {hint})"
        return None

    owners: dict[str, list[str]] = {}  # 索引号 → 用它的底稿 (程序编号, 或 "系统底稿")
    for t in templates:
        if t.workpaper is not None:
            problems += [p for p in [index_problem(t.id, t.workpaper.ref, t.workpaper.area)] if p is not None]
            owners.setdefault(t.workpaper.ref, []).append(t.id)
    for w in config.system:
        owner = f"系统底稿 {w.ref}"
        if w.area is not None:
            problems += [p for p in [index_problem(owner, w.ref, w.area)] if p is not None]
        elif w.categories:
            problems.append(f"{owner}: 导引表 (有 categories) 须属一个区")
        elif (like := w.ref.partition("-")[0]) in areas:
            problems.append(f"{owner}: 不属任何区, 索引号却归到 {like} 区 ({like} 或以 {like}- 开头)")
        owners.setdefault(w.ref, []).append("系统底稿")
    problems += [f"索引号 {ref} 重复: {', '.join(who)}" for ref, who in owners.items() if len(who) > 1]
    for r in config.required:
        problems += [f"必填项 {r.id}: 索引号 {ref} 不存在" for ref in r.refs if ref not in owners]
    for s in config.standards:
        problems += [f"准则对照 {s.standard}: 索引号 {ref} 不存在" for ref in s.refs if ref not in owners]
        problems += [f"准则对照 {s.standard}: 区 {a} 不在 areas 中" for a in s.areas if a not in areas]
    return problems


def manual_program(template: ProcedureTemplate) -> bool:
    """人工程序 (需求 R92): engine manual 且不是闸门程序, 由 A10 派 manual 任务给人执行. 闸门程序 (ACC-01, PLN-01/03,
    FIN-01, RPT-01) 也是 engine manual, 但由闸门节点处理, 不算."""
    return template.engine is Engine.manual and template.gate is None


def manual_problems(templates: Iterable[ProcedureTemplate]) -> list[str]:
    """人工程序的写法问题, 每条含程序编号: 在 P2 (本级不做 P2 人工程序), 没写 manual 段, 或 manual.result_model 没在
    contracts/manual_results.RESULT_MODELS 登记; 不是人工程序 (含闸门程序) 却写了 manual 段."""
    problems: list[str] = []
    for t in templates:
        if not manual_program(t):
            if t.manual is not None:
                problems.append(f"{t.id}: 不是人工程序 (engine manual 且不是闸门), 不能写 manual 段")
            continue
        if t.phase is Phase.P2_Executing:
            problems.append(f"{t.id}: 本级不做 P2 的人工程序")
        if t.manual is None:
            problems.append(f"{t.id}: engine manual 且不是闸门程序, 须有 manual 段")
        elif t.manual.result_model is not None and t.manual.result_model not in RESULT_MODELS:
            problems.append(f"{t.id}: 结果模型 {t.manual.result_model} 没有登记 (contracts/manual_results)")
    return problems


def judging_program(template: ProcedureTemplate) -> bool:
    """判断程序: llm.output_model 为 JudgmentDraft 或其子类 (经判断公共流程出 Judgment)."""
    model = getattr(llm_outputs, template.llm.output_model, None) if template.llm is not None else None
    return isinstance(model, type) and issubclass(model, JudgmentDraft)


def manual_result_problems(templates: Iterable[ProcedureTemplate]) -> list[str]:
    """开了 manual_result_allowed 而不是判断程序 (judging_program) 的模板编号 (需求 R158 第 1 轮审核 R158-07). 失败任务
    的 manual_result 由 DecisionSubmit 建人工判断取代该对象的判断; 别的程序 (抽取, 规则, 起草) 的对象该产出的领域对象
    (如 DOC-04 的合同) 没人补, 程序照样记完成, 下游悄悄少一个对象."""
    return [t.id for t in templates if t.manual_result_allowed and not judging_program(t)]


def update_autonomy_problems(templates: Iterable[ProcedureTemplate]) -> list[str]:
    """带错报更新类别 (misstatement_update_kind) 而自主度高于 L2 的模板编号 (需求 R159 第 1 轮审核 R159-03). 仲裁
    a7_upheld 与 neither 建的人工判断不带载荷 (第 7 节), L3 起 A7 复核会出争议任务, 仲裁之后错报就不写; 要放开须先让
    仲裁收人给的载荷."""
    return [t.id for t in templates if t.misstatement_update_kind is not None and t.autonomy_level not in LOW_AUTONOMY]


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


def population_problems(templates: Iterable[ProcedureTemplate]) -> list[str]:
    """规则被测总体的写法问题 (需求 R324, 消息含程序与规则编号): emit rows 的规则以返回行为总体, 不能写 population;
    总体 SQL 的绑定参数只能用 params 中的与 eid (执行时参数取参数快照, 别的参数绑不上)."""
    problems = []
    for t in templates:
        for rule in t.rules:
            if rule.population is None:
                continue
            if rule.emit == "rows":
                problems.append(f"{t.id}.{rule.id}: emit rows 的规则以返回行为总体, 不能写 population")
            unknown = sorted(set(SQL_BIND.findall(rule.population.sql)) - set(t.params) - {"eid"})
            if unknown:
                problems.append(f"{t.id}.{rule.id} 的总体 SQL 用了 params 以外的参数 {', '.join(unknown)}")
    return problems


def text_problems(templates: Iterable[ProcedureTemplate]) -> list[str]:
    """程序 YAML 里文字的写法问题 (需求 R324, 消息含程序编号与位置). 带参数占位的 scope 与规则总体的 description:
    语法错, 用了不认识的过滤器或检验 (test, 条件块里的也查: Jinja2 编译时不查它们), 或占位用了 params 以外的变量;
    检查项名称 check 不渲染, 不能写占位 (语法块, 变量或注释). zh 与 en 都要给由字段校验保证 (LocalizedText)."""
    problems = []
    for t in templates:
        texts = [
            ("scope", t.scope),
            *((f"{r.id}.population", r.population.description) for r in t.rules if r.population),
        ]
        for where, text in texts:
            if text is None:
                continue
            for lang, source in (("zh", text.zh), ("en", text.en)):
                try:
                    tree = TEXT_ENV.parse(source)
                except TemplateSyntaxError as e:
                    problems.append(f"{t.id}.{where}.{lang}: {e}")
                    continue
                filters = sorted({f.name for f in tree.find_all(nodes.Filter)} - set(TEXT_ENV.filters))
                if filters:
                    problems.append(f"{t.id}.{where}.{lang} 用了不认识的过滤器 {', '.join(filters)}")
                tests = sorted({x.name for x in tree.find_all(nodes.Test)} - set(TEXT_ENV.tests))
                if tests:
                    problems.append(f"{t.id}.{where}.{lang} 用了不认识的检验 {', '.join(tests)}")
                unknown = sorted(meta.find_undeclared_variables(tree) - set(t.params))
                if unknown:
                    problems.append(f"{t.id}.{where}.{lang} 的占位不在 params 内: {', '.join(unknown)}")
        for rule in t.rules:
            if rule.check is None:
                continue
            for lang, source in (("zh", rule.check.zh), ("en", rule.check.en)):
                if has_markup(source):
                    problems.append(f"{t.id}.{rule.id}.check.{lang} 不渲染, 不能写占位")
    return problems


def has_markup(source: str) -> bool:
    """文字里有没有 Jinja2 的语法块, 变量或注释 (词法上只有纯文字才算没有; 写坏了的标记也算有)."""
    try:
        return any(kind != "data" for _, kind, _ in TEXT_ENV.lex(source))
    except TemplateSyntaxError:
        return True


def read_problems(templates: Iterable[ProcedureTemplate]) -> list[str]:
    """规则 SQL (含执行前检查的 SQL, 需求 R130, 与被测总体的 SQL, 需求 R324) 读到而 reads 没声明的账表 (消息含程序
    编号): 过期判断按 reads 看数据变动, 漏了就标不出过期. SQL 里整词出现的账表名都算读了 (FROM, JOIN, 逗号连接,
    子查询); 列名或别名与账表同名时也算, 宁多勿漏. 大模型与 Agent 另读的数据 (如 REV-02 的发运单, 合同) 靠 YAML 手写."""
    problems = []
    for t in templates:
        sqls = [
            sql
            for rule in t.rules
            for sql in (
                rule.sql,
                *(check.sql for check in rule.checks),
                *((rule.population.sql,) if rule.population else ()),
            )
        ]
        used = {name.lower() for sql in sqls for name in SQL_TABLE.findall(sql)}
        missing = sorted(used - {i.value for i in t.reads})
        if missing:
            problems.append(f"{t.id} 读 {', '.join(missing)}")
    return problems


def document_read_problems(templates: Iterable[ProcedureTemplate]) -> list[str]:
    """读文件登记的声明问题 (消息含程序编号, 需求 R304): reads 含 documents 而没写 document_reads (读哪几类文件的
    登记; 不写就分不出哪些登记变动与它有关, 所以要求必须写), 或写了 document_reads 而 reads 没有 documents."""
    problems = []
    for t in templates:
        reads = DataInput.documents in t.reads
        if reads and not t.document_reads:
            problems.append(f"{t.id}: reads 含 documents 而没写 document_reads")
        if t.document_reads and not reads:
            problems.append(f"{t.id}: 写了 document_reads 而 reads 没有 documents")
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
