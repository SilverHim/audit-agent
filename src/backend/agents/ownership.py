"""A5 的 GEN-08 股权穿透与关系图 (需求 R137; 程序 YAML 的注释写口径, 接口文档第 6 节写节点与关系的写法).

EvidenceAgent 按程序编号分派到这里: Penetrator 依次查登记 (每次一行 external_call, PDF 存档为 registry_extract),
build_graph 由取得的公司摘录建 OwnershipGraph, undeclared_leads 与画像申报清单比对出 R01 例外. 不调大模型.

穿透顺序: ABN Lookup 客户 ABN 取 ACN → 报告主体的公司摘录 → 母公司链: 逐层取持股不低于 significant_pct 的法人股东的
摘录 (至多 max_depth 层) → 对报告主体及母公司链上有出生日期的高管做人员检索, 取其任职或持股的其他公司的摘录
(person_hops 轮). 查不到摘录, 没有 ACN, 或超过 max_depth 的公司照样建节点 (searched 为假) 与关系, 不再往下穿透;
其穿透中断例外 (R02) 与按留痕重建, 失败路径由需求 R138 做. 持股低于 significant_pct 的法人股东不在链上: 只由摘录建
节点与关系, 不查询, 不出 R01.
"""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from backend.agents.base import AgentDeps, RunContext, finish_run
from backend.contracts.models import (
    OWNERSHIP_GRAPH_ID,
    AgentId,
    AsicExtract,
    AuditException,
    ClientProfile,
    DeclaredHolding,
    DeclaredParty,
    DocumentRef,
    DocumentType,
    EvidencePointer,
    EvidenceType,
    OwnershipGraph,
    PartyLink,
    PartyNode,
    Reliability,
    Shareholding,
    TrailEntry,
    TrailKind,
    company_node_id,
    node_name_part,
    organisation_node_id,
    person_node_id,
    split_full_name,
)
from backend.contracts.state import StatePatch
from backend.services.party_names import party_name_key

PROCEDURE_ID = "GEN-08"
OFFICEHOLDER_PAGE = 1  # 公司摘录第 1 页为公司与高管, 第 2 页为股本与股东 (设计稿第 2.5 节)
SHAREHOLDER_PAGE = 2
PERSON_PAGE = 1  # 人员检索结果只有 1 页


def pointer(document: DocumentRef, page: int, locator: str) -> EvidencePointer:
    """指向存档的登记 PDF 某一行的证据指针 (外部证据, 可靠性取文件登记)."""
    return EvidencePointer(
        type=EvidenceType.external,
        source_type="document",
        source_id=document.id,
        locator=locator,
        page=page,
        reliability=document.reliability,
    )


def person_ref(name: str, birth_date: date | None) -> tuple[str, str, str]:
    """摘录上的个人: (节点 ID, 姓, 名). 姓名按 split_full_name 拆, 出生日期为空时 ID 末段为 NODOB."""
    family, given = split_full_name(name)
    return person_node_id(family, given, birth_date), family, given


def name_key(name: str) -> tuple[str, str]:
    """同一摘录里股东行与高管行按规范化姓名合并的键: 姓与名各自经 node_name_part."""
    family, given = split_full_name(name)
    return node_name_part(family), node_name_part(given)


def known_people(extract: AsicExtract) -> dict[tuple[str, str], str]:
    """摘录高管栏中有出生日期的个人, 规范化姓名 → 节点 ID; 同一摘录的股东行据此并入高管的节点."""
    return {name_key(o.name): person_ref(o.name, o.birth_date)[0] for o in extract.officeholders if o.birth_date}


def shareholder_node_id(holder: Shareholding, people: dict[tuple[str, str], str]) -> str:
    """股东行的节点 ID: 个人按规范化姓名取 people 中的节点 (同一摘录有出生日期的高管, 或发现这家公司的检索对象),
    取不到为 NODOB; 公司为 ACN-{acn} (ACN 去空白), 没有 ACN 为 ORG-{名称}."""
    if holder.kind == "person":
        return people.get(name_key(holder.name)) or person_ref(holder.name, None)[0]
    return company_node_id(holder.acn) if holder.acn else organisation_node_id(holder.name)


