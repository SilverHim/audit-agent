"""底稿内容与导出共用的轻量样例: 用真实被测总体建 D-3, 不再跑一遍全流程."""

from backend.contracts.models import Phase, Workpaper
from backend.orchestration.runner import GraphRunner
from backend.services.workpapers import refresh
from tests.fakes import FakeEngagementStore, FakeOutputStore, make_procedure, make_runner
from tests.test_workpapers import rev08_outputs, rev08_run
from tests.xero_fakes import xero_engagement, xero_profile


async def d3_case() -> tuple[GraphRunner, FakeOutputStore, Workpaper]:
    profile, engagement = xero_profile(), xero_engagement()
    runner, store = make_runner(engagement_store=FakeEngagementStore([profile], [engagement]))
    await store.put(
        engagement.id, [make_procedure("REV-08", Phase.P2_Executing), *rev08_outputs(rev08_run(1))], actor="A6"
    )
    [paper] = [p for p in await refresh(runner.deps, engagement.id) if p.ref == "D-3"]
    return runner, store, paper


async def archived_copy(runner: GraphRunner, eid: str, *, include_workpapers: bool = True, trail=None):
    """复用已完成走通的只读材料另建存储, 测归档与补充不改共享结果、不再执行程序."""
    from dataclasses import replace

    from tests.fakes import FakeTrail, bind_stores

    original = runner.deps
    assert isinstance(original.output_store, FakeOutputStore)
    engagement = await original.engagement_store.get(eid)
    profile = await original.engagement_store.client_profile(engagement.client_id, engagement.client_profile_version)
    engagements = FakeEngagementStore([profile], [engagement])
    rows = [row for row in original.output_store.rows_of(eid) if include_workpapers or not isinstance(row, Workpaper)]
    store = FakeOutputStore(rows, eid=eid)
    bind_stores(engagements, store)
    copied_trail = trail or FakeTrail()
    copied_trail.output_store = store
    deps = replace(original, engagement_store=engagements, output_store=store, trail=copied_trail)
    return GraphRunner(runner.app, deps), store, copied_trail
