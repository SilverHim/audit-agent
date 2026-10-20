"""A5 的 GEN-08 股权穿透与关系图 (需求 R137, R138; 程序 YAML 的注释写口径, 接口文档第 6 节写节点与关系的写法).

EvidenceAgent 按程序编号分派到这里: Penetrator 依次查登记 (每次一行 external_call, PDF 存档为 registry_extract;
按幂等键已有成功留痕的不再查询, 由留痕重建), build_graph 由取得的公司摘录建 OwnershipGraph, gen08_exceptions 出 R01
(与画像申报清单比对) 与 R02 (穿透中断) 例外. 不调大模型.

穿透顺序: ABN Lookup 客户 ABN 取 ACN → 报告主体的公司摘录 → 母公司链: 逐层取持股不低于 significant_pct 的法人股东的
摘录 (至多 max_depth 层) → 对报告主体及母公司链上有出生日期的高管做人员检索, 取其任职或持股的其他公司的摘录
(person_hops 轮). 查不到摘录, 没有 ACN, 或超过 max_depth 的公司照样建节点 (searched 为假) 与关系, 不再往下穿透,
出 R02. 持股低于 significant_pct 的法人股东不在链上: 只由摘录建节点与关系, 不查询, 不出 R01, R02.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from functools import partial
from typing import Any, Literal

from pydantic import BaseModel

from backend.agents.base import AgentDeps, RunContext, finish_run
from backend.contracts.errors import RegistryUnavailable
from backend.contracts.models import (
    OWNERSHIP_GRAPH_ID,
    AbnRecord,
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
    PersonSearch,
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

# 没有摘录的原因 (Lead.missing) → R02 的 detail.reason
Missing = Literal["not_found", "no_acn", "beyond_depth"]
MISSING_REASONS: dict[Missing, str] = {
    "not_found": "查不到摘录",
    "no_acn": "没有 ACN",
    "beyond_depth": "达到 max_depth",
}
NO_PERSON_SOURCE = "没有人员检索来源, 高管未检索"  # R02 的 detail.reason: 新建的客户没有人员检索来源


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
    报告主体两者为空. missing 为没有取得摘录的原因 (登记时由 reach 记下, R02 据此), 取得了为空."""

    node_id: str
    name: str
    acn: str | None
    via: str | None = None
    pointer: EvidencePointer | None = None
    missing: Missing | None = None


@dataclass
class Penetration:
    """GEN-08 的查询所得. leads 按发现顺序, 报告主体在首; extracts 只含取得了摘录的公司."""

    profile: ClientProfile  # 本项目采用的那版画像
    leads: list[Lead] = field(default_factory=list)
    extracts: dict[str, Found[AsicExtract]] = field(default_factory=dict)  # 公司节点 ID → 摘录
    searches: dict[str, bool] = field(default_factory=dict)  # 检索过的人员节点 ID → 是否取得了检索结果
    documents: list[DocumentRef] = field(default_factory=list)  # 存档或重建的 PDF, 按查询顺序
    unsearched: list[str] = field(default_factory=list)  # 有高管因没有人员检索来源而未检索的公司, 按发现顺序

    def known(self, node_id: str) -> bool:
        return any(lead.node_id == node_id for lead in self.leads)


