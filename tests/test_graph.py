"""主图骨架：节点尚为占位，验证拓扑、四道静态中断、闸门决定经 aupdate_state(as_node=闸门) 越过闸门。"""

from datetime import UTC, date, datetime

import pytest
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from backend.contracts.models import Engagement, GateDecision, GateId, GateVerdict, Phase
from backend.contracts.state import initial_state
from backend.core.config import Settings, get_settings
from backend.main import build_deps
from backend.orchestration.checkpoint import checkpoint_serde, open_checkpointer
from backend.orchestration.graph import GATE_NODES, compile_graph
from backend.orchestration.nodes import GraphNodes
from backend.orchestration.runner import GraphRunner, fixed_procedures

NOW = datetime(2026, 9, 24, tzinfo=UTC)


def _decision(n: int, gate: GateId, verdict: GateVerdict) -> GateDecision:
    return GateDecision(
        id=f"D-{n}",
        gate=gate,
        target_id="T",
        target_version=1,
        decision=verdict,
        reason=None if verdict is GateVerdict.approve else "test",
        decided_by="U-1",
        decided_at=NOW,
    )


async def test_walk_through_all_gates() -> None:
    await _walk(InMemorySaver(serde=checkpoint_serde()), "ENG-2026-001")


@pytest.mark.slow
async def test_walk_through_all_gates_postgres() -> None:
    """需先 docker compose up -d db 并建检查点表；用 .env 中的 DATABASE_URL（应用角色）。"""
    settings = Settings(CHECKPOINT_BACKEND="postgres")
    async with open_checkpointer(settings) as checkpointer:
        await _walk(checkpointer, f"ENG-TEST-{datetime.now(UTC):%H%M%S%f}")


async def _walk(checkpointer: BaseCheckpointSaver[str], eid: str) -> None:
    settings = get_settings()
    deps = build_deps(settings)
    app = compile_graph(GraphNodes(deps, settings.PARALLEL_CAP), checkpointer)
    engagement = Engagement(
        id=eid,
        client_id="CLIENT-001",
        client_name="Test Pty Ltd",
        abn="00000000000",
        period_start=date(2025, 7, 1),
        period_end=date(2026, 6, 30),
        dataset="demo",
        owner_id="U-1",
        phase=Phase.P0_Acceptance,
        created_at=NOW,
        updated_at=NOW,
    )
    config = GraphRunner.config(engagement.id)
    await app.ainvoke(initial_state(engagement, fixed_procedures(deps.library)), config)

    steps = [
        (GateId.ACC_01, GateVerdict.approve, "p1_ingest", "gate_p1_risk"),
        (GateId.PLN_01_03, GateVerdict.approve, "assemble_pack", "gate_fin"),
        (GateId.FIN_01, GateVerdict.approve, "report_draft", "gate_rpt01"),
        (GateId.RPT_01, GateVerdict.reject, "report_draft", "gate_rpt01"),
        (GateId.RPT_01, GateVerdict.approve, "archive", None),
    ]
    for n, (gate, verdict, after_decision, next_gate) in enumerate(steps):
        snapshot = await app.aget_state(config)
        assert snapshot.next == (GATE_NODES[gate],)
        await app.aupdate_state(config, {"gate_decisions": [_decision(n, gate, verdict)]}, as_node=GATE_NODES[gate])
        assert (await app.aget_state(config)).next == (after_decision,)
        await app.ainvoke(None, config)
        assert (await app.aget_state(config)).next == ((next_gate,) if next_gate else ())

    final = (await app.aget_state(config)).values
    assert [d.id for d in final["gate_decisions"]] == ["D-0", "D-1", "D-2", "D-3", "D-4"]
    assert isinstance(final["gate_decisions"][0], GateDecision)
