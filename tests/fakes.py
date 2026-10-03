"""测试用假实现与样例数据. B 的真实实现就绪前, Agent 与节点测试都注入这里的假工具."""

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from typing import Any, ClassVar, TypeVar

from langgraph.checkpoint.memory import InMemorySaver

from backend.agents.base import AgentDeps, ProcedureAgent, RunContext
from backend.contracts.errors import VersionConflict
from backend.contracts.models import (
    AbnRecord,
    AgentId,
    AsicCompanyRecord,
    AuditObjective,
    AutonomyLevel,
    ClientProfile,
    Decision,
    DocumentRef,
    DocumentType,
    Engagement,
    Phase,
    Procedure,
    ProcedureRun,
    ProcedureStatus,
    ReviewRecord,
    ReviewTask,
    RunStatus,
    RunTrigger,
    TaskStatus,
    TrailEntry,
    TrailKind,
    Versioned,
)
from backend.contracts.state import StatePatch, initial_state
from backend.core.config import get_settings
from backend.main import build_deps
from backend.orchestration.checkpoint import checkpoint_serde
from backend.orchestration.graph import compile_graph
from backend.orchestration.nodes import GraphNodes
from backend.orchestration.runner import GraphRunner

NOW = datetime(2026, 9, 30, tzinfo=UTC)

# ── 样例数据: 合成数据集的 Numbat 公司 (合成数据集设计.md 2.1, 2.2 节) ──

NUMBAT_ABN = AbnRecord(
    abn="68900042015",
    acn="900042015",
    entity_name="Numbat Electronic Components Pty Ltd",
    status="Active",
    entity_type="Australian Private Company",
)
NUMBAT_ASIC = AsicCompanyRecord(
    acn="900042015",
    name="Numbat Electronic Components Pty Ltd",
    status="Registered",
    registered_date=date(2011, 3, 14),
)
NUMBAT_PROFILE = ClientProfile(
    id="CLIENT-001",
    legal_name="Numbat Electronic Components Pty Ltd",
    abn="68900042015",
    entity_type="small_proprietary",
    industry_anzsic="3494",
    listed=False,
    directors=["Jordan Avery", "Priya Raman"],
)
# 客户申报的名称写错了: name_match 不通过, 审计师要客户更正
TYPO_PROFILE = NUMBAT_PROFILE.model_copy(update={"legal_name": "Numbat Electronics Pty Ltd"})
ENGAGEMENT = Engagement(
    id="ENG-2026-001",
    client_id="CLIENT-001",
    period_start=date(2025, 7, 1),
    period_end=date(2026, 6, 30),
    dataset="demo",
    owner_id="U-1",
    client_profile_version=1,
    phase=Phase.P0_Acceptance,
    created_at=NOW,
    updated_at=NOW,
)
# 数据集预置的审计目标 (合成数据集设计.md 2.4 节): 收入两条为特别风险, 货币资金为高风险
NUMBAT_OBJECTIVES = [
    AuditObjective(
        id="OBJ-REV-OCC", account="Revenue", assertion="Occurrence", risk_level="significant", is_material=True
    ),
    AuditObjective(id="OBJ-REV-CUT", account="Revenue", assertion="Cutoff", risk_level="significant", is_material=True),
    AuditObjective(id="OBJ-CASH-EXI", account="Cash", assertion="Existence", risk_level="high", is_material=True),
]


# ── 假工具 ──


class FakeRegistry:
    """假登记系统: 从字典里查, 查不到返回 None. asic_queries 记下查过哪些 ACN."""

    def __init__(
        self,
        abn_records: dict[str, AbnRecord] | None = None,
        asic_records: dict[str, AsicCompanyRecord] | None = None,
    ) -> None:
        self.abn_records = abn_records or {}  # ABN -> 登记记录
        self.asic_records = asic_records or {}  # ACN -> 公司登记记录
        self.asic_queries: list[str] = []  # 查过的 ACN, 按查询顺序

    async def abn(self, abn: str) -> AbnRecord | None:
        return self.abn_records.get(abn)

    async def asic_company(self, acn: str) -> AsicCompanyRecord | None:
        self.asic_queries.append(acn)
        return self.asic_records.get(acn)


