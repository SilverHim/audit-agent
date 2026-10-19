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
