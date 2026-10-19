"""ABN 实时核查 (需求 R289): ABR ABN Lookup 适配器, 短时缓存, 组合登记来源, 表单的在线核对接口, GUID 不外泄.

全部离线: ABR 用假接口 (tests/fakes.FakeAbr, httpx.MockTransport) 回放 tests/fixtures/abr/ 下按官方 XSD 手写的
合成响应; 假接口像真实接口一样在响应里回显请求的 GUID. 不调真实 ABR.
"""

import json
import logging
from dataclasses import replace
from typing import Any

import httpx
import pytest

from backend.adapters.abn_lookup import CachedAbnLookup, parse_abn_response
from backend.adapters.registry import CompositeRegistrySource
from backend.agents.client_intel import ClientIntelAgent
from backend.contracts.errors import RegistryUnavailable
from backend.contracts.models import AbnRecord
from backend.services.abn import abn_problem
from tests.fakes import (
    ABR_URL,
    ENGAGEMENT,
    FAKE_GUID,
    NUMBAT_ABN,
    NUMBAT_ASIC,
    NUMBAT_PROFILE,
    FakeAbr,
    FakeEngagementStore,
    FakeRegistry,
    FakeTrail,
    api_client,
    make_ctx,
    make_deps,
    make_runner,
    no_uploads,
)

WOMBAT = AbnRecord(
    abn="44900043101",
    acn="900043101",
    entity_name="WOMBAT ANALYTICS PTY LTD",
    status="Active",
    entity_type="Australian Private Company",
)
SOLE = "25900043102"  # 合成的个人独资 ABN (校验位按算法补)
LOOKUP = "/api/v1/clients/abn-lookup"
ACTIVE = FakeAbr.fixture("active").replace(b"__ABN__", WOMBAT.abn.encode())  # 有效登记的响应 (不经假接口)


# ── 适配器: 按官方响应结构映射, 查不了时只带类别 ──


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("active", WOMBAT),
        ("cancelled", WOMBAT.model_copy(update={"status": "Cancelled"})),
        # 个人: legalName 的名与姓拼成全名; 没有 ACN (空元素) 记 None
        (
            "sole_trader",
            AbnRecord(
                abn=SOLE,
                acn=None,
                entity_name="JANE MARY CITIZEN",
                status="Active",
                entity_type="Individual/Sole Trader",
            ),
        ),
        # 受限的 ABN 只给 ABN 与状态: 名称与主体类型为空
        ("suppressed", AbnRecord(abn=SOLE, acn=None, entity_name="", status="Active", entity_type="")),
        ("not_found", None),  # SEARCH "No records found"
        ("invalid_abn", None),  # ABR 判为无效 ABN, 同查不到
    ],
)
async def test_lookup_maps_the_response(reply: str, expected: AbnRecord | None) -> None:
    abn = expected.abn if expected is not None else WOMBAT.abn
    assert await FakeAbr({abn: reply}).client().abn(abn) == expected


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        # GUID 用不了的三种官方文案 (HTTP 200, 正文是 WEBSERVICES 异常): 未登记, 未注册该服务, 注册已取消
        ("guid_rejected", "rejected"),
        ("guid_unregistered", "rejected"),
        ("guid_cancelled", "rejected"),
        ("system_error", "service_error"),  # 其余 WEBSERVICES 异常
        (500, "service_error"),
        (307, "service_error"),  # 不跟随重定向: 带 GUID 的请求体不重发到 Location 的主机
        ("bad_gzip", "service_error"),  # httpx 读正文时抛 DecodingError (不是传输错误)
        ("unreachable", "unreachable"),
    ],
)
async def test_lookup_failures_carry_only_the_category(reply: str | int, reason: str) -> None:
    """查不了时抛 RegistryUnavailable, reason 为类别, 只发一次请求; 异常文本不带原始响应, 也不接原异常 (from None:
    原异常不作 __cause__, 作 __context__ 时也被压制, 记 traceback 不会带出; 两者都回显了 GUID)."""
    abr = FakeAbr({WOMBAT.abn: reply})
    with pytest.raises(RegistryUnavailable) as e:
        await abr.client().abn(WOMBAT.abn)
    assert (e.value.reason, len(abr.requests)) == (reason, 1)
    assert e.value.__cause__ is None and (e.value.__context__ is None or e.value.__suppress_context__)
    assert FAKE_GUID not in str(e.value)