def acn_of(node_id: str) -> str | None:
    """公司节点 ID 中规范化后的 ACN (ACN-{acn} 的数字部分); ORG- 节点为空. 查询, 幂等键与节点都用它."""
    return node_id.removeprefix("ACN-") if node_id.startswith("ACN-") else None


@dataclass(frozen=True)
class Found[T]:
    """一次登记查询的结果与存档的 PDF."""

    record: T
    document: DocumentRef


@dataclass(frozen=True)
class Lead:
    """穿透中发现的一家公司. via 为发现它的节点 (人员检索的个人, 或它持股的下一层公司), pointer 为发现它的那一行;
    报告主体两者为空."""

    node_id: str
    name: str
    acn: str | None
    via: str | None = None
    pointer: EvidencePointer | None = None


@dataclass
class Penetration:
    """GEN-08 的查询所得. leads 按发现顺序, 报告主体在首; extracts 只含取得了摘录的公司."""

    profile: ClientProfile  # 本项目采用的那版画像
    leads: list[Lead] = field(default_factory=list)
    extracts: dict[str, Found[AsicExtract]] = field(default_factory=dict)  # 公司节点 ID → 摘录
    searches: dict[str, bool] = field(default_factory=dict)  # 检索过的人员节点 ID → 是否取得了检索结果
    documents: list[DocumentRef] = field(default_factory=list)  # 存档的 PDF, 按查询顺序

    def known(self, node_id: str) -> bool:
        return any(lead.node_id == node_id for lead in self.leads)


