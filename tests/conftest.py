import os
from pathlib import Path

os.environ["CHECKPOINT_BACKEND"] = "memory"
os.environ["LLM_MODE"] = "mock"

# Unit-test document IDs are synthetic; runtime demo fixtures use real PDF hashes.
os.environ["LLM_FIXTURES_ROOT"] = str(Path(__file__).parent / "fixtures" / "llm")