def test_current_abn_is_taken_when_the_record_lists_several() -> None:
    """登记里列了几个 ABN 时 (被替换过的旧号), 取 isCurrentIndicator 为 Y 的那个."""
    current = b"<ABN>\n        <identifierValue>" + WOMBAT.abn.encode()
    replaced = b"<ABN><identifierValue>51824753556</identifierValue><isCurrentIndicator>N</isCurrentIndicator></ABN>"
    content = ACTIVE.replace(current, replaced + b"\n      " + current, 1)
    assert content.count(b"<ABN>") == 2 and parse_abn_response(content) == WOMBAT


@pytest.mark.parametrize(
    "content",
    [
        b"not xml",
        # 带 DOCTYPE 的不解析 (防实体展开), 哪怕其余部分是一份有效的登记
        ACTIVE.replace(
            b"<ABRPayloadSearchResults ",
            b'<!DOCTYPE ABRPayloadSearchResults [<!ENTITY n "W">]>\n<ABRPayloadSearchResults ',
            1,
        ),
        b'<ABRPayloadSearchResults xmlns="http://abr.business.gov.au/ABRXMLSearch/"><response />'
        b"</ABRPayloadSearchResults>",  # 没有登记记录也没有异常
        FakeAbr.fixture("active").replace(b"<entityStatusCode>Active", b"<entityStatusCode>Deregistered"),
    ],
)
def test_unreadable_responses_are_service_errors(content: bytes) -> None:
    """读不了, 没有登记记录也没有异常, 或状态不是 active / cancelled: service_error."""
    with pytest.raises(RegistryUnavailable) as e:
        parse_abn_response(content)
    assert e.value.reason == "service_error"


async def test_request_is_a_post_form_without_guid_in_url() -> None:
    """SearchByABNv202001 的 HTTP POST: 表单三项 (ABN 去空格, 不要历史, GUID), GUID 不进 URL; 没配 GUID 时不发请求,
    抛 not_configured."""
    abr = FakeAbr({WOMBAT.abn: "active"})
    await abr.client().abn("44 900 043 101")
    [request] = abr.requests
    assert (request.method, str(request.url)) == ("POST", ABR_URL)
    form = dict(httpx.QueryParams(request.content.decode()))
    assert form == {"searchString": WOMBAT.abn, "includeHistoricalDetails": "N", "authenticationGuid": FAKE_GUID}
    with pytest.raises(RegistryUnavailable) as e:
        await abr.client(guid="").abn(WOMBAT.abn)
    assert e.value.reason == "not_configured" and len(abr.requests) == 1


# ── 短时缓存: 同一 ABN 不重复查 ──


async def test_cache_reuses_results_within_ttl_and_skips_failures() -> None:
    """查到与查不到的结果按 ABN (去空格) 记 ttl 秒, 过期再查; 查不了的不记, 下次照查; 满了丢最早记的."""
    now = [0.0]
    abr = FakeAbr({WOMBAT.abn: "active", SOLE: 500})
    cache = CachedAbnLookup(abr.client(), ttl=300, size=2, clock=lambda: now[0])
    assert await cache.abn("44 900 043 101") == WOMBAT
    assert await cache.abn(WOMBAT.abn) == WOMBAT and len(abr.requests) == 1
    now[0] = 300.0
    await cache.abn(WOMBAT.abn)
    assert len(abr.requests) == 2  # 过期
    for _ in range(2):
        with pytest.raises(RegistryUnavailable):
            await cache.abn(SOLE)
    assert len(abr.requests) == 4  # 查不了的不记
    await cache.abn("51824753556")  # 查不到 (None) 也记
    await cache.abn("51824753556")
    assert len(abr.requests) == 5
    await cache.abn("68900042015")  # 第 3 个: 丢掉最早记的 (WOMBAT)
    await cache.abn(WOMBAT.abn)
    assert len(abr.requests) == 7


