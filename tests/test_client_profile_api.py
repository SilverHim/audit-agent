"""客户资料 (R2): GET /engagements/{eid}/client-profile.

画像, 评分卡与历史用 Xero Demo Company 快照的项目 (本机没有快照时跳过); 过了 ACC-01 之后不可编辑用 Numbat:
Xero 项目过 ACC-01 后的计划阶段在测试替身里取的是 Numbat 的财务数据, 两者混用说明不了问题.
"""

from typing import Any

from backend.contracts.models import ClientProfile
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
from tests.test_flow import start_numbat

EID = ENGAGEMENT.id
URL = f"/api/v1/engagements/{EID}/client-profile"


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
