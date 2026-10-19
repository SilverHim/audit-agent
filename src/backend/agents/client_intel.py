"""A2: ACC-04. 返回字段: acceptance_scorecard, exceptions, procedure_runs.

数据集客户 (Numbat, Xero) 三项检查: abn_active, name_match, company_registered (ASIC 登记 mock). 新建的客户 (没有
数据集) 四项 (需求 R290): ABN 两项照旧 (ABR 实时查); company_registered 与 directors_match 用审计师上传的 ASIC 摘录
(最新一版抽取结果) 核对, 证据指针指向摘录文件. 新客户是公司而还没有摘录时, 主图在 ACC-04 之前等 (p0_inputs,
见 company_check).
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar

from backend.agents.base import AgentDeps, ProcedureAgent, RunContext, finish_run
from backend.contracts.errors import RegistryReason, RegistryUnavailable
from backend.contracts.models import (
    AbnRecord,
    AcceptanceScorecard,
    AgentId,
    AsicCompanyRecord,
    AsicExtract,
    AuditException,
    ClientProfile,
    Engagement,
    EvidencePointer,
    ProcedureRun,
    RegistryCheck,
    TrailEntry,
    TrailKind,
)
from backend.contracts.state import StatePatch
from backend.services.judgment_engine import trail_evidence


def normalize_name(name: str) -> str:
    """名称比对前的规范化: 转小写, 只留字母和数字 (去掉空白与标点)."""
    result = ""
    for ch in name.lower():
        if ch.isalnum():
            result += ch

    return result


# 没有查询的留痕 (payload available 为 false): trail_evidence 据此定为内部原件
NOT_QUERIED: dict[str, Any] = {"available": False}


def trail_pointer(trail_id: str, locator: str, payload: dict[str, Any] | None = None) -> EvidencePointer:
    """指向一条登记查询留痕的证据指针: locator 写结论依据的是查询结果里的哪个字段. type 与可靠性按留痕的 payload
    定 (judgment_engine.trail_evidence, 与判断引擎同一处解释): 真查了登记的 (缺省) 是审计师直接向官方登记系统查得,
    属外部原件; 没有查询的 (NOT_QUERIED) 只是系统自己的记录, 为内部原件 (type voucher), 只作定位."""
    evidence_type, reliability = trail_evidence(payload or {})
    return EvidencePointer(
        type=evidence_type, source_type="trail", source_id=trail_id, locator=locator, reliability=reliability
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
    """名称一致: 登记名称与客户申报的法定名称规范化后相等. 登记名称不公开 (ABN Lookup 对受限的 ABN 只给 ABN 与
    状态, 名称为空) 时待人工确认 (passed None), 不当作不一致."""
    evidence = trail_pointer(trail_id=trail_id, locator="entity_name")

    passed: bool | None
    if record is None:
        passed = False
        detail = "ABN record not found, names cannot be compared"
    elif not record.entity_name:
        passed = None
        detail = f"Registered name not public for ABN {record.abn}; confirm manually against the declared name"
    else:
        passed = normalize_name(record.entity_name) == normalize_name(legal_name)
        detail = f"Registered name: {record.entity_name}; declared name: {legal_name}"

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
NEW_CLIENT_CHECKS = (*MANUAL_CHECKS, "directors_match")  # 新建的客户的四项 (需求 R290)


def extract_pointer(extract: AsicExtract, locator: str) -> EvidencePointer:
    """上传的 ASIC 摘录上该字段的页码指针 (需求 R290; 抽取时校验过 acn, name, status, officeholders 都有指针, 可靠性
    取文件现在的登记)."""
    return next(p for p in extract.evidence if p.locator == locator)


def check_extract_registered(acn: str, extract: AsicExtract, legal_name: str) -> RegistryCheck:
    """新客户的公司已注册 (需求 R290): 上传的摘录 (按 ABN 登记的 ACN 取得) 状态为 Registered, 名称与画像法定名称
    规范化后相同. 指针: 状态不对指向 status, 名称不同指向 name, 都对指向 status."""
    registered = extract.status == "Registered"
    same_name = normalize_name(extract.name) == normalize_name(legal_name)
    detail = f"ASIC extract for ACN {acn}: status {extract.status}, name {extract.name}; declared name: {legal_name}"
    locator = "name" if registered and not same_name else "status"
    return RegistryCheck(
        check="company_registered",
        passed=registered and same_name,
        detail=detail,
        evidence=extract_pointer(extract, locator),
    )


def check_directors(extract: AsicExtract, directors: list[str]) -> RegistryCheck:
    """新客户的董事名单 (需求 R290): 画像的董事与摘录上 role 为 director 的姓名集合 (规范化同 name_match) 相同;
    秘书不比. 不一致时 detail 列出两边各多出的姓名."""
    on_extract = {normalize_name(o.name): o.name for o in extract.officeholders if o.role == "director"}
    declared = {normalize_name(name): name for name in directors}
    absent = [declared[k] for k in declared if k not in on_extract]
    undeclared = [on_extract[k] for k in on_extract if k not in declared]
    listed = ", ".join(on_extract.values()) or "none"
    detail = f"Directors on the ASIC extract: {listed}; declared: {', '.join(directors) or 'none'}"
    if absent:
        detail += f"; declared but not on the extract: {', '.join(absent)}"
    if undeclared:
        detail += f"; on the extract but not declared: {', '.join(undeclared)}"
    return RegistryCheck(
        check="directors_match",
        passed=not absent and not undeclared,
        detail=detail,
        evidence=extract_pointer(extract, "officeholders"),
    )


# 查不了登记的原因 (RegistryUnavailable.reason) 写进待人工确认的 detail; 只写类别, 不带原始响应
UNAVAILABLE: dict[RegistryReason, str] = {
    "no_source": "No registry source for {subject} (client without a dataset)",
    "not_configured": "ABN Lookup is not configured; {subject} not queried",
    "unreachable": "ABN Lookup could not be reached; {subject} not queried",
    "rejected": "ABN Lookup did not accept the authentication GUID; {subject} not queried",
    "service_error": "ABN Lookup returned an error; {subject} not queried",
    "not_company": "No ACN on the ABN record for {subject}: not a company, no ASIC extract required",
    "no_extract": "No uploaded ASIC extract could be used for {subject}",
}


def manual_detail(subject: str, reason: RegistryReason, against: str, unread: int = 0) -> str:
    """待人工确认的说明: 原因的类别; unread 为已上传而没能读出的 ASIC 摘录份数 (no_extract 时), 不为 0 时写明."""
    note = f" ({unread} uploaded ASIC extract{'s' if unread > 1 else ''} could not be read)" if unread else ""
    return f"{UNAVAILABLE[reason].format(subject=subject)}{note}; confirm manually against {against}"


def manual_checks(
    abn: str, trail_id: str, reason: RegistryReason, checks: tuple[str, ...] = MANUAL_CHECKS
) -> list[RegistryCheck]:
    """ABN 查不了 (没有登记来源, ABN Lookup 未配置, 不可达, 不认可 GUID 或出错) 时的各项检查 (数据集客户三项, 新
    客户另有 directors_match): passed 为 None, 待审计师人工确认, 不出例外; detail 写原因的类别; 证据指向记下 "没有
    查询" 的那条留痕 (内部原件, 只作定位)."""
    detail = manual_detail(f"ABN {abn}", reason, "ABN Lookup and ASIC")
    evidence = trail_pointer(trail_id, "available", NOT_QUERIED)
    return [
        RegistryCheck.model_validate({"check": check, "passed": None, "detail": detail, "evidence": evidence})
        for check in checks
    ]