# ── 组合登记来源: 有数据集的项目不发 ABR 请求 ──


def composite(abr: FakeAbr, dataset: str | None, guid: str = FAKE_GUID) -> CompositeRegistrySource:
    """项目 ENGAGEMENT (dataset 按给定) 的组合登记来源: 数据集一侧是存着 Numbat 登记的假登记, ABN 实查走假 ABR."""
    store = FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT.model_copy(update={"dataset": dataset})])
    datasets: Any = FakeRegistry({NUMBAT_ABN.abn: NUMBAT_ABN}, {"900042015": NUMBAT_ASIC})
    return CompositeRegistrySource(store, datasets, abr.client(guid), no_uploads())


async def test_composite_routes_by_dataset() -> None:
    """有数据集 (Numbat, Xero) 照旧读数据集的登记, 不发 ABR 请求; 新建的客户 (没有数据集) ABN 实查, ASIC 取上传的
    摘录 (R290, 这里一份都没有: no_extract); 项目不存在抛 LookupError."""
    abr = FakeAbr({WOMBAT.abn: "active"})
    with_dataset = composite(abr, "demo")
    assert await with_dataset.abn(ENGAGEMENT.id, NUMBAT_ABN.abn) == NUMBAT_ABN
    assert await with_dataset.asic_company(ENGAGEMENT.id, "900042015") == NUMBAT_ASIC
    assert abr.requests == []
    new_client = composite(abr, None)
    assert await new_client.abn(ENGAGEMENT.id, WOMBAT.abn) == WOMBAT
    with pytest.raises(RegistryUnavailable) as e:
        await new_client.asic_company(ENGAGEMENT.id, "900043101")
    assert e.value.reason == "no_extract" and len(abr.requests) == 1
    with pytest.raises(LookupError):
        await new_client.abn("ENG-NONE", WOMBAT.abn)


WOMBAT_RESULT = {  # 假 ABR 回放的有效登记 (tests/fixtures/abr/active.xml, 合成), 写成字面量
    "abn": "44900043101",
    "acn": "900043101",
    "entity_name": "WOMBAT ANALYTICS PTY LTD",
    "status": "Active",
    "entity_type": "Australian Private Company",
}
SOLE_RESULT = {
    "abn": "44900043101",
    "acn": None,
    "entity_name": "JANE MARY CITIZEN",
    "status": "Active",
    "entity_type": "Individual/Sole Trader",
}


