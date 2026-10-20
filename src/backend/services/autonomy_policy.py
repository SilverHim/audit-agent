"""自主度动态降级 (规格书 7.4, 需求 R168; 功能 autonomy, 关闭时不调用): 装配级与判断级两组规则的纯函数.

不调大模型, 不读写存储. 规则表 = knowledge/rules.json 的 autonomy 段 (默认表), 程序 YAML 的 downgrade_rules 按条件
覆盖; 函数只看条件, 不按程序编号分支. 只降不升, 下限 L1: 越过 L1 时截在 L1, 不另作处理 (盲判本版不做).

- 装配 (assembly_level): 起点为模板级别, 看程序关联审计目标的最高风险 (只算一次): significant 命中
  significant_risk, high 命中 high_risk. 没有关联目标的程序 (固定集, 只经依赖加入的, P2 以外) 不降级.
- 判断 (judgment_level): 起点为程序的运行时级别 (装配后的), 理由接着程序的往下列; 看判断金额 (与复核档位同一口径,
  review_queue.judgment_amount) 是否超过执行重要性, 置信度, 推理链有无 unsupported 步骤, 舞弊迹象. A7 的结论不影响
  级别 (不一致的判断由 classify 定为争议任务).
"""

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from backend.contracts.knowledge import DowngradeAt, DowngradeRule, ProcedureTemplate
from backend.contracts.models import (
    AuditException,
    AutonomyLevel,
    DowngradeCondition,
    Judgment,
    Procedure,
    RiskLevel,
    StepStatus,
)
from backend.services.procedure_library import ProcedureLibrary

LEVELS = list(AutonomyLevel)  # 由低到高: L1 < L2 < L3 < L4; 下标 0 即下限 L1
# 装配看的风险等级 → 条件; 其余等级不降级
RISK_CONDITIONS = {
    RiskLevel.significant: DowngradeCondition.significant_risk,
    RiskLevel.high: DowngradeCondition.high_risk,
}


@dataclass(frozen=True)
class AutonomyDecision:
    """一次降级的结果: 级别与命中的条件 (按规则表的顺序; 越过下限的那条照记). 不带别的标志."""

    level: AutonomyLevel
    reasons: list[DowngradeCondition]


@dataclass(frozen=True)
class AutonomyPolicy:
    """降级用到的知识: 默认表 (每个条件恰一条, 按 rules.json 的顺序), 低置信度阈值, 标了 fraud_flag 的规则."""

    defaults: tuple[DowngradeRule, ...]
    low_confidence_below: float  # 置信度低于它命中 low_confidence
    fraud_rules: frozenset[tuple[str, str]] = frozenset()  # (程序编号, 规则编号): RuleSpec.fraud_flag 为真的规则

    def __post_init__(self) -> None:
        """默认表须每个条件恰一条 (模板只按条件覆盖, 不另加条件)."""
        conditions = [r.condition for r in self.defaults]
        if sorted(conditions) != sorted(DowngradeCondition):
            raise ValueError(f"默认降级表须每个条件恰一条, 现为 {', '.join(conditions)}")

    @classmethod
    def from_rules(cls, rules: dict[str, Any], templates: Iterable[ProcedureTemplate] = ()) -> "AutonomyPolicy":
        """按 rules.json 的内容 (取 autonomy 段) 与程序模板 (取标了 fraud_flag 的规则) 建."""
        autonomy = rules["autonomy"]
        return cls(
            defaults=tuple(DowngradeRule.model_validate(r) for r in autonomy["rules"]),
            low_confidence_below=float(autonomy["low_confidence_below"]),
            fraud_rules=frozenset((t.id, r.id) for t in templates for r in t.rules if r.fraud_flag),
        )

    @classmethod
    def load(cls, library: ProcedureLibrary) -> "AutonomyPolicy":
        """程序库 knowledge 目录下的 rules.json 与全部模板 (含未启用的: 旧项目的例外可能出自它们)."""
        rules = json.loads((library.root / "rules.json").read_text(encoding="utf-8"))
        return cls.from_rules(rules, library.all(include_disabled=True))

    def table(self, template: ProcedureTemplate, at: DowngradeAt) -> list[DowngradeRule]:
        """某程序在某时点的规则表: 默认表按条件换成模板 downgrade_rules 的那条, 顺序同默认表."""
        overrides = {r.condition: r for r in template.downgrade_rules}
        return [overrides.get(r.condition, r) for r in self.defaults if r.at == at]


def lowered(start: AutonomyLevel, hits: Sequence[DowngradeRule]) -> AutonomyLevel:
    """从 start 起按命中的规则降级: delta 相加, target_level 取不高于它, 只降不升; 越过 L1 时截在 L1."""
    index = LEVELS.index(start) + sum(r.delta for r in hits if r.delta is not None)
    for rule in hits:
        if rule.target_level is not None:
            index = min(index, LEVELS.index(rule.target_level))
    return LEVELS[max(index, 0)]


def assembly_level(policy: AutonomyPolicy, template: ProcedureTemplate, risk: RiskLevel | None) -> AutonomyDecision:
    """装配时的级别: 起点为模板级别; risk 为程序关联审计目标的最高风险 (procedure_library.highest_risk, 没有关联目标
    为 None, 不降级). 例: REV-02 L2, risk significant → L1 [significant_risk] (−2 越过下限, 截在 L1)."""
    condition = RISK_CONDITIONS.get(risk) if risk is not None else None
    hits = [r for r in policy.table(template, "assembly") if r.condition is condition]
    return AutonomyDecision(lowered(template.autonomy_level, hits), [r.condition for r in hits])


def judgment_level(
    policy: AutonomyPolicy,
    template: ProcedureTemplate,
    procedure: Procedure,
    judgment: Judgment,
    *,
    amount: int | None,
    performance_cents: int | None,
    exceptions: Sequence[AuditException] = (),
) -> AutonomyDecision:
    """判断时的级别: 起点为程序的运行时级别 (procedure.autonomy_level), 理由先列程序装配时的
    (procedure.downgrade_reasons), 再列判断时命中的.

    amount: 判断金额 (review_queue.judgment_amount, 与复核档位同一口径); performance_cents: 执行重要性, 未定
    (PLN-01/03 之前) 为 None, 此时 amount_over_pm 不适用. exceptions: 判断解释的例外 (看所属规则是否标 fraud_flag).
    """
    hit = {
        DowngradeCondition.amount_over_pm: performance_cents is not None
        and amount is not None
        and abs(amount) > performance_cents,
        DowngradeCondition.low_confidence: judgment.confidence < policy.low_confidence_below,
        DowngradeCondition.unsupported_steps: any(
            step.status is StepStatus.unsupported for step in judgment.reasoning_chain
        ),
        DowngradeCondition.fraud_flag: bool(
            judgment.misstatement_update is not None and judgment.misstatement_update.fraud_indicative
        )
        or any((x.procedure_id, x.rule_id) in policy.fraud_rules for x in exceptions),
    }
    hits = [r for r in policy.table(template, "judgment") if hit[r.condition]]
    reasons = [*procedure.downgrade_reasons, *(r.condition for r in hits)]
    return AutonomyDecision(lowered(procedure.autonomy_level, hits), reasons)
