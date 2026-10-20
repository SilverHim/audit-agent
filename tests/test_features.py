"""功能开关. R75: 登记表与校验, 程序库按功能过滤 (固定集, 装配, 依赖), build_agents, A7 的 critic 开关, 节点只执行
程序包中有的程序. R76: 功能集按项目冻结, 与当前进程不同时不推进, 决定被拒.
自测用的程序 (X-NEW, X-P2, Y) 写在 tmp 复制的 knowledge 目录里, 不进仓库."""

import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any, ClassVar

import pytest

from backend.agents import registry
from backend.agents.base import ProcedureAgent, RunContext
from backend.agents.fieldwork import FieldworkAgent
from backend.contracts.errors import DecisionRejected
from backend.contracts.models import AgentId, GateVerdict, Phase, TaskKind, TrailKind
from backend.contracts.state import StatePatch
from backend.core.config import Settings, get_settings
from backend.main import build_deps
from backend.orchestration import nodes as graph_nodes
from backend.orchestration.decisions import GraphDecisionSubmit
from backend.orchestration.graph import compile_graph
from backend.orchestration.query import GraphStateQuery
from backend.orchestration.runner import GraphRunner, features_mismatch, fixed_procedures
from backend.services.features import (
    BASELINE,
    FEATURES,
    check_features,
    features_text,
    frozen_features,
    parse_features,
)
from backend.services.procedure_library import ProcedureLibrary
from tests.fakes import (
    ENGAGEMENT,
    NUMBAT_FEATURES,
    NUMBAT_PROFILE,
    PLANNING_FEATURES,
    FakeEngagementStore,
    FakeRegistry,
    FakeTrail,
    ScriptAgent,
    api_client,
    finished,
    make_deps,
    make_graph_nodes,
    make_runner,
    profile_update,
    wait_idle,
)
from tests.test_assemble import P2, added_programs, after_pln_approve
from tests.test_decisions import acc01_decision, started
from tests.test_fieldwork_csh02 import setup as csh02_setup
from tests.test_qc import misstatement
from tests.test_recovery import submit_lost
from tests.test_review_decisions import record

EID = ENGAGEMENT.id
KNOWLEDGE = get_settings().KNOWLEDGE_ROOT
X_NEW = """id: X-NEW
name: 自测 P3 程序
phase: P3_Completion
cycle: Completion
owner: A9
engine: rule
autonomy_level: L1
feature: misstatement
"""
X_P2 = """id: X-P2
name: 自测 P2 程序
phase: P2_Executing
cycle: Revenue
owner: A6
engine: rule
autonomy_level: L2
feature: journals
triggers:
  - {account: Revenue, assertions: [Cutoff], level: [medium, high, significant]}
"""
Y = """id: Y
name: 自测基线程序, 依赖 X-P2
phase: P2_Executing
cycle: Revenue
owner: A6
engine: rule
autonomy_level: L2
depends_on: [DOC-04, X-P2]
"""


def knowledge(tmp_path: Path, **extra: str) -> Path:
    """tmp 复制的 knowledge 目录, 另加 extra 中的程序 YAML (文件名主干 → 内容)."""
    root = tmp_path / "knowledge"
    shutil.copytree(KNOWLEDGE, root)
    for name, text in extra.items():
        (root / "procedures" / f"{name}.yaml").write_text(text, encoding="utf-8")
    return root


