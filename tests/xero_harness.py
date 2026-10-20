"""Xero 程序级测试台 (需求 R118; 设计稿 v2.1 第 8.1, 8.9 节): 注入测试与 Xero 程序级评测共用的装配, 在注入副本
(tests/xero_injection.XeroCase) 上按离线或连库装配依赖, 按依赖分层跑指定程序 (仿 tests/test_p2_agents_demo 按层
直接调 Agent 的做法, 不经主图与闸门).

- offline_deps(case): 不连库, 副本演示数据导入之后的状态装进 tests/xero_fakes 的假存储 (make_xero_deps(loader=...)),
  规则引擎为预置 XERO_RULES 的 FakeRuleEngine.
- db_deps(case): 连 TEST_DATABASE_URL 的测试库, 组装根的真实实现; 数据集载入, 登记与函证 mock 指向副本; 经真实
  DataService 做演示数据导入 (acceptance, fieldwork) 并确认数据已齐.
- run_programs(h, ids): 先跑 DOC-04, PLN-07, PLN-01 并按 MATERIALITY 定重要性, 再按依赖闭包分层跑 ids; rerun 时
  出了 failed 任务的程序照 rerun 决定重跑一次 (rerun_failed).
- LayeredMockLLMClient, RoutedLLMClient: mock 输出分层覆盖; 指定的 (程序, fixture_key) 或整条程序交真实模型 (经预算).
- imported_tables, table_diff: 导入后的各表, 两份之间按表按行 ID 的差异.

大模型缺省读副本补造目录的运行副本 (llm/, 同原目录的; 生成脚本从 tests/fixtures/llm_xero 复制).
"""

import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from pydantic import BaseModel

from backend.adapters.confirmation_mock import DatasetConfirmationChannel
from backend.adapters.llm import SwitchableLLMClient, llm_version
from backend.adapters.registry_mock import DatasetRegistrySource
from backend.agents.base import AgentDeps
from backend.contracts.data import TableKind
from backend.contracts.models import (
    AgentId,
    AuditException,
    DocumentRef,
    Engagement,
    Judgment,
    MaterialityChoice,
    Procedure,
    ReviewStatus,
    TaskKind,
    TaskStatus,
)
from backend.contracts.ports import LLMClient, LLMRequest, LLMResponse, T_out
from backend.contracts.state import EngagementState, StatePatch, initial_state
from backend.orchestration.decisions import confirm_effects
from backend.orchestration.gates import materiality_amounts
from backend.orchestration.nodes import GraphNodes, apply_patch, rerun_subjects, save_outputs, update_procedure
from backend.orchestration.runner import fixed_procedures
from backend.orchestration.trail import data_ready_entry, record_transitions
from backend.persistence.stores import RoutingDatasetLoader, XeroDatasetLoader
from backend.services.data_service import DataService
from tests.fakes import FakeRuleEngine, FakeTrail, LayeredMockLLM
from tests.tools.live_budget import Budget, BudgetedLLMClient
from tests.xero_fakes import (
    PLANNING,
    XERO_RULES,
    make_xero_deps,
    xero_documents,
    xero_employees,
    xero_engagement,
    xero_tables,
)
from tests.xero_flow import READY_REASON, import_stage, offline_runner
from tests.xero_injection import XeroCase

PLANNING_PROGRAMS = ("DOC-04", "PLN-07", "PLN-01")  # run_programs 先跑的 P1 程序, 按图中的次序
# PLN-01/03 闸门的选择 (设计稿第 7.1 节): 收入 1%, 执行重要性 50%, 明显微小 5%; 金额随副本的收入变化
MATERIALITY = MaterialityChoice(basis="revenue", pct=0.01, performance_pct=0.5, clearly_trivial_pct=0.05)
AUDITOR_NAME = "Jordan Ellis"  # 连库时新建的项目负责人 (设计稿第 2 节, 虚构的合伙人)
RulePresets = Mapping[str, Any]  # FakeRuleEngine 的预置: 程序编号 → (执行记录 → RuleResult)


@dataclass(frozen=True)
class XeroHarness:
    """一个副本上装配好的依赖与起始状态 (程序包为固定集, 文件为导入后的登记, P1 程序尚未执行)."""

    case: XeroCase
    deps: AgentDeps
    engagement: Engagement
    state: EngagementState
    actor: str  # 项目负责人: 自动确认判断的确认人