class FakeTrail:
    """假留痕: 存在内存列表里, 按写入顺序发号 TR-1, TR-2, ... 读取按项目过滤. 独立于业务事务, 写入即生效."""

    def __init__(self) -> None:
        self.entries: list[TrailEntry] = []  # 已写入的留痕, 各项目混在一起, 按写入顺序

    async def record(self, entry: TrailEntry) -> str:
        trail_id = f"TR-{len(self.entries) + 1}"
        self.entries.append(entry.model_copy(update={"id": trail_id}))
        return trail_id

    async def get(self, eid: str, id: str) -> TrailEntry | None:
        return next((e for e in self.entries if e.engagement_id == eid and e.id == id), None)

    async def find_success(self, eid: str, idempotency_key: str) -> TrailEntry | None:
        return next((e for e in self.entries if e.engagement_id == eid and e.idempotency_key == idempotency_key), None)

    async def list(
        self, eid: str, kind: TrailKind | None = None, limit: int = 100, offset: int = 0
    ) -> list[TrailEntry]:
        hits = [e for e in self.entries if e.engagement_id == eid and (kind is None or e.kind is kind)]
        return hits[offset : offset + limit]


class FakeFinancialStore:
    """假财务数据存储: 目前只提供证据文件登记 (documents), 其余方法用到时再补. B 的 Postgres 实现就绪前代替它."""

    def __init__(self, documents: list[DocumentRef] | None = None) -> None:
        self.document_refs = list(documents or [])  # 已登记的证据文件, 各项目共用

    async def documents(self, eid: str, type: DocumentType | None = None) -> list[DocumentRef]:
        return [d for d in self.document_refs if type is None or d.type is type]


@dataclass(frozen=True)
class EngagementWrite:
    """项目存储的一次写入, 与审计产出一起记进 FakeOutputStore.commits, 用来检查它们是否在同一事务里."""

    op: str  # "update" 或 "save_profile"
    target: str  # update 为项目编号, save_profile 为客户编号
    changes: dict[str, Any]  # update 为改了的字段, save_profile 为 {"version": 版本号}


@dataclass
class FakeTxn:
    """一个进行中的假事务: 块内的写入先暂存在这里, 块正常结束才一起生效, 块内抛错则全部丢弃."""

    eid: str  # 事务所属项目
    owner: asyncio.Task[Any] | None  # 开事务的任务; 只有它 (含其中的嵌套调用) 在事务里
    active: bool = True  # 块结束后置假, 之后不再有人能加入
    objs: list[Versioned] = field(default_factory=list)  # 暂存的审计产出
    log: list[Versioned | EngagementWrite] = field(default_factory=list)  # 全部写入, 按调用顺序
    engagements: dict[str, Engagement] = field(default_factory=dict)  # 暂存的项目改动, 项目编号 -> 改后的项目
    profiles: list[ClientProfile] = field(default_factory=list)  # 暂存的新画像


