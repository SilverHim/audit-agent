import os
from pathlib import Path

os.environ["CHECKPOINT_BACKEND"] = "memory"
os.environ["LLM_MODE"] = "mock"

# 测试固定读 tests/fixtures/llm, 不受 .env 的 LLM_FIXTURES_ROOT (运行用 data/demo/llm) 影响; 两处文档 ID 都取 manifest.
os.environ["LLM_FIXTURES_ROOT"] = str(Path(__file__).parent / "fixtures" / "llm")