@dataclass(frozen=True)
class Ran:
    """run_programs 的结果: 并入各程序片段后的状态与所用的依赖."""

    state: EngagementState
    deps: AgentDeps


async def commit(deps: AgentDeps, state: EngagementState, patch: StatePatch, actor: str) -> EngagementState:
    """片段在一个事务内经 save_outputs 写入存储, 状态取存储返回的对象并入 (同 GraphNodes 写入片段)."""
    eid = state["engagement_id"]
    async with deps.output_store.transaction(eid):
        stored = await save_outputs(deps.output_store, eid, patch, actor, deps.evidence_store)
    return apply_patch(state, stored)


async def start(
    case: XeroCase, deps: AgentDeps, engagement: Engagement, actor: str, documents: list[DocumentRef]
) -> XeroHarness:
    """起始状态: 固定集程序第 1 版写入存储 (同项目启动), 状态带上导入后的文件登记."""
    state = initial_state(engagement, [], engagement.client_profile_version)
    state = await commit(deps, state, {"procedure_pack": fixed_procedures(deps.library)}, AgentId.A1)
    return XeroHarness(case, deps, engagement, apply_patch(state, {"documents": documents}), actor)


async def offline_deps(
    case: XeroCase, *, llm: LLMClient | None = None, rules: RulePresets | None = None
) -> XeroHarness:
    """不连库: 副本经补造清单得到的导入后状态装进假存储 (make_xero_deps(loader=case.loader), 载入器指向副本且
    allow_injected): 账表为 XeroDatasetLoader.tables 与工资册, 文件登记为 fieldwork 阶段为止上传的 21 份 (补登记字段
    取清单, 页文本取副本的 PDF), 合同按 DOC-04 的 fixture 抽取; 登记与函证 mock 读副本的补造目录. 规则引擎为
    FakeRuleEngine(rules), 缺省 XERO_RULES (原目录上的结果, 场景改了规则输入时自己给). 主模型与复核模型同为 llm,
    缺省读副本的运行副本. 留痕为 FakeTrail."""
    client = llm if llm is not None else LayeredMockLLMClient([case.supplements / "llm"])
    deps = make_xero_deps(
        trail=FakeTrail(),
        rules=FakeRuleEngine(dict(rules if rules is not None else XERO_RULES)),
        llm=client,
        review_llm=client,
        loader=case.loader,
    )
    engagement = xero_engagement(case.loader)
    return await start(case, deps, engagement, engagement.owner_id, xero_documents("fieldwork", case.loader))