@pytest.mark.parametrize(
    ("reply", "checks", "result", "asic"),
    [
        # 公司而没有上传 ASIC 摘录 (标了客户无此类资料时 ACC-04 才走到这里, R290): 公司两项待人工确认, 记一条 "没有查询"
        ("active", [True, True, None, None], WOMBAT_RESULT, True),
        ("cancelled", [False, True, None, None], {**WOMBAT_RESULT, "status": "Cancelled"}, True),
        # 没有 ACN (个人独资) 或查不到: 不要求摘录, 公司两项待人工确认, 不判不通过, 不另记 ASIC 留痕
        ("sole_trader", [True, False, None, None], SOLE_RESULT, False),
        ("not_found", [False, False, None, None], None, False),
    ],
)
async def test_acc04_checks_new_clients_against_abn_lookup(
    reply: str, checks: list[bool | None], result: dict[str, Any] | None, asic: bool
) -> None:
    """新客户 (没有数据集) 的 ACC-04 经组合登记来源实查 ABN Lookup: 有效, 已注销, 个人独资, 查不到, 每次查询一条
    留痕 (沿用现有的摘要与 payload, result 为解析出的登记记录); 是公司时摘录一项另记一条 "没有查询"."""
    abr = FakeAbr({WOMBAT.abn: reply})
    trail = FakeTrail()
    new_client = ENGAGEMENT.model_copy(update={"dataset": None})
    store = FakeEngagementStore([NUMBAT_PROFILE], [new_client])
    agent = ClientIntelAgent(make_deps(composite(abr, None), trail, store))  # type: ignore[arg-type]
    profile = NUMBAT_PROFILE.model_copy(update={"abn": WOMBAT.abn, "legal_name": "Wombat Analytics Pty Ltd"})
    found = await agent.collect_checks(make_ctx(agent.deps, engagement=new_client), profile)
    assert [c.check for c in found] == ["abn_active", "name_match", "company_registered", "directors_match"]
    assert [c.passed for c in found] == checks
    abn_entry, *asic_entries = trail.entries
    assert (abn_entry.summary, abn_entry.payload) == (f"ABN Lookup {WOMBAT.abn}", {"abn": WOMBAT.abn, "result": result})
    expected = [{"acn": WOMBAT.acn, "available": False, "reason": "no_extract", "result": None}] if asic else []
    assert [e.payload for e in asic_entries] == expected
    assert len(abr.requests) == 1


# ── 表单的在线核对: GET /clients/abn-lookup ──


async def lookup(abr: FakeAbr, *queries: dict[str, str], guid: str = FAKE_GUID) -> list[httpx.Response]:
    """依次请求在线核对接口 (同一个缓存), 返回各响应."""
    runner, _ = make_runner()
    deps = replace(runner.deps, abn_lookup=CachedAbnLookup(abr.client(guid)))
    async with api_client(runner, deps=deps) as client:
        return [await client.get(LOOKUP, params=q) for q in queries]


# 在线核对的各情形: (假接口的回复, 填写的法定名称, 预期的 status, name_matches, reason, record.status)
ENDPOINT_CASES: list[tuple[str | int, str, tuple[str, bool | None, str | None, str | None]]] = [
    ("active", "Wombat Analytics Pty. Ltd.", ("found", True, None, "Active")),  # 规范化后相同
    ("active", "Wombat Data Pty Ltd", ("found", False, None, "Active")),
    ("active", "", ("found", None, None, "Active")),  # 没给名称不比较
    ("cancelled", "Wombat Analytics Pty Ltd", ("found", True, None, "Cancelled")),
    ("suppressed", "Wombat Analytics Pty Ltd", ("found", None, None, "Active")),  # 名称不公开不比较
    ("not_found", "Wombat Analytics Pty Ltd", ("not_found", None, None, None)),
    (500, "Wombat Analytics Pty Ltd", ("unavailable", None, "service_error", None)),
    ("guid_rejected", "", ("unavailable", None, "rejected", None)),
]


async def test_lookup_endpoint() -> None:
    """各情形共用一个假接口与一个客户端 (每种回复一个合成 ABN, 同回复的同一 ABN; ABN 带空格写): 查到时返回登记
    记录 (含已注销的状态) 与名称是否一致, 查不到为 not_found, 查不了为 unavailable 与原因类别, 都是 200."""
    replies = list(dict.fromkeys(reply for reply, _, _ in ENDPOINT_CASES))
    abns = {reply: synthetic_abn(20 + n) for n, reply in enumerate(replies)}
    spaced = {reply: f"{a[:2]} {a[2:5]} {a[5:8]} {a[8:]}" for reply, a in abns.items()}
    queries = [{"abn": spaced[reply], "legal_name": name} for reply, name, _ in ENDPOINT_CASES]
    responses = await lookup(FakeAbr({abn: reply for reply, abn in abns.items()}), *queries)
    for (reply, name, expected), response in zip(ENDPOINT_CASES, responses, strict=True):
        body = response.json()
        record = body["record"]
        got = (body["status"], body["name_matches"], body["reason"], record["status"] if record else None)
        assert (response.status_code, got) == (200, expected), (reply, name)
        assert record is None or record["abn"] == abns[reply]