def company_pending(detail: str, evidence: EvidencePointer) -> list[RegistryCheck]:
    """新客户的公司两项 (company_registered, directors_match) 待人工确认 (需求 R290), 同一说明与指针."""
    return [
        RegistryCheck(check="company_registered", passed=None, detail=detail, evidence=evidence),
        RegistryCheck(check="directors_match", passed=None, detail=detail, evidence=evidence),
    ]


async def company_check(deps: AgentDeps, engagement: Engagement, profile: ClientProfile) -> bool | None:
    """新建的客户是否为公司 (ABN 登记有 ACN; 需求 R290, 主图 p0_inputs 判断 ACC-04 要不要等 ASIC 摘录): 经
    RegistrySource 查一次 ABN, 写一行 external_call 留痕 (actor A2, procedure_id ACC-04, 没有 run_id; payload 同
    ACC-04 的查询留痕). 查得到且有 ACN 为真, 查不到或没有 ACN 为假; 查不了 (RegistryUnavailable) 为 None, 记一行
    "没有查询" 的留痕 (ACC-04 执行时同样转人工确认, 不等摘录)."""
    abn = profile.abn
    summary = f"ABN Lookup {abn} (before ACC-04: is an ASIC extract needed)"
    try:
        record = await deps.registry.abn(engagement.id, abn)
    except RegistryUnavailable as exc:
        payload: dict[str, Any] = {"abn": abn, **NOT_QUERIED, "reason": exc.reason, "result": None}
        await deps.trail.record(precheck_entry(engagement.id, f"{summary}: {exc.reason}", payload))
        return None
    result = record.model_dump(mode="json") if record is not None else None
    await deps.trail.record(precheck_entry(engagement.id, summary, {"abn": abn, "result": result}))
    return record is not None and record.acn is not None


