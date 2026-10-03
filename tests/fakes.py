"""测试用假实现与样例数据. B 的真实实现就绪前, Agent 与节点测试都注入这里的假工具."""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any, TypeVar

from langgraph.checkpoint.memory import InMemorySaver

from backend.agents.base import AgentDeps, RunContext
from backend.contracts.errors import VersionConflict
from backend.contracts.models import (
    AbnRecord,
    AsicCompanyRecord,
    AutonomyLevel,
    ClientProfile,
    Engagement,
    Phase,
    Procedure,
    ProcedureRun,
    ProcedureStatus,
    RunStatus,
    RunTrigger,
    TrailEntry,
    TrailKind,
    Versioned,
)
from backend.contracts.state import initial_state
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
    phase=Phase.P0_Acceptance,
    created_at=NOW,
    updated_at=NOW,
)


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
    """假留痕: 存在内存列表里, 按写入顺序发号 TR-1, TR-2, ..."""

    def __init__(self) -> None:
        self.entries: list[TrailEntry] = []  # 已写入的留痕, 按写入顺序

    async def record(self, entry: TrailEntry) -> str:
        trail_id = f"TR-{len(self.entries) + 1}"
        self.entries.append(entry.model_copy(update={"id": trail_id}))
        return trail_id

    async def get(self, eid: str, id: str) -> TrailEntry | None:
        return next((e for e in self.entries if e.id == id), None)

    async def find_success(self, eid: str, idempotency_key: str) -> TrailEntry | None:
        return next((e for e in self.entries if e.idempotency_key == idempotency_key), None)

    async def list(
        self, eid: str, kind: TrailKind | None = None, limit: int = 100, offset: int = 0
    ) -> list[TrailEntry]:
        hits = [e for e in self.entries if kind is None or e.kind is kind]
        return hits[offset : offset + limit]


class FakeEngagementStore:
    """假项目存储: 实现读项目, 改项目状态, 读写客户画像; create, list 调用即抛 NotImplementedError."""

    def __init__(
        self, profiles: list[ClientProfile] | None = None, engagements: list[Engagement] | None = None
    ) -> None:
        self.profiles = profiles or []  # 客户画像, 同一客户可有多个版本
        self.engagements = engagements or []  # 审计项目

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
        return next((e for e in self.engagements if e.id == eid), None)

    async def update(
        self,
        eid: str,
        *,
        phase: Phase | None = None,
        locked: bool | None = None,
        version_snapshot: dict[str, str] | None = None,
    ) -> None:
        """改项目的阶段, 锁定与版本快照, 传 None 的不改; 同时刷新 updated_at. locked 置真后不可回退."""
        for i, e in enumerate(self.engagements):
            if e.id == eid:
                if e.locked and locked is False:
                    raise ValueError(f"{eid} 已锁定, 不可解锁")
                changes = {"phase": phase, "locked": locked, "version_snapshot": version_snapshot}
                kept = {k: v for k, v in changes.items() if v is not None}
                self.engagements[i] = e.model_copy(update={**kept, "updated_at": datetime.now(UTC)})
                return
        raise LookupError(f"Engagement {eid} not found")

    async def client_profile(self, client_id: str, version: int | None = None) -> ClientProfile | None:
        """version 为空取最新版本; 查不到返回 None."""
        hits = [p for p in self.profiles if p.id == client_id and (version is None or p.version == version)]
        return max(hits, key=lambda p: p.version, default=None)

    async def save_profile(self, profile: ClientProfile) -> ClientProfile:
        """version 须为已有最大值 +1."""
        latest = await self.client_profile(profile.id)
        expected = latest.version + 1 if latest is not None else 1
        if profile.version != expected:
            raise VersionConflict(f"{profile.id}: v{profile.version}, 应为 v{expected}")
        self.profiles.append(profile)
        return profile

    async def list(self, owner_id: str | None = None) -> list[Engagement]:
        raise NotImplementedError


T = TypeVar("T", bound=Versioned)


