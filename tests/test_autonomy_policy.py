"""自主度动态降级 (需求 R168, 功能 autonomy; 规格书 7.4): 默认表, 装配级与判断级纯函数, 下限 L1, 模板按条件覆盖.

输入取 Xero 演示的设计数值 (设计稿 v2.1 第 2.7, 6, 7.1 节): 收入两条目标 significant, 现金 high, 执行重要性 16216.
接入装配与判断两个时点由需求 R170 做, 这里只测纯函数.
"""

import dataclasses
import json
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import backend.services.autonomy_policy as autonomy_policy
from backend.contracts.knowledge import DowngradeRule, ProcedureTemplate
from backend.contracts.models import (
    AuditException,
    AuditObjective,
    AutonomyLevel,
    CriticVerdict,
    Direction,
    DowngradeCondition,
    Judgment,
    Misstatement,
    MisstatementType,
    MisstatementUpdate,
    Phase,
    Procedure,
    ProposedAction,
    ReasoningStep,
    RiskLevel,
    StepKind,
    StepStatus,
)
from backend.core.config import get_settings
from backend.services.autonomy_policy import (
    AutonomyDecision,
    AutonomyPolicy,
    assembly_level,
    judgment_level,
)
from backend.services.procedure_library import ProcedureLibrary, highest_risk
from backend.services.review_queue import judgment_amount
from tests.fakes import NOW

LIBRARY = ProcedureLibrary(get_settings().KNOWLEDGE_ROOT)
POLICY = AutonomyPolicy.load(LIBRARY)
PM = 16216  # Xero 的执行重要性 (收入 1%, 50%), 单位分
L1, L2, L3, L4 = AutonomyLevel.L1, AutonomyLevel.L2, AutonomyLevel.L3, AutonomyLevel.L4
SIG, HIGH = DowngradeCondition.significant_risk, DowngradeCondition.high_risk
OVER_PM, LOW = DowngradeCondition.amount_over_pm, DowngradeCondition.low_confidence
UNSUPPORTED, FRAUD = DowngradeCondition.unsupported_steps, DowngradeCondition.fraud_flag
# Xero 预置的审计目标 (设计稿 v2.1 第 2.7 节): 收入发生与截止为特别风险, 现金存在为 high
XERO_OBJECTIVES = [
    AuditObjective(id=oid, account=account, assertion=assertion, risk_level=level, is_material=True)
    for oid, account, assertion, level in (
        ("OBJ-REV-OCC", "Revenue", "Occurrence", RiskLevel.significant),
        ("OBJ-REV-CUT", "Revenue", "Cutoff", RiskLevel.significant),
        ("OBJ-CASH-EXI", "Cash", "Existence", RiskLevel.high),
    )
]


def template_of(procedure_id: str) -> ProcedureTemplate:
    """程序的模板; 本版还没有模板的程序 (GEN-06, GEN-04, GEN-01, FIN-09, FIN-15) 用一份不覆盖降级表的模板代替,
    函数只看模板的 downgrade_rules."""
    try:
        return LIBRARY.load(procedure_id)
    except KeyError:
        return LIBRARY.load("REV-02").model_copy(update={"id": procedure_id})


def procedure(procedure_id: str, level: AutonomyLevel, reasons: list[DowngradeCondition] | None = None) -> Procedure:
    """程序的运行时级别与装配时的降级理由."""
    return Procedure(
        id=procedure_id,
        phase=Phase.P2_Executing,
        template_commit="test",
        autonomy_level=level,
        downgrade_reasons=reasons or [],
    )


def judgment(confidence: float = 0.8, **changes: Any) -> Judgment:
    base = Judgment(
        id="J-X-1-S",
        procedure_id="X",
        run_id="RUN-X-1",
        subject_id="S",
        conclusion="",
        confidence=confidence,
        confidence_parts={},
        reasoning_chain=[],
        evidence=[],
        counter_argument="",
        proposed_action=ProposedAction.accept,
        autonomy_level=L3,
        performed_by="U-1",
        created_at=NOW,
    )
    return base.model_copy(update=changes)


