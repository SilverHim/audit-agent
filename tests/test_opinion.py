"""需求 R184: 意见类型的候选与可选范围 (services/opinion.allowed_opinions), A9 的候选与 FIN-01 的校验共用 (见
test_qc, test_fin_gate). 段号按 docs/参考资料 的 ASA 570 原文 (第 21 段否定, 第 22 段无保留加专段, 第 23 段保留或否定).
"""

import pytest

from backend.contracts.models import GoingConcernOutcome, OpinionType, UncorrectedSchedule
from backend.services.opinion import OpinionOptions, allowed_opinions

UNQUALIFIED, QUALIFIED = OpinionType.unqualified, OpinionType.qualified
BOTH = (UNQUALIFIED, QUALIFIED)
XERO_MATERIALITY = 32431  # Xero 演示的整体重要性 (分)
NUMBAT_MATERIALITY = 12000000  # Numbat 的整体重要性 (分)


def schedule(total: int, materiality: int) -> UncorrectedSchedule:
    """合计为 total, 整体重要性为 materiality 的汇总表 (只看这两个数; 候选字段随便填, 函数不读它)."""
    return UncorrectedSchedule(
        id="SCHEDULE",
        items=[],
        excluded_trivial_ids=[],
        total_cents=total,
        materiality_cents=materiality,
        candidate_opinion=UNQUALIFIED,
    )


@pytest.mark.parametrize(
    ("total", "materiality", "candidate"),
    [
        (14200000, NUMBAT_MATERIALITY, QUALIFIED),  # Numbat SCHEDULE v1 (需求 R23)
        (NUMBAT_MATERIALITY, NUMBAT_MATERIALITY, QUALIFIED),  # 等于重要性为保留
        (NUMBAT_MATERIALITY - 1, NUMBAT_MATERIALITY, UNQUALIFIED),
        (4853, XERO_MATERIALITY, UNQUALIFIED),  # Xero 只开本组 (未更正的 M-5)
        (0, XERO_MATERIALITY, UNQUALIFIED),  # Xero 全部接入 (都已更正)
    ],
)
@pytest.mark.parametrize("going_concern", [None, GoingConcernOutcome.no_material_uncertainty])
def test_amount_path(
    total: int, materiality: int, candidate: OpinionType, going_concern: GoingConcernOutcome | None
) -> None:
    """验收 2, 3 前半, 5: 金额路径. 合计不低于整体重要性候选保留, 否则无保留; 可选为无保留与保留 (候选可由审计师推翻,
    需求 R24); 没有持续经营结论与 no_material_uncertainty 都不改金额路径. 理由一句, 写明合计与重要性."""
    options = allowed_opinions(schedule(total, materiality), going_concern)
    assert (options.candidate, options.allowed) == (candidate, BOTH)
    [reason] = options.reasons
    assert f"{total} 分" in reason and f"{materiality} 分" in reason and "ASA 450 para 11" in reason


@pytest.mark.parametrize(("total", "candidate"), [(4853, UNQUALIFIED), (0, UNQUALIFIED), (40000, QUALIFIED)])
def test_material_uncertainty_disclosed_keeps_opinion_type(total: int, candidate: OpinionType) -> None:
    """验收 3: 存在重大不确定性且披露充分时意见类型不变 (无保留加专段, ASA 570 第 22 段): Xero 只开本组 (4853) 与全部
    接入 (0) 都为候选无保留, 可选无保留与保留; 金额已重大时仍为保留."""
    options = allowed_opinions(schedule(total, XERO_MATERIALITY), GoingConcernOutcome.material_uncertainty_disclosed)
    assert (options.candidate, options.allowed) == (candidate, BOTH)
    assert "ASA 570 para 22" in options.reasons[-1]


@pytest.mark.parametrize("total", [0, 4853, 40000])
def test_material_uncertainty_not_disclosed_needs_qualified(total: int) -> None:
    """验收 3: 存在重大不确定性而披露不充分时至少保留 (ASA 570 第 23 段; 否定意见本版不支持), 只可选保留."""
    options = allowed_opinions(
        schedule(total, XERO_MATERIALITY), GoingConcernOutcome.material_uncertainty_not_disclosed
    )
    assert (options.candidate, options.allowed) == (QUALIFIED, (QUALIFIED,))
    assert any("ASA 570 para 23" in r for r in options.reasons)


def test_basis_inappropriate_allows_nothing() -> None:
    """验收 3: 持续经营假设不恰当本版不支持 (ASA 570 第 21 段要求否定意见; FIN-02 闸门已拒绝批准它, 正常走不到): 没有
    可选的意见类型, 理由写明不支持."""
    options = allowed_opinions(schedule(0, XERO_MATERIALITY), GoingConcernOutcome.basis_inappropriate)
    assert (options.candidate, options.allowed) == (OpinionType.adverse, ())
    assert any("本版不支持持续经营假设不恰当 (否定意见)" in r for r in options.reasons)


@pytest.mark.parametrize("going_concern", [None, *GoingConcernOutcome])
@pytest.mark.parametrize("total", [0, NUMBAT_MATERIALITY - 1, NUMBAT_MATERIALITY, 14200000])
def test_adverse_and_disclaimer_never_allowed(total: int, going_concern: GoingConcernOutcome | None) -> None:
    """验收 1, 4: 任何输入下否定意见与无法表示意见都不可选; 可选的按 OpinionType 的顺序; 同输入同输出 (纯函数)."""
    options = allowed_opinions(schedule(total, NUMBAT_MATERIALITY), going_concern)
    assert not {OpinionType.adverse, OpinionType.disclaimer} & set(options.allowed)
    assert list(options.allowed) == sorted(options.allowed, key=list(OpinionType).index)
    assert allowed_opinions(schedule(total, NUMBAT_MATERIALITY), going_concern) == options
    assert isinstance(options, OpinionOptions)
