"""需求 R184: 意见类型的候选与可选范围 (services/opinion.allowed_opinions), A9 的候选与 FIN-01 的校验共用 (A9 与
FIN-01 的行为见 test_qc, test_fin_gate). 段号按 docs/参考资料 的 ASA 570 原文 (第 21 段否定, 第 22 段无保留加专段,
第 23 段保留或否定).
"""

import pytest

from backend.contracts.models import GoingConcernOutcome, OpinionType
from backend.services.opinion import OpinionOptions, allowed_opinions

UNQUALIFIED, QUALIFIED = OpinionType.unqualified, OpinionType.qualified
BOTH = (UNQUALIFIED, QUALIFIED)
XERO_MATERIALITY = 32431  # Xero 演示的整体重要性 (分)
NUMBAT_MATERIALITY = 12000000  # Numbat 的整体重要性 (分)


def options(total: int, materiality: int, going_concern: GoingConcernOutcome | None) -> OpinionOptions:
    return allowed_opinions(total_cents=total, materiality_cents=materiality, going_concern=going_concern)


@pytest.mark.parametrize(
    ("total", "materiality", "candidate", "compared"),
    [
        (14200000, NUMBAT_MATERIALITY, QUALIFIED, "不低于"),  # Numbat SCHEDULE v1 (需求 R23)
        (NUMBAT_MATERIALITY, NUMBAT_MATERIALITY, QUALIFIED, "不低于"),  # 等于重要性为保留
        (NUMBAT_MATERIALITY - 1, NUMBAT_MATERIALITY, UNQUALIFIED, "低于"),
        (4853, XERO_MATERIALITY, UNQUALIFIED, "低于"),  # Xero 只开本组 (未更正的 M-5)
        (0, XERO_MATERIALITY, UNQUALIFIED, "低于"),  # Xero 全部接入 (都已更正)
    ],
)
@pytest.mark.parametrize("going_concern", [None, GoingConcernOutcome.no_material_uncertainty])
def test_amount_path(
    total: int, materiality: int, candidate: OpinionType, compared: str, going_concern: GoingConcernOutcome | None
) -> None:
    """验收 2, 3 前半, 5: 金额路径. 合计不低于整体重要性候选保留, 否则无保留; 可选为无保留与保留 (候选可由审计师推翻,
    需求 R24); 没有持续经营结论与 no_material_uncertainty 都不改金额路径. 理由一句, 写成金额路径的结论."""
    found = options(total, materiality, going_concern)
    assert (found.candidate, found.allowed) == (candidate, BOTH)
    assert found.reasons == [
        f"金额路径: 未更正错报合计 {total} 分{compared}整体重要性 {materiality} 分, 金额路径候选 {candidate.value} "
        "(可由审计师推翻, ASA 450 para 11)"
    ]


@pytest.mark.parametrize(("total", "candidate"), [(4853, UNQUALIFIED), (0, UNQUALIFIED), (40000, QUALIFIED)])
def test_material_uncertainty_disclosed_keeps_opinion_type(total: int, candidate: OpinionType) -> None:
    """验收 3: 存在重大不确定性且披露充分时意见类型不变 (无保留加专段, ASA 570 第 22 段): Xero 只开本组 (4853) 与全部
    接入 (0) 都为候选无保留, 可选无保留与保留; 金额已重大时仍为保留."""
    found = options(total, XERO_MATERIALITY, GoingConcernOutcome.material_uncertainty_disclosed)
    assert (found.candidate, found.allowed) == (candidate, BOTH)
    assert len(found.reasons) == 2 and "ASA 570 para 22" in found.reasons[-1]


@pytest.mark.parametrize(("total", "changed"), [(0, "改为"), (4853, "改为"), (40000, "仍为")])
def test_material_uncertainty_not_disclosed_needs_qualified(total: int, changed: str) -> None:
    """验收 3: 存在重大不确定性而披露不充分时至少保留 (ASA 570 第 23 段; 否定意见本版不支持), 只可选保留. 金额那句仍是
    金额路径自己的结论, 较严的结果另写一句, 两句不矛盾 (审核 R184-02)."""
    found = options(total, XERO_MATERIALITY, GoingConcernOutcome.material_uncertainty_not_disclosed)
    assert (found.candidate, found.allowed) == (QUALIFIED, (QUALIFIED,))
    amount, going_concern = found.reasons
    assert amount.startswith("金额路径: ") and "金额路径候选 " in amount
    assert "ASA 570 para 23" in going_concern
    assert going_concern.endswith(f"较严, 候选{changed} qualified, 不可选 unqualified")


def test_basis_inappropriate_allows_nothing() -> None:
    """验收 3: 持续经营假设不恰当本版不支持 (ASA 570 第 21 段要求否定意见; GC-10 起 FIN-02 闸门拒绝批准它, 正常走不到):
    没有可选的意见类型, 理由写明不支持与段号."""
    found = options(0, XERO_MATERIALITY, GoingConcernOutcome.basis_inappropriate)
    assert (found.candidate, found.allowed) == (OpinionType.adverse, ())
    assert "本版不支持持续经营假设不恰当 (否定意见)" in found.reasons[-1]
    assert "ASA 570 para 21" in found.reasons[-1]
    assert found.reasons[-1].endswith("较严, 候选改为 adverse, 没有可选的意见类型")


@pytest.mark.parametrize("going_concern", [None, *GoingConcernOutcome])
@pytest.mark.parametrize("total", [0, NUMBAT_MATERIALITY - 1, NUMBAT_MATERIALITY, 14200000])
def test_adverse_and_disclaimer_never_allowed(total: int, going_concern: GoingConcernOutcome | None) -> None:
    """验收 1, 4: 任何输入下否定意见与无法表示意见都不可选; 可选的按 OpinionType 的顺序; 可选非空时候选在其中
    (OpinionOptions 的不变式); 同输入同输出 (纯函数)."""
    found = options(total, NUMBAT_MATERIALITY, going_concern)
    assert not {OpinionType.adverse, OpinionType.disclaimer} & set(found.allowed)
    assert list(found.allowed) == sorted(found.allowed, key=list(OpinionType).index)
    assert not found.allowed or found.candidate in found.allowed
    assert options(total, NUMBAT_MATERIALITY, going_concern) == found


@pytest.mark.parametrize("going_concern", ["no_such_outcome", OpinionType.qualified], ids=["unknown", "opinion_type"])
def test_input_that_is_not_an_outcome_fails_closed(going_concern: object) -> None:
    """不是持续经营结论取值的输入 (未知的值, 误传的 OpinionType) 报错 (assert_never), 不按金额路径放行. 枚举由读取
    边界还原, 本函数不承诺接受字符串 (审核 R184-10)."""
    with pytest.raises(AssertionError):
        options(0, XERO_MATERIALITY, going_concern)  # type: ignore[arg-type]