async def db_deps(case: XeroCase, *, llm: LLMClient | None = None) -> XeroHarness:
    """连库 (TEST_DATABASE_URL, 已迁移的独立测试库; 没配时跳过): 组装根的真实实现 (SqlRuleEngine, 证据文件存于副本
    旁的 evidence/), 数据集载入换成指向副本的 XeroDatasetLoader(allow_injected), 登记与函证 mock 按数据集读副本的
    补造目录. 新建项目负责人 (Jordan Ellis) 与项目 (画像 ID 换成随机的, 同库重复建不冲突), 经真实 DataService 做演示
    数据导入的 acceptance 与 fieldwork, 再以负责人确认数据已齐 (DataStore.confirm_ready, 理由同 xero_flow, 记当时
    体检不通过的项与 ready 留痕; 不经主图, 所以不走 DataService.confirm_ready 的等数据检查). 主模型与复核模型换成
    llm, 缺省读副本的运行副本 (上传处理的分类与合同抽取也用它)."""
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to a migrated disposable PostgreSQL database")
    from backend.core.config import Settings
    from backend.main import build_deps

    settings = Settings(DATABASE_URL=url, EVIDENCE_ROOT=case.snapshot.parents[1] / "evidence", LLM_MODE="mock")
    deps = build_deps(settings)
    xero = XeroDatasetLoader(
        case.snapshot.parent,
        PLANNING,
        cast(Any, deps.data).engine,
        cast(Any, deps.evidence_store),
        supplements=case.supplements.parent,
        allow_injected=True,
    )
    datasets = RoutingDatasetLoader(cast(RoutingDatasetLoader, deps.datasets).default, {XeroDatasetLoader.PREFIX: xero})
    deps = replace(
        deps,
        datasets=datasets,
        registry=DatasetRegistrySource(deps.engagement_store, datasets.supplement_dir),
        confirmations=DatasetConfirmationChannel(
            deps.engagement_store, datasets.supplement_dir, settings.EVIDENCE_ROOT / ".confirmation_receipts"
        ),
    )
    client = llm if llm is not None else LayeredMockLLMClient([case.supplements / "llm"])
    for switchable in (deps.llm, deps.review_llm):
        cast(SwitchableLLMClient, switchable).reload(client, llm_version(settings)["model"])
    user = await deps.users.create(f"jordan.ellis+{uuid4().hex[:12]}@example.com", AUDITOR_NAME, "x")
    assert user is not None
    period = xero.period(case.dataset)
    assert period is not None
    profile = xero.client_profile(case.dataset).model_copy(update={"id": "CLIENT-" + uuid4().hex})
    engagement = await cast(Any, deps.engagement_store).create(
        owner_id=user.id, period_start=period[0], period_end=period[1], dataset=case.dataset, profile=profile
    )
    eid = engagement.id
    # 项目锁取 runner; 主图配上但不启动 (内存检查点为空), 数据写入后 DataService 查图是否在等数据时照常读得到
    service = DataService(deps, offline_runner(deps), demo=datasets)
    for stage in ("acceptance", "fieldwork"):
        result = await import_stage(service, eid, stage, user.id)
        assert result is not None and result.status == "imported", (stage, result)
    failed = [check.id for check in await deps.data.checks(eid) if check.status == "failed"]
    readiness = await deps.data.confirm_ready(eid, user.id, reason=READY_REASON, failed_checks=failed)
    await record_transitions(deps.trail, [data_ready_entry(eid, readiness)])
    return await start(case, deps, engagement, user.id, await deps.financial_store.documents(eid))


def layers(ids: Sequence[str], depends_on: Mapping[str, Sequence[str]]) -> list[list[str]]:
    """按依赖分层 (同 scheduler.build_schedule 的层): 只依赖 ids 以外程序的在第 0 层; 层内按编号排序."""
    depth: dict[str, int] = {}

    def of(pid: str) -> int:
        if pid not in depth:
            inner = [d for d in depends_on[pid] if d in ids]
            depth[pid] = 0 if not inner else 1 + max(of(d) for d in inner)
        return depth[pid]

    found: dict[int, list[str]] = {}
    for pid in sorted(ids):
        found.setdefault(of(pid), []).append(pid)
    return [found[n] for n in sorted(found)]


async def confirm_upstream(h: XeroHarness, state: EngagementState, layer: Sequence[str]) -> EngagementState:
    """下游要读的上游判断自动确认: 本层各程序 depends_on 中程序的待复核判断出新版本置 confirmed, 并归因它们解释的
    例外 (同复核 confirm 的效果 confirm_effects), 以项目负责人写入. 复核任务不动."""
    upstream = {d for pid in layer for d in h.deps.library.load(pid).depends_on}
    exceptions = {x.id: x for x in state["exceptions"]}
    effects = [
        effect
        for j in state["judgments"]
        if j.procedure_id in upstream and j.review_status is ReviewStatus.pending
        for effect in confirm_effects(j, [exceptions[x] for x in j.exception_ids])
    ]
    if not effects:
        return state
    patch: StatePatch = {
        "judgments": [e for e in effects if isinstance(e, Judgment)],
        "exceptions": [e for e in effects if isinstance(e, AuditException)],
    }
    return await commit(h.deps, state, patch, h.actor)


async def rerun_failed(h: XeroHarness, nodes: GraphNodes, state: EngagementState, pid: str) -> EngagementState:
    """pid 有待决的 failed 任务时照审计师的 rerun 决定重跑一次: 整组任务置 decided, 程序出新版本, 只重判这组任务的对象
    (rerun_subjects; 有整次失败的任务时整条重跑), 再执行一次并写入 (同 decisions 的 rerun_group 与 rerun_procedure,
    以项目负责人写入). 没有时原样返回. 带规则例外的对象失败时建的是 unattributed 任务, 不在其中 (不能重跑)."""
    failed = [
        t
        for t in state["review_tasks"]
        if t.procedure_id == pid and t.kind is TaskKind.failed and t.status is TaskStatus.pending
    ]
    if not failed:
        return state
    patch: StatePatch = {
        "procedure_pack": update_procedure(state["procedure_pack"], pid, rerun_subject_ids=rerun_subjects(failed)),
        "review_tasks": [t.model_copy(update={"version": t.version + 1, "status": TaskStatus.decided}) for t in failed],
    }
    state = await commit(h.deps, state, patch, h.actor)
    result = await nodes.perform(state, h.engagement, pid)
    return await commit(h.deps, state, result["patch"], result["writer"])


