"""项目列表 (R4): GET /engagements 每行带审计期间. 用 Xero Demo Company 快照的项目 (本机没有快照时跳过)."""

from tests.fakes import FakeEngagementStore, api_client, make_runner, wait_idle, xero_engagement, xero_snapshot


async def test_list_has_period_of_xero_engagement() -> None:
    profile, engagement = xero_engagement(xero_snapshot())
    runner, _ = make_runner(engagement_store=FakeEngagementStore([profile], [engagement]))
    await runner.start(engagement.id)
    await wait_idle(runner, engagement.id)
    async with api_client(runner) as client:
        rows = (await client.get("/api/v1/engagements")).json()

    assert [(r["id"], r["client_name"], r["period_start"], r["period_end"]) for r in rows] == [
        (engagement.id, "Demo Company (AU)", "2026-07-01", "2026-11-30")
    ]
    assert (rows[0]["phase"], rows[0]["current_gate"]) == ("P0_Acceptance", "ACC-01")
