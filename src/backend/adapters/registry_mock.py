"""ABN Lookup 与 ASIC 查询的 mock (B 负责): 读数据集目录下的 registry/. MockRegistrySource 读固定目录;
DatasetRegistrySource 按项目的数据集定位目录 (见 dataset_directory)."""

import json
from collections.abc import Callable
from pathlib import Path

from backend.contracts.models import AbnRecord, AsicCompanyRecord
from backend.contracts.ports import EngagementStore


class MockRegistrySource:
    """登记查询的 mock: 读固定数据集目录下 registry/ 的 JSON; eid 不使用."""

    def __init__(self, dataset_dir: Path) -> None:
        self.dataset_dir = dataset_dir  # 数据集目录

    async def abn(self, eid: str, abn: str) -> AbnRecord | None:
        data = json.loads((self.dataset_dir / "registry" / "abn.json").read_text())
        row = data.get("".join(abn.split()))
        return AbnRecord.model_validate(row) if row is not None else None

    async def asic_company(self, eid: str, acn: str) -> AsicCompanyRecord | None:
        data = json.loads((self.dataset_dir / "registry" / "asic.json").read_text())
        row = data.get("".join(acn.split()))
        return AsicCompanyRecord.model_validate(row) if row is not None else None


async def dataset_directory(
    engagements: EngagementStore, directory: Callable[[str], Path | None], eid: str
) -> Path | None:
    """项目数据集的目录 (directory 即 RoutingDatasetLoader.supplement_dir); 项目不存在抛 LookupError."""
    engagement = await engagements.get(eid)
    if engagement is None:
        raise LookupError(f"没有项目 {eid}")
    return directory(engagement.dataset)


class DatasetRegistrySource:
    """按项目的数据集定位登记 mock: Numbat (demo) 读 data/demo/registry/, Xero 读补造目录的 registry/
    (补造与快照不匹配时抛 SupplementMismatch); 没有目录或没有 registry/ 时查不到 (None)."""

    def __init__(self, engagements: EngagementStore, directory: Callable[[str], Path | None]) -> None:
        self.engagements = engagements
        self.directory = directory

    async def source(self, eid: str) -> MockRegistrySource | None:
        found = await dataset_directory(self.engagements, self.directory, eid)
        return MockRegistrySource(found) if found is not None and (found / "registry").is_dir() else None

    async def abn(self, eid: str, abn: str) -> AbnRecord | None:
        source = await self.source(eid)
        return await source.abn(eid, abn) if source else None

    async def asic_company(self, eid: str, acn: str) -> AsicCompanyRecord | None:
        source = await self.source(eid)
        return await source.asic_company(eid, acn) if source else None