def test_registry_and_parsing() -> None:
    """验收 1, 2: 11 个功能名与 requires (R313: planning 只含 PLN-15 且不依赖其他功能, PLN-19 另立 reassessment);
    逗号分隔解析; 未登记的名字与依赖不满足都报出名字."""
    assert list(FEATURES) == [
        "critic",
        "collab",
        "journals",
        "related_parties",
        "going_concern",
        "misstatement",
        "autonomy",
        "acceptance",
        "planning",
        "reassessment",
        "presign",
    ]
    assert {name: set(spec.requires) for name, spec in FEATURES.items() if spec.requires} == {
        "collab": {"critic"},
        "reassessment": {"misstatement", "planning"},
        "presign": {"misstatement"},
    }
    assert "PLN-15" in FEATURES["planning"].description and "PLN-19" not in FEATURES["planning"].description
    assert "PLN-19" in FEATURES["reassessment"].description
    assert check_features(["critic", "planning"]) == {"critic", "planning"}
    with pytest.raises(ValueError, match="reassessment 需要 misstatement, planning"):
        check_features(["critic", "reassessment"])
    with pytest.raises(ValueError, match=r"reassessment 需要 planning$"):
        check_features(["critic", "misstatement", "reassessment"])
    assert BASELINE == NUMBAT_FEATURES == {"critic"}
    assert parse_features("critic, journals,,") == {"critic", "journals"} and parse_features("") == frozenset()
    assert check_features(["critic", "journals"]) == {"critic", "journals"}
    assert check_features([]) == frozenset()
    with pytest.raises(ValueError, match="foo"):
        check_features(["critic", "foo"])
    with pytest.raises(ValueError, match="presign 需要 misstatement"):
        check_features(["presign"])
    with pytest.raises(ValueError, match="collab 需要 critic"):
        check_features(["collab"])


def test_build_deps_reads_features() -> None:
    """验收 2: 配置项 FEATURES 缺省为基线; "critic,journals" 进 AgentDeps 与程序库; 未登记的名字与依赖不满足时
    build_deps 抛 ValueError."""
    assert Settings.model_fields["FEATURES"].default == "critic" and Settings(FEATURES="critic").features == BASELINE
    deps = build_deps(Settings(FEATURES="critic,journals"))
    assert deps.features == deps.library.features == {"critic", "journals"}
    with pytest.raises(ValueError, match="foo"):
        build_deps(Settings(FEATURES="critic,foo"))
    with pytest.raises(ValueError, match=r"presign.*misstatement"):
        build_deps(Settings(FEATURES="presign"))


async def test_library_filters_fixed_set_and_pack(tmp_path: Path) -> None:
    """验收 3: X-NEW (P3, misstatement) 与 X-P2 (P2, journals) 在基线下不启用: all() 不含, all(include_disabled)
    含, 固定集为原 10 条, 装配新增原 7 条; 开 journals 时装配多 X-P2 与同属 journals 的固定集 GEN-05, GEN-02
    (需求 R131, R134), 开 misstatement 时固定集多 X-NEW."""
    root = knowledge(tmp_path, **{"X-NEW": X_NEW, "X-P2": X_P2})
    base = ProcedureLibrary(root)
    assert {"X-NEW", "X-P2"}.isdisjoint(t.id for t in base.all())
    assert {"X-NEW", "X-P2"} <= {t.id for t in base.all(include_disabled=True)}
    assert base.load("X-P2").feature == "journals" and not base.enabled("X-P2")  # load 不限
    assert [p.id for p in fixed_procedures(base)] == [p.id for p in fixed_procedures(ProcedureLibrary(KNOWLEDGE))]
    assert len(fixed_procedures(base)) == 10

    nodes, _, _, state = after_pln_approve(library=base)
    added = added_programs((await nodes.assemble_pack(state))["procedure_pack"])
    assert [p.id for p in added] == P2

    journals = ProcedureLibrary(root, {"critic", "journals"})
    nodes, _, _, state = after_pln_approve(library=journals, features=journals.features)
    added = added_programs((await nodes.assemble_pack(state))["procedure_pack"])
    assert len(added) == 10 and [p.id for p in added if p.id not in P2] == ["GEN-05", "GEN-02", "X-P2"]
    assert next(p for p in added if p.id == "X-P2").objective_ids == ["OBJ-REV-CUT"]

    # 开 misstatement 时固定集比现有程序库同功能集的多 X-NEW (现有库此时另有 FIN-03, 需求 R157)
    fixed = fixed_procedures(ProcedureLibrary(root, {"critic", "misstatement"}))
    real = fixed_procedures(ProcedureLibrary(KNOWLEDGE, {"critic", "misstatement"}))
    assert [p.id for p in fixed if p.id != "X-NEW"] == [p.id for p in real] and len(fixed) == len(real) + 1
    assert next(p for p in fixed if p.id == "X-NEW").phase is Phase.P3_Completion


