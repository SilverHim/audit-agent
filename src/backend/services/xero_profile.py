"""Xero 组织信息与客户画像的核对 (需求 R291) 的纯函数: 从组织信息取三项, 规范化名称, 比较, 核对键.

组织信息的字段以 Xero 官方 OpenAPI 规格 (XeroAPI/Xero-OpenAPI 的 xero_accounting.yaml, Organisation) 与
data/external/xero/api/ 快照的 organisation.json 为准: Name 为 Xero 里显示的名称, LegalName 为报表上的名称,
RegistrationNumber 在澳洲组织为 ABN (同 adapters.xero_dataset.client_profile). TaxNumber 在澳洲是税号 (TFN),
不读, 不进留痕.
"""

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from backend.adapters.xero_dataset import abn as abn_digits
from backend.contracts.data import XeroProfileCheck, XeroProfileField
from backend.contracts.models import ClientProfile

# 名称规范化时视为相同的写法 (按词替换; 公司后缀本身不去掉, 缺后缀算不一致)
SAME_WORDS = {"proprietary": "pty", "limited": "ltd", "&": "and"}


def organisation_info(org: Mapping[str, Any]) -> dict[str, str | None]:
    """同步取到的组织信息 (Organisations 的第一项) 中核对要用的三项: name, legal_name (空时同 name), abn
    (RegistrationNumber 去非数字后 11 位, 否则为空). 只取这三项, 其余字段 (含 TaxNumber) 不带出."""
    name = str(org.get("Name") or "")
    return {
        "name": name,
        "legal_name": str(org.get("LegalName") or "") or name,
        "abn": abn_digits(org.get("RegistrationNumber")),
    }


def normalise_name(name: str) -> str:
    """名称的比较形式: 不分大小写, & 记作 and, 去标点, 空白压成一个; Pty 与 Proprietary, Ltd 与 Limited 视为相同.
    例: "DEMO Company (AU) Pty. Ltd." 与 "Demo Company (AU) Proprietary Limited" 都是 "demo company au pty ltd"."""
    text = name.casefold().replace("&", " & ")
    words = re.sub(r"[^\w&]+", " ", text).split()
    return " ".join(SAME_WORDS.get(word, word) for word in words)


def profile_differences(legal_name: str, abn: str | None, profile: ClientProfile) -> list[XeroProfileField]:
    """Xero 的法定名称与 ABN 比画像, 返回对不上的项 (按 legal_name, abn 的顺序). ABN 两边都有才比 (只比数字);
    Xero 没给 ABN 时只比名称."""
    found: list[XeroProfileField] = []
    if normalise_name(legal_name) != normalise_name(profile.legal_name):
        found.append("legal_name")
    if abn is not None and abn != re.sub(r"\D", "", profile.abn):
        found.append("abn")
    return found


def pending(check: XeroProfileCheck | None) -> XeroProfileCheck | None:
    """待审计师处理的核对: 有对不上的项且还没作出选择; 否则为空."""
    return check if check is not None and check.differences and check.resolution is None else None


def check_key(tenant_id: str | None, legal_name: str, abn: str | None, profile_version: int) -> str:
    """核对键: 组织 ID, Xero 的法定名称与 ABN, 画像版本的 sha256 前 12 位. 任一项变了即是新的一次核对."""
    material = json.dumps([tenant_id, legal_name, abn, profile_version], ensure_ascii=False, separators=(",", ":"))
    return "XP-" + hashlib.sha256(material.encode()).hexdigest()[:12]