async def test_lookup_endpoint_checks_digits_first_and_queries_each_abn_once() -> None:
    """校验位不对 422 INVALID_VALUE, 不发请求; 同一 ABN 改法定名称只重新比较, 不重新请求; 没配 GUID 为
    unavailable (not_configured), 不报错."""
    abr = FakeAbr({WOMBAT.abn: "active"})
    bad, first, second = await lookup(
        abr,
        {"abn": "44 900 043 102"},
        {"abn": WOMBAT.abn, "legal_name": "Wombat Analytics Pty Ltd"},
        {"abn": WOMBAT.abn, "legal_name": "Wombat Data Pty Ltd"},
    )
    assert (bad.status_code, bad.json()["detail"]["code"]) == (422, "INVALID_VALUE")
    assert (first.json()["name_matches"], second.json()["name_matches"]) == (True, False)
    assert len(abr.requests) == 1
    [unconfigured] = await lookup(FakeAbr(), {"abn": WOMBAT.abn}, guid="")
    assert unconfigured.json() == {
        "status": "unavailable",
        "record": None,
        "reason": "not_configured",
        "name_matches": None,
    }


# ── GUID 不出现在日志, 留痕, 响应与异常里 ──


def synthetic_abn(n: int) -> str:
    """合成的有效 ABN: 9 位编号 9000439nn, 校验位按算法补."""
    body = f"9000439{n:02d}"
    return next(f"{p}{body}" for p in range(10, 100) if abn_problem(f"{p}{body}") is None)


# 每种响应配一个合成 ABN: 一个假接口, 一个项目, 一个接口客户端走遍
REPLIES: dict[str, str | int] = {
    synthetic_abn(n): reply
    for n, reply in enumerate(
        [
            "active",
            "cancelled",
            "suppressed",
            "not_found",
            "invalid_abn",
            "guid_rejected",
            "guid_unregistered",
            "guid_cancelled",
            "system_error",
            500,
            307,
            "bad_gzip",
            "unreachable",
        ]
    )
}


async def test_guid_never_leaves_the_adapter(caplog: pytest.LogCaptureFixture) -> None:
    """假接口的每种响应 (都回显了 GUID, 含重定向与正文解压失败) 各走一遍适配器, ACC-04 (新客户, 组合登记来源) 与
    在线核对接口, DEBUG 日志全开: 日志全文, 全部留痕, 接口响应正文与各异常的文本里都没有假 GUID; 请求 URL 也没有."""
    caplog.set_level(logging.DEBUG)
    abr = FakeAbr(REPLIES)
    assert all(FAKE_GUID.encode() in abr.echoed(abn) for abn in REPLIES)  # 假接口确实回显了 GUID
    seen: list[str] = [repr(abr.client())]
    for abn in REPLIES:
        try:
            seen.append(repr(await abr.client().abn(abn)))
        except RegistryUnavailable as exc:
            seen += [str(exc), repr(exc)]
    trail = FakeTrail()
    agent = ClientIntelAgent(make_deps(composite(abr, None), trail))  # type: ignore[arg-type]
    for abn in REPLIES:
        checks = await agent.collect_checks(make_ctx(agent.deps), NUMBAT_PROFILE.model_copy(update={"abn": abn}))
        seen.append(repr(checks))
    seen.append(json.dumps([e.model_dump(mode="json") for e in trail.entries]))
    seen += [r.text for r in await lookup(abr, *({"abn": abn, "legal_name": "Wombat"} for abn in REPLIES))]
    seen += [str(r.url) for r in abr.requests]
    assert len(abr.requests) == 3 * len(REPLIES) and caplog.text
    assert [text for text in [caplog.text, *seen] if FAKE_GUID in text] == []