def test_dependencies_on_disabled_templates_are_dropped(tmp_path: Path) -> None:
    """验收 4: 启用的 Y 依赖未启用的 X-P2 时构造不报错, 启用视图中 Y 的依赖去掉 X-P2 (闭包也不补它); 开 journals
    时依赖照原样. 模板的 feature 为未登记的名字时 ValueError."""
    root = knowledge(tmp_path, **{"X-P2": X_P2, "Y": Y})
    base = ProcedureLibrary(root)
    assert base.load("Y").depends_on == ["DOC-04"] and base.enabled("Y")
    assert [t.id for t in base.closure(["Y"])] == ["DOC-04", "Y"]
    assert base.load("X-P2").depends_on == []
    journals = ProcedureLibrary(root, {"critic", "journals"})
    assert journals.load("Y").depends_on == ["DOC-04", "X-P2"]
    assert [t.id for t in journals.closure(["Y"])] == ["DOC-04", "X-P2", "Y"]
    with pytest.raises(KeyError):
        base.closure(["X-P2"])  # 未启用的程序不进闭包
    (root / "procedures" / "Y.yaml").write_text(Y + "feature: foo\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Y: foo"):
        ProcedureLibrary(root)
    with pytest.raises(ValueError, match="bar"):
        ProcedureLibrary(KNOWLEDGE, {"bar"})


class XAgent(ProcedureAgent[None]):
    """声明 X-P2 的自测 Agent (不执行)."""

    agent_id: ClassVar[AgentId] = AgentId.A6
    procedures: ClassVar[frozenset[str]] = frozenset({"X-P2"})

    async def execute(self, ctx: RunContext) -> None:
        raise NotImplementedError

    async def interpret(self, ctx: RunContext, findings: None) -> StatePatch:
        raise NotImplementedError


def test_build_agents_maps_enabled_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """验收 5: Agent 声明的 X-P2 未启用时不报缺模板, 也不映射; 开 journals 时映射; 删掉 X-P2 的 YAML 仍报缺模板.
    基线下 GEN-09 由 A7 映射, critic 关闭时不映射."""
    monkeypatch.setattr(registry, "AGENT_CLASSES", (*registry.AGENT_CLASSES, XAgent))
    root = knowledge(tmp_path, **{"X-P2": X_P2})

    def agents(features: frozenset[str]) -> dict[str, ProcedureAgent[Any]]:
        library = ProcedureLibrary(root, features)
        return registry.build_agents(make_deps(FakeRegistry(), FakeTrail(), library=library, features=features))

    base = agents(BASELINE)
    assert "X-P2" not in base and base["GEN-09"].agent_id is AgentId.A7
    assert isinstance(agents(frozenset({"critic", "journals"}))["X-P2"], XAgent)
    assert "GEN-09" not in agents(frozenset())
    (root / "procedures" / "X-P2.yaml").unlink()
    with pytest.raises(LookupError, match="X-P2"):
        agents(BASELINE)