async def run_programs(h: XeroHarness, ids: Sequence[str], *, rerun: bool = False) -> Ran:
    """先跑 DOC-04 (合同已在导入时抽取时不调大模型), PLN-07, PLN-01, 按 MATERIALITY 与 PLN-01 的候选定重要性 (同
    PLN-01/03 闸门的算法); 再把 ids 补齐依赖闭包 (除去以上三条), 程序第 1 版写入存储 (关联目标同 assemble_pack 的
    匹配), 按依赖分层执行: 每层先自动确认上游判断 (confirm_upstream), 同层的程序都以本层开始时的状态执行, 再逐个
    写入并入 (同 fan_in). 每次执行经 GraphNodes.perform (建执行记录, 调 Agent; Agent 出错时为 A1 的失败片段),
    片段写入存储后经 apply_patch 并入状态. rerun 时每条程序写入后, 出了 failed 任务的重跑一次 (rerun_failed), 下游
    看到的是重跑后的结果."""
    deps, library = h.deps, h.deps.library
    nodes = GraphNodes(deps, 2)
    state = h.state

    async def settle(state: EngagementState, pid: str) -> EngagementState:
        return await rerun_failed(h, nodes, state, pid) if rerun else state

    for pid in PLANNING_PROGRAMS:
        result = await nodes.perform(state, h.engagement, pid)
        state = await settle(await commit(deps, state, result["patch"], result["writer"]), pid)
    candidates = state["materiality_candidates"]
    assert candidates is not None, "PLN-01 没有给出重要性候选"
    overall, performance, trivial = materiality_amounts(candidates, MATERIALITY)
    state = apply_patch(
        state,
        {"materiality_cents": overall, "performance_materiality_cents": performance, "clearly_trivial_cents": trivial},
    )
    wanted = [t for t in library.closure(sorted(ids)) if t.id not in PLANNING_PROGRAMS]
    held = {p.id for p in state["procedure_pack"]}
    objectives = state["audit_objectives"]
    added = [
        Procedure(
            id=t.id,
            phase=t.phase,
            template_commit=library.commit,
            objective_ids=[o.id for o in objectives if t in library.match(o)],
            autonomy_level=t.autonomy_level,
        )
        for t in wanted
        if t.id not in held
    ]
    async with deps.output_store.transaction(h.engagement.id):
        stored = await deps.output_store.put(h.engagement.id, added, actor=AgentId.A1)
    state = apply_patch(state, {"procedure_pack": [*state["procedure_pack"], *cast(list[Procedure], stored)]})
    for layer in layers([t.id for t in wanted], {t.id: t.depends_on for t in wanted}):
        state = await confirm_upstream(h, state, layer)
        results = [await nodes.perform(state, h.engagement, pid) for pid in layer]
        for result in results:
            state = await commit(deps, state, result["patch"], result["writer"])
        for pid in layer:
            state = await settle(state, pid)
    return Ran(state, deps)


# ── 大模型: 分层 mock 与按键分流 ──


@dataclass(frozen=True)
class FixtureRead:
    """LayeredMockLLMClient 的一次读取: 读了哪个根下的哪个文件 (都没找到时为 None)."""

    procedure_id: str
    fixture_key: str
    root: Path | None
    file: str | None  # 相对根, 如 REV-02/SI-....json 或 REV-02/default.json


class LayeredMockLLMClient(LayeredMockLLM):
    """tests.fakes.LayeredMockLLM (按 roots 顺序找 {程序}/{fixture_key}.json, 都没有再按顺序找 default.json, 读法同
    MockLLMClient), 另记下每次的请求全文 (requests, 含 system 与 messages) 与读了哪个文件 (reads)."""

    def __init__(self, roots: Sequence[Path]) -> None:
        super().__init__(*roots)
        self.requests: list[LLMRequest] = []
        self.reads: list[FixtureRead] = []

    async def complete(self, req: LLMRequest, output_model: type[T_out]) -> LLMResponse[T_out]:
        self.requests.append(req)
        found = self.locate(req)
        root, name = (found[0].root, found[1]) if found is not None else (None, None)
        self.reads.append(FixtureRead(req.procedure_id, req.fixture_key, root, name))
        return await super().complete(req, output_model)