def decide(
    pid: str,
    start: Procedure,
    j: Judgment,
    amount: int | None,
    pm: int | None = PM,
    exceptions: list[AuditException] | None = None,
    policy: AutonomyPolicy = POLICY,
    template: ProcedureTemplate | None = None,
) -> tuple[AutonomyLevel, list[DowngradeCondition]]:
    by_id = {x.id: x for x in exceptions or []}
    d = judgment_level(
        policy, template or template_of(pid), start, j, amount=amount, performance_cents=pm, exceptions=by_id
    )
    return d.level, d.reasons


def update(misstatement_id: str, **fields: Any) -> MisstatementUpdate:
    kind = "verification" if "status" in fields else "classification"
    return MisstatementUpdate(misstatement_id=misstatement_id, base_version=1, kind=kind, **fields)


UNSUPPORTED_CHAIN = [ReasoningStep(kind=StepKind.fact, text="x", status=StepStatus.unsupported)]
SAYS_FRAUD = update("M-1", fraud_indicative=True)  # FIN-03 认定的舞弊迹象


# ── 验收 1: 默认表 ──


def test_default_table_in_rules_json() -> None:
    """装配 significant_risk −2, high_risk −1; 判断 amount_over_pm → L1, low_confidence −1 (低于 0.70),
    unsupported_steps −1, fraud_flag → L1. 程序 YAML 都没有标 fraud_flag 的规则."""
    assert [(r.condition, r.at, r.delta, r.target_level) for r in POLICY.defaults] == [
        (SIG, "assembly", -2, None),
        (HIGH, "assembly", -1, None),
        (OVER_PM, "judgment", None, L1),
        (LOW, "judgment", -1, None),
        (UNSUPPORTED, "judgment", -1, None),
        (FRAUD, "judgment", None, L1),
    ]
    assert POLICY.low_confidence_below == 0.70 and POLICY.fraud_rules == frozenset()


# ── 验收 2: 装配级 ──


@pytest.mark.parametrize(
    ("pid", "objective_ids", "level", "reasons"),
    [
        ("REV-08", ["OBJ-REV-OCC", "OBJ-REV-CUT"], L1, [SIG]),  # L3 −2
        ("REV-03", ["OBJ-REV-OCC", "OBJ-REV-CUT"], L1, [SIG]),  # L2 −2 越过下限, 截在 L1
        ("REV-02", ["OBJ-REV-CUT"], L1, [SIG]),
        ("CSH-04", ["OBJ-CASH-EXI"], L3, [HIGH]),  # L4 −1
        ("CSH-05", ["OBJ-CASH-EXI"], L3, [HIGH]),
        ("CSH-02", ["OBJ-CASH-EXI"], L2, [HIGH]),  # L3 −1
        ("DOC-06", [], L3, []),  # 只经依赖加入, 没有关联目标
        ("FIN-04", [], L3, []),  # P3 程序, 没有关联目标
        ("FIN-03", [], L2, []),  # 固定集, 没有关联目标
    ],
)
def test_assembly_level_with_xero_objectives(
    pid: str, objective_ids: list[str], level: AutonomyLevel, reasons: list[DowngradeCondition]
) -> None:
    """程序关联的审计目标取最高风险 (只算一次): 收入目标 significant −2, 现金目标 high −1; 没有关联目标的程序取模板
    级别, 理由为空."""
    decision = assembly_level(POLICY, LIBRARY.load(pid), highest_risk(XERO_OBJECTIVES, objective_ids))
    assert decision == AutonomyDecision(level, reasons)


@pytest.mark.parametrize(
    ("risk", "expected"),
    [
        (RiskLevel.significant, AutonomyDecision(L2, [SIG])),
        (RiskLevel.high, AutonomyDecision(L3, [HIGH])),
        (RiskLevel.medium, AutonomyDecision(L4, [])),
        (RiskLevel.low, AutonomyDecision(L4, [])),
        (None, AutonomyDecision(L4, [])),
    ],
)
def test_assembly_level_by_risk(risk: RiskLevel | None, expected: AutonomyDecision) -> None:
    """每个风险等级: 只有 significant 与 high 降级, medium, low 与没有关联目标取模板级别 (CSH-04 L4)."""
    assert assembly_level(POLICY, LIBRARY.load("CSH-04"), risk) == expected


