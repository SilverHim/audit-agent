"""ACC-04 (A2 ClientIntelAgent). 登记系统, 留痕, 客户画像用假实现; B 的 mock 就绪后另补接真实 mock 的测试."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from backend.agents.client_intel import (
    ClientIntelAgent,
    build_exception,
    build_scorecard,
    check_abn_active,
    check_company_registered,
    check_name_match,
    finish_run,
    normalize_name,
    trail_pointer,
)
from backend.contracts.errors import RegistryReason, RegistryUnavailable
from backend.contracts.models import (
    AbnRecord,
    AcceptanceScorecard,
    AsicCompanyRecord,
    EvidencePointer,
    EvidenceType,
    ExceptionDisposition,
    RegistryCheck,
    Reliability,
    RunStatus,
    TrailKind,
)
from tests.fakes import (
    NUMBAT_ABN,
    NUMBAT_ASIC,
    NUMBAT_PROFILE,
    FakeEngagementStore,
    FakeRegistry,
    FakeTrail,
    make_ctx,
    make_deps,
    make_run,
)


def test_normalize_name_ignores_case_space_punctuation() -> None:
    assert normalize_name("Numbat Electronic Components Pty. Ltd.") == "numbatelectroniccomponentsptyltd"


def test_normalize_name_different_companies_not_equal() -> None:
    assert normalize_name("Numbat Electronic Components Pty. Ltd.") != normalize_name("Numbat Pty Ltd")


def test_abn_active_passes_when_status_active() -> None:
    check = check_abn_active("68900042015", NUMBAT_ABN, "TR-1")
    assert check.check == "abn_active"
    assert check.passed
    assert check.evidence.source_id == "TR-1"


def test_abn_active_fails_when_cancelled() -> None:
    check = check_abn_active("68900042015", NUMBAT_ABN.model_copy(update={"status": "Cancelled"}), "TR-1")
    assert not check.passed
    assert "Cancelled" in check.detail


def test_abn_active_fails_when_not_found() -> None:
    check = check_abn_active("68900042015", None, "TR-1")
    assert not check.passed
    assert "68900042015" in check.detail
    assert check.evidence.locator == "status"


def test_checks_point_to_registry_query_trail() -> None:
    """三项核查的证据都指向登记查询那行留痕 (ACC-04.yaml 的规定), 整体比较全部字段."""
    checks = [
        check_abn_active("68900042015", NUMBAT_ABN, "TR-1"),
        check_name_match(NUMBAT_ABN, NUMBAT_PROFILE.legal_name, "TR-1"),
        check_company_registered("900042015", NUMBAT_ASIC, "TR-2"),
    ]

    def pointer(source_id: str, locator: str) -> EvidencePointer:
        return EvidencePointer(
            type=EvidenceType.external,
            source_type="trail",
            source_id=source_id,
            locator=locator,
            reliability=Reliability.external_original,
        )

    assert [c.evidence for c in checks] == [
        pointer("TR-1", "status"),
        pointer("TR-1", "entity_name"),
        pointer("TR-2", "status"),
    ]


# ── name_match ──


def test_name_match_ignores_case_and_punctuation() -> None:
    check = check_name_match(NUMBAT_ABN, "NUMBAT ELECTRONIC COMPONENTS PTY. LTD.", "TR-1")
    assert check.check == "name_match"
    assert check.passed
    assert check.evidence.source_id == "TR-1"
    assert check.evidence.locator == "entity_name"


def test_name_match_fails_when_names_differ() -> None:
    check = check_name_match(NUMBAT_ABN, "Numbat Pty Ltd", "TR-1")
    assert not check.passed
    assert "Numbat Electronic Components Pty Ltd" in check.detail
    assert "Numbat Pty Ltd" in check.detail


def test_name_match_fails_when_abn_not_found() -> None:
    check = check_name_match(None, "Numbat Electronic Components Pty Ltd", "TR-1")
    assert not check.passed


# ── company_registered ──


def test_company_registered_passes_when_registered() -> None:
    check = check_company_registered("900042015", NUMBAT_ASIC, "TR-2")
    assert check.check == "company_registered"
    assert check.passed
    assert check.evidence.source_id == "TR-2"
    assert check.evidence.locator == "status"


def test_company_registered_fails_when_deregistered() -> None:
    check = check_company_registered("900042015", NUMBAT_ASIC.model_copy(update={"status": "Deregistered"}), "TR-2")
    assert not check.passed
    assert "Deregistered" in check.detail


def test_company_registered_fails_when_asic_not_found() -> None:
    check = check_company_registered("900042015", None, "TR-2")
    assert not check.passed
    assert "900042015" in check.detail
    assert check.evidence.locator == "status"


def test_company_registered_fails_without_acn() -> None:
    check = check_company_registered(None, None, "TR-1")
    assert not check.passed
    assert check.evidence.source_id == "TR-1"
    assert check.evidence.locator == "acn"


# ── 2.7 查询与留痕: 注入假登记系统与假留痕 ──


def make_agent(
    registry: FakeRegistry, engagements: FakeEngagementStore | None = None
) -> tuple[ClientIntelAgent, FakeTrail]:
    trail = FakeTrail()
    return ClientIntelAgent(make_deps(registry, trail, engagements)), trail


async def test_record_query_writes_external_call_trail() -> None:
    agent, trail = make_agent(FakeRegistry())
    ctx = make_ctx(agent.deps)

    trail_id = await agent.record_query(ctx, "ABN Lookup 68900042015", {"abn": "68900042015"})

    assert trail_id == "TR-1"
    [entry] = trail.entries
    assert entry.engagement_id == "ENG-2026-001"
    assert entry.kind is TrailKind.external_call
    assert entry.actor == "A2"
    assert entry.procedure_id == "ACC-04"
    assert entry.run_id == "RUN-ACC-04-1"
    assert entry.payload == {"abn": "68900042015"}


async def test_lookup_abn_returns_record_and_trail_id() -> None:
    agent, trail = make_agent(FakeRegistry(abn_records={"68900042015": NUMBAT_ABN}))

    record, trail_id = await agent.lookup_abn(make_ctx(agent.deps), "68900042015")

    assert record == NUMBAT_ABN
    assert trail_id == "TR-1"
    [entry] = trail.entries
    assert "68900042015" in entry.summary
    assert entry.payload["abn"] == "68900042015"
    assert entry.payload["result"]["entity_name"] == "Numbat Electronic Components Pty Ltd"


async def test_lookup_abn_not_found_still_records_trail() -> None:
    agent, trail = make_agent(FakeRegistry())

    record, trail_id = await agent.lookup_abn(make_ctx(agent.deps), "68900042015")

    assert record is None
    assert trail_id == "TR-1"
    assert trail.entries[0].payload["result"] is None


async def test_lookup_asic_returns_record_and_trail_id() -> None:
    agent, trail = make_agent(FakeRegistry(asic_records={"900042015": NUMBAT_ASIC}))

    record, trail_id = await agent.lookup_asic(make_ctx(agent.deps), "900042015")

    assert record == NUMBAT_ASIC
    assert trail_id == "TR-1"
    [entry] = trail.entries
    assert "900042015" in entry.summary
    assert entry.payload["acn"] == "900042015"
    assert entry.payload["result"]["registered_date"] == "2011-03-14"


async def test_collect_checks_all_pass() -> None:
    registry = FakeRegistry({"68900042015": NUMBAT_ABN}, {"900042015": NUMBAT_ASIC})
    agent, trail = make_agent(registry)

    ctx = make_ctx(agent.deps)
    checks = await agent.collect_checks(ctx, NUMBAT_PROFILE)

    assert [c.check for c in checks] == ["abn_active", "name_match", "company_registered"]
    assert all(c.passed for c in checks)
    assert registry.asic_queries == ["900042015"]  # ACN 取自 ABN 登记记录
    assert registry.eids == [ctx.engagement.id, ctx.engagement.id]  # 登记按项目的数据集定位, 须传本项目 ID
    assert [c.evidence.source_id for c in checks] == ["TR-1", "TR-1", "TR-2"]
    assert len(trail.entries) == 2


async def test_collect_checks_without_acn_skips_asic() -> None:
    registry = FakeRegistry({"68900042015": NUMBAT_ABN.model_copy(update={"acn": None})}, {"900042015": NUMBAT_ASIC})
    agent, trail = make_agent(registry)

    checks = await agent.collect_checks(make_ctx(agent.deps), NUMBAT_PROFILE)

    assert registry.asic_queries == []
    assert len(trail.entries) == 1
    assert not checks[2].passed
    assert checks[2].evidence.source_id == "TR-1"
    assert checks[2].evidence.locator == "acn"


async def test_collect_checks_abn_not_found() -> None:
    registry = FakeRegistry(asic_records={"900042015": NUMBAT_ASIC})
    agent, trail = make_agent(registry)

    checks = await agent.collect_checks(make_ctx(agent.deps), NUMBAT_PROFILE)

    assert not checks[0].passed
    assert not checks[1].passed
    assert registry.asic_queries == []  # 查不到 ABN 就拿不到 ACN, 不查 ASIC
    assert len(trail.entries) == 1
    assert not checks[2].passed
    assert checks[2].evidence.locator == "acn"


# ── 2.8 组装: 例外, 评分卡, 执行记录终态 ──


def test_build_exception_fields() -> None:
    check = RegistryCheck(
        check="company_registered",
        passed=False,
        detail="ACN 900042015 status: Deregistered",
        evidence=trail_pointer("TR-2", "status"),
    )

    exc = build_exception(check, make_run(run_no=3), "68900042015")

    assert exc.id == "X-ACC-04-3-company_registered-1"
    assert exc.procedure_id == "ACC-04"
    assert exc.run_id == "RUN-ACC-04-3"
    assert exc.rule_id == "company_registered"
    assert exc.severity == "high"
    assert exc.source_ids == ["68900042015"]
    assert exc.evidence == [check.evidence]
    assert exc.version == 1
    assert exc.disposition == ExceptionDisposition.open


def test_build_exception_takes_rule_and_abn_from_arguments() -> None:
    check = RegistryCheck(
        check="name_match",
        passed=False,
        detail="Registered name: Numbat Pty Ltd; declared name: Numbat Electronic Components Pty Ltd",
        evidence=trail_pointer("TR-1", "entity_name"),
    )

    exc = build_exception(check, make_run(run_no=2), "51824753556")

    assert exc.id == "X-ACC-04-2-name_match-1"
    assert exc.rule_id == "name_match"
    assert exc.source_ids == ["51824753556"]


# 一次执行的三项检查: 第 1 项通过, 后 2 项未通过
CHECKS = [
    RegistryCheck(
        check="abn_active",
        passed=True,
        detail="ABN 68900042015 status: Active",
        evidence=trail_pointer("TR-1", "status"),
    ),
    RegistryCheck(
        check="name_match",
        passed=False,
        detail="Registered name: Numbat Pty Ltd; declared name: Numbat Electronic Components Pty Ltd",
        evidence=trail_pointer("TR-1", "entity_name"),
    ),
    RegistryCheck(
        check="company_registered",
        passed=False,
        detail="ACN 900042015 status: Deregistered",
        evidence=trail_pointer("TR-2", "status"),
    ),
]


def test_build_scorecard_first_version() -> None:
    run = make_run()
    exceptions = [build_exception(CHECKS[1], run, "68900042015"), build_exception(CHECKS[2], run, "68900042015")]
    profile = NUMBAT_PROFILE.model_copy(update={"version": 2})

    scorecard = build_scorecard(CHECKS, exceptions, profile, None)

    assert scorecard.id == "SCORECARD"
    assert scorecard.version == 1
    assert scorecard.registry_checks == CHECKS
    assert scorecard.exception_ids == ["X-ACC-04-1-name_match-1", "X-ACC-04-1-company_registered-1"]
    assert scorecard.client_profile_version == 2


def test_build_scorecard_bumps_previous_version() -> None:
    previous = AcceptanceScorecard(
        id="SCORECARD", version=4, registry_checks=[], exception_ids=[], client_profile_version=1
    )

    scorecard = build_scorecard(CHECKS, [], NUMBAT_PROFILE, previous)

    assert scorecard.version == 5
    assert scorecard.exception_ids == []
    assert scorecard.client_profile_version == 1


def test_finish_run_marks_succeeded() -> None:
    run = make_run(run_no=2)
    ended = datetime(2026, 9, 30, 1, 30, tzinfo=UTC)

    done = finish_run(run, ended)

    assert done.status == RunStatus.succeeded
    assert done.ended_at == ended
    assert done.id == "RUN-ACC-04-2"
    assert done.started_at == run.started_at
    assert done.version == 1
    assert run.status == RunStatus.running  # 原记录不变: 模型是 frozen, 只能复制出新对象


# ── 2.8d run: 读画像, 检查, 组装返回片段 ──


def numbat_registry(abn_status: str = "Active", asic_status: str = "Registered") -> FakeRegistry:
    """Numbat 的 ABN 与 ASIC 登记, 状态可改."""
    return FakeRegistry(
        {"68900042015": NUMBAT_ABN.model_copy(update={"status": abn_status})},
        {"900042015": NUMBAT_ASIC.model_copy(update={"status": asic_status})},
    )


async def test_run_all_pass() -> None:
    agent, _ = make_agent(numbat_registry())

    patch = await agent.run(make_ctx(agent.deps))

    assert set(patch) == {"acceptance_scorecard", "exceptions", "procedure_runs"}
    scorecard = patch["acceptance_scorecard"]
    assert scorecard.version == 1
    assert [c.passed for c in scorecard.registry_checks] == [True, True, True]
    assert scorecard.exception_ids == []
    assert patch["exceptions"] == []
    runs = patch["procedure_runs"]
    assert len(runs) == 1
    assert runs[0].id == "RUN-ACC-04-1"
    assert runs[0].status == RunStatus.succeeded
    assert runs[0].ended_at is not None


async def test_run_failed_checks_create_exceptions() -> None:
    agent, _ = make_agent(numbat_registry(abn_status="Cancelled", asic_status="Deregistered"))

    patch = await agent.run(make_ctx(agent.deps))

    ids = ["X-ACC-04-1-abn_active-1", "X-ACC-04-1-company_registered-1"]  # name_match 通过, 不出例外
    assert [e.id for e in patch["exceptions"]] == ids
    assert patch["acceptance_scorecard"].exception_ids == ids
    assert patch["exceptions"][0].source_ids == ["68900042015"]
    assert patch["procedure_runs"][0].status == RunStatus.succeeded  # 检查不通过不算执行失败


async def test_run_reads_profile_version_in_state() -> None:
    """读状态中本项目采用的第 3 版 (名称正确), 不读更早的第 1 版, 也不读别的项目改出的第 5 版 (名称都写错)."""
    typo = NUMBAT_PROFILE.model_copy(update={"legal_name": "Numbat Electronics Pty Ltd"})
    other = NUMBAT_PROFILE.model_copy(update={"id": "CLIENT-002", "abn": "51824753556", "version": 7})
    store = FakeEngagementStore(
        [typo, NUMBAT_PROFILE.model_copy(update={"version": 3}), typo.model_copy(update={"version": 5}), other]
    )
    agent, trail = make_agent(numbat_registry(), store)
    ctx = make_ctx(agent.deps)
    ctx.state["client_profile_version"] = 3

    patch = await agent.run(ctx)

    assert patch["acceptance_scorecard"].client_profile_version == 3
    assert patch["exceptions"] == []  # 第 3 版名称与登记一致; 读错成第 1 或第 5 版会出 name_match 例外
    assert trail.entries[0].payload["abn"] == "68900042015"


async def test_run_bumps_existing_scorecard_version() -> None:
    agent, _ = make_agent(numbat_registry())
    ctx = make_ctx(agent.deps)
    state = ctx.state.copy()
    state["acceptance_scorecard"] = AcceptanceScorecard(
        id="SCORECARD", version=2, registry_checks=[], exception_ids=[], client_profile_version=1
    )

    patch = await agent.run(replace(ctx, state=state))

    assert patch["acceptance_scorecard"].version == 3


async def test_run_raises_when_profile_missing() -> None:
    agent, trail = make_agent(numbat_registry(), FakeEngagementStore([]))

    with pytest.raises(LookupError):
        await agent.run(make_ctx(agent.deps))

    assert trail.entries == []  # 没有画像就不查登记


# ── 查不了登记 (需求 R288, R289): 待人工确认, 原因只写类别 ──


class Unavailable(FakeRegistry):
    """查不了登记的假登记: ABN 查询 (asic_only 时只有 ASIC 查询) 抛 RegistryUnavailable(reason); ABN 照常查,
    登记记录缺省为 Numbat 的."""

    def __init__(self, reason: RegistryReason, *, asic_only: bool = False, record: AbnRecord = NUMBAT_ABN) -> None:
        super().__init__({"68900042015": record})
        self.reason, self.asic_only = reason, asic_only

    async def abn(self, eid: str, abn: str) -> AbnRecord | None:
        if not self.asic_only:
            raise RegistryUnavailable("x", reason=self.reason)
        return await super().abn(eid, abn)

    async def asic_company(self, eid: str, acn: str | None) -> AsicCompanyRecord | None:
        raise RegistryUnavailable("x", reason=self.reason)


REASONS: list[RegistryReason] = ["no_source", "not_configured", "unreachable", "rejected", "service_error"]
NOT_QUERIED = (EvidenceType.voucher, Reliability.internal_original)  # 没有查询的留痕只作定位, 是内部原件
QUERIED = (EvidenceType.external, Reliability.external_original)


async def test_abn_unavailable_leaves_all_three_checks_for_manual_confirmation() -> None:
    """ABN 查不了时三项都待人工确认 (passed None), 不出例外; 记一条留痕 (available false, 原因类别, 无结果), 三项的
    证据都指向它 (内部原件); 原因不同 detail 也不同."""
    details = set()
    for reason in REASONS:
        agent, trail = make_agent(Unavailable(reason))
        patch = await agent.run(make_ctx(agent.deps))
        checks = patch["acceptance_scorecard"].registry_checks
        [entry] = trail.entries
        assert entry.payload == {"abn": "68900042015", "available": False, "reason": reason, "result": None}
        assert [(c.passed, c.evidence.source_id) for c in checks] == [(None, entry.id)] * 3
        assert {(c.evidence.type, c.evidence.reliability) for c in checks} == {NOT_QUERIED}
        assert patch["exceptions"] == []
        details |= {c.detail for c in checks}
    assert len(details) == len(REASONS)


@pytest.mark.parametrize("acn", ["900042015", None])  # None: ABN 记录没有 ACN (个人独资, 信托等)
async def test_asic_unavailable_leaves_only_company_registered(acn: str | None) -> None:
    """ABN 查到 (外部原件), 没有 ASIC 来源 (新建的客户, 摘录未接入): 前两项照常判, 只有 company_registered 待人工
    确认, 证据指向另记的 ASIC "未查询" 留痕; ABN 记录没有 ACN 时同样, 不判不通过 (company_registered 只由 ASIC 来源
    判定)."""
    agent, trail = make_agent(
        Unavailable("no_source", asic_only=True, record=NUMBAT_ABN.model_copy(update={"acn": acn}))
    )
    patch = await agent.run(make_ctx(agent.deps))
    checks = patch["acceptance_scorecard"].registry_checks
    abn_entry, asic_entry = trail.entries
    assert asic_entry.payload == {"acn": acn, "available": False, "reason": "no_source", "result": None}
    assert [(c.passed, c.evidence.source_id) for c in checks] == [
        (True, abn_entry.id),
        (True, abn_entry.id),
        (None, asic_entry.id),
    ]
    assert [(c.evidence.type, c.evidence.reliability) for c in checks] == [QUERIED, QUERIED, NOT_QUERIED]
    assert patch["exceptions"] == []


async def test_name_not_public_is_left_for_manual_confirmation() -> None:
    """ABN 受限 (登记名称不公开, entity_name 为空): name_match 待人工确认, 不出 "名称不一致" 例外."""
    agent, _ = make_agent(
        FakeRegistry({"68900042015": NUMBAT_ABN.model_copy(update={"entity_name": ""})}, {"900042015": NUMBAT_ASIC})
    )
    patch = await agent.run(make_ctx(agent.deps))
    assert [c.passed for c in patch["acceptance_scorecard"].registry_checks] == [True, None, True]
    assert patch["exceptions"] == []