class RoutedLLMClient:
    """(程序, fixture_key) 在 live_keys 中的, 或程序在 programs 中的 (整条程序的调用) 交 live (经 BudgetedLLMClient
    计入 budget, 超限即停发), 其余交 mock. 记下每次的去向与请求全文 (requests: (live 或 mock, 请求))."""

    def __init__(
        self,
        mock: LLMClient,
        live: LLMClient,
        live_keys: Iterable[tuple[str, str]],
        budget: Budget,
        *,
        programs: Iterable[str] = (),
    ) -> None:
        self.mock = mock
        self.live = BudgetedLLMClient(live, budget)
        self.live_keys = frozenset(live_keys)
        self.programs = frozenset(programs)
        self.requests: list[tuple[str, LLMRequest]] = []

    async def complete(self, req: LLMRequest, output_model: type[T_out]) -> LLMResponse[T_out]:
        live = req.procedure_id in self.programs or (req.procedure_id, req.fixture_key) in self.live_keys
        self.requests.append(("live" if live else "mock", req))
        return await (self.live if live else self.mock).complete(req, output_model)


def request_text(req: LLMRequest) -> str:
    """请求全文: system 与各条消息, 按顺序以换行连接."""
    return "\n".join([req.system, *(m.content for m in req.messages)])


# ── 账表差异 ──

ROW_KEYS = {  # 行 ID 的字段 (多个时以 ":" 连接); 没列的表为 id
    TableKind.accounts: ("code",),
    TableKind.trial_balance: ("period", "account_code"),
    TableKind.employees: ("employee_id",),
}


@dataclass(frozen=True)
class TableDiff:
    """一张表在两份之间的差异, 都按行 ID 排序."""

    added: list[str]  # 只在副本中的行
    deleted: list[str]  # 只在原件中的行
    changed: dict[str, dict[str, tuple[Any, Any]]]  # 两边都有而内容不同的行: 行 ID → 字段 → (原件值, 副本值)


def imported_tables(loader: XeroDatasetLoader | None = None) -> dict[str, list[Any]]:
    """演示数据导入之后的各表 (不连库): XeroDatasetLoader.tables 的八张 (同补造清单) 与工资册; loader 缺省为原目录."""
    return {**xero_tables(loader), TableKind.employees: xero_employees(loader)}


def keyed(kind: str, rows: Sequence[BaseModel]) -> dict[str, dict[str, Any]]:
    """行 ID → 行的字段; 同一张表里行 ID 重复时抛 ValueError (差异按 ID 比, 重复会被吞掉)."""
    keys = ROW_KEYS.get(TableKind(kind), ("id",))
    found: dict[str, dict[str, Any]] = {}
    for row in rows:
        values = row.model_dump()
        rid = ":".join(str(values[k]) for k in keys)
        if rid in found:
            raise ValueError(f"{kind} 的行 ID {rid} 重复")
        found[rid] = values
    return found


def table_diff(
    original: Mapping[str, Sequence[BaseModel]], copy: Mapping[str, Sequence[BaseModel]]
) -> dict[str, TableDiff]:
    """按表 (两边表名的并集, 只在一边的表视作另一边为空) 按行 ID 列出新增, 删除与改动的行和字段; 没有差异的表不列,
    两份相同时为空."""
    diffs: dict[str, TableDiff] = {}
    for kind in sorted(set(original) | set(copy)):
        before, after = keyed(kind, original.get(kind, [])), keyed(kind, copy.get(kind, []))
        changed = {
            rid: {
                name: (before[rid].get(name), after[rid].get(name))
                for name in sorted(before[rid].keys() | after[rid].keys())
                if before[rid].get(name) != after[rid].get(name)
            }
            for rid in sorted(before.keys() & after.keys())
            if before[rid] != after[rid]
        }
        diff = TableDiff(sorted(after.keys() - before.keys()), sorted(before.keys() - after.keys()), changed)
        if diff.added or diff.deleted or diff.changed:
            diffs[kind] = diff
    return diffs
