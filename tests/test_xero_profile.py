"""Xero 组织信息与客户画像的核对 (需求 R291): 比较口径的纯函数, 与一次 Xero 场景运行 (xero/20261017-mock 的项目,
假 Xero 回放快照; 改过名称或 ABN 的组织信息副本只在测试临时目录). 组织名称与 ABN 都取快照或合成值."""

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from backend.agents.concierge_chat.tools import NoArgs, ToolEnv, list_open_items
from backend.contracts.data import XeroConnection, XeroProfileField
from backend.contracts.models import ProcedureRun, ProfileSource, TrailEntry, TrailKind
from backend.orchestration.query import GraphStateQuery
from backend.services.xero_link import StoredXeroConnections
from backend.services.xero_profile import organisation_info, profile_differences
from tests.external import require_xero_snapshot
from tests.fakes import (
    ENGAGEMENT,
    NOW,
    NUMBAT_PROFILE,
    XERO_SNAPSHOT,
    FakeEngagementStore,
    FakeTrail,
    FakeXeroConnectionStore,
    wait_idle,
)
from tests.test_xero_link import API, DEMO_ORG, Case, case, client, connect, xero_source

# ── 比较口径 (Q2, Q3) ──


@pytest.mark.parametrize(
    ("xero_name", "xero_abn", "profile_name", "profile_abn", "expected"),
    [
        ("Demo Company (AU)", "11111111138", "Demo Company (AU)", "11111111138", []),
        ("DEMO  company (au).", "11111111138", "Demo Company (AU)", "11111111138", []),  # 大小写, 空白, 标点
        ("Numbat Electronic Components Proprietary Limited", None, "Numbat Electronic Components Pty Ltd", "", []),
        ("Smith & Jones Pty. Ltd.", None, "Smith and Jones Pty Ltd", "", []),
        ("Demo Company (AU)", None, "Demo Company (AU) Pty Ltd", "", ["legal_name"]),  # 后缀不能省
        ("Numbat Electronic Components Ltd", None, "Numbat Electronic Components Pty Ltd", "", ["legal_name"]),
        ("Demo Company (AU)", None, "Demo Company (AU)", "68900042015", []),  # Xero 没给 ABN 时只比名称
        ("Demo Company (AU)", "11111111138", "Demo Company (AU)", "68900042015", ["abn"]),
        ("Demo Trading", "11111111138", "Demo Company (AU)", "68900042015", ["legal_name", "abn"]),
    ],
)
def test_profile_differences(
    xero_name: str, xero_abn: str | None, profile_name: str, profile_abn: str, expected: list[XeroProfileField]
) -> None:
    """名称规范化后比 (不分大小写, 去标点, Pty/Proprietary, Ltd/Limited, & 与 and 相同, 公司后缀不能省); ABN 两边都有才
    比. 防两种错: 写法不同就打扰审计师, 漏掉 "Pty Ltd" 与 "Ltd" 这类实质差异."""
    profile = NUMBAT_PROFILE.model_copy(update={"legal_name": profile_name, "abn": profile_abn})
    assert profile_differences(xero_name, xero_abn, profile) == expected


def test_organisation_info_takes_only_three_fields() -> None:
    """只带出名称, 法定名称 (空时取名称) 与 ABN (RegistrationNumber 的 11 位数字); 防 TaxNumber (澳洲为税号) 等其余字段
    进留痕."""
    org = {"Name": "Synthetic Pty Ltd", "LegalName": "", "RegistrationNumber": "68 900 042 015", "TaxNumber": "x"}
    assert organisation_info(org) == {
        "name": "Synthetic Pty Ltd",
        "legal_name": "Synthetic Pty Ltd",
        "abn": "68900042015",
    }
    assert organisation_info({"Name": "A", "RegistrationNumber": "900042015"})["abn"] is None  # 9 位是 ACN, 不当 ABN


