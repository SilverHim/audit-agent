"""A2: ACC-04. 返回字段: acceptance_scorecard, exceptions, procedure_runs."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar

from backend.agents.base import ProcedureAgent, RunContext, finish_run
from backend.contracts.errors import RegistryUnavailable
from backend.contracts.models import (
    AbnRecord,
    AcceptanceScorecard,
    AgentId,
    AsicCompanyRecord,
    AuditException,
    ClientProfile,
    EvidencePointer,
    EvidenceType,
    ProcedureRun,
    RegistryCheck,
    Reliability,
    TrailEntry,
    TrailKind,
)
from backend.contracts.state import StatePatch


def normalize_name(name: str) -> str:
    """名称比对前的规范化: 转小写, 只留字母和数字 (去掉空白与标点)."""
    result = ""
    for ch in name.lower():
        if ch.isalnum():
            result += ch

    return result


def trail_pointer(trail_id: str, locator: str) -> EvidencePointer:
    """指向一次登记查询留痕的证据指针: locator 写结论依据的是查询结果里的哪个字段."""

    # 审计师直接向官方登记系统查询取得, 属外部原件 (ASA 500 A31)
    return EvidencePointer(
        type=EvidenceType.external,
        source_type="trail",
        source_id=trail_id,
        locator=locator,
        reliability=Reliability.external_original,
    )


def check_abn_active(abn: str, record: AbnRecord | None, trail_id: str) -> RegistryCheck:
    """ABN 有效: 登记系统里查得到这个 ABN, 且 status 为 Active."""
    evidence = trail_pointer(trail_id=trail_id, locator="status")

    if record is not None:
        passed = record.status == "Active"
        detail = f"ABN {abn} status: {record.status}"
    else:
        passed = False
        detail = f"ABN {abn} not found in ABN Lookup"

    return RegistryCheck(check="abn_active", passed=passed, detail=detail, evidence=evidence)


def check_name_match(record: AbnRecord | None, legal_name: str, trail_id: str) -> RegistryCheck:
    """名称一致: 登记名称与客户申报的法定名称规范化后相等."""
    evidence = trail_pointer(trail_id=trail_id, locator="entity_name")

    if record is not None:
        passed = normalize_name(record.entity_name) == normalize_name(legal_name)
        detail = f"Registered name: {record.entity_name}; declared name: {legal_name}"
    else:
        passed = False
        detail = "ABN record not found, names cannot be compared"

    return RegistryCheck(check="name_match", passed=passed, detail=detail, evidence=evidence)


def check_company_registered(acn: str | None, record: AsicCompanyRecord | None, trail_id: str) -> RegistryCheck:
    """公司已注册: 按 ACN 在 ASIC 查得到记录, 且 status 为 Registered."""
    if acn is None:
        passed = False
        locator = "acn"
        detail = "No ACN on ABN record, ASIC not queried"
    elif record is None:
        passed = False
        locator = "status"
        detail = f"ACN {acn} not found in ASIC register"
    else:
        passed = record.status == "Registered"
        locator = "status"
        detail = f"ACN {acn} status: {record.status}"

    evidence = trail_pointer(trail_id=trail_id, locator=locator)

    return RegistryCheck(check="company_registered", passed=passed, detail=detail, evidence=evidence)


MANUAL_CHECKS = ("abn_active", "name_match", "company_registered")  # 三项检查, 顺序同 collect_checks


def unavailable_pointer(trail_id: str) -> EvidencePointer:
    """指向记下 "登记未接入, 没有查询" 的那条留痕: 没有取得外部证据, 只是系统自己的记录, 故为内部原件 (type voucher,
    同内部记录), 不标外部原件; 只作定位."""
    return EvidencePointer(
        type=EvidenceType.voucher,
        source_type="trail",
        source_id=trail_id,
        locator="available",
        reliability=Reliability.internal_original,
    )


def manual_checks(abn: str, trail_id: str) -> list[RegistryCheck]:
    """没有可查的登记来源 (新建的客户, ABR 与 ASIC 摘录未接入) 时的三项检查: passed 为 None, 待审计师人工确认,
    不出例外; 证据指向记下 "未接登记" 的那条留痕 (unavailable_pointer, 不是外部证据)."""
    detail = (
        f"No registry source for ABN {abn} (client without a dataset); confirm manually against ABN Lookup and ASIC"
    )
    return [
        RegistryCheck(check=check, passed=None, detail=detail, evidence=unavailable_pointer(trail_id))
        for check in MANUAL_CHECKS
    ]


def build_exception(check: RegistryCheck, run: ProcedureRun, abn: str) -> AuditException:
    """未通过的检查生成一条例外: rule_id 取检查名, severity 为 high, 涉及对象为客户 ABN."""
    return AuditException(
        id=f"X-{run.procedure_id}-{run.run_no}-{check.check}-1",
        procedure_id=run.procedure_id,
        rule_id=check.check,
        run_id=run.id,
        severity="high",
        source_ids=[abn],
        evidence=[check.evidence],
    )


def build_scorecard(
    checks: list[RegistryCheck],
    exceptions: list[AuditException],
    profile: ClientProfile,
    previous: AcceptanceScorecard | None,
) -> AcceptanceScorecard:
    """汇总三项检查生成承接评分卡. previous 是状态里已有的评分卡, 首次执行为 None."""

    version = previous.version + 1 if previous is not None else 1

    return AcceptanceScorecard(
        id="SCORECARD",
        version=version,
        registry_checks=checks,
        exception_ids=[n.id for n in exceptions],
        client_profile_version=profile.version,
    )


@dataclass(frozen=True)
class Acc04Findings:
    """ACC-04 的原始发现: 客户画像与三项登记检查结果."""

    profile: ClientProfile  # 客户画像
    checks: list[RegistryCheck]  # 三项检查, 顺序为 abn_active, name_match, company_registered


class ClientIntelAgent(ProcedureAgent[Acc04Findings]):
    """A2: 执行 ACC-04, 查 ABN 与 ASIC 登记, 产出承接评分卡."""

    agent_id: ClassVar[AgentId] = AgentId.A2  # 智能体编号
    procedures: ClassVar[frozenset[str]] = frozenset({"ACC-04"})  # 负责的程序

    async def record_query(self, ctx: RunContext, summary: str, payload: dict[str, Any]) -> str:
        """写一行 external_call 留痕, 返回留痕 ID."""
        entry = TrailEntry(
            engagement_id=ctx.engagement.id,
            kind=TrailKind.external_call,
            actor=self.agent_id,
            procedure_id=ctx.procedure.id,
            run_id=ctx.run.id,
            summary=summary,
            payload=payload,
        )

        return await self.deps.trail.record(entry)

    async def lookup_abn(self, ctx: RunContext, abn: str) -> tuple[AbnRecord | None, str]:
        """查 ABN 登记并记一条留痕, 返回 (查询结果, 留痕 ID). 查不到也要记留痕."""
        record = await self.deps.registry.abn(ctx.engagement.id, abn)
        trail_id = await self.record_query(
            ctx=ctx,
            summary=f"ABN Lookup {abn}",
            payload={"abn": abn, "result": record.model_dump(mode="json") if record is not None else None},
        )

        return (record, trail_id)

    async def lookup_asic(self, ctx: RunContext, acn: str) -> tuple[AsicCompanyRecord | None, str]:
        """按 ACN 查 ASIC 公司登记并记一条留痕, 返回 (查询结果, 留痕 ID)."""
        record = await self.deps.registry.asic_company(ctx.engagement.id, acn)
        trail_id = await self.record_query(
            ctx=ctx,
            summary=f"ASIC company search {acn}",
            payload={"acn": acn, "result": record.model_dump(mode="json") if record is not None else None},
        )

        return (record, trail_id)

    async def collect_checks(self, ctx: RunContext, profile: ClientProfile) -> list[RegistryCheck]:
        """查 ABN 与 ASIC, 返回三项检查结果, 顺序为 abn_active, name_match, company_registered. 没有可查的登记来源
        (RegistryUnavailable) 时记一条留痕, 三项都待人工确认."""
        try:
            abn_record, abn_trail_id = await self.lookup_abn(ctx=ctx, abn=profile.abn)
        except RegistryUnavailable:
            trail_id = await self.record_query(
                ctx=ctx,
                summary=f"ABN Lookup {profile.abn}: no registry source, manual confirmation",
                payload={"abn": profile.abn, "available": False, "result": None},
            )
            return manual_checks(abn=profile.abn, trail_id=trail_id)
        check_list = []
        check_list.append(check_abn_active(abn=profile.abn, record=abn_record, trail_id=abn_trail_id))
        check_list.append(check_name_match(record=abn_record, legal_name=profile.legal_name, trail_id=abn_trail_id))

        acn = abn_record.acn if abn_record is not None else None
        if acn is not None:
            asic_record, asic_trail_id = await self.lookup_asic(ctx=ctx, acn=acn)
        else:
            asic_record, asic_trail_id = None, abn_trail_id

        check_list.append(check_company_registered(acn=acn, record=asic_record, trail_id=asic_trail_id))

        return check_list

    async def execute(self, ctx: RunContext) -> Acc04Findings:
        """执行 ACC-04: 读本项目采用的那版客户画像, 查 ABN 与 ASIC, 返回三项检查结果.

        画像版本取状态中的 client_profile_version, 不取全局最新: 同客户的其他项目改过画像, 不影响本项目的核查.
        """
        version = ctx.state["client_profile_version"]
        profile = await self.deps.engagement_store.client_profile(ctx.engagement.client_id, version)

        if profile is None:
            raise LookupError(f"client profile {ctx.engagement.client_id} v{version} not found")

        check_list = await self.collect_checks(ctx=ctx, profile=profile)

        return Acc04Findings(profile=profile, checks=check_list)

    async def interpret(self, ctx: RunContext, findings: Acc04Findings) -> StatePatch:
        """未通过的检查出例外 (待人工确认的不出), 汇总评分卡, 执行记录置成功终态."""
        exceptions = []
        for check in findings.checks:
            if check.passed is False:
                exceptions.append(build_exception(check=check, run=ctx.run, abn=findings.profile.abn))

        scorecard = build_scorecard(
            checks=findings.checks,
            exceptions=exceptions,
            profile=findings.profile,
            previous=ctx.state["acceptance_scorecard"],
        )

        return {
            "acceptance_scorecard": scorecard,
            "exceptions": exceptions,
            "procedure_runs": [finish_run(ctx.run, datetime.now(UTC))],
        }
