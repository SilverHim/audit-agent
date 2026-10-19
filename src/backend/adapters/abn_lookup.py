"""ABR ABN Lookup web services 的实时查询 (需求 R289, B 负责).

按官方文档 (abr.business.gov.au 的 Tools/WebServices 与 Documentation/WebServiceMethods, WebServiceResponse,
Exceptions, 及 AbnLookupSchema.zip 的 XSD; 页面版本 9.9.7, 2026-10-19 核对) 调最新的 SearchByABNv202001, 用 HTTP
POST 表单 (searchString, includeHistoricalDetails=N, authenticationGuid): GUID 在请求体里, 不进 URL 与访问日志.
不用 JSON 接口: 它把 GUID 放在 URL 查询串里.

ABR 的响应会原样回显请求, 含 <authenticationGUID>. 所以原始响应只在 parse_abn_response 里解析, 不返回, 不记日志,
不放进异常; 查不了时只抛带类别的 RegistryUnavailable (见 contracts.errors.RegistryReason).
"""

import logging
import time
from collections.abc import Callable
from xml.etree import ElementTree

import httpx

from backend.adapters.llm import install_redaction
from backend.contracts.errors import RegistryUnavailable
from backend.contracts.models import AbnRecord
from backend.contracts.ports import AbnLookup
from backend.services.abn import normalize_abn

logger = logging.getLogger(__name__)

NS = {"a": "http://abr.business.gov.au/ABRXMLSearch/"}  # 响应的命名空间 (WSDL targetNamespace)
TIMEOUT = 10.0  # 秒
STATUS = {"active": "Active", "cancelled": "Cancelled"}  # entityStatusCode (词汇表: active 或 cancelled)
# WEBSERVICES 类异常中表示 GUID 不能用的说明 (Documentation/Exceptions 的原文片段): 未登记, 未注册该服务, 注册已取消
REJECTED = (
    "not recognised as a Registered Party",
    "has not been registered for the requested service",
    "has been cancelled",
)
INVALID_ABN = "not a valid ABN"  # "Search text is not a valid ABN or ACN": ABR 判为无效 ABN, 当查不到


class AbnLookupClient:
    """AbnLookup 的实现: 没配 GUID 时不发请求, 抛 RegistryUnavailable (not_configured). transport 只供测试替换
    (httpx.MockTransport). 建实例时把 GUID 登记到 HTTP 日志的脱敏过滤 (adapters/llm.install_redaction), 多一道保险."""

    def __init__(self, guid: str, url: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._guid = guid
        self.url = url
        self.transport = transport
        install_redaction(guid)

    def __repr__(self) -> str:
        return f"AbnLookupClient(url={self.url!r}, configured={bool(self._guid)})"

    async def abn(self, abn: str) -> AbnRecord | None:
        if not self._guid:
            raise RegistryUnavailable("ABN Lookup 未配置 GUID", reason="not_configured")
        form = {"searchString": normalize_abn(abn), "includeHistoricalDetails": "N", "authenticationGuid": self._guid}
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=TIMEOUT) as client:
                response = await client.post(self.url, data=form)
        except httpx.TransportError as exc:  # 连接失败, 超时等; 异常只带类别, 不接原异常 (请求体含 GUID)
            logger.warning("ABN Lookup 不可达: %s", type(exc).__name__)
            raise RegistryUnavailable("ABN Lookup 不可达", reason="unreachable") from None
        if response.status_code != 200:
            logger.warning("ABN Lookup 返回 HTTP %s", response.status_code)
            raise RegistryUnavailable(f"ABN Lookup 返回 HTTP {response.status_code}", reason="service_error")
        return parse_abn_response(response.content)