async def test_check_follows_adopted_version_and_connection() -> None:
    """按留痕现算的两条边界 (组织信息为合成值): Q6 a 保持原样绑定当时的画像版本, 本项目改用画像新版本后仍对不上,
    是新的一次核对, 再次提示 (防只按 Xero 的值认已处理); 断开后组织信息属于旧连接, 没有核对 (防断开再连前沿用)."""
    v2 = NUMBAT_PROFILE.model_copy(update={"version": 2, "notes": "synthetic"})
    engagements = FakeEngagementStore([NUMBAT_PROFILE, v2], [ENGAGEMENT])
    trail = FakeTrail()
    info = {"name": "Numbat", "legal_name": "Numbat Electronics Ltd", "abn": None}
    entry = TrailEntry(engagement_id=ENGAGEMENT.id, kind=TrailKind.data, actor="U-1", summary="", created_at=NOW)
    await trail.record(entry.model_copy(update={"payload": {"action": "xero_synced", "organisation_info": info}}))
    connections = StoredXeroConnections(FakeXeroConnectionStore(), lambda: NOW, trail, engagements=engagements)
    connection = XeroConnection(tenant_id="T-1", connected_by="U-1", connected_at=NOW, refreshed_at=NOW)
    first = await connections.profile_check(ENGAGEMENT.id, connection)
    assert first is not None and (first.differences, first.profile_version) == (["legal_name"], 1)
    kept = {"action": "xero_profile_kept", "key": first.key, "reason": "synthetic"}
    later = NOW + timedelta(minutes=1)
    await trail.record(entry.model_copy(update={"payload": kept, "id": "kept", "created_at": later}))
    resolved = await connections.profile_check(ENGAGEMENT.id, connection)
    assert resolved is not None and resolved.resolution == "kept"
    await engagements.update(ENGAGEMENT.id, client_profile_version=2)
    again = await connections.profile_check(ENGAGEMENT.id, connection)
    assert again is not None and (again.profile_version, again.resolution) == (2, None) and again.key != first.key
    gone = {"payload": {"action": "xero_disconnect"}, "id": "gone", "created_at": later + timedelta(minutes=1)}
    await trail.record(entry.model_copy(update=gone))
    assert await connections.profile_check(ENGAGEMENT.id, connection) is None


# ── 一次 Xero 场景运行 ──

PROFILE = API + "/profile-check"
SYNTH_ABN = "53900040171"  # 合成, 校验位正确, 不是任何客户或数据集画像的 ABN


def organisation_copy(c: Case, tmp_path: Path, name: str, **fields: str) -> None:
    """假 Xero 的组织信息换成改过字段的副本 (只在测试临时目录)."""
    orgs: list[dict[str, Any]] = c.fake.load("organisation")
    orgs[0].update(fields)
    path = tmp_path / f"organisation-{name}.json"
    path.write_text(json.dumps(orgs), encoding="utf-8")
    c.fake.files["organisation"] = path


async def sync(http: httpx.AsyncClient, c: Case) -> dict[str, Any] | None:
    """同步一次, 返回概览里的核对结果."""
    response = await http.post(API.format(eid=c.eid) + "/sync")
    assert response.status_code == 202, response.text
    await c.drain()
    check: dict[str, Any] | None = (await xero_source(http, c))["profile_check"]
    return check


async def choose(
    http: httpx.AsyncClient, c: Case, key: str, decision: str, reason: str | None = None
) -> httpx.Response:
    return await http.post(PROFILE.format(eid=c.eid), json={"key": key, "decision": decision, "reason": reason})


async def open_types(c: Case) -> list[tuple[str, bool]]:
    """对话待办各项的类型与是否已标稍后处理."""
    runner = c.xero.data.runner
    env = ToolEnv(c.eid, runner.deps, GraphStateQuery(runner.app, runner.deps, runner), 6000, c.xero.connections)
    return [(item["type"], item["deferred"]) for item in (await list_open_items(env, NoArgs())).data["items"]]


def resolutions(c: Case) -> list[dict[str, Any]]:
    return [
        e.payload
        for e in c.trail.entries
        if e.kind is TrailKind.data and e.payload.get("action") in ("xero_profile_kept", "xero_profile_updated")
    ]


async def at_gate(http: httpx.AsyncClient, c: Case) -> dict[str, Any] | None:
    response = await http.get(f"/api/v1/engagements/{c.eid}/gate")
    return response.json() if response.status_code == 200 else None