class FakeEngagementStore:
    """假项目存储: 实现读项目, 改项目状态, 读写客户画像; create, list 调用即抛 NotImplementedError.

    bind 到 FakeOutputStore 后, 在它的 transaction 块内调用 update 与 save_profile 会加入该事务 (第 4 节):
    块内读得到暂存的改动, 块正常结束才生效并与审计产出记进同一个 commits 项, 块内抛错一并丢弃.
    """

    def __init__(
        self, profiles: list[ClientProfile] | None = None, engagements: list[Engagement] | None = None
    ) -> None:
        self.profiles = profiles or []  # 已生效的客户画像, 同一客户可有多个版本
        self.engagements = engagements or []  # 已生效的审计项目
        self.output_store: FakeOutputStore | None = None  # 共用事务的审计产出存储, bind 后才有

    def bind(self, output_store: "FakeOutputStore") -> None:
        """之后在 output_store 的事务块内写入时加入该事务."""
        self.output_store = output_store

    def _txn(self) -> FakeTxn | None:
        return self.output_store.active_txn() if self.output_store is not None else None

    async def create(
        self,
        *,
        owner_id: str,
        period_start: date,
        period_end: date,
        dataset: str,
        profile: ClientProfile,
    ) -> Engagement:
        raise NotImplementedError

    async def get(self, eid: str) -> Engagement | None:
        txn = self._txn()
        if txn is not None and eid in txn.engagements:
            return txn.engagements[eid]
        return next((e for e in self.engagements if e.id == eid), None)

    async def update(
        self,
        eid: str,
        *,
        phase: Phase | None = None,
        locked: bool | None = None,
        version_snapshot: dict[str, str] | None = None,
        client_profile_version: int | None = None,
    ) -> None:
        """改项目的阶段, 锁定, 版本快照与采用的画像版本, 传 None 的不改; 同时刷新 updated_at. locked 置真后不可回退."""
        current = await self.get(eid)
        if current is None:
            raise LookupError(f"Engagement {eid} not found")
        if current.locked and locked is False:
            raise ValueError(f"{eid} 已锁定, 不可解锁")
        changes = {
            "phase": phase,
            "locked": locked,
            "version_snapshot": version_snapshot,
            "client_profile_version": client_profile_version,
        }
        kept = {k: v for k, v in changes.items() if v is not None}
        updated = current.model_copy(update={**kept, "updated_at": datetime.now(UTC)})
        txn = self._txn()
        if txn is not None:
            txn.engagements[eid] = updated
            txn.log.append(EngagementWrite("update", eid, kept))
        else:
            self._apply_engagement(updated)

    def _apply_engagement(self, engagement: Engagement) -> None:
        self.engagements = [engagement if e.id == engagement.id else e for e in self.engagements]

    async def client_profile(self, client_id: str, version: int | None = None) -> ClientProfile | None:
        """version 为空取最新版本; 查不到返回 None. 事务块内读得到本事务暂存的画像."""
        txn = self._txn()
        visible = self.profiles + (txn.profiles if txn is not None else [])
        hits = [p for p in visible if p.id == client_id and (version is None or p.version == version)]
        return max(hits, key=lambda p: p.version, default=None)

    async def save_profile(self, profile: ClientProfile) -> ClientProfile:
        """version 须为已有最大值 +1."""
        latest = await self.client_profile(profile.id)
        expected = latest.version + 1 if latest is not None else 1
        if profile.version != expected:
            raise VersionConflict(f"{profile.id}: v{profile.version}, 应为 v{expected}")
        txn = self._txn()
        if txn is not None:
            txn.profiles.append(profile)
            txn.log.append(EngagementWrite("save_profile", profile.id, {"version": profile.version}))
        else:
            self.profiles.append(profile)
        return profile

    def commit(self, txn: FakeTxn) -> None:
        """FakeOutputStore 提交事务时调用: 暂存的项目改动与画像生效."""
        for engagement in txn.engagements.values():
            self._apply_engagement(engagement)
        self.profiles.extend(txn.profiles)

    async def list(self, owner_id: str | None = None) -> list[Engagement]:
        raise NotImplementedError


T = TypeVar("T", bound=Versioned)