class Penetrator:
    """执行一次 GEN-08 的登记查询. 每次查询写一行 external_call (幂等键 {eid}:GEN-08:{类别}:{编号}), payload 为查询
    参数, 结构化结果 (去掉 PDF 原件) 与存档文件 ID (ABN 查询没有文件); 查不到时结果与文件 ID 为空."""

    def __init__(self, deps: AgentDeps, ctx: RunContext) -> None:
        self.deps = deps
        self.ctx = ctx
        self.eid = ctx.engagement.id
        self.params = ctx.run.params_snapshot

    async def run(self) -> Penetration:
        """按模块说明的顺序查完, 返回查询所得. 画像取本项目采用的那一版 (状态中的 client_profile_version), 查不到抛
        LookupError; 客户 ABN 查不到或没有 ACN 时写完那行留痕后抛 LookupError."""
        from backend.agents.ingest import client_profile  # 在此导入: ingest 导入 evidence, evidence 导入本模块

        profile = await client_profile(self.deps, self.ctx.engagement, self.ctx.state["client_profile_version"])
        record = await self.deps.registry.abn(self.eid, profile.abn)
        result = record.model_dump(mode="json") if record is not None else None
        await self.record(f"ABN Lookup {profile.abn}", f"abn:{profile.abn}", {"abn": profile.abn, "result": result})
        if record is None or record.acn is None:
            raise LookupError(f"{PROCEDURE_ID}: 客户 ABN {profile.abn} 查不到, 或登记没有 ACN (不是公司), 无从穿透")
        found = Penetration(profile)
        entity = company_node_id(record.acn)
        chain = await self.reach(found, [Lead(entity, record.entity_name, acn_of(entity))])
        level = chain
        for _ in range(int(self.params["max_depth"])):
            level = await self.reach(found, self.parents(found, level))
            chain += level
        await self.reach(found, self.parents(found, level), fetch=False)  # 超过 max_depth 的只建节点
        companies = chain
        for _ in range(int(self.params["person_hops"])):
            leads: list[Lead] = []
            for node_id, family, given, birth_date in self.people(found, companies):
                leads += await self.search(found, node_id, family, given, birth_date)
            companies = await self.reach(found, leads)
        return found

    async def reach(self, found: Penetration, leads: list[Lead], fetch: bool = True) -> list[Lead]:
        """登记新发现的公司 (已登记的与列表中重复的跳过, 留首次出现的), 有 ACN 且 fetch 时取其摘录; 返回取得了摘录的."""
        fetched: list[Lead] = []
        for lead in leads:
            if found.known(lead.node_id):
                continue
            found.leads.append(lead)
            if not fetch or lead.acn is None:
                continue
            extract = await self.deps.registry.asic_extract(self.eid, lead.acn)
            document = await self.archive(found, extract.file_name, extract.file_bytes) if extract else None
            await self.record(
                f"ASIC company extract {lead.acn}",
                f"extract:{lead.acn}",
                {
                    "acn": lead.acn,
                    "result": extract.model_dump(mode="json", by_alias=True, exclude={"file_bytes"})
                    if extract
                    else None,
                    "document_id": document.id if document else None,
                },
            )
            if extract is not None and document is not None:
                found.extracts[lead.node_id] = Found(extract, document)
                fetched.append(lead)
        return fetched

    def parents(self, found: Penetration, companies: list[Lead]) -> list[Lead]:
        """这些公司在母公司链上的法人股东: 持股不低于 significant_pct 的, 按摘录顺序 (去重与已登记的由 reach 处理).
        持股更低的不在链上, 只由 build_graph 按摘录建节点与关系."""
        threshold = float(self.params["significant_pct"])
        leads: list[Lead] = []
        for company in companies:
            got = found.extracts[company.node_id]
            for i, holder in enumerate(got.record.shareholders):
                if holder.kind != "company" or holder.pct < threshold:
                    continue
                node_id = shareholder_node_id(holder, {})
                at = pointer(got.document, SHAREHOLDER_PAGE, f"shareholders[{i}]")
                leads.append(Lead(node_id, holder.name, acn_of(node_id), company.node_id, at))
        return leads

    def people(self, found: Penetration, companies: list[Lead]) -> list[tuple[str, str, str, date]]:
        """要检索的个人 (节点 ID, 姓, 名, 出生日期): 这些公司有出生日期的高管, 还没检索过; 按公司与高管栏顺序, 去重.
        持股个人的出生日期只能来自同一摘录的高管栏, 所以持股达到 significant_pct 而出生日期已知的个人都已是高管,
        不另取股东行 (YAML 注释同)."""
        wanted: dict[str, tuple[str, str, str, date]] = {}
        for company in companies:
            for officer in found.extracts[company.node_id].record.officeholders:
                if officer.birth_date is None:
                    continue
                node_id, family, given = person_ref(officer.name, officer.birth_date)
                if node_id not in found.searches and node_id not in wanted:
                    wanted[node_id] = (node_id, family, given, officer.birth_date)
        return list(wanted.values())

    async def search(self, found: Penetration, node_id: str, family: str, given: str, birth_date: date) -> list[Lead]:
        """人员检索一人, 返回其任职或持股的公司 (按检索结果的顺序; 去重与已登记的由 reach 处理). 留痕摘要只写本次
        执行的检索序号, 不写姓名与出生日期 (摘要会经审计助理的工具发给对话模型); 参数在 payload 与幂等键里."""
        person = await self.deps.registry.asic_person(self.eid, family, given, birth_date)
        document = await self.archive(found, person.file_name, person.file_bytes) if person else None
        await self.record(
            f"ASIC person search #{len(found.searches) + 1}",
            f"person:{node_id.removeprefix('PER-')}",
            {
                "family_name": family,
                "given_name": given,
                "birth_date": birth_date.isoformat(),
                "result": person.model_dump(mode="json", exclude={"file_bytes"}) if person else None,
                "document_id": document.id if document else None,
            },
        )
        found.searches[node_id] = person is not None and document is not None
        if person is None or document is None:
            return []
        leads: list[Lead] = []
        for i, role in enumerate(person.roles):
            company = company_node_id(role.acn)
            at = pointer(document, PERSON_PAGE, f"roles[{i}]")
            leads.append(Lead(company, role.company_name, acn_of(company), node_id, at))
        return leads

    async def archive(self, found: Penetration, file_name: str, data: bytes) -> DocumentRef:
        """登记 PDF 存档 (registry_extract, 外部原件, 结构化结果已取得故为 extracted)."""
        document = await self.deps.evidence_store.save(
            self.eid,
            file_name,
            data,
            type=DocumentType.registry_extract,
            reliability=Reliability.external_original,
            status="extracted",
        )
        found.documents.append(document)
        return document

    async def record(self, summary: str, key: str, payload: dict[str, Any]) -> None:
        await self.deps.trail.record(
            TrailEntry(
                engagement_id=self.eid,
                kind=TrailKind.external_call,
                actor=AgentId.A5,
                procedure_id=self.ctx.procedure.id,
                run_id=self.ctx.run.id,
                summary=summary,
                idempotency_key=f"{self.eid}:{PROCEDURE_ID}:{key}",
                payload=payload,
            )
        )


