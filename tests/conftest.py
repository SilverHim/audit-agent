import os
from pathlib import Path

os.environ["CHECKPOINT_BACKEND"] = "memory"
os.environ["LLM_MODE"] = "mock"
# 功能开关固定为基线 (services/features.BASELINE), 不受开发者 .env 或环境里 FEATURES 的影响; 要别的功能集的用例显式传
os.environ["FEATURES"] = "critic"
# 演示默认数据集固定为缺省值 (core/config.py), 不受 .env 的 DEMO_DATASET 影响; GET /datasets 的 default 行按它
os.environ["DEMO_DATASET"] = "xero/20261017-q1"

# 测试固定读 tests/fixtures/llm, 不受 .env 的 LLM_FIXTURES_ROOT (运行用 data/demo/llm) 影响; 两处文档 ID 都取 manifest.
os.environ["LLM_FIXTURES_ROOT"] = str(Path(__file__).parent / "fixtures" / "llm")


import pytest


@pytest.fixture(scope="session", autouse=True)
def xero_originals_unchanged():
    """会话开始与结束各算一次 Xero 快照与补造原目录的逐文件哈希, 不同即失败 (只报文件名); 目录不存在时不核对
    (没有快照时只核对补造目录). 注入错误只能在副本上做 (决定 6)."""
    from tests.xero_injection import changed, originals

    before = originals()
    yield
    moved = changed(before, originals())
    assert not moved, "原目录被改动: " + ", ".join(moved)


@pytest.fixture
def xero_case(tmp_path):
    """注入场景的副本: xero_case(场景名或补丁, base=None, **选项) → XeroCase (见 tests/xero_injection.py).
    一个目录只能建一个副本; 同一用例要建多个 (如基线 T0 与注入场景成对断言) 时给不同的 base 子目录名."""
    from tests.xero_injection import xero_case as build

    return lambda scenario, base=None, **options: build(tmp_path / base if base else tmp_path, scenario, **options)


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    """调真实大模型的测试 (标 live) 须同时标 slow (需求 R114): 否则默认的 `-m 'not slow'` 也会收进来联网, 收集阶段
    即报错. 排在按标记筛选之前, 被筛掉的也查."""
    from tests.external import live_without_slow

    bad = live_without_slow(items)
    if bad:
        raise pytest.UsageError("标了 live 的测试须同时标 slow: " + ", ".join(bad))


@pytest.fixture(scope="session")
def live_session_budget(request):
    """整个会话的大模型用量预算 (需求 R115): 上限缺省为本次选中的各 live 测试上限之和, LIVE_SESSION_MAX_* 可调低;
    各测试的预算挂在它下面, 记账一并计入. 会话结束时有调用就写一份汇总."""
    from tests.tools.live_budget import Budget, session_limits, summary, write_summary

    budget = Budget(**session_limits(request.session.items))  # type: ignore[arg-type]
    yield budget
    if budget.used.calls:
        write_summary("live-session", summary([], budget))


@pytest.fixture
def live_budget(request, live_session_budget):
    """一个 live 测试的大模型用量预算: 上限取 @pytest.mark.live(max_calls=...) 的默认值, LIVE_MAX_* 覆盖. 测试自己没
    写汇总 (reported) 而有调用时, 结束时按包装记的账写一份 (没有留痕的尝试与失败级别)."""
    from tests.tools.live_budget import Budget, declared_limits, summary, write_summary

    marker = request.node.get_closest_marker("live")
    budget = Budget(**declared_limits(marker.kwargs if marker else {}), parent=live_session_budget)  # type: ignore[arg-type]
    yield budget
    if budget.used.calls and not budget.reported:
        write_summary(request.node.name, summary([], budget))