async def test_critic_off_skips_independent_review() -> None:
    """验收 6: R49 的 Agent 层集成 (Numbat, 真实判断引擎, mock). 基线: consistent, 置信度 0.65, GEN-09 两轮调用;
    critic 关闭: skipped, 理由含 critic, GEN-09 不调用, 置信度 0.5625 (没有复核分项), 仍建 normal 任务."""
    for features, expected in ((BASELINE, ("consistent", 0.65, 2)), (frozenset(), ("skipped", 0.5625, 0))):
        agent, ctx, trail = csh02_setup(real_engine=True)
        library = ProcedureLibrary(KNOWLEDGE, features)
        agent = FieldworkAgent(replace(agent.deps, features=features, library=library))
        patch = await agent.run(ctx)
        calls = [e for e in trail.entries if e.kind is TrailKind.llm_call]
        [j] = patch["judgments"]
        assert j.id == "J-CSH-02-1-BA-01"
        assert (j.critic_verdict, j.confidence, sum(e.procedure_id == "GEN-09" for e in calls)) == expected
        assert [e.procedure_id for e in calls if e.procedure_id != "GEN-09"] == ["CSH-02"]
        [task] = patch["review_tasks"]
        assert task.kind is TaskKind.normal  # CSH-02 always_review
    assert j.critic_reason is not None and "critic" in j.critic_reason


async def test_node_runs_only_procedures_in_pack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """验收 7: completion 节点的程序表改为 (X-NEW, FIN-04). 基线下 X-NEW 不在固定集, 只执行 FIN-04, 片段与留痕同
    现状; 开 misstatement 时先执行 X-NEW (脚本 Agent) 再执行 FIN-04."""
    root = knowledge(tmp_path, **{"X-NEW": X_NEW})

    async def completion(features: frozenset[str], patched: bool) -> tuple[StatePatch, list[Any], list[str]]:
        library = ProcedureLibrary(root, features)
        nodes, _, _, state = after_pln_approve(library=library, features=features)
        state["misstatements"] = [misstatement(1, 14200000)]
        order: list[str] = []

        def script(ctx: RunContext) -> StatePatch:
            order.append(ctx.procedure.id)
            return {"procedure_runs": [finished(ctx.run)]}

        nodes.agents["X-NEW"] = ScriptAgent(nodes.deps, script)
        with monkeypatch.context() as m:
            if patched:
                m.setitem(graph_nodes.NODE_PROCEDURES, "completion", ("X-NEW", "FIN-04"))
            patch = await nodes.completion(state)
        entries = nodes.deps.trail.entries  # type: ignore[attr-defined]
        trail = [(e.kind, e.actor, e.procedure_id, e.summary, e.payload.get("object")) for e in entries]
        return patch, trail, order

    current, current_trail, _ = await completion(BASELINE, patched=False)
    patch, trail, order = await completion(BASELINE, patched=True)
    assert order == [] and [r.id for r in patch["procedure_runs"]] == ["RUN-FIN-04-1"]
    assert patch["uncorrected_schedule"] == current["uncorrected_schedule"]
    assert patch["procedure_pack"] == current["procedure_pack"] and trail == current_trail

    phase = [t[2] for t in current_trail if t[0] is TrailKind.transition and t[4] == "engagement"]
    assert phase == ["FIN-04"]  # 阶段 P1 → P3 的留痕归在执行的程序下
    patch, trail, order = await completion(frozenset({"critic", "misstatement"}), patched=True)
    assert order == ["X-NEW"]
    # 两条程序都带 phase: 第一条已把阶段推进到 P3_Completion, 阶段留痕只有 1 行, 归在第一条程序下
    assert [t[2] for t in trail if t[0] is TrailKind.transition and t[4] == "engagement"] == ["X-NEW"]
    assert [r.id for r in patch["procedure_runs"]] == ["RUN-X-NEW-1", "RUN-FIN-04-1"]
    assert patch["uncorrected_schedule"].total_cents == 14200000 and patch["phase"] is Phase.P3_Completion
    statuses = {p.id: p.status.value for p in patch["procedure_pack"] if p.id in ("X-NEW", "FIN-04")}
    assert statuses == {"X-NEW": "completed", "FIN-04": "completed"}


