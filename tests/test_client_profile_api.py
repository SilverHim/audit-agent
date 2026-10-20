"""客户资料 (R2): GET /engagements/{eid}/client-profile; PUT 时没给的字段沿用被替代的那一版 (R127).

画像, 评分卡与历史用 Xero Demo Company 快照的项目 (本机没有快照时跳过); 过了 ACC-01 之后不可编辑用 Numbat:
Xero 项目过 ACC-01 后的计划阶段在测试替身里取的是 Numbat 的财务数据, 两者混用说明不了问题.
"""

from typing import Any

import pytest

from backend.contracts.models import ClientProfile
from tests.external import supplements_dir
from tests.fakes import (
    ENGAGEMENT,
    FakeEngagementStore,
    api_client,
    make_runner,
    wait_idle,
    xero_engagement,
    xero_snapshot,
)
from tests.test_decisions import Started
from tests.test_demo_default import CreatingEngagementStore
from tests.test_flow import start_numbat

EID = ENGAGEMENT.id
URL = f"/api/v1/engagements/{EID}/client-profile"
PERIOD = (ENGAGEMENT.period_start, ENGAGEMENT.period_end)
ORIGINAL = ("legal_name", "abn", "entity_type", "industry_anzsic", "listed", "directors", "notes")  # 扩展前的 7 个字段


async def xero_at_acc01() -> tuple[Started, ClientProfile]:
    """Xero 项目跑完 ACC-04, 停在 ACC-01."""
    profile, engagement = xero_engagement(xero_snapshot())
    engagement_store = FakeEngagementStore([profile], [engagement])
    runner, store = make_runner(engagement_store=engagement_store)
    await runner.start(EID)
    await wait_idle(runner, EID)
    return Started(runner, store, engagement_store), profile


def profile_body(profile: ClientProfile, **changes: Any) -> dict[str, Any]:
    """PUT /client-profile 的请求体: 画像去掉 id, 版本 +1."""
    body = profile.model_dump(mode="json", exclude={"id"})
    return {**body, "version": profile.version + 1, **changes}


@pytest.mark.external
async def test_xero_profile_and_scorecard_at_acc01() -> None:
    s, profile = await xero_at_acc01()
    async with api_client(s.runner) as client:
        view = (await client.get(URL)).json()

    assert (view["profile"]["legal_name"], view["profile"]["abn"]) == ("Demo Company (AU)", "11111111138")
    assert view["profile"] == profile.model_dump(mode="json")
    assert [p["version"] for p in view["history"]] == [1]
    assert view["engagement_profile_version"] == 1
    scorecard = view["scorecard"]
    assert [c["check"] for c in scorecard["registry_checks"]] == ["abn_active", "name_match", "company_registered"]
    # 登记查询 (测试替身, 只有 Numbat 的登记记录) 查不到 Xero 的 ABN, 未通过的检查各出一条例外
    failed = [c for c in scorecard["registry_checks"] if not c["passed"]]
    assert failed and [x["id"] for x in view["exceptions"]] == scorecard["exception_ids"]
    assert len(view["exceptions"]) == len(failed)
    assert view["editable"] is True


@pytest.mark.external
async def test_xero_profile_update_adds_history_and_reruns_scorecard() -> None:
    s, profile = await xero_at_acc01()
    async with api_client(s.runner) as client:
        saved = await client.put(URL, json=profile_body(profile, directors=["Owner A"]))
        assert saved.status_code == 202
        await wait_idle(s.runner, EID)
        view = (await client.get(URL)).json()

    assert [p["version"] for p in view["history"]] == [1, 2]
    assert view["history"][0] == profile.model_dump(mode="json")
    assert (view["profile"]["version"], view["profile"]["directors"]) == (2, ["Owner A"])
    assert view["engagement_profile_version"] == 2
    assert (view["scorecard"]["version"], view["scorecard"]["client_profile_version"]) == (2, 2)  # ACC-04 已重跑
    assert view["editable"] is True  # 重新呈报, 仍停在 ACC-01


async def test_not_editable_after_acc01() -> None:
    s = await start_numbat()  # 已通过 ACC-01, 停在 PLN-01/03
    async with api_client(s.runner) as client:
        view = (await client.get(URL)).json()
    assert view["profile"]["legal_name"] == "Numbat Electronic Components Pty Ltd"
    assert view["scorecard"]["exception_ids"] == []
    assert view["editable"] is False


async def q1_at_acc01() -> tuple[Started, ClientProfile]:
    """补造画像 CLIENT-XERO-3C1332BB-Q1 v1 (带扩展字段: 审批限额 500000, 上期未审) 的项目跑完 ACC-04, 停在 ACC-01.
    项目存储能再建项目 (采用画像的规则同 PostgresEngagementStore.create)."""
    profile = ClientProfile.model_validate_json((supplements_dir("20261017-q1") / "client_profile.json").read_bytes())
    _, engagement = xero_engagement(xero_snapshot())
    engagement = engagement.model_copy(update={"client_id": profile.id, "client_profile_version": profile.version})
    engagement_store = CreatingEngagementStore([profile], [engagement])
    runner, store = make_runner(engagement_store=engagement_store)
    await runner.start(EID)
    await wait_idle(runner, EID)
    return Started(runner, store, engagement_store), profile