class Penetrator:
    """执行一次 GEN-08 的登记查询 (query): 每次查询写一行 external_call (幂等键 {eid}:GEN-08:{类别}:{编号}, 跨执行
    共用), payload 为查询参数, 结构化结果 (去掉 PDF 原件) 与存档文件 ID (ABN 查询没有文件); 查不到时结果与文件 ID 为空,
    且记 success 为假. 已有成功留痕的查询不再调 RegistrySource, 由留痕重建 (rebuilt)."""

    def __init__(self, deps: AgentDeps, ctx: RunContext) -> None:
        self.deps = deps
        self.ctx = ctx
        self.eid = ctx.engagement.id
        self.params = ctx.run.params_snapshot
        self.registered: dict[str, DocumentRef] | None = None  # 已登记的 registry_extract 文件, 重建时才取
        self.person_rows = 0  # 本次执行写的人员检索留痕行数 (摘要的检索序号)

    async def run(self) -> Penetration:
        """按模块说明的顺序查完, 返回查询所得. 画像取本项目采用的那一版 (状态中的 client_profile_version), 查不到抛
        LookupError; 客户 ABN 查不到或没有 ACN 时写完那行留痕后抛 LookupError (执行失败, A1 建 failed 任务)."""
        from backend.agents.ingest import client_profile  # 在此导入: ingest 导入 evidence, evidence 导入本模块

        profile = await client_profile(self.deps, self.ctx.engagement, self.ctx.state["client_profile_version"])
        found = Penetration(profile)
        record, _ = await self.query(
            found,
            f"abn:{profile.abn}",
            f"ABN Lookup {profile.abn}",
            {"abn": profile.abn},
            partial(self.deps.registry.abn, self.eid, profile.abn),
            AbnRecord,
        )
        if record is None or record.acn is None:
            raise LookupError(f"{PROCEDURE_ID}: 客户 ABN {profile.abn} 查不到, 或登记没有 ACN (不是公司), 无从穿透")
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
            unavailable: set[str] = set()
            for node_id, family, given, birth_date in self.people(found, companies):
                found_leads = await self.search(found, node_id, family, given, birth_date)
                if found_leads is None:
                    unavailable.add(node_id)
                else:
                    leads += found_leads
            found.unsearched += [c.node_id for c in companies if self.officers(found, c) & unavailable]
            companies = await self.reach(found, leads)
        return found

    async def query[T: BaseModel](
        self,
        found: Penetration,
        key: str,
        summary: str,
        params: dict[str, Any],
        call: Callable[[], Awaitable[T | None]],
        model: type[T],
    ) -> tuple[T | None, DocumentRef | None]:
        """一次登记查询 (接口文档第 6 节外部动作三步): 幂等键已有成功留痕时由留痕重建 (rebuilt), 不调 RegistrySource,
        不存档; 否则 call, 查到的 (摘录与人员检索) 存档 PDF, 再写留痕. 返回结果与存档文件 (ABN 查询与查不到为空).
        上传的摘录 (新建的客户, 结果带 document_id) 不复用留痕: 读的是本所已有的文件, 审计师修改后须读到新的抽取结果.
        查不到的留痕记 success 为假, 不算成功, 之后的执行照常再查 (登记可能之后有了, 如审计师上传了摘录)."""
        full_key = f"{self.eid}:{PROCEDURE_ID}:{key}"
        done = await self.deps.trail.find_success(self.eid, full_key)
        if done is not None and not uploaded(done.payload):
            return await self.rebuilt(found, full_key, done.payload, model)
        record = await call()
        archived = model is not AbnRecord
        document = None
        if record is not None and archived:
            data = record.model_dump()
            document = await self.archive(found, str(data["file_name"]), bytes(data["file_bytes"]))
        result = record.model_dump(mode="json", by_alias=True, exclude={"file_bytes"}) if record is not None else None
        payload: dict[str, Any] = {**params, "result": result}
        if archived:
            payload["document_id"] = document.id if document else None
        if record is None:
            payload["success"] = False  # 查不到: 记下查过, 但不作为成功复用 (AuditTrail.find_success 不认)
        await self.record(summary, key, payload)
        return record, document

    async def rebuilt[T: BaseModel](
        self, found: Penetration, key: str, payload: dict[str, Any], model: type[T]
    ) -> tuple[T, DocumentRef | None]:
        """由成功留痕重建一次查询: 结构化结果取 payload 的 result, 文件取 FinancialDataStore.documents 中已登记的
        registry_extract (document_id). payload 缺 result, 摘录与人员检索缺 document_id, 或文件未登记时抛 LookupError
        (消息含幂等键). 重建的结果不带 PDF 原件 (file_bytes 为空): 原件已在登记的文件里, 不再存档."""
        result = payload.get("result")
        if not isinstance(result, dict):
            raise LookupError(f"{key}: 留痕 payload 缺结构化结果 (result), 无从重建")
        if model is AbnRecord:
            return model.model_validate(result), None
        document_id = payload.get("document_id")
        if not isinstance(document_id, str):
            raise LookupError(f"{key}: 留痕 payload 缺 document_id, 无从重建")
        if self.registered is None:
            documents = await self.deps.financial_store.documents(self.eid, DocumentType.registry_extract)
            self.registered = {d.id: d for d in documents}
        document = self.registered.get(document_id)
        if document is None:
            raise LookupError(f"{key}: 留痕记的文件 {document_id} 未登记, 无从重建")
        found.documents.append(document)
        return model.model_validate({**result, "file_bytes": b""}), document

    async def reach(self, found: Penetration, leads: list[Lead], fetch: bool = True) -> list[Lead]:
        """登记新发现的公司 (已登记的与列表中重复的跳过, 留首次出现的), 有 ACN 且 fetch 时取其摘录; 返回取得了摘录的.
        没有取得摘录的在 Lead.missing 记下原因: 没有 ACN, 超过 max_depth (fetch 为假), 查不到."""
        fetched: list[Lead] = []
        for lead in leads:
            if found.known(lead.node_id):
                continue
            if lead.acn is None or not fetch:
                found.leads.append(replace(lead, missing="no_acn" if lead.acn is None else "beyond_depth"))
                continue
            acn = lead.acn
            extract, document = await self.query(
                found,
                f"extract:{acn}",
                f"ASIC company extract {acn}",
                {"acn": acn},
                partial(self.deps.registry.asic_extract, self.eid, acn),
                AsicExtract,
            )
            if extract is None or document is None:
                found.leads.append(replace(lead, missing="not_found"))
                continue
            found.leads.append(lead)
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

    @staticmethod
    def officers(found: Penetration, company: Lead) -> set[str]:
        """一家公司摘录中有出生日期的高管的节点 ID."""
        extract = found.extracts[company.node_id].record
        return {person_ref(o.name, o.birth_date)[0] for o in extract.officeholders if o.birth_date is not None}

    async def search(
        self, found: Penetration, node_id: str, family: str, given: str, birth_date: date
    ) -> list[Lead] | None:
        """人员检索一人, 返回其任职或持股的公司 (按检索结果的顺序; 去重与已登记的由 reach 处理). 没有人员检索来源
        (RegistryUnavailable no_source, 新建的客户) 时不写留痕, 返回 None, 由 run 记进 unsearched 出 R02; 其他查不了
        的情形 (不可达, 出错) 照常抛出, 整次执行失败, rerun 时已查过的不再查. 留痕摘要只写本次执行的检索序号, 不写
        姓名与出生日期 (摘要会经审计助理的工具发给对话模型); 参数在 payload 与幂等键里."""
        params = {"family_name": family, "given_name": given, "birth_date": birth_date.isoformat()}
        try:
            person, document = await self.query(
                found,
                f"person:{node_id.removeprefix('PER-')}",
                f"ASIC person search #{self.person_rows + 1}",
                params,
                partial(self.deps.registry.asic_person, self.eid, family, given, birth_date),
                PersonSearch,
            )
        except RegistryUnavailable as exc:
            if exc.reason != "no_source":
                raise
            found.searches[node_id] = False
            return None
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
        if key.startswith("person:"):
            self.person_rows += 1
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


