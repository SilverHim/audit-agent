"""ABN Lookup 与 ASIC 查询的 mock (B 负责): 读数据集目录下的 registry/."""

import json
from pathlib import Path

from backend.contracts.models import AbnRecord, AsicCompanyRecord


class MockRegistrySource:
    """登记查询的 mock: 读数据集目录下 registry/ 的 JSON."""

    def __init__(self, dataset_dir: Path) -> None:
        self.dataset_dir = dataset_dir  # 数据集目录

    async def abn(self, abn: str) -> AbnRecord | None:
        data = json.loads((self.dataset_dir / "registry" / "abn.json").read_text())
        row = data.get("".join(abn.split()))
        return AbnRecord.model_validate(row) if row is not None else None

    async def asic_company(self, acn: str) -> AsicCompanyRecord | None:
        data = json.loads((self.dataset_dir / "registry" / "asic.json").read_text())
        row = data.get("".join(acn.split()))
        return AsicCompanyRecord.model_validate(row) if row is not None else None