class FakeOutputStore:
    """假审计产出存储, 按项目分开存. 规则同开发接口文档第 4 节 AuditOutputStore.

    transaction 块内的 put 先暂存, 块正常结束才一起提交, 块内抛错则全部丢弃; 块外的每次 put 各自提交.
    事务记在 contextvar 里, 只对开事务的任务有效: 与真实连接一样, 别的任务 (含事务块内新建的子任务) 不在事务里.
    """

    def __init__(self, rows: list[Versioned] | None = None, eid: str = ENGAGEMENT.id) -> None:
        self.default_eid = eid  # rows 属性对应的项目, 单项目测试不必传
        self.by_eid: dict[str, list[Versioned]] = {eid: list(rows or [])}  # 项目编号 -> 已提交对象, 按写入顺序
        self.commits: list[list[Versioned | EngagementWrite]] = []  # 每次提交写入的内容, 各项目按提交顺序
        self.current: ContextVar[FakeTxn | None] = ContextVar(f"fake_txn_{id(self)}", default=None)  # 当前事务
        self.engagement_store: FakeEngagementStore | None = None  # 共用事务的项目存储

    @property
    def rows(self) -> list[Versioned]:
        """默认项目的已提交对象."""
        return self.rows_of(self.default_eid)

    def rows_of(self, eid: str) -> list[Versioned]:
        """某个项目的已提交对象."""
        return self.by_eid.setdefault(eid, [])

    def active_txn(self) -> FakeTxn | None:
        """当前任务自己开的, 尚未结束的事务; 不在事务中为 None.

        contextvar 会被事务块内新建的子任务继承, 事务结束后也可能还被引用, 所以只认开事务的任务与未结束的事务.
        """
        txn = self.current.get()
        if txn is None or not txn.active or txn.owner is not asyncio.current_task():
            return None
        return txn

    def _txn(self, eid: str) -> FakeTxn | None:
        """当前任务所在的本项目事务; 不在事务中为 None. 在别的项目的事务里写入属于调用方错误, 抛 AssertionError."""
        txn = self.active_txn()
        if txn is not None and txn.eid != eid:
            raise AssertionError(f"在 {txn.eid} 的事务里写 {eid}")
        return txn

    def _visible(self, eid: str) -> list[Versioned]:
        """读写可见的对象: 本项目已提交对象与本任务事务暂存的本项目对象."""
        txn = self.active_txn()
        staged = txn.objs if txn is not None and txn.eid == eid else []
        return self.rows_of(eid) + staged

    async def put(self, eid: str, objs: Sequence[Versioned], *, actor: str) -> list[Versioned]:
        """同 (类型, id, version) 已存在则跳过并返回已存对象; 否则 version 须为已有最大值 +1, 不符抛 VersionConflict."""
        txn = self._txn(eid)
        result: list[Versioned] = []
        new: list[Versioned] = []
        for obj in objs:
            same_id = [r for r in self._visible(eid) + new if type(r) is type(obj) and r.id == obj.id]
            stored = next((r for r in same_id if r.version == obj.version), None)
            if stored is not None:
                result.append(stored)
                continue
            expected = max((r.version for r in same_id), default=0) + 1
            if obj.version != expected:
                raise VersionConflict(f"{type(obj).__name__} {obj.id}: v{obj.version}, 应为 v{expected}")
            new.append(obj)
            result.append(obj)
        if txn is not None:
            txn.objs.extend(new)
            txn.log.extend(new)
        else:
            self.rows_of(eid).extend(new)
            self.commits.append(list(new))
        return result

    async def get(self, eid: str, kind: type[T], id: str, version: int | None = None) -> T | None:
        """version 为空取最新版本; 查不到返回 None."""
        hits = [r for r in self._visible(eid) if isinstance(r, kind) and r.id == id]
        if version is not None:
            hits = [r for r in hits if r.version == version]
        return max(hits, key=lambda r: r.version, default=None)

    async def latest(self, eid: str, kind: type[T], **filters: Any) -> list[T]:
        """每个 id 的最新版本, 再按顶层字段相等过滤."""
        newest: dict[str, T] = {}
        for r in self._visible(eid):
            if isinstance(r, kind) and (r.id not in newest or r.version > newest[r.id].version):
                newest[r.id] = r
        return [r for r in newest.values() if all(getattr(r, k) == v for k, v in filters.items())]

    async def history(self, eid: str, kind: type[T], id: str) -> list[T]:
        """某个 id 的全部版本, 按版本号升序."""
        return sorted((r for r in self._visible(eid) if isinstance(r, kind) and r.id == id), key=lambda r: r.version)

    @asynccontextmanager
    async def transaction(self, eid: str) -> AsyncIterator[None]:
        """块内的 put 与绑定的项目存储写入共用一个事务; 嵌套调用加入外层事务."""
        if self._txn(eid) is not None:
            yield
            return
        txn = FakeTxn(eid, owner=asyncio.current_task())
        token = self.current.set(txn)
        try:
            yield
            self.rows_of(eid).extend(txn.objs)
            if self.engagement_store is not None:
                self.engagement_store.commit(txn)
            self.commits.append(txn.log)
        finally:
            txn.active = False
            self.current.reset(token)