@pytest.mark.external
async def test_profile_check_through_acc01_and_after(tmp_path: Path) -> None:
    """R291 验收 1-3 与 Q4, Q6, Q7: xero/20261017-mock 的项目 (画像由同一份 organisation.json 生成) 推进到 ACC-01 闸门.

    1. 同步快照原样的组织信息: 一致, 不提示, 待办里没有这一类.
    2. 换成改了法定名称的副本再同步: 提示 legal_name; 待办里排在补交资料之后; 可标稍后处理 (key 不对 409).
    3. 项目锁被占时 409 ENGAGEMENT_BUSY; keep 没写理由 422 MISSING_FIELD; 写理由后已处理 (稍后处理随之失效),
       留痕一条, 只记 key, 两项, 画像版本与理由.
    4. 副本再改 ABN 再同步: 新的一次核对 (key 变了), 两项都提示; 旧 key 409 PROFILE_CHECK_STALE.
    5. 闸门内 update: 画像第 2 版 (来源 xero, 两项取 Xero 的), 本项目采用, decision 留痕带 source, 重跑 ACC-04 回到
       ACC-01; 之后一致, 不提示.
    6. 通过 ACC-01 后再改名称同步, update 只给客户写第 3 版, 本项目采用的仍是第 2 版, 核对记为已处理 (adopted 假);
       同一 key 再选 409.
    7. 再改名称同步后项目锁定: 待办里不列, 选择 423 ENGAGEMENT_LOCKED.
    """
    require_xero_snapshot(XERO_SNAPSHOT)  # 假 Xero 回放这个快照
    c = case(tmp_path)
    runner = c.xero.data.runner
    es = runner.deps.engagement_store
    await runner.start(c.eid)
    await wait_idle(runner, c.eid)
    async with client(c) as http:
        assert (await at_gate(http, c) or {}).get("gate") == "ACC-01"
        await connect(http, c)
        same = await sync(http, c)
        assert same is not None and same["differences"] == [] and same["xero_abn"] == DEMO_ORG["abn"]
        before = await open_types(c)  # 阶段决定与补交资料 (Xero 没有发运单与审批日志)
        assert {t for t, _ in before} == {"gate_decision", "data_request"} and before[0][0] == "gate_decision"

        renamed = "Demo Company (AU) Trading Pty Ltd"  # 合成
        organisation_copy(c, tmp_path, "renamed", LegalName=renamed)
        first = await sync(http, c)
        assert first is not None
        assert (first["differences"], first["xero_legal_name"], first["resolution"]) == (["legal_name"], renamed, None)
        deferral = {"target_type": "xero_profile", "target_id": first["key"], "deferred": True}
        wrong = await http.post(f"/api/v1/engagements/{c.eid}/deferrals", json={**deferral, "target_id": "XP-other"})
        assert (wrong.status_code, wrong.json()["detail"]["code"]) == (409, "PROFILE_CHECK_STALE")
        assert (await http.post(f"/api/v1/engagements/{c.eid}/deferrals", json=deferral)).status_code == 202
        assert await open_types(c) == [*before, ("xero_profile", True)]  # 排在补交资料之后

        runner.acquire(c.eid)  # 同断开, 选择持项目锁
        try:
            busy = await choose(http, c, first["key"], "keep", "busy")
        finally:
            runner.release(c.eid)
        assert (busy.status_code, busy.json()["detail"]["code"]) == (409, "ENGAGEMENT_BUSY")
        missing = await choose(http, c, first["key"], "keep", "  ")
        assert (missing.status_code, missing.json()["detail"]["code"]) == (422, "MISSING_FIELD")
        kept = await choose(
            http, c, first["key"], "keep", "Xero shows the trading name; the register has the profile name"
        )
        assert kept.status_code == 200, kept.text
        assert (kept.json()["resolution"], kept.json()["reason"][:4]) == ("kept", "Xero")
        assert [p["action"] for p in resolutions(c)] == ["xero_profile_kept"]
        assert set(resolutions(c)[0]) == {"action", "key", "legal_name", "abn", "profile_version", "reason"}
        assert await open_types(c) == before
        deferrals = (await http.get(f"/api/v1/engagements/{c.eid}/deferrals")).json()
        assert [d["target_type"] for d in deferrals] == []

        organisation_copy(c, tmp_path, "abn", LegalName=renamed, RegistrationNumber=SYNTH_ABN)
        second = await sync(http, c)
        assert second is not None and second["key"] != first["key"]
        assert (second["differences"], second["resolution"]) == (["legal_name", "abn"], None)
        assert await open_types(c) == [*before, ("xero_profile", False)]  # 上一次核对的稍后处理不带到新的一次
        assert (await http.get(f"/api/v1/engagements/{c.eid}/deferrals")).json() == []
        stale = await choose(http, c, first["key"], "keep", "again")
        assert (stale.status_code, stale.json()["detail"]["code"]) == (409, "PROFILE_CHECK_STALE")

        updated = await choose(http, c, second["key"], "update")
        assert updated.status_code == 200, updated.text
        await wait_idle(runner, c.eid)
        engagement = await es.get(c.eid)
        assert engagement is not None and engagement.client_profile_version == 2
        v2 = await es.client_profile(engagement.client_id, 2)
        assert v2 is not None and (v2.legal_name, v2.abn) == (renamed, SYNTH_ABN)
        assert (await es.profile_history(engagement.client_id))[-1].source is ProfileSource.xero
        [decision] = [e for e in c.trail.entries if e.kind is TrailKind.decision and e.idempotency_key.endswith("-v2")]
        assert decision.payload["source"] == "xero"
        runs = await runner.deps.output_store.latest(c.eid, ProcedureRun, procedure_id="ACC-04")
        assert sorted(r.id for r in runs) == ["RUN-ACC-04-1", "RUN-ACC-04-2"]  # 更新后重跑了一次
        gate = await at_gate(http, c)
        assert gate is not None and gate["gate"] == "ACC-01"
        after = (await xero_source(http, c))["profile_check"]
        assert (after["profile_version"], after["differences"]) == (2, [])
        assert resolutions(c)[-1]["adopted"] is True

        approve = {"gate": "ACC-01", "target_id": gate["target_id"], "target_version": gate["target_version"]}
        passed = await http.post(f"/api/v1/engagements/{c.eid}/gate-decisions", json={**approve, "decision": "approve"})
        assert passed.status_code == 202, passed.text
        await wait_idle(runner, c.eid)
        assert (await at_gate(http, c) or {}).get("gate") != "ACC-01", runner.run_error(c.eid)

        again = "Demo Company (AU) Holdings Pty Ltd"  # 合成
        organisation_copy(c, tmp_path, "later", LegalName=again, RegistrationNumber=SYNTH_ABN)
        third = await sync(http, c)
        assert third is not None and third["differences"] == ["legal_name"]
        later = await choose(http, c, third["key"], "update")
        assert later.status_code == 200, later.text
        assert (later.json()["resolution"], later.json()["updated_version"], later.json()["adopted"]) == (
            "updated",
            3,
            False,
        )
        engagement = await es.get(c.eid)
        assert engagement is not None and engagement.client_profile_version == 2
        latest = await es.client_profile(engagement.client_id)
        assert latest is not None and (latest.version, latest.legal_name) == (3, again)
        repeat = await choose(http, c, third["key"], "update")
        assert (repeat.status_code, repeat.json()["detail"]["code"]) == (409, "PROFILE_CHECK_STALE")

        organisation_copy(c, tmp_path, "locked", LegalName="Demo Company (AU) Services Pty Ltd")  # 合成
        fourth = await sync(http, c)
        assert fourth is not None and fourth["differences"] == ["legal_name"]
        assert ("xero_profile", False) in await open_types(c)
        await es.update(c.eid, locked=True)
        assert "xero_profile" not in {t for t, _ in await open_types(c)}
        locked = await choose(http, c, fourth["key"], "keep", "locked")
        assert (locked.status_code, locked.json()["detail"]["code"]) == (423, "ENGAGEMENT_LOCKED")
