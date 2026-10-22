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
    [paper] = await refresh(runner.deps, engagement.id)
    return runner, store, paper
