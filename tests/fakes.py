"""测试用假实现与样例数据. B 的真实实现就绪前, Agent 与节点测试都注入这里的假工具."""

from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any

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
)
from backend.contracts.state import initial_state
from backend.core.config import get_settings
from backend.main import build_deps

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
    """假项目存储: 只实现读项目, 读写客户画像, 其余方法调用即抛 NotImplementedError."""

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
        raise NotImplementedError

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


# ── 组装 ──


def make_deps(registry: FakeRegistry, trail: FakeTrail, engagements: FakeEngagementStore | None = None) -> AgentDeps:
    """真实依赖里换掉登记系统, 留痕与项目存储. 项目存储缺省只有 Numbat 的画像与 ENGAGEMENT. 其余依赖只构造不连接."""
    if engagements is None:
        engagements = FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT])
    return replace(build_deps(get_settings()), registry=registry, trail=trail, engagements=engagements)


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