def build_graph(run_id: str, found: Penetration) -> OwnershipGraph:
    """由取得的公司摘录建关系图. 公司节点为发现的各公司 (取得了摘录的名称按摘录, searched 为真; 报告主体另带画像的
    ABN) 与这些摘录里其余的法人股东 (持股低于 significant_pct 的, 以及人员检索发现的公司的法人股东); 人员节点为摘录里的
    高管与个人股东, searched 为取得了人员检索结果. 股东行按规范化姓名并入同一摘录有出生日期的高管; 由某人的检索结果
    发现的公司, 其摘录里与该人同名的股东行并入该人的节点 (只持股不任职时也是一个节点). 关系只取公司摘录: 按发现顺序
    逐份, 高管 (第 1 页, officeholders[i]) 在前, 股东 (第 2 页, shareholders[i]) 在后. 节点按 ID 排序."""
    entity = found.leads[0]
    nodes: dict[str, PartyNode] = {}
    for lead in found.leads:
        got = found.extracts.get(lead.node_id)
        nodes[lead.node_id] = PartyNode(
            id=lead.node_id,
            kind="company",
            name=got.record.name if got else lead.name,
            acn=lead.acn,
            abn=found.profile.abn if lead is entity else None,
            searched=got is not None,
        )
    links: list[PartyLink] = []
    for lead in found.leads:
        got = found.extracts.get(lead.node_id)
        if got is None:
            continue
        extract, document = got.record, got.document
        people = known_people(extract)
        if lead.via in found.searches:  # 由这个人的检索结果发现的公司; 节点在他任职的那份摘录里已建
            people.setdefault(name_key(nodes[lead.via].name), lead.via)
        for i, officer in enumerate(extract.officeholders):
            node_id = person_ref(officer.name, officer.birth_date)[0]
            nodes.setdefault(
                node_id, PartyNode(id=node_id, kind="person", name=officer.name, birth_date=officer.birth_date)
            )
            at = pointer(document, OFFICEHOLDER_PAGE, f"officeholders[{i}]")
            links.append(
                PartyLink(
                    from_id=node_id, to_id=lead.node_id, relation=officer.role, appointed=officer.appointed, evidence=at
                )
            )
        for i, holder in enumerate(extract.shareholders):
            node_id = shareholder_node_id(holder, people)
            if node_id not in nodes:  # 并入高管或检索对象的已建好; 新建的个人没有出生日期
                nodes[node_id] = PartyNode(id=node_id, kind=holder.kind, name=holder.name, acn=acn_of(node_id))
            at = pointer(document, SHAREHOLDER_PAGE, f"shareholders[{i}]")
            links.append(
                PartyLink(
                    from_id=node_id,
                    to_id=lead.node_id,
                    relation="shareholder",
                    shares=holder.shares,
                    pct=holder.pct,
                    evidence=at,
                )
            )
    for node_id, result in found.searches.items():
        nodes[node_id] = nodes[node_id].model_copy(update={"searched": result})
    return OwnershipGraph(
        id=OWNERSHIP_GRAPH_ID,
        run_id=run_id,
        reporting_entity_id=entity.node_id,
        nodes=sorted(nodes.values(), key=lambda node: node.id),
        links=links,
    )


