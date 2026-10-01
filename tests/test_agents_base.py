"""ProcedureAgent 三步: 按归属选程序, run 先 execute 后 interpret, 注册表覆盖全部声明的程序."""

from typing import ClassVar

import pytest

from backend.agents.base import AgentDeps, ProcedureAgent, RunContext
from backend.agents.fieldwork import FieldworkAgent
from backend.agents.registry import AGENT_CLASSES, build_agents
from backend.contracts.models import AgentId
from backend.contracts.state import StatePatch
from tests.fakes import FakeRegistry, FakeTrail, make_ctx, make_deps


def make_plain_deps() -> AgentDeps:
    return make_deps(FakeRegistry({}, {}), FakeTrail())


class ProbeAgent(ProcedureAgent[str]):
    """记下两步的调用顺序, interpret 把 execute 的发现原样放进片段."""

    agent_id: ClassVar[AgentId] = AgentId.A2  # 智能体编号
    procedures: ClassVar[frozenset[str]] = frozenset({"ACC-04"})  # 负责的程序

    def __init__(self, deps: AgentDeps) -> None:
        super().__init__(deps)
        self.calls: list[str] = []  # 调用顺序

    async def execute(self, ctx: RunContext) -> str:
        self.calls.append("execute")
        return f"findings for {ctx.procedure.id}"

    async def interpret(self, ctx: RunContext, findings: str) -> StatePatch:
        self.calls.append("interpret")
        return {"phase": findings}


async def test_run_executes_then_interprets() -> None:
    deps = make_plain_deps()
    agent = ProbeAgent(deps)

    patch = await agent.run(make_ctx(deps))

    assert agent.calls == ["execute", "interpret"]
    assert patch == {"phase": "findings for ACC-04"}


def test_select_procedures_filters_by_ownership() -> None:
    deps = make_plain_deps()
    selected = FieldworkAgent(deps).select_procedures(deps.library.all())
    assert {t.id for t in selected} == {"REV-08", "REV-03", "REV-02", "CSH-05", "CSH-02"}


def test_build_agents_maps_every_declared_procedure() -> None:
    agents = build_agents(make_plain_deps())
    declared = {pid for cls in AGENT_CLASSES for pid in cls.procedures}
    assert set(agents) == declared


def test_build_agents_rejects_procedure_without_template(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(FieldworkAgent, "procedures", frozenset({"REV-08", "XYZ-99"}))
    with pytest.raises(LookupError, match="XYZ-99"):
        build_agents(make_plain_deps())