def uploaded(payload: dict[str, Any]) -> bool:
    """留痕的结果来自审计师上传的摘录 (新建的客户, 结构化结果带 document_id): 这种查询不按留痕复用."""
    result = payload.get("result")
    return isinstance(result, dict) and result.get("document_id") is not None


def build_graph(run_id: str, found: Penetration, version: int = 1) -> OwnershipGraph:
    """由取得的公司摘录建关系图. 公司节点为发现的各公司 (取得了摘录的名称按摘录, searched 为真; 报告主体另带画像的
    ABN) 与这些摘录里其余的法人股东 (持股低于 significant_pct 的, 以及人员检索发现的公司的法人股东); 人员节点为摘录里的
    高管与个人股东, searched 为取得了人员检索结果. 股东行按规范化姓名并入同一摘录有出生日期的高管; 由某人的检索结果
    发现的公司, 其摘录里与该人同名的股东行并入该人的节点 (只持股不任职时也是一个节点). 关系只取公司摘录: 按发现顺序
    逐份, 高管 (第 1 页, officeholders[i]) 在前, 股东 (第 2 页, shareholders[i]) 在后. 节点按 ID 排序. version 为图的
    版本 (重跑时为状态中的版本 +1)."""
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
        version=version,
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


def gen08_exceptions(ctx: RunContext, found: Penetration, graph: OwnershipGraph) -> list[AuditException]:
    """本次执行的例外: R01 (undeclared_leads) 在前, R02 (interrupted_leads) 在后, 各按发现顺序编号. 状态中本程序已有的
    同 (rule_id, source_ids) 例外不再出 (重跑时不重复), 编号只数本次出的."""
    existing = {(x.rule_id, tuple(x.source_ids)) for x in ctx.state["exceptions"] if x.procedure_id == PROCEDURE_ID}
    exceptions: list[AuditException] = []
    for rule_id, severity, leads in (
        ("R01", "high", undeclared_leads(ctx, found, graph)),
        ("R02", "medium", interrupted_leads(found, graph)),
    ):
        fresh = [
            (source, detail, evidence) for source, detail, evidence in leads if (rule_id, (source,)) not in existing
        ]
        exceptions += [
            AuditException(
                id=f"X-{PROCEDURE_ID}-{ctx.run.run_no}-{rule_id}-{n}",
                procedure_id=ctx.procedure.id,
                run_id=ctx.run.id,
                rule_id=rule_id,
                severity=severity,
                source_ids=[source],
                detail=detail,
                evidence=evidence,
            )
            for n, (source, detail, evidence) in enumerate(fresh, start=1)
        ]
    return exceptions


