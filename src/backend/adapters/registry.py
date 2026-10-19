"""登记查询的组合实现 (需求 R289, R290, B 负责): 有数据集的项目读数据集的登记 mock; 新建的客户 (没有数据集) 的 ABN
经 ABR ABN Lookup 实时查, ASIC 公司信息取审计师上传的 ASIC 摘录的最新抽取结果 (UploadedExtracts); 新客户没有人员
检索的来源."""

import asyncio
from datetime import date

from backend.adapters.registry_mock import DatasetRegistrySource
from backend.contracts.data import REGISTRY_TARGET, ExtractionRecord
from backend.contracts.errors import RegistryUnavailable
from backend.contracts.llm_outputs import RegistryExtraction
from backend.contracts.models import AbnRecord, AsicCompanyRecord, AsicExtract, DocumentRef, DocumentType, PersonSearch
from backend.contracts.ports import AbnLookup, DataStore, EngagementStore, EvidenceStore, FinancialDataStore


async def latest_extractions(
    financial: FinancialDataStore, data: DataStore, eid: str
) -> list[tuple[DocumentRef, ExtractionRecord]]:
    """各份 ASIC 摘录的当前抽取结果 (需求 R290): 现在登记为 registry_extract 的文件中有抽取版本的, 各取最新一版
    (审计师修改出的新版本即取它), 按文件 ID. ACC-04 (UploadedExtracts) 与接口 (DataService.registry_extracts) 共用."""
    documents = {d.id: d for d in await financial.documents(eid, DocumentType.registry_extract)}
    latest: dict[str, ExtractionRecord] = {}
    for record in await data.extraction_versions(eid, REGISTRY_TARGET):
        if record.target_id in documents:
            latest[record.target_id] = record  # 按版本升序读出, 后面的覆盖前面的
    return [(documents[k], latest[k]) for k in sorted(latest)]


class UploadedExtracts:
    """新建的客户的 ASIC 摘录 (需求 R290): 现在登记为 registry_extract 且有抽取结果的上传文件, 各取抽取版本表里的
    最新一版 (A3 抽取通过即可用, 审计师修改出新版本, 不另经确认). 原件取证据存储; 证据指针的可靠性取文件现在的登记
    (上传缺省 external_copy, 补登记可改)."""

    def __init__(self, financial: FinancialDataStore, data: DataStore, evidence: EvidenceStore) -> None:
        self.financial = financial
        self.data = data
        self.evidence = evidence

    async def latest(self, eid: str) -> list[tuple[DocumentRef, RegistryExtraction]]:
        """各份可用的摘录 (登记, 最新一版抽取结果, 见 latest_extractions), 按上传时间与文件 ID; 没有为空. 缺
        REGISTRY_POINTED 中某字段指针的 (正常不会有: 抽取的引用级校验不放过, 修改不动指针) 按抽取没通过处理, 不可用,
        ACC-04 不会因取不到指针而出错."""
        found = [
            (document, extraction)
            for document, record in await latest_extractions(self.financial, self.data, eid)
            if not (extraction := RegistryExtraction.model_validate(record.data)).unpointed()
        ]
        return sorted(found, key=lambda item: (item[0].uploaded_at, item[0].id))

    async def extract(self, eid: str, acn: str) -> AsicExtract | None:
        """ACN 为 acn 的摘录 (有几份时取最后上传的), 带原件; 有可用的摘录而 ACN 都对不上为 None; 一份可用的都没有
        抛 RegistryUnavailable (no_extract)."""
        found = await self.latest(eid)
        if not found:
            raise RegistryUnavailable(f"项目 {eid} 没有可用的 ASIC 摘录", reason="no_extract")
        matched = [item for item in found if item[1].acn == acn]
        if not matched:
            return None
        document, extraction = matched[-1]
        data = await asyncio.to_thread((await self.evidence.path(eid, document.id)).read_bytes)
        return AsicExtract(
            acn=extraction.acn,
            name=extraction.name,
            type=extraction.type,
            company_class=extraction.company_class,
            subclass=extraction.subclass,
            status=extraction.status,
            registered_date=extraction.registered_date,
            officeholders=extraction.officeholders,
            shareholders=extraction.shareholders,
            paid_up_cents=extraction.paid_up_cents,
            file_name=document.file_name,
            file_bytes=data,
            abn=extraction.abn,
            registered_address=extraction.registered_address,
            document_id=document.id,
            evidence=[p.model_copy(update={"reliability": document.reliability}) for p in extraction.evidence],
        )


class CompositeRegistrySource:
    """RegistrySource 的组装根实现: 按项目有没有数据集分流. 有数据集 (Numbat, Xero) 照旧走 DatasetRegistrySource,
    不发 ABN Lookup 请求. 没有数据集时: abn 走 AbnLookup (查不了抛 RegistryUnavailable, reason 为类别); asic_extract
    取上传的摘录 (UploadedExtracts; 一份可用的摘录都没有抛 no_extract, ACN 都对不上为 None); asic_company 与
    asic_person 抛 RegistryUnavailable (no_source): 新客户的 ACC-04 只调 asic_extract, 不经这两个方法. 项目不存在抛
    LookupError."""

    def __init__(
        self,
        engagements: EngagementStore,
        datasets: DatasetRegistrySource,
        abn_lookup: AbnLookup,
        extracts: UploadedExtracts,
    ) -> None:
        self.engagements = engagements
        self.datasets = datasets
        self.abn_lookup = abn_lookup
        self.extracts = extracts

    async def has_dataset(self, eid: str) -> bool:
        engagement = await self.engagements.get(eid)
        if engagement is None:
            raise LookupError(f"没有项目 {eid}")
        return engagement.dataset is not None

    async def abn(self, eid: str, abn: str) -> AbnRecord | None:
        if await self.has_dataset(eid):
            return await self.datasets.abn(eid, abn)
        return await self.abn_lookup.abn(abn)

    async def asic_company(self, eid: str, acn: str | None) -> AsicCompanyRecord | None:
        if await self.has_dataset(eid):
            return await self.datasets.asic_company(eid, acn)
        raise RegistryUnavailable(f"项目 {eid} 的客户没有数据集, 公司登记看上传的 ASIC 摘录", reason="no_source")

    async def asic_extract(self, eid: str, acn: str) -> AsicExtract | None:
        if await self.has_dataset(eid):
            return await self.datasets.asic_extract(eid, acn)
        return await self.extracts.extract(eid, acn)

    async def asic_person(self, eid: str, family_name: str, given_name: str, birth_date: date) -> PersonSearch | None:
        if await self.has_dataset(eid):
            return await self.datasets.asic_person(eid, family_name, given_name, birth_date)
        raise RegistryUnavailable(f"项目 {eid} 的客户没有数据集, 没有人员检索的来源", reason="no_source")