# ── 验收 3: 下限 L1, 只有级别与理由 ──


def test_floor_is_l1_and_every_hit_is_listed() -> None:
    """起点 L2 累计 −3 (装配 significant_risk −2, 判断 low_confidence −1): 截在 L1, 理由列出全部命中的规则 (含越过
    下限的那条); 起点 L1 再命中仍为 L1, 理由照记; 结果只有 level 与 reasons 两项, 不产生任何标志."""
    assembled = assembly_level(POLICY, LIBRARY.load("REV-02"), RiskLevel.significant)
    assert assembled == AutonomyDecision(L1, [SIG])
    start = procedure("REV-02", assembled.level, assembled.reasons)
    assert decide("REV-02", start, judgment(0.6), None) == (L1, [SIG, LOW])
    assert decide("GEN-02", procedure("GEN-02", L2), judgment(0.6, reasoning_chain=UNSUPPORTED_CHAIN), None) == (
        L1,
        [LOW, UNSUPPORTED],
    )  # L2 −2 越过下限
    assert [f.name for f in dataclasses.fields(AutonomyDecision)] == ["level", "reasons"]


# ── 验收 4: 判断级 ──


@pytest.mark.parametrize(
    ("start", "j", "amount", "pm", "over_pm_target", "expected"),
    [
        (L3, judgment(0.6), 45000, PM, None, (L1, [OVER_PM, LOW])),
        (L3, judgment(0.6), 45000, None, None, (L2, [LOW])),  # 执行重要性未定 (PLN-01/03 之前)
        (L3, judgment(0.9), -45000, PM, None, (L1, [OVER_PM])),  # 按绝对值比较
        (L3, judgment(0.9), PM, PM, None, (L3, [])),  # 等于执行重要性不算超过
        (L3, judgment(0.70), None, PM, None, (L3, [])),  # 等于阈值不算低
        (L4, judgment(0.6, reasoning_chain=UNSUPPORTED_CHAIN), None, PM, None, (L2, [LOW, UNSUPPORTED])),  # delta 相加
        # 先加 delta 再取不高于 target: L4 −1 = L3, target L3 不更低 (先取 target 会得 L2); 覆盖的条件留在默认表的位置
        (L4, judgment(0.6), 45000, PM, L3, (L3, [OVER_PM, LOW])),
        (L2, judgment(0.9), 45000, PM, L3, (L2, [OVER_PM])),  # 目标级高于当前级别时不升
        # A7 不一致照常算级别 (不一致的判断由 classify 定为争议任务)
        (L3, judgment(0.9, critic_verdict=CriticVerdict.inconsistent), None, PM, None, (L3, [])),
        (L2, judgment(0.9, misstatement_update=SAYS_FRAUD), None, PM, None, (L1, [FRAUD])),
        (L2, judgment(0.9, misstatement_update=update("M-1", fraud_indicative=False)), None, PM, None, (L2, [])),
        # 理由按规则表的顺序: low_confidence 在 fraud_flag 之前 (按名称排序会反过来)
        (L4, judgment(0.6, misstatement_update=SAYS_FRAUD), None, PM, None, (L1, [LOW, FRAUD])),
    ],
)
def test_judgment_level_rules(
    start: AutonomyLevel,
    j: Judgment,
    amount: int | None,
    pm: int | None,
    over_pm_target: AutonomyLevel | None,
    expected: tuple[AutonomyLevel, list[DowngradeCondition]],
) -> None:
    """起点为程序运行时级别; 命中规则的 delta 相加, target_level 取不高于它, 不低于 L1; 执行重要性未定时
    amount_over_pm 不适用. over_pm_target: 模板把 amount_over_pm 覆盖为该目标级."""
    template = LIBRARY.load("GEN-02")
    if over_pm_target is not None:
        rule = DowngradeRule(condition=OVER_PM, at="judgment", target_level=over_pm_target)
        template = template.model_copy(update={"downgrade_rules": [rule]})
    assert decide("GEN-02", procedure("GEN-02", start), j, amount, pm, template=template) == expected