Finding = tuple[str, dict[str, Any], list[EvidencePointer]]  # 一条例外的 (节点 ID, detail, 证据)


def undeclared_leads(ctx: RunContext, found: Penetration, graph: OwnershipGraph) -> list[Finding]:
    """R01 关联候选未申报 (high): 报告主体以外发现的公司, 不在画像申报清单中, 按发现顺序. detail 写名称, ACN, 发现它的
    节点 (via) 及两者之间的关系; 证据为发现它的那一行与它自己摘录的第 1 页 (取得了摘录时)."""
    names = {node.id: node.name for node in graph.nodes}
    suffixes = list(ctx.run.params_snapshot["entity_suffixes"])
    findings: list[Finding] = []
    for lead in found.leads[1:]:
        if declared(found.profile, names[lead.node_id], lead.acn, suffixes) is not None:
            continue
        evidence = [lead.pointer] if lead.pointer is not None else []
        got = found.extracts.get(lead.node_id)
        if got is not None:
            evidence.append(pointer(got.document, OFFICEHOLDER_PAGE, "acn"))
        between = {lead.via, lead.node_id}
        detail = {
            "name": names[lead.node_id],
            "acn": lead.acn,
            "via": lead.via,
            "via_relations": [relation_text(x) for x in graph.links if {x.from_id, x.to_id} == between],
            "declared": False,
            "profile_version": found.profile.version,
        }
        findings.append((lead.node_id, detail, evidence))
    return findings


def interrupted_leads(found: Penetration, graph: OwnershipGraph) -> list[Finding]:
    """R02 穿透中断 (medium): 发现的公司 (含报告主体) 没有取得摘录 (Lead.missing: 查不到, 没有 ACN, 达到 max_depth),
    或有高管因没有人员检索来源而未检索 (unsearched), 按发现顺序. detail 写名称, ACN, via 与原因 (reason); 证据为
    发现它的那一行 (报告主体没有), 未检索的另加该公司摘录的高管页."""
    names = {node.id: node.name for node in graph.nodes}
    findings: list[Finding] = []
    for lead in found.leads:
        if lead.missing is not None:
            reason = MISSING_REASONS[lead.missing]
        elif lead.node_id in found.unsearched:
            reason = NO_PERSON_SOURCE
        else:
            continue
        evidence = [lead.pointer] if lead.pointer is not None else []
        got = found.extracts.get(lead.node_id)
        if got is not None:
            evidence.append(pointer(got.document, OFFICEHOLDER_PAGE, "officeholders"))
        detail = {"name": names[lead.node_id], "acn": lead.acn, "via": lead.via, "reason": reason}
        findings.append((lead.node_id, detail, evidence))
    return findings


def ownership_patch(ctx: RunContext, found: Penetration) -> StatePatch:
    """GEN-08 的片段: 关系图 (版本取状态中的 +1, 没有为 1), R01 与 R02 例外, 存档或重建的登记 PDF (按 ID 去重, 保持
    查询顺序) 与执行记录的成功终态."""
    previous = ctx.state["ownership_graph"]
    graph = build_graph(ctx.run.id, found, previous.version + 1 if previous is not None else 1)
    documents = {document.id: document for document in found.documents}
    return {
        "ownership_graph": graph,
        "exceptions": gen08_exceptions(ctx, found, graph),
        "documents": list(documents.values()),
        "procedure_runs": [finish_run(ctx.run, datetime.now(UTC))],
    }