class FakeOutputStore:
    """假审计产出存储, 只服务一个项目 (忽略 eid). 规则同开发接口文档第 4 节 AuditOutputStore.

    transaction 块内的 put 先暂存, 块正常结束才一起提交, 块内抛错则全部丢弃; 块外的每次 put 各自提交.
    """

    def __init__(self, rows: list[Versioned] | None = None) -> None:
        self.rows = rows or []  # 已提交对象, 按写入顺序; 构造时传入的视为此前已提交
        self.commits: list[list[Versioned]] = []  # 每次提交写入的新对象, 用于检查哪些对象属于同一事务
        self.pending: list[Versioned] | None = None  # 事务块内暂存的新对象, 不在事务中时为 None

    def _visible(self) -> list[Versioned]:
        """读写可见的对象: 已提交对象与本事务暂存对象."""
        return self.rows + (self.pending or [])

    async def put(self, eid: str, objs: Sequence[Versioned], *, actor: str) -> list[Versioned]:
        """同 (类型, id, version) 已存在则跳过并返回已存对象; 否则 version 须为已有最大值 +1, 不符抛 VersionConflict."""
        result: list[Versioned] = []
        new: list[Versioned] = []
        for obj in objs:
            same_id = [r for r in self._visible() + new if type(r) is type(obj) and r.id == obj.id]
            stored = next((r for r in same_id if r.version == obj.version), None)
            if stored is not None:
                result.append(stored)
                continue
            expected = max((r.version for r in same_id), default=0) + 1
            if obj.version != expected:
                raise VersionConflict(f"{type(obj).__name__} {obj.id}: v{obj.version}, 应为 v{expected}")
            new.append(obj)
            result.append(obj)
        if self.pending is not None:
            self.pending.extend(new)
        else:
            self.rows.extend(new)
            self.commits.append(new)
        return result

    async def get(self, eid: str, kind: type[T], id: str, version: int | None = None) -> T | None:
        """version 为空取最新版本; 查不到返回 None."""
        hits = [r for r in self._visible() if isinstance(r, kind) and r.id == id]
        if version is not None:
            hits = [r for r in hits if r.version == version]
        return max(hits, key=lambda r: r.version, default=None)

    async def latest(self, eid: str, kind: type[T], **filters: Any) -> list[T]:
        """每个 id 的最新版本, 再按顶层字段相等过滤."""
        newest: dict[str, T] = {}
        for r in self._visible():
            if isinstance(r, kind) and (r.id not in newest or r.version > newest[r.id].version):
                newest[r.id] = r
        return [r for r in newest.values() if all(getattr(r, k) == v for k, v in filters.items())]

    async def history(self, eid: str, kind: type[T], id: str) -> list[T]:
        """某个 id 的全部版本, 按版本号升序."""
        return sorted((r for r in self._visible() if isinstance(r, kind) and r.id == id), key=lambda r: r.version)

    @asynccontextmanager
    async def transaction(self, eid: str) -> AsyncIterator[None]:
        """块内的 put 共用一个事务; 嵌套调用加入外层事务."""
        if self.pending is not None:
            yield
            return
        self.pending = []
        try:
            yield
        except BaseException:
            self.pending = None
            raise
        self.rows.extend(self.pending)
        self.commits.append(self.pending)
        self.pending = None


# ── 组装 ──


def make_deps(
    registry: FakeRegistry,
    trail: FakeTrail,
    engagement_store: FakeEngagementStore | None = None,
    output_store: FakeOutputStore | None = None,
) -> AgentDeps:
    """真实依赖里换掉登记系统, 留痕, 项目存储与审计产出存储. 其余依赖只构造不连接.

    项目存储缺省只有 Numbat 的画像与 ENGAGEMENT; 审计产出存储缺省为空.
    """
    if engagement_store is None:
        engagement_store = FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT])
    if output_store is None:
        output_store = FakeOutputStore()
    return replace(
        build_deps(get_settings()),
        registry=registry,
        trail=trail,
        engagement_store=engagement_store,
        output_store=output_store,
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
    registry: FakeRegistry | None = None, engagement_store: FakeEngagementStore | None = None
) -> tuple[GraphRunner, FakeOutputStore]:
    """新项目的 runner: 登记系统缺省有 Numbat 的记录, Store 为空 (由 start 写入固定集). 返回 runner 与审计产出存储."""
    if registry is None:
        registry = FakeRegistry({NUMBAT_ABN.abn: NUMBAT_ABN}, {NUMBAT_ASIC.acn: NUMBAT_ASIC})
    output_store = FakeOutputStore()
    deps = make_deps(registry, FakeTrail(), engagement_store, output_store)
    app = compile_graph(GraphNodes(deps, parallel_cap=2), InMemorySaver(serde=checkpoint_serde()))
    return GraphRunner(app, deps), output_store


async def wait_idle(runner: GraphRunner, eid: str) -> None:
    """等该项目的后台任务结束; 没有后台任务时直接返回."""
    task = runner.tasks.get(eid)
    if task is not None:
        await task