class CachedAbnLookup:
    """AbnLookup 加短时缓存, 供表单的在线核对用 (同一 ABN 不重复查; 改法定名称只重新比较, 不重新请求): 查到与
    查不到的结果按 ABN 记 ttl 秒, 最多 size 条, 满了丢最早记的; 查不了 (RegistryUnavailable) 不记, 下次照查.
    ACC-04 不经它, 每次实查并留痕."""

    def __init__(
        self, inner: AbnLookup, ttl: float = 300.0, size: int = 256, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.inner = inner
        self.ttl = ttl
        self.size = size
        self.clock = clock
        self.entries: dict[str, tuple[float, AbnRecord | None]] = {}  # ABN → (记下的时刻, 结果), 按记下的先后

    async def abn(self, abn: str) -> AbnRecord | None:
        key = normalize_abn(abn)
        hit = self.entries.get(key)
        if hit is not None and self.clock() - hit[0] < self.ttl:
            return hit[1]
        record = await self.inner.abn(key)
        self.entries.pop(key, None)
        self.entries[key] = (self.clock(), record)
        while len(self.entries) > self.size:
            del self.entries[next(iter(self.entries))]
        return record


def text(node: ElementTree.Element | None, path: str) -> str:
    """node 下 path (命名空间前缀 a:) 的文字, 去首尾空白; 没有为空串."""
    found = node.find(path, NS) if node is not None else None
    return (found.text or "").strip() if found is not None else ""


def entity_name(entity: ElementTree.Element) -> str:
    """登记名称: 非个人取 mainName/organisationName, 个人取 legalName 的名与姓 (fullName 优先); ABN 受限
    (suppressed) 时都没有, 为空串."""
    organisation = text(entity, "a:mainName/a:organisationName")
    if organisation:
        return organisation
    legal = entity.find("a:legalName", NS)
    if legal is None:
        return ""
    full = text(legal, "a:fullName")
    parts = [text(legal, f"a:{name}") for name in ("givenName", "otherGivenName", "familyName")]
    return full or " ".join(p for p in parts if p)


def parse_abn_response(content: bytes) -> AbnRecord | None:
    """解析 SearchByABNv202001 的响应 (ABRPayloadSearchResults): 查到返回 AbnRecord, 查不到 (SEARCH "No records
    found", 或 ABR 判为无效 ABN) 为 None; GUID 不能用抛 RegistryUnavailable (rejected); 其余异常, 读不了或缺字段抛
    RegistryUnavailable (service_error). 带 DOCTYPE 的响应不解析 (防实体展开). 只读解析出的字段, 原文不外传."""
    if b"<!DOCTYPE" in content:
        raise RegistryUnavailable("ABN Lookup 的响应读不了", reason="service_error")
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError:
        raise RegistryUnavailable("ABN Lookup 的响应读不了", reason="service_error") from None
    response = root.find("a:response", NS)
    exception = response.find("a:exception", NS) if response is not None else None
    if exception is not None:
        code, description = text(exception, "a:exceptionCode"), text(exception, "a:exceptionDescription")
        if code == "SEARCH" or INVALID_ABN in description:
            return None
        if any(marker in description for marker in REJECTED):
            raise RegistryUnavailable("ABN Lookup 不认可 GUID", reason="rejected")
        raise RegistryUnavailable("ABN Lookup 服务出错", reason="service_error")
    entity = response.find("a:businessEntity202001", NS) if response is not None else None
    status = STATUS.get(text(entity, "a:entityStatus/a:entityStatusCode").casefold())
    # 不要历史 (includeHistoricalDetails=N) 时各项只有当前的一条; ABN 仍按 isCurrentIndicator 取当前的, 没标时取第一条
    numbers = entity.findall("a:ABN", NS) if entity is not None else []
    current = next((n for n in numbers if text(n, "a:isCurrentIndicator") == "Y"), numbers[0] if numbers else None)
    abn = text(current, "a:identifierValue")
    if entity is None or status is None or not abn:
        raise RegistryUnavailable("ABN Lookup 的响应缺字段", reason="service_error")
    return AbnRecord(
        abn=abn,
        acn=text(entity, "a:ASICNumber") or None,
        entity_name=entity_name(entity),
        status=status,
        entity_type=text(entity, "a:entityType/a:entityDescription"),
    )