def test_node_procedures_keeps_order_and_skips_feature_programs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """node_procedures 按 NODE_PROCEDURES 的顺序取程序包中有的. 挂了功能名的程序不在程序包中时跳过 (功能未启用,
    或旧项目的程序包缺后来新增的程序); 基线程序不在程序包中时抛 LookupError, 节点不静默跳过 (如 completion 缺 FIN-04
    时不出 SCHEDULE 就走到 gate_fin)."""
    library = ProcedureLibrary(knowledge(tmp_path, **{"X-NEW": X_NEW}))
    pack = fixed_procedures(library)
    assert graph_nodes.node_procedures("p1_analytics", pack, library) == ["PLN-07", "PLN-01"]
    with pytest.raises(LookupError, match="p1_analytics: 程序包中没有基线程序 PLN-07"):
        graph_nodes.node_procedures("p1_analytics", [p for p in pack if p.id != "PLN-07"], library)
    with pytest.raises(LookupError, match="FIN-04"):
        graph_nodes.node_procedures("completion", [], library)
    monkeypatch.setitem(graph_nodes.NODE_PROCEDURES, "completion", ("X-NEW", "FIN-04"))
    assert graph_nodes.node_procedures("completion", pack, library) == ["FIN-04"]  # X-NEW 未启用, 不在固定集


def test_make_deps_keeps_features_and_library_consistent(tmp_path: Path) -> None:
    """make_deps: 没给 library 时按 features 建程序库 (缺省为基线, 沿用组装根的); 给了 library 时功能集缺省取它的,
    显式给的与之不同时抛 ValueError."""
    journals = frozenset({"critic", "journals"})
    base = make_deps(FakeRegistry(), FakeTrail())
    assert base.features == base.library.features == BASELINE
    rebuilt = make_deps(FakeRegistry(), FakeTrail(), features=journals)
    assert rebuilt.features == rebuilt.library.features == journals and rebuilt.library is not base.library
    library = ProcedureLibrary(KNOWLEDGE, journals)
    assert make_deps(FakeRegistry(), FakeTrail(), library=library).features == journals
    with pytest.raises(ValueError, match="不一致"):
        make_deps(FakeRegistry(), FakeTrail(), library=library, features=BASELINE)


# ── R76: 功能集按项目冻结与不一致拒绝 ──

JOURNALS = frozenset({"critic", "journals"})


def rebuilt(runner: GraphRunner, features: frozenset[str]) -> GraphRunner:
    """同一检查点与存储, 换成另一功能集的进程 (模拟改了 FEATURES 后重启): 程序库, 依赖与主图按 features 重建."""
    deps = replace(runner.deps, features=features, library=ProcedureLibrary(KNOWLEDGE, features))
    return GraphRunner(compile_graph(make_graph_nodes(deps), runner.app.checkpointer), deps)


async def frozen_state(runner: GraphRunner) -> tuple[Any, ...]:
    """检查点 (值, 下一步, 历史条数) 与 Store 的行数, 用来断言被拒时什么都没写."""
    config = GraphRunner.config(EID)
    snapshot = await runner.app.aget_state(config)
    history = [h async for h in runner.app.aget_state_history(config)]
    rows = len(runner.deps.output_store.rows)  # type: ignore[attr-defined]
    return snapshot.values, snapshot.next, len(history), rows