class NoTaskAsyncio:
    """替换 runner 模块里的 asyncio: 建后台任务即失败, 其余照常. 用来测任务建不起来时项目锁是否释放.

    收到的协程记在 coros 里, 不替调用方关闭: 由测试检查 launch 自己有没有关.
    """

    def __init__(self) -> None:
        self.coros: list[Coroutine[Any, Any, Any]] = []  # create_task 收到的协程

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)

    def create_task(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        self.coros.append(coro)
        raise RuntimeError("事件循环已关闭")


def bind_stores(engagement_store: FakeEngagementStore, output_store: FakeOutputStore) -> None:
    """让项目存储加入审计产出存储的事务, 同真实实现共用一个数据库事务."""
    engagement_store.bind(output_store)
    output_store.engagement_store = engagement_store


# ── 组装 ──


def make_deps(
    registry: FakeRegistry,
    trail: FakeTrail,
    engagement_store: FakeEngagementStore | None = None,
    output_store: FakeOutputStore | None = None,
    financial_store: FakeFinancialStore | None = None,
) -> AgentDeps:
    """真实依赖里换掉登记系统, 留痕, 项目存储, 审计产出存储与财务数据存储. 其余依赖只构造不连接.

    项目存储缺省只有 Numbat 的画像与 ENGAGEMENT; 审计产出存储与财务数据存储缺省为空.
    项目存储与审计产出存储绑定, 项目存储的写入加入审计产出的事务.
    """
    if engagement_store is None:
        engagement_store = FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT])
    if output_store is None:
        output_store = FakeOutputStore()
    bind_stores(engagement_store, output_store)
    return replace(
        build_deps(get_settings()),
        registry=registry,
        trail=trail,
        engagement_store=engagement_store,
        output_store=output_store,
        financial_store=financial_store or FakeFinancialStore(),
    )


def make_procedure(
    procedure_id: str,
    phase: Phase = Phase.P0_Acceptance,
    version: int = 1,
    status: ProcedureStatus = ProcedureStatus.planned,
) -> Procedure:
    """程序包里的一条程序, 状态缺省为 planned."""
    return Procedure(
        id=procedure_id,
        phase=phase,
        template_commit="abc123",
        autonomy_level=AutonomyLevel.L3,
        version=version,
        status=status,
    )


def make_run(procedure_id: str = "ACC-04", run_no: int = 1, params: dict[str, Any] | None = None) -> ProcedureRun:
    """某条程序第 run_no 次执行的记录, 状态为 running."""
    return ProcedureRun(
        id=f"RUN-{procedure_id}-{run_no}",
        procedure_id=procedure_id,
        run_no=run_no,
        trigger=RunTrigger.initial if run_no == 1 else RunTrigger.rerun,
        params_snapshot=params or {},
        started_at=NOW,
        status=RunStatus.running,
    )


def finished(run: ProcedureRun, status: RunStatus = RunStatus.succeeded) -> ProcedureRun:
    """执行记录的终态: 置 succeeded (或给定状态), 结束时间为 NOW."""
    return run.model_copy(update={"status": status, "ended_at": NOW})


class ScriptAgent(ProcedureAgent[None]):
    """脚本 Agent: 不查数据也不调 LLM, 片段由 script(ctx) 给出 (也可在其中抛异常), 编排测试用它代替真实 Agent.

    以 A2 的身份写片段; 要以别的 Agent 身份写 (如 A3 才能写 review_tasks) 时用子类改 agent_id. calls 为被调用次数.
    """

    agent_id: ClassVar[AgentId] = AgentId.A2  # 智能体编号, 决定片段可写哪些字段
    procedures: ClassVar[frozenset[str]] = frozenset()  # 不经注册表, 由测试直接放进 GraphNodes.agents

    def __init__(self, deps: AgentDeps, script: Callable[[RunContext], StatePatch]) -> None:
        super().__init__(deps)
        self.script = script  # 收到上下文, 返回片段
        self.calls = 0  # 被调用次数

    async def execute(self, ctx: RunContext) -> None:
        self.calls += 1

    async def interpret(self, ctx: RunContext, findings: None) -> StatePatch:
        return self.script(ctx)


class ScriptA3(ScriptAgent):
    """以 A3 (文件抽取) 身份写片段的脚本 Agent, 可写 documents, confirmations, review_tasks."""

    agent_id: ClassVar[AgentId] = AgentId.A3


class ScriptA4(ScriptAgent):
    """以 A4 (风险评估) 身份写片段的脚本 Agent, 可写分析结果, 重要性候选, 审计目标, 风险清单, 例外."""

    agent_id: ClassVar[AgentId] = AgentId.A4


