"""登记查询的组合实现 (需求 R289, B 负责): 有数据集的项目读数据集的登记 mock, 新建的客户 (没有数据集) 的 ABN
经 ABR ABN Lookup 实时查; 新客户的 ASIC (公司登记, 摘录与人员检索) 在摘录接入 (R290) 前没有来源."""

from datetime import date

from backend.adapters.registry_mock import DatasetRegistrySource
from backend.contracts.errors import RegistryUnavailable
from backend.contracts.models import AbnRecord, AsicCompanyRecord, AsicExtract, PersonSearch
from backend.contracts.ports import AbnLookup, EngagementStore


class CompositeRegistrySource:
    """RegistrySource 的组装根实现: 按项目有没有数据集分流. 有数据集 (Numbat, Xero) 照旧走 DatasetRegistrySource,
    不发 ABN Lookup 请求; 没有数据集时 abn 走 AbnLookup (查不了抛 RegistryUnavailable, reason 为类别), ASIC 的三个
    方法 (asic_company, asic_extract, asic_person) 抛 RegistryUnavailable (no_source). 项目不存在抛 LookupError."""

    def __init__(self, engagements: EngagementStore, datasets: DatasetRegistrySource, abn_lookup: AbnLookup) -> None:
        self.engagements = engagements
        self.datasets = datasets
        self.abn_lookup = abn_lookup

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
        raise self.no_asic(eid)

    async def asic_extract(self, eid: str, acn: str) -> AsicExtract | None:
        if await self.has_dataset(eid):
            return await self.datasets.asic_extract(eid, acn)
        raise self.no_asic(eid)

    async def asic_person(self, eid: str, family_name: str, given_name: str, birth_date: date) -> PersonSearch | None:
        if await self.has_dataset(eid):
            return await self.datasets.asic_person(eid, family_name, given_name, birth_date)
        raise self.no_asic(eid)

    @staticmethod
    def no_asic(eid: str) -> RegistryUnavailable:
        """新建的客户没有 ASIC 来源 (摘录由审计师上传, R290)."""
        return RegistryUnavailable(f"项目 {eid} 的客户没有数据集, ASIC 摘录尚未接入", reason="no_source")