async def test_mismatched_features_do_not_advance(monkeypatch: pytest.MonkeyPatch) -> None:
    """R76 验收 1, 3: 基线启动的项目冻结 features "critic", 停在 ACC-01. 换成 {critic, journals} 的 runner:
    recover 不调 ainvoke, run_error 为 FEATURES_MISMATCH; 提交 ACC-01 决定被拒, Store 与检查点不变, 经 HTTP 为 409;
    resume 同样不推进. 换回基线 runner 后 recover 与提交照常."""
    s = await started()
    engagement = await s.engagement_store.get(EID)
    assert engagement is not None and engagement.version_snapshot["features"] == "critic"
    assert await s.next_nodes() == ("gate_acc01",)

    other = rebuilt(s.runner, JOURNALS)
    calls: list[Any] = []
    monkeypatch.setattr(other.app, "ainvoke", lambda *args, **kwargs: calls.append(args))
    before = await frozen_state(other)
    await other.recover()
    assert calls == [] and not other.is_running(EID) and not other.tasks
    assert other.run_error(EID) == "FEATURES_MISMATCH: 项目冻结 critic, 当前 critic,journals"
    with pytest.raises(DecisionRejected) as raised:
        await GraphDecisionSubmit(other.app, other.deps, other).submit(EID, acc01_decision(GateVerdict.approve))
    assert (raised.value.code, raised.value.message) == ("FEATURES_MISMATCH", "项目冻结 critic, 当前 critic,journals")
    assert not other.is_running(EID)  # 项目锁已释放
    async with api_client(other) as client:
        response = await client.post(
            f"/api/v1/engagements/{EID}/gate-decisions",
            json={"gate": "ACC-01", "target_id": "SCORECARD", "target_version": 1, "decision": "approve"},
        )
        assert response.status_code == 409 and response.json()["detail"]["code"] == "FEATURES_MISMATCH"
        overview = (await client.get(f"/api/v1/engagements/{EID}")).json()
        assert overview["run_error"].startswith("FEATURES_MISMATCH: ")
    await other.resume(EID)
    assert calls == [] and not other.is_running(EID)
    assert await frozen_state(other) == before

    back = rebuilt(s.runner, BASELINE)
    await back.recover()
    assert back.run_error(EID) is None and not back.is_running(EID)  # 停在闸门, 不推进也不报错
    await GraphDecisionSubmit(back.app, back.deps, back).submit(EID, acc01_decision(GateVerdict.approve))
    await wait_idle(back, EID)
    assert back.run_error(EID) is None
    assert (await back.app.aget_state(GraphRunner.config(EID))).next == ("gate_p1_risk",)


async def test_legacy_snapshot_counts_as_baseline() -> None:
    """R76 验收 2: version_snapshot 没有 features 键 (冻结功能集之前建的项目) 按 "critic" 处理; 尚未冻结时不比较."""
    legacy = ENGAGEMENT.model_copy(update={"version_snapshot": {"procedure_library_commit": "abc"}})
    base = make_deps(FakeRegistry(), FakeTrail())
    assert features_mismatch(base, legacy) is None
    assert features_mismatch(replace(base, features=JOURNALS), legacy) == "项目冻结 critic, 当前 critic,journals"
    assert features_mismatch(replace(base, features=frozenset()), legacy) == "项目冻结 critic, 当前 (空)"
    assert features_mismatch(replace(base, features=JOURNALS), ENGAGEMENT) is None  # 没有版本快照
    assert features_text(JOURNALS) == "critic,journals" and features_text([]) == ""

    runner, _ = make_runner(engagement_store=FakeEngagementStore([NUMBAT_PROFILE], [legacy]))
    await runner.recover()  # 没有检查点, 以初始状态推进; 快照沿用旧的 (没有 features 键)
    await wait_idle(runner, EID)
    assert runner.run_error(EID) is None
    assert (await runner.app.aget_state(GraphRunner.config(EID))).next == ("gate_acc01",)


OLD_PLANNING = frozenset({"critic", "misstatement", "planning"})  # R313 之前 planning 须带 misstatement