async def preset_rerun(
    store: FakeOutputStore,
    task: ReviewTask,
    record_id: str = "D-1",
    rerun_params: dict[str, Any] | None = None,
    eid: str = ENGAGEMENT.id,
) -> ReviewRecord:
    """模拟审计师对 failed 任务提交 rerun 后 DecisionSubmit 写入 Store 的对象 (接口文档第 7 节决定的效果 rerun 行),
    在一个事务内: 决定, 任务 decided 新版本, 程序 executing 新版本 (params 合并 rerun_params, rerun_subject_ids
    追加任务的 subject_id). 真正的 DecisionSubmit 由 R21 实现. 返回写入的决定.
    """
    record = ReviewRecord(
        id=record_id,
        task_id=task.id,
        target_version=task.target_version,
        decision=Decision.rerun,
        reason="外部系统已恢复, 重跑",
        rerun_params=rerun_params,
        reviewed_by="U-001",
        decided_at=NOW,
    )
    procedure = await store.get(eid, Procedure, task.procedure_id)
    assert procedure is not None
    subjects = [*procedure.rerun_subject_ids, task.subject_id] if task.subject_id else []
    rerun = procedure.model_copy(
        update={
            "version": procedure.version + 1,
            "status": ProcedureStatus.executing,
            "params": {**procedure.params, **(rerun_params or {})},
            "rerun_subject_ids": subjects,
        }
    )
    decided = task.model_copy(update={"version": task.version + 1, "status": TaskStatus.decided})
    async with store.transaction(eid):
        await store.put(eid, [record, decided, rerun], actor="A1")
    return record


async def assert_pack_synced(pack: list[Procedure], store: FakeOutputStore, eid: str = ENGAGEMENT.id) -> None:
    """程序包每条与 Store 中该程序的最新版本逐字段相等: 写 Procedure 新版本的节点与决定都须保持这一点."""
    for procedure in pack:
        assert await store.get(eid, Procedure, procedure.id) == procedure, procedure.id


def make_ctx(deps: AgentDeps, procedure_id: str = "ACC-04", engagement: Engagement = ENGAGEMENT) -> RunContext:
    """某条程序第 1 次执行的上下文: Procedure 为 planned 第 1 版, Run 置 running."""
    template = deps.library.load(procedure_id)
    procedure = Procedure(
        id=procedure_id,
        phase=template.phase,
        template_commit=deps.library.commit,
        autonomy_level=template.autonomy_level,
    )
    return RunContext(
        engagement=engagement,
        procedure=procedure,
        template=template,
        run=make_run(procedure_id, params=dict(template.params)),
        state=initial_state(engagement, [procedure], NUMBAT_PROFILE.version),
    )


def make_runner(
    registry: FakeRegistry | None = None,
    engagement_store: FakeEngagementStore | None = None,
    output_store: FakeOutputStore | None = None,
    agents: Callable[[AgentDeps], dict[str, ProcedureAgent[Any]]] | None = None,
) -> tuple[GraphRunner, FakeOutputStore]:
    """新项目的 runner: 登记系统缺省有 Numbat 的记录, Store 缺省为空 (由 start 写入固定集). 返回 runner 与审计产出存储.

    agents: 按依赖造出要替换的 Agent, 如 lambda deps: {"ACC-04": ScriptAgent(deps, script)}; 其余程序用真实 Agent.
    """
    if registry is None:
        registry = FakeRegistry({NUMBAT_ABN.abn: NUMBAT_ABN}, {NUMBAT_ASIC.acn: NUMBAT_ASIC})
    if output_store is None:
        output_store = FakeOutputStore()
    deps = make_deps(registry, FakeTrail(), engagement_store, output_store)
    nodes = GraphNodes(deps, parallel_cap=2)
    if agents is not None:
        nodes.agents.update(agents(deps))
    app = compile_graph(nodes, InMemorySaver(serde=checkpoint_serde()))
    return GraphRunner(app, deps), output_store


async def wait_idle(runner: GraphRunner, eid: str) -> None:
    """等该项目的后台任务结束; 没有后台任务时直接返回."""
    task = runner.tasks.get(eid)
    if task is not None:
        await task
