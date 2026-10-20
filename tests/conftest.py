import os
from pathlib import Path

os.environ["CHECKPOINT_BACKEND"] = "memory"
os.environ["LLM_MODE"] = "mock"
# 功能开关固定为基线 (services/features.BASELINE), 不受开发者 .env 或环境里 FEATURES 的影响; 要别的功能集的用例显式传
os.environ["FEATURES"] = "critic"
# 演示默认数据集固定为缺省值 (core/config.py), 不受 .env 的 DEMO_DATASET 影响; GET /datasets 的 default 行按它
os.environ["DEMO_DATASET"] = "xero/20261017-q1"
# ABN 实时核查 (需求 R289) 一律未配置: 测试不调真实的 ABR ABN Lookup, 不受 .env 里 GUID 的影响; 要查的用例注入假接口
os.environ["ABN_LOOKUP_GUID"] = ""

# 测试固定读 tests/fixtures/llm, 不受 .env 的 LLM_FIXTURES_ROOT (运行用 data/demo/llm) 影响; 两处文档 ID 都取 manifest.
os.environ["LLM_FIXTURES_ROOT"] = str(Path(__file__).parent / "fixtures" / "llm")


import pytest
import pytest_asyncio


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def demo_run():
    """Numbat 回归 R55 的一次演示 (tests/test_e2e_mock.record_demo, 离线约半秒), 会话里只跑一次: test_e2e_mock 的用例读
    它的存储, 留痕与图状态 (用例标 loop_scope session, 与它同一个事件循环); test_collab_flow 与 test_e2e_xero17 只取它的
    留痕 (FakeTrail.entries, 普通列表) 比较状态迁移. 用例只读, 不改它."""
    from tests.test_e2e_mock import record_demo

    return await record_demo()


@pytest.fixture(scope="session", autouse=True)
def xero_originals_unchanged():
    """会话开始与结束各算一次 Xero 快照与补造原目录的逐文件哈希, 不同即失败 (只报文件名); 目录不存在时不核对
    (没有快照时只核对补造目录). 注入错误只能在副本上做 (决定 6)."""
    from tests.xero_injection import changed, originals

    before = originals()
    yield
    moved = changed(before, originals())
    assert not moved, "原目录被改动: " + ", ".join(moved)


# ── Xero 外部数据 (R294): 经 tests.external 定位核对, 没有时跳过, 跟踪了却找不到时失败; 用例经 external.needs 取 ──


@pytest.fixture
def q1_snapshot():
    """Xero 快照 20261017-q1 的目录."""
    from tests.external import snapshot_dir

    return snapshot_dir("20261017-q1")


@pytest.fixture
def mock_snapshot():
    """Xero 快照 20261017-mock (带付费接口模拟) 的目录."""
    from tests.external import snapshot_dir

    return snapshot_dir("20261017-mock")


@pytest.fixture
def q1_supplements():
    """Xero 演示的补造目录 data/xero_demo/20261017-q1."""
    from tests.external import supplements_dir

    return supplements_dir("20261017-q1")


@pytest.fixture
def xero_case(tmp_path):
    """注入场景的副本: xero_case(场景名或补丁, base=None, **选项) → XeroCase (见 tests/xero_injection.py).
    一个目录只能建一个副本; 同一用例要建多个 (如基线 T0 与注入场景成对断言) 时给不同的 base 子目录名."""
    from tests.xero_injection import xero_case as build

    return lambda scenario, base=None, **options: build(tmp_path / base if base else tmp_path, scenario, **options)


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    """调真实大模型的测试 (标 live) 须同时标 slow (需求 R114): 否则默认的 `-m 'not slow'` 也会收进来联网; 还须在
    标记里写用量上限 max_calls 并取 live_budget 夹具 (需求 R115), 否则跑起来没有预算. 收集阶段即报错, 排在按标记
    筛选之前, 被筛掉的也查."""
    from tests.external import live_without_slow
    from tests.tools.live_budget import live_without_limits

    bad = live_without_slow(items)
    if bad:
        raise pytest.UsageError("标了 live 的测试须同时标 slow: " + ", ".join(bad))
    unlimited = live_without_limits(items)
    if unlimited:
        raise pytest.UsageError(
            "标了 live 的测试须写上限 @pytest.mark.live(max_calls=n) 并取 live_budget 夹具: " + ", ".join(unlimited)
        )


@pytest.fixture(scope="session")
def live_session_budget(request):
    """整个会话的大模型用量预算 (需求 R115): 上限缺省为本次选中的各 live 测试上限之和, LIVE_SESSION_MAX_* 可调低;
    各测试的预算挂在它下面, 记账一并计入. 会话结束时有调用就写一份汇总. 主体在 tests/tools/live_budget."""
    from tests.tools.live_budget import finish, session_budget

    budget = session_budget(request.session.items)
    yield budget
    finish("live-session", budget)


@pytest.fixture
def live_budget(request, live_session_budget):
    """一个 live 测试的大模型用量预算: 上限取 @pytest.mark.live(max_calls=...) 的默认值, LIVE_MAX_* 覆盖. 测试自己没
    写汇总 (reported) 而有调用时, 结束时按包装记的账写一份 (没有留痕的尝试与失败级别)."""
    from tests.tools.live_budget import budget_for, finish

    budget = budget_for(request.node, live_session_budget)
    yield budget
    finish(request.node.name, budget)