@pytest.mark.parametrize(
    ("started_with", "restarted_with", "error", "next_nodes"),
    [
        (
            PLANNING_FEATURES,
            OLD_PLANNING,
            "FEATURES_MISMATCH: 项目冻结 critic,planning, 当前 critic,misstatement,planning",
            ("p0_intel",),
        ),
        (OLD_PLANNING, OLD_PLANNING, None, ("gate_acc01",)),  # 对照: 同一功能集下 recover 确实往下推进
    ],
)
async def test_planning_split_keeps_frozen_features(
    started_with: frozenset[str], restarted_with: frozenset[str], error: str | None, next_nodes: tuple[str, ...]
) -> None:
    """R313 验收 4: 拆分后 planning 不带 misstatement 也能启动, 冻结为 "critic,planning"; 换成拆分前常用的
    {critic, misstatement, planning} 重启即 FEATURES_MISMATCH, 不推进. 拆分前按 "critic,misstatement,planning"
    冻结的项目在同一功能集下照常恢复 (功能名没改). 防的是拆分改了名字或冻结写法, 让在途项目误判或误推进.
    启动后停在 p0_intel 前 (闸门以外的中断; 停在闸门时 recover 本来就不推进, 看不出差别), 重启的进程不带这个中断:
    功能集不符时仍停 p0_intel, 相符时推进到 gate_acc01 (审核意见 R313-02)."""
    runner, _ = make_runner(
        engagement_store=FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT]),
        features=started_with,
        interrupt_before=["p0_intel"],
    )
    await runner.start(EID)
    await wait_idle(runner, EID)
    assert (await runner.app.aget_state(GraphRunner.config(EID))).next == ("p0_intel",)
    engagement = await runner.deps.engagement_store.get(EID)
    assert engagement is not None and engagement.version_snapshot["features"] == features_text(started_with)
    other = rebuilt(runner, restarted_with)
    await other.recover()
    await wait_idle(other, EID)
    assert other.run_error(EID) == error and not other.is_running(EID)
    assert (await other.app.aget_state(GraphRunner.config(EID))).next == next_nodes


async def test_check_order_around_features_mismatch() -> None:
    """R76 验收 4: ENGAGEMENT_LOCKED → ENGAGEMENT_BUSY → FEATURES_MISMATCH → RESUME_REQUIRED."""
    s = await started()
    other = rebuilt(s.runner, JOURNALS)

    async def code(runner: GraphRunner) -> str:
        decision = acc01_decision(GateVerdict.approve, id="D-9")
        with pytest.raises(DecisionRejected) as raised:
            await GraphDecisionSubmit(runner.app, runner.deps, runner).submit(EID, decision)
        assert not runner.is_running(EID)  # 被拒时项目锁已释放
        return raised.value.code

    # Store 中有检查点里没有的决定 (须先 resume): 功能集不同时先报 FEATURES_MISMATCH, 相同时才报 RESUME_REQUIRED
    s.output_store.rows.append(acc01_decision(GateVerdict.insufficient_info, id="D-STORE"))
    assert (await code(other), await code(s.runner)) == ("FEATURES_MISMATCH", "RESUME_REQUIRED")
    other.acquire(EID)
    with pytest.raises(DecisionRejected, match="ENGAGEMENT_BUSY"):
        await GraphDecisionSubmit(other.app, other.deps, other).submit(EID, acc01_decision(GateVerdict.approve))
    other.release(EID)
    await s.engagement_store.update(EID, locked=True)
    assert await code(other) == "ENGAGEMENT_LOCKED"


async def test_frozen_project_without_checkpoint_is_not_started() -> None:
    """R76: 快照已冻结而没有检查点 (start 的事务已提交, 第一步未落盘) 时, 换功能集后 recover 不以初始状态推进,
    也不按新功能集写固定集; 换回基线后照常推进到 ACC-01."""
    frozen = ENGAGEMENT.model_copy(
        update={"version_snapshot": {"procedure_library_commit": "abc", "features": "critic"}}
    )
    runner, store = make_runner(engagement_store=FakeEngagementStore([NUMBAT_PROFILE], [frozen]))
    other = rebuilt(runner, JOURNALS)
    await other.recover()
    await wait_idle(other, EID)
    assert other.run_error(EID) == "FEATURES_MISMATCH: 项目冻结 critic, 当前 critic,journals"
    assert (await other.app.aget_state(GraphRunner.config(EID))).metadata is None and store.rows == []
    back = rebuilt(runner, BASELINE)
    await back.recover()
    await wait_idle(back, EID)
    assert back.run_error(EID) is None
    assert (await back.app.aget_state(GraphRunner.config(EID))).next == ("gate_acc01",)