def declared(
    profile: ClientProfile, name: str, acn: str | None, suffixes: list[str]
) -> DeclaredParty | DeclaredHolding | None:
    """画像申报清单 (related_parties 与 ownership 中的公司, 按此顺序) 中与该公司匹配的第一项, 没有为 None (GEN-03
    取同一项, 需求 R139). 申报项有 acn 或 abn 时按 ACN 比 (acn, 或 abn 的后 9 位), 不再比名称; 两者都没有时按名称比,
    两边经 party_name_key (不去前缀, 去 suffixes) 后相等; 键为 None (规范化后为空: 只有非拉丁字母, 或只剩公司后缀)
    的不参与比对, 不算相等."""
    parties: list[tuple[DeclaredParty | DeclaredHolding, str, str | None, str | None]] = [
        (p, p.name, p.acn, p.abn) for p in profile.related_parties if p.kind == "company"
    ]
    parties += [(h, h.holder, None, None) for h in profile.ownership if h.kind == "company"]
    for party, party_name, party_acn, party_abn in parties:
        if party_acn or party_abn:
            if acn is not None and acn in {party_acn, (party_abn or "")[-9:]}:
                return party
        else:
            key = party_name_key(party_name, (), suffixes)
            if key is not None and key == party_name_key(name, (), suffixes):
                return party
    return None


def relation_text(link: PartyLink) -> str:
    """detail.via_relations 的一项: 任职写职务, 持股写 "shareholder {比例}" (如 shareholder 0.5)."""
    if link.relation == "shareholder" and link.pct is not None:
        return f"shareholder {link.pct:g}"
    return link.relation


def undeclared_leads(ctx: RunContext, found: Penetration, graph: OwnershipGraph) -> list[AuditException]:
    """R01 关联候选未申报 (high): 报告主体以外发现的公司, 不在画像申报清单中, 按发现顺序编号. detail 写名称, ACN,
    发现它的节点 (via) 及两者之间的关系; 证据为发现它的那一行与它自己摘录的第 1 页 (取得了摘录时)."""
    names = {node.id: node.name for node in graph.nodes}
    suffixes = list(ctx.run.params_snapshot["entity_suffixes"])
    exceptions: list[AuditException] = []
    for lead in found.leads[1:]:
        if declared(found.profile, names[lead.node_id], lead.acn, suffixes) is not None:
            continue
        evidence = [lead.pointer] if lead.pointer is not None else []
        got = found.extracts.get(lead.node_id)
        if got is not None:
            evidence.append(pointer(got.document, OFFICEHOLDER_PAGE, "acn"))
        between = {lead.via, lead.node_id}
        exceptions.append(
            AuditException(
                id=f"X-{PROCEDURE_ID}-{ctx.run.run_no}-R01-{len(exceptions) + 1}",
                procedure_id=ctx.procedure.id,
                run_id=ctx.run.id,
                rule_id="R01",
                severity="high",
                source_ids=[lead.node_id],
                detail={
                    "name": names[lead.node_id],
                    "acn": lead.acn,
                    "via": lead.via,
                    "via_relations": [relation_text(x) for x in graph.links if {x.from_id, x.to_id} == between],
                    "declared": False,
                    "profile_version": found.profile.version,
                },
                evidence=evidence,
            )
        )
    return exceptions


def ownership_patch(ctx: RunContext, found: Penetration) -> StatePatch:
    """GEN-08 的片段: 关系图, R01 例外, 存档的登记 PDF (按 ID 去重, 保持查询顺序) 与执行记录的成功终态."""
    graph = build_graph(ctx.run.id, found)
    documents = {document.id: document for document in found.documents}
    return {
        "ownership_graph": graph,
        "exceptions": undeclared_leads(ctx, found, graph),
        "documents": list(documents.values()),
        "procedure_runs": [finish_run(ctx.run, datetime.now(UTC))],
    }