@pytest.mark.parametrize(
    ("source", "explained", "marked", "expected"),
    [
        ("GEN-05", True, True, (L1, [FRAUD])),  # 判断解释的例外出自标了 fraud_flag 的规则
        ("GEN-05", False, True, (L2, [])),  # 例外不在判断的 exception_ids 中 (别的判断解释的), 不算
        ("REV-08", True, True, (L2, [])),  # 别的程序同编号的规则: 按 (程序编号, 规则编号) 认
        ("GEN-05", True, False, (L2, [])),  # 规则没标 fraud_flag
    ],
)
def test_fraud_flag_from_marked_rules(
    source: str, explained: bool, marked: bool, expected: tuple[AutonomyLevel, list[DowngradeCondition]]
) -> None:
    """RuleSpec.fraud_flag 经 from_rules 收集为 (程序编号, 规则编号); exceptions 与 judgment_amount 同为例外 ID ->
    例外, 只看判断 exception_ids 中的. 程序 YAML 都没标 fraud_flag, 这里给 GEN-05 的第一条规则标上."""
    gen05 = LIBRARY.load("GEN-05")
    rule = gen05.rules[0]
    if marked:
        gen05 = gen05.model_copy(update={"rules": [rule.model_copy(update={"fraud_flag": True}), *gen05.rules[1:]]})
    rules = json.loads((LIBRARY.root / "rules.json").read_text(encoding="utf-8"))
    policy = AutonomyPolicy.from_rules(rules, [gen05, LIBRARY.load("REV-08")])
    assert policy.fraud_rules == ({("GEN-05", rule.id)} if marked else set())
    x = AuditException(
        id=f"X-{source}-1-{rule.id}-1",
        procedure_id=source,
        run_id=f"RUN-{source}-1",
        rule_id=rule.id,
        severity="high",
        source_ids=[],
        evidence=[],
    )
    j = judgment(0.9, exception_ids=[x.id] if explained else ["X-GEN-05-1-OTHER-1"])
    assert decide("GEN-02", procedure("GEN-02", L2), j, None, exceptions=[x], policy=policy) == expected


# ── 验收 5: Xero 推导表 ──


def misstatement(misstatement_id: str, amount: int) -> Misstatement:
    return Misstatement(
        id=misstatement_id,
        judgment_id="J",
        exception_id=None,
        review_record_id="D",
        type=MisstatementType.factual,
        account="Revenue",
        assertion="Occurrence",
        amount_cents=amount,
        direction=Direction.overstated,
    )