async def test_store_ahead_is_not_caught_up_under_other_features() -> None:
    """R76: Store 领先检查点 (决定已写, 检查点未跟上) 时, 换功能集后 recover 先比对: 不补做, 检查点, Store 与留痕都不变,
    总览显示 FEATURES_MISMATCH (先于 RESUME_REQUIRED 提示); 换回基线后照常补做, 越过 ACC-01."""
    s = await started()
    await submit_lost(s, acc01_decision(GateVerdict.approve))
    other = rebuilt(s.runner, JOURNALS)
    trail = other.deps.trail
    assert isinstance(trail, FakeTrail)
    before = (await frozen_state(other), len(trail.entries))
    await other.recover()
    await wait_idle(other, EID)
    assert other.run_error(EID) == "FEATURES_MISMATCH: 项目冻结 critic, 当前 critic,journals"
    assert (await frozen_state(other), len(trail.entries)) == before
    overview = await GraphStateQuery(other.app, other.deps, other).overview(EID)
    assert overview.run_error == other.run_error(EID)
    back = rebuilt(s.runner, BASELINE)
    await back.recover()
    await wait_idle(back, EID)
    assert back.run_error(EID) is None
    assert (await back.app.aget_state(GraphRunner.config(EID))).next == ("gate_p1_risk",)


async def test_ended_project_is_not_flagged() -> None:
    """R76: 已结束的项目 (ACC-01 拒绝承接后到 END) 换功能集后 recover 不记错误, 也不推进."""
    s = await started()
    await s.submit.submit(EID, acc01_decision(GateVerdict.reject))
    await wait_idle(s.runner, EID)
    snapshot = await s.runner.app.aget_state(GraphRunner.config(EID))
    assert snapshot.next == () and not snapshot.tasks
    other = rebuilt(s.runner, JOURNALS)
    await other.recover()
    assert other.run_error(EID) is None and not other.is_running(EID)


async def test_profile_update_and_review_record_rejected() -> None:
    """R76: 更新客户资料与复核决定同样报 FEATURES_MISMATCH, 项目锁已释放, 检查点, Store 与画像版本都不变."""
    s = await started()
    other = rebuilt(s.runner, JOURNALS)
    submit = GraphDecisionSubmit(other.app, other.deps, other)
    before = await frozen_state(other)
    profile = NUMBAT_PROFILE.model_copy(update={"version": NUMBAT_PROFILE.version + 1})
    for attempt in (
        submit.update_client_profile(EID, *profile_update(profile), updated_by="U-1"),
        submit.submit(EID, record()),
    ):
        with pytest.raises(DecisionRejected) as raised:
            await attempt
        assert raised.value.code == "FEATURES_MISMATCH" and not other.is_running(EID)
    assert await frozen_state(other) == before
    engagement = await s.engagement_store.get(EID)
    assert engagement is not None and engagement.client_profile_version == ENGAGEMENT.client_profile_version


def test_frozen_features_of_engagement() -> None:
    """项目的功能集 (总览的 features): 有版本快照取冻结的; 快照没有 features 键 (冻结功能集之前的项目) 按基线;
    还没有快照 (未启动) 取当前进程的."""
    assert frozen_features({"features": "collab,critic"}, BASELINE) == {"collab", "critic"}
    assert frozen_features({"features": ""}, {"critic", "collab"}) == frozenset()
    assert frozen_features({"procedure_library_commit": "abc"}, {"critic", "collab"}) == BASELINE
    assert frozen_features({}, {"critic", "journals"}) == {"critic", "journals"}