def original_body(profile: ClientProfile, version: int, **changes: Any) -> dict[str, Any]:
    """只有扩展前 7 个字段 (与版本号) 的请求体."""
    body = profile.model_dump(mode="json", include=set(ORIGINAL))
    return {**body, "version": version, **changes}


@pytest.mark.external
async def test_update_without_extension_fields_keeps_them() -> None:
    """请求体只给原有 7 个字段: 新版本画像的扩展字段等于被替代的那一版 (审批限额仍为 500000, 上期未审照旧)."""
    s, profile = await q1_at_acc01()
    async with api_client(s.runner) as client:
        saved = await client.put(URL, json=original_body(profile, 2, directors=["Owner A"]))
        assert saved.status_code == 202
        await wait_idle(s.runner, EID)
    second = await s.engagement_store.client_profile(profile.id, 2)
    assert second is not None and second.directors == ["Owner A"]
    assert (second.approval_limit_cents, second.prior_period_audited) == (500000, False)
    assert second.model_dump(exclude={"version", "directors"}) == profile.model_dump(exclude={"version", "directors"})


@pytest.mark.external
async def test_update_overrides_only_given_extension_fields() -> None:
    """给出的字段覆盖, 给 null 的可空字段清空, 没给的沿用; 再改一版时以第 2 版为底, 不回到第 1 版."""
    s, profile = await q1_at_acc01()
    async with api_client(s.runner) as client:
        body = profile_body(profile, approval_limit_cents=600000, prior_period_audited=None)
        assert (await client.put(URL, json=body)).status_code == 202
        await wait_idle(s.runner, EID)
        second = await s.engagement_store.client_profile(profile.id, 2)
        assert second is not None and (second.approval_limit_cents, second.prior_period_audited) == (600000, None)
        assert second.fees == profile.fees and second.related_parties == profile.related_parties

        notes = {k: v for k, v in original_body(second, 3).items() if k != "notes"}  # notes 也可省略
        assert (await client.put(URL, json=notes)).status_code == 202
        await wait_idle(s.runner, EID)
    third = await s.engagement_store.client_profile(profile.id, 3)
    assert third is not None and third.model_dump(exclude={"version"}) == second.model_dump(exclude={"version"})


@pytest.mark.external
async def test_new_engagement_adopts_the_auditor_edited_profile() -> None:
    """审计师在项目 A 的 ACC-01 改过画像 (第 2 版) 后, 用同一数据集建项目 B: 数据集画像与第 1 版相同, B 采用改后的
    第 2 版, 不把数据集原样写成新一版; A 之后再改也以第 2 版为底 (R127 第 1 轮审核 R127-01)."""
    s, profile = await q1_at_acc01()
    async with api_client(s.runner) as client:
        body = original_body(profile, 2, approval_limit_cents=600000)
        assert (await client.put(URL, json=body)).status_code == 202
        await wait_idle(s.runner, EID)
        store = s.engagement_store
        assert isinstance(store, CreatingEngagementStore)
        b = await store.create(
            owner_id="USR-B",
            period_start=PERIOD[0],
            period_end=PERIOD[1],
            dataset="xero/20261017-q1",
            profile=profile,  # 数据集带的画像, 即第 1 版原样
        )
        assert b.client_profile_version == 2
        assert [p.version for p in store.profiles if p.id == profile.id] == [1, 2]  # 没有多写版本

        assert (await client.put(URL, json=original_body(profile, 3, directors=["Owner A"]))).status_code == 202
        await wait_idle(s.runner, EID)
    third = await s.engagement_store.client_profile(profile.id, 3)
    assert third is not None and (third.approval_limit_cents, third.directors) == (600000, ["Owner A"])


@pytest.mark.external
async def test_update_rejects_invalid_merged_profile() -> None:
    """合并后不合 ClientProfile (财年末不是合法月日): 422 INVALID_VALUE, 不出新版本."""
    s, profile = await q1_at_acc01()
    async with api_client(s.runner) as client:
        response = await client.put(URL, json=original_body(profile, 2, financial_year_end="13-01"))
    assert response.status_code == 422 and response.json()["detail"]["code"] == "INVALID_VALUE"
    assert await s.engagement_store.client_profile(profile.id, 2) is None


def test_client_profile_in_has_every_profile_field() -> None:
    """ClientProfileIn 与 ClientProfile 的字段 (去掉 id) 一一对应, 类型相同; 原有 6 个字段之外都可省略.
    新建客户的 ClientCreate (R288) 同为这些字段, 只是没有 version."""
    from backend.api.schemas import ClientCreate, ClientProfileIn

    model = {k: f for k, f in ClientProfile.model_fields.items() if k != "id"}
    assert sorted(ClientProfileIn.model_fields) == sorted(model)
    assert list(ClientCreate.model_fields) == [k for k in model if k != "version"]
    for schema in (ClientProfileIn, ClientCreate):
        for name, field in schema.model_fields.items():
            assert field.annotation == model[name].annotation, name
    required = {k for k, f in ClientProfileIn.model_fields.items() if f.is_required()}
    assert required == {"version", *ORIGINAL} - {"notes"}
    assert {k for k, f in ClientCreate.model_fields.items() if f.is_required()} == set(ORIGINAL) - {"notes"}