# (判断, 程序, 运行时级别与装配理由, 判断金额, 置信度, 结果). 置信度标 "以 fixture 实算" 的取不低于 0.7 的代表值
# 0.8; "accept 且有例外" 的取上限 0.625 (hybrid 解释例外为没问题时, critique C6); FIN-03 0.85, FIN-09 0.8625
XERO_TABLE: list[tuple[str, str, AutonomyLevel, list[DowngradeCondition], int | None, float, Any]] = [
    *((f"J-REV-03-1-CON-X0{n}", "REV-03", L1, [SIG], None, 0.8, (L1, [SIG])) for n in (1, 2, 3)),
    *((f"J-REV-02-1-SI-ORC10{n}", "REV-02", L1, [SIG], 55000, 0.8, (L1, [SIG, OVER_PM])) for n in (20, 21, 22, 23, 24)),
    ("J-REV-02-1-SI-77b338ef (ORC1026)", "REV-02", L1, [SIG], 330000, 0.625, (L1, [SIG, OVER_PM, LOW])),
    ("J-CSH-02-1-BA-091", "CSH-02", L2, [HIGH], 4853, 0.8, (L2, [HIGH])),
    ("J-CSH-02-1-BA-091 (低于 0.7)", "CSH-02", L2, [HIGH], 4853, 0.65, (L1, [HIGH, LOW])),
    *(
        (f"J-GEN-02-1-{xj}", "GEN-02", L2, [], amount, 0.625, (L1, [OVER_PM, LOW]))
        for xj, amount in (
            ("XJ-397", 1921221),
            ("XJ-405", 55000),
            ("XJ-430", 55000),
            ("XJ-449", 55000),
            ("XJ-601", 350000),
        )
    ),
    *(
        (f"J-GEN-02-1-{xj}", "GEN-02", L2, [], amount, 0.8, (L1, [OVER_PM]))
        for xj, amount in (("XJ-499", 75000), ("XJ-575", 29695))
    ),
    ("J-GEN-06-1-* (45000)", "GEN-06", L3, [], 45000, 0.625, (L1, [OVER_PM, LOW])),
    ("J-GEN-06-1-* (630000, A7 一致)", "GEN-06", L3, [], 630000, 0.75, (L1, [OVER_PM])),
    *((f"J-GEN-03-1-ACN-{acn}", "GEN-03", L2, [], None, 0.8, (L2, [])) for acn in ("900043030", "900043049")),
    ("J-GEN-04-1-CLIENT-XERO-3C1332BB-Q1", "GEN-04", L2, [], None, 0.8, (L2, [])),
    ("J-GEN-01-1-CLIENT-XERO-3C1332BB-Q1", "GEN-01", L1, [], None, 0.8, (L1, [])),
    ("J-ACC-08-1-TERMS (闸门程序)", "ACC-08", L1, [], None, 0.8, (L1, [])),
    ("J-PLN-19-1-* (闸门程序)", "PLN-19", L1, [], None, 0.8, (L1, [])),
    ("J-FIN-15-1-* (F01)", "FIN-15", L2, [], 201000, 0.625, (L1, [OVER_PM, LOW])),
    ("J-FIN-15-1-* (F03)", "FIN-15", L2, [], 426120, 0.8, (L1, [OVER_PM])),  # 有例外且 misstatement, c_rule 一致
]
# FIN-03 分类与 FIN-09 验证: 判断金额取所指错报 (judgment_amount 第 1 步), 错报 ID 为 M-{决定 ID}
MISSTATEMENTS = {
    m.id: m
    for m in (
        misstatement("M-D-XJ499", 75000),
        misstatement("M-D-F03", 426120),
        misstatement("M-D-F04", 201000),
        misstatement("M-D-F05", 268736),
        misstatement("M-D-CSH02-091", 4853),
    )
}
MISSTATEMENT_TABLE = [
    *(
        (pid, mid, confidence, (L2, []) if mid == "M-D-CSH02-091" else (L1, [OVER_PM]))
        for mid in MISSTATEMENTS
        for pid, confidence in (("FIN-03", 0.85), ("FIN-09", 0.8625))
    ),
]


@pytest.mark.parametrize(("name", "pid", "level", "inherited", "amount", "confidence", "expected"), XERO_TABLE)
def test_xero_judgment_levels(
    name: str,
    pid: str,
    level: AutonomyLevel,
    inherited: list[DowngradeCondition],
    amount: int | None,
    confidence: float,
    expected: tuple[AutonomyLevel, list[DowngradeCondition]],
) -> None:
    """验收 5 推导表 (第 13 批删盲判后只去掉 blind 标志, 级别不变)."""
    assert decide(pid, procedure(pid, level, inherited), judgment(confidence), amount) == expected, name