def precheck_entry(eid: str, summary: str, payload: dict[str, Any]) -> TrailEntry:
    """company_check 的查询留痕: external_call, actor A2, procedure_id ACC-04, 没有 run_id (ACC-04 还没执行)."""
    return TrailEntry(
        engagement_id=eid,
        kind=TrailKind.external_call,
        actor=AgentId.A2,
        procedure_id="ACC-04",
        summary=summary,
        payload=payload,
    )


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
    """汇总各项检查生成承接评分卡. previous 是状态里已有的评分卡, 首次执行为 None."""

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
    """ACC-04 的原始发现: 客户画像与各项登记检查结果."""

    profile: ClientProfile  # 客户画像
    # 各项检查, 顺序为 abn_active, name_match, company_registered (新建的客户另有 directors_match)
    checks: list[RegistryCheck]


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

    async def record_unavailable(
        self, ctx: RunContext, kind: str, key: str, value: str | None, reason: RegistryReason
    ) -> str:
        """查不了登记时写一行 external_call 留痕 (payload 记 available false 与原因类别, result 为空), 返回留痕 ID.
        value 为空 (ABN 记录没有 ACN) 时摘要写 "no ACN"."""
        return await self.record_query(
            ctx=ctx,
            summary=f"{kind} {value or '(no ACN)'}: {reason}, manual confirmation",
            payload={key: value, **NOT_QUERIED, "reason": reason, "result": None},
        )

    async def collect_checks(self, ctx: RunContext, profile: ClientProfile) -> list[RegistryCheck]:
        """查 ABN 与 ASIC, 返回各项检查结果, 顺序为 abn_active, name_match, company_registered (新建的客户另有
        directors_match, 见 extract_checks). ABN 查不了 (RegistryUnavailable) 时记一条 "没有查询" 的留痕, 各项都待
        人工确认. 数据集客户的 company_registered 只由 ASIC 来源判定: 没有 ASIC 来源时另记一条 "没有查询" 的 ASIC
        留痕, 这一项待人工确认, 不因 ABN 记录没有 ACN 判不通过; 有来源而 ABN 记录没有 ACN 时照旧不通过 (ASIC 无从查)."""
        new_client = ctx.engagement.dataset is None
        try:
            abn_record, abn_trail_id = await self.lookup_abn(ctx=ctx, abn=profile.abn)
        except RegistryUnavailable as exc:
            trail_id = await self.record_unavailable(ctx, "ABN Lookup", "abn", profile.abn, exc.reason)
            checks = NEW_CLIENT_CHECKS if new_client else MANUAL_CHECKS
            return manual_checks(abn=profile.abn, trail_id=trail_id, reason=exc.reason, checks=checks)
        check_list = []
        check_list.append(check_abn_active(abn=profile.abn, record=abn_record, trail_id=abn_trail_id))
        check_list.append(check_name_match(record=abn_record, legal_name=profile.legal_name, trail_id=abn_trail_id))
        if new_client:
            return check_list + await self.extract_checks(ctx, profile, abn_record, abn_trail_id)

        acn = abn_record.acn if abn_record is not None else None
        try:
            if acn is None:  # 不查, 只确认有没有 ASIC 来源 (没有时抛 RegistryUnavailable)
                await self.deps.registry.asic_company(ctx.engagement.id, None)
                asic_record, asic_trail_id = None, abn_trail_id
            else:
                asic_record, asic_trail_id = await self.lookup_asic(ctx=ctx, acn=acn)
        except RegistryUnavailable as exc:
            trail_id = await self.record_unavailable(ctx, "ASIC company search", "acn", acn, exc.reason)
            subject = f"ACN {acn}" if acn is not None else "the company (no ACN on the ABN record)"
            detail = manual_detail(subject, exc.reason, "ASIC")
            evidence = trail_pointer(trail_id, "available", NOT_QUERIED)
            manual = RegistryCheck(check="company_registered", passed=None, detail=detail, evidence=evidence)
            check_list.append(manual)
            return check_list

        check_list.append(check_company_registered(acn=acn, record=asic_record, trail_id=asic_trail_id))

        return check_list

    async def extract_checks(
        self, ctx: RunContext, profile: ClientProfile, abn_record: AbnRecord | None, abn_trail_id: str
    ) -> list[RegistryCheck]:
        """新建的客户的 company_registered 与 directors_match (需求 R290), 用 ABN 登记的 ACN 取上传的 ASIC 摘录:
        - ABN 查不到或没有 ACN (不是公司): 不要求摘录, 两项待人工确认, 指针指向 ABN 查询 (locator acn).
        - 没有对得上的可用摘录, 而一份可用的都没有 (标了客户无此类资料, 或抽取没成) 或另有已上传而没能读出的
          (RegistryUnavailable no_extract, 对得上的那份可能正是没读出的): 记一条 "没有查询" 的 ASIC 留痕, 两项待人工
          确认, 说明写明没能读出的份数.
        - 摘录都读出了而 ACN 都对不上: company_registered 不通过, directors_match 待人工确认, 指针指向 ABN 查询.
        - 有该 ACN 的摘录: 两项按摘录判 (check_extract_registered, check_directors), 指针指向摘录文件."""
        acn = abn_record.acn if abn_record is not None else None
        if acn is None:
            subject = f"ABN {profile.abn}"
            detail = (
                manual_detail(subject, "not_company", "the ABN record")
                if abn_record is not None
                else f"ABN {profile.abn} not found in ABN Lookup; ASIC extract not checked"
            )
            return company_pending(detail, trail_pointer(abn_trail_id, "acn"))
        try:
            extract = await self.deps.registry.asic_extract(ctx.engagement.id, acn)
        except RegistryUnavailable as exc:
            trail_id = await self.record_unavailable(ctx, "ASIC extract", "acn", acn, exc.reason)
            evidence = trail_pointer(trail_id, "available", NOT_QUERIED)
            detail = manual_detail(f"ACN {acn}", exc.reason, "the ASIC register", exc.unread)
            return company_pending(detail, evidence)
        if extract is None:
            evidence = trail_pointer(abn_trail_id, "acn")
            detail = f"No uploaded ASIC extract for ACN {acn} (from the ABN record)"
            return [
                RegistryCheck(check="company_registered", passed=False, detail=detail, evidence=evidence),
                RegistryCheck(check="directors_match", passed=None, detail=detail, evidence=evidence),
            ]
        return [check_extract_registered(acn, extract, profile.legal_name), check_directors(extract, profile.directors)]

    async def execute(self, ctx: RunContext) -> Acc04Findings:
        """执行 ACC-04: 读本项目采用的那版客户画像, 查 ABN 与 ASIC, 返回各项检查结果.

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
