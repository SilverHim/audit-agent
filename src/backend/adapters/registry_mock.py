"""ABN Lookup 与 ASIC 查询的 mock (B 负责): 读数据集目录下的 registry/. MockRegistrySource 读固定目录;
DatasetRegistrySource 按项目的数据集定位目录 (见 dataset_directory).

registry/ 下: abn.json, asic.json (ABN 与 ASIC 公司登记, 按编号的字典); extracts/{acn}.json 与同名 PDF (公司摘录);
persons/{姓}-{名}-{yyyymmdd}.json 与同名 PDF (人员检索, 姓与名转大写). 格式见 Xero演示数据集设计第 9.5 节.
"""

import json
import re
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from backend.contracts.errors import RegistryUnavailable
from backend.contracts.models import AbnRecord, AsicCompanyRecord, AsicExtract, PersonSearch
from backend.contracts.ports import EngagementStore

M = TypeVar("M", bound=BaseModel)
NAME_PART = re.compile(r"[A-Z][A-Z' -]*")  # 拼文件名的姓或名 (转大写后): 字母, 空格, 撇号与连字符, 不会串出目录


def person_stem(family_name: str, given_name: str, birth_date: date) -> str | None:
    """人员检索的文件名主干 {姓}-{名}-{yyyymmdd} (姓与名去首尾空白转大写); 姓名含别的字符时为 None (查不到)."""
    parts = [family_name.strip().upper(), given_name.strip().upper()]
    if not all(NAME_PART.fullmatch(p) for p in parts):
        return None
    return f"{parts[0]}-{parts[1]}-{birth_date:%Y%m%d}"


class MockRegistrySource:
    """登记查询的 mock: 读固定数据集目录下 registry/ 的 JSON; eid 不使用."""

    def __init__(self, dataset_dir: Path) -> None:
        self.dataset_dir = dataset_dir  # 数据集目录

    async def abn(self, eid: str, abn: str) -> AbnRecord | None:
        data = json.loads((self.dataset_dir / "registry" / "abn.json").read_text())
        row = data.get("".join(abn.split()))
        return AbnRecord.model_validate(row) if row is not None else None

    async def asic_company(self, eid: str, acn: str | None) -> AsicCompanyRecord | None:
        if acn is None:  # 没有 ACN 不查 (RegistrySource.asic_company)
            return None
        data = json.loads((self.dataset_dir / "registry" / "asic.json").read_text())
        row = data.get("".join(acn.split()))
        return AsicCompanyRecord.model_validate(row) if row is not None else None

    async def asic_extract(self, eid: str, acn: str) -> AsicExtract | None:
        return self.extract(acn)

    async def asic_person(self, eid: str, family_name: str, given_name: str, birth_date: date) -> PersonSearch | None:
        return self.person(family_name, given_name, birth_date)

    def extract(self, acn: str) -> AsicExtract | None:
        """registry/extracts/{acn}.json 与同名 PDF; ACN 去空白后不是纯数字或没有文件时为 None."""
        key = "".join(acn.split())
        return self.read(AsicExtract, "extracts", key) if key.isdigit() else None

    def person(self, family_name: str, given_name: str, birth_date: date) -> PersonSearch | None:
        """registry/persons/{姓}-{名}-{yyyymmdd}.json 与同名 PDF (姓名不分大小写); 没有文件 (姓名或出生日期不符) 时为
        None."""
        stem = person_stem(family_name, given_name, birth_date)
        return self.read(PersonSearch, "persons", stem) if stem is not None else None

    def read(self, model: type[M], folder: str, stem: str) -> M | None:
        """registry/{folder}/{stem}.json 加同名 PDF 原件 (file_bytes); 没有 JSON 时为 None, 有 JSON 而没有 PDF 抛
        FileNotFoundError (数据不全)."""
        path = self.dataset_dir / "registry" / folder / f"{stem}.json"
        if not path.is_file():
            return None
        pdf = path.with_suffix(".pdf")
        if not pdf.is_file():
            raise FileNotFoundError(f"登记 mock 缺 {folder}/{pdf.name}")
        data = json.loads(path.read_text(encoding="utf-8"))
        return model.model_validate({**data, "file_bytes": pdf.read_bytes()})


async def dataset_directory(
    engagements: EngagementStore, directory: Callable[[str], Path | None], eid: str
) -> Path | None:
    """项目数据集的目录 (directory 即 RoutingDatasetLoader.supplement_dir); 项目不存在抛 LookupError, 项目没有数据集
    (新建的客户) 抛 RegistryUnavailable (不读任何数据集的 mock)."""
    engagement = await engagements.get(eid)
    if engagement is None:
        raise LookupError(f"没有项目 {eid}")
    if engagement.dataset is None:
        raise RegistryUnavailable(f"项目 {eid} 的客户没有数据集, 没有可查的登记来源")
    return directory(engagement.dataset)


class DatasetRegistrySource:
    """按项目的数据集定位登记 mock: Numbat (demo) 读 data/demo/registry/, Xero 读补造目录的 registry/
    (补造与快照不匹配时抛 SupplementMismatch); 没有目录或没有 registry/ 时查不到 (None); 项目没有数据集 (新建的
    客户) 时抛 RegistryUnavailable."""

    def __init__(self, engagements: EngagementStore, directory: Callable[[str], Path | None]) -> None:
        self.engagements = engagements
        self.directory = directory

    async def source(self, eid: str) -> MockRegistrySource | None:
        found = await dataset_directory(self.engagements, self.directory, eid)
        return MockRegistrySource(found) if found is not None and (found / "registry").is_dir() else None

    async def abn(self, eid: str, abn: str) -> AbnRecord | None:
        source = await self.source(eid)
        return await source.abn(eid, abn) if source else None

    async def asic_company(self, eid: str, acn: str | None) -> AsicCompanyRecord | None:
        source = await self.source(eid)  # 没有数据集时抛 RegistryUnavailable, acn 为空也先查这一步
        return await source.asic_company(eid, acn) if source else None

    async def asic_extract(self, eid: str, acn: str) -> AsicExtract | None:
        source = await self.source(eid)
        return source.extract(acn) if source else None

    async def asic_person(self, eid: str, family_name: str, given_name: str, birth_date: date) -> PersonSearch | None:
        source = await self.source(eid)
        return source.person(family_name, given_name, birth_date) if source else None