@pytest.mark.parametrize(("pid", "misstatement_id", "confidence", "expected"), MISSTATEMENT_TABLE)
def test_xero_misstatement_judgment_levels(
    pid: str, misstatement_id: str, confidence: float, expected: tuple[AutonomyLevel, list[DowngradeCondition]]
) -> None:
    """验收 5 推导表的 FIN-03, FIN-09 行: 判断金额按 judgment_amount 取所指错报的金额 (与复核档位同一口径)."""
    fields = {"status": "corrected"} if pid == "FIN-09" else {"objective_id": "OBJ-REV-OCC"}
    j = judgment(confidence, misstatement_update=update(misstatement_id, **fields))
    amount = judgment_amount(j, {}, misstatements=MISSTATEMENTS)
    assert amount == MISSTATEMENTS[misstatement_id].amount_cents
    assert decide(pid, procedure(pid, L2), j, amount) == expected


# ── 验收 7: 模板按条件覆盖, 函数不按程序编号分支 ──


def test_template_overrides_by_condition() -> None:
    """模板把 high_risk 改为 −2: CSH-02 L3 → L1 (正好到下限); 没覆盖的条件照默认表."""
    rule = DowngradeRule(condition=HIGH, at="assembly", delta=-2)
    csh02 = LIBRARY.load("CSH-02").model_copy(update={"downgrade_rules": [rule]})
    assert assembly_level(POLICY, csh02, RiskLevel.high) == AutonomyDecision(L1, [HIGH])
    assert assembly_level(POLICY, csh02, RiskLevel.significant) == AutonomyDecision(L1, [SIG])
    assert assembly_level(POLICY, LIBRARY.load("CSH-02"), RiskLevel.high) == AutonomyDecision(L2, [HIGH])


def test_policy_has_no_procedure_ids() -> None:
    """降级函数体内没有程序编号字面量 (按条件判断, 不按程序编号分支); 说明里的例子不算."""
    source = Path(autonomy_policy.__file__).read_text(encoding="utf-8")
    code = re.sub(r'"""[\s\S]*?"""', "", source)  # 去掉文档字符串
    code = "\n".join(line.split("#", 1)[0] for line in code.splitlines())  # 去掉注释
    assert re.findall(r"\b[A-Z]{3}-\d{2}\b", code) == []


# ── 验收 8 与规则写法 ──


def test_conditions_are_only_the_six_of_this_version() -> None:
    """条件只有六种; 首次服务该客户, 财报层次风险, GEN-17 与人机一致率本版不做, 不在其中."""
    assert [c.value for c in DowngradeCondition] == [
        "significant_risk",
        "high_risk",
        "amount_over_pm",
        "low_confidence",
        "unsupported_steps",
        "fraud_flag",
    ]


@pytest.mark.parametrize(
    ("rule", "message"),
    [
        ({"condition": "high_risk", "at": "assembly"}, "恰给一个"),
        ({"condition": "high_risk", "at": "assembly", "delta": -1, "target_level": "L1"}, "恰给一个"),
        ({"condition": "high_risk", "at": "assembly", "delta": 1}, "只降不升"),
        ({"condition": "high_risk", "at": "assembly", "delta": 0}, "只降不升"),  # 不降级却会记理由
        ({"condition": "high_risk", "at": "judgment", "delta": -1}, "assembly 时看"),
        ({"condition": "first_service", "at": "assembly", "delta": -1}, "condition"),
    ],
)
def test_rule_shape_is_checked(rule: dict[str, Any], message: str) -> None:
    """delta 与 target_level 恰给一个, delta 为负; 时点须与条件一致; 条件只有六种."""
    with pytest.raises(ValidationError, match=message):
        DowngradeRule.model_validate(rule)


def test_template_and_default_table_shape_is_checked() -> None:
    """模板 downgrade_rules 同一条件只写一条; 默认表须每个条件恰一条."""
    rule = DowngradeRule(condition=HIGH, at="assembly", delta=-2)
    data = LIBRARY.load("CSH-02").model_dump()
    with pytest.raises(ValidationError, match="条件重复"):
        ProcedureTemplate.model_validate({**data, "downgrade_rules": [rule.model_dump(), rule.model_dump()]})
    with pytest.raises(ValueError, match="每个条件恰一条"):
        AutonomyPolicy(defaults=POLICY.defaults[1:], low_confidence_below=0.7)
