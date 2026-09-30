"""审计师决定的效果. 纯函数直接调用, 不经图."""

from datetime import UTC, datetime

from backend.contracts.models import GateDecision, GateId, GateVerdict, Phase, ProcedureStatus
from backend.orchestration.decisions import acc01_effects
from tests.fakes import make_procedure

NOW = datetime(2026, 10, 1, tzinfo=UTC)


def acc01_decision(verdict: GateVerdict) -> GateDecision:
    return GateDecision(
        id="D-1",
        gate=GateId.ACC_01,
        target_id="SCORECARD",
        target_version=1,
        decision=verdict,
        reason=None if verdict is GateVerdict.approve else "test",
        decided_by="U-1",
        decided_at=NOW,
    )


# ── 4a ACC-01 闸门决定的效果 ──

# 刚过 p0_intel 的程序包: ACC-04 已完成第 2 版, ACC-01 计划中第 1 版
PACK = [
    make_procedure("ACC-04", version=2).model_copy(update={"status": ProcedureStatus.completed}),
    make_procedure("ACC-01"),
]


def test_acc01_approve_completes_gate_and_enters_p1() -> None:
    decision = acc01_decision(GateVerdict.approve)

    patch = acc01_effects(PACK, decision)

    assert set(patch) == {"gate_decisions", "procedure_pack", "phase"}
    assert patch["gate_decisions"] == [decision]
    assert patch["phase"] == Phase.P1_Planning
    pack = patch["procedure_pack"]
    assert [p.id for p in pack] == ["ACC-04", "ACC-01"]
    assert pack[1].status == ProcedureStatus.completed
    assert pack[1].version == 2
    assert pack[0] is PACK[0]  # ACC-04 不动


def test_acc01_reject_completes_gate_and_declines() -> None:
    patch = acc01_effects(PACK, acc01_decision(GateVerdict.reject))

    assert patch["phase"] == Phase.Declined
    assert patch["procedure_pack"][1].status == ProcedureStatus.completed


def test_acc01_insufficient_info_only_records_decision() -> None:
    decision = acc01_decision(GateVerdict.insufficient_info)

    patch = acc01_effects(PACK, decision)

    assert patch == {"gate_decisions": [decision]}
