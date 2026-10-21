"""需求 R258: Numbat 回归的全关守卫. 新功能全部接入后, Numbat 的 R55 (test_e2e_mock), R56 (同文件的真实存储变体),
R57 (test_e2e_restart), R59 (test_e2e_live) 都只开基线功能 (NUMBAT_FEATURES, 即 critic), 原有断言不改; 本文件守住
"全关" 本身: 各入口显式用基线功能集, 演示 (conftest 的 demo_run, 即 R55 那一次) 的程序包, 状态, 任务, 闸门决定与
参数都没有新功能的痕迹.

X-08 的新状态字段取接口文档第 3 节 "各组将加的状态字段" 表 (新加的行自动受查), 只查 EngagementState 已有的键.
"""

import inspect
from collections import Counter
from pathlib import Path
from typing import Any, get_type_hints

import pytest

from backend.contracts.models import GateDecision, Phase, ProcedureRun, ReviewTask, TaskKind
from backend.contracts.records import DatasetTraits
from backend.contracts.state import STATE_DEFAULTS, EngagementState, fill_defaults
from backend.core.config import get_settings
from backend.orchestration.gates import GATES
from backend.orchestration.runner import GraphRunner
from backend.persistence.stores import CsvDatasetLoader
from backend.services.features import BASELINE
from backend.services.procedure_library import ProcedureLibrary
from tests import test_e2e_mock
from tests.fakes import ENGAGEMENT, NUMBAT_FEATURES

EID = ENGAGEMENT.id
REPO = Path(__file__).resolve().parents[1]
DOC = REPO / "docs" / "开发接口文档.md"
shared_loop = pytest.mark.asyncio(loop_scope="session")  # 读会话共用的演示 (conftest 的 demo_run), 同 test_e2e_mock
P2_SEVEN = {"REV-08", "REV-03", "REV-02", "CSH-04", "DOC-06", "CSH-05", "CSH-02"}  # 原 7 条 P2 程序
LIBRARY = ProcedureLibrary(get_settings().KNOWLEDGE_ROOT, NUMBAT_FEATURES)


def x08_fields() -> list[str]:
    """接口文档第 3 节 "各组将加的状态字段" 表的字段名 (表头之后到表尾的各行第一列)."""
    text = DOC.read_text(encoding="utf-8")
    start = text.index("各组将加的状态字段")
    table = text[start:].split("\n\n", 2)[1]
    rows = [line for line in table.splitlines() if line.startswith("| ") and not line.startswith("| 字段")]
    return [line.split("|")[1].strip() for line in rows]


def test_numbat_regression_runs_baseline_only() -> None:
    """验收 5 (前提): Numbat 回归的功能集即基线 (只有 critic), R55, R57 的 start_demo 缺省用它; 演示终态冻结的功能集
    另由下一条断言 (版本快照为 critic)."""
    assert NUMBAT_FEATURES == BASELINE == frozenset({"critic"})
    assert inspect.signature(test_e2e_mock.start_demo).parameters["features"].default is NUMBAT_FEATURES


@shared_loop
async def test_numbat_demo_leaves_no_trace_of_new_features(demo_run: Any) -> None:
    """验收 5: 演示 (R55 那一次, 基线功能) 跑完后:
    - 程序包的 P2 程序只有原 7 条; 程序包与执行过的程序都没有挂功能名的 (模板 feature 为空, 基线的 critic 只管
      GEN-09, 它由 A6 同步调用, 不进程序包);
    - X-08 表中 EngagementState 已有的新状态字段终态都等于缺省值 (空列表或 None), 版本快照冻结的功能集为 critic;
    - 没有人工队列任务 (manual), 闸门决定只落在基线闸门 (登记表中不挂功能的): ACC-01, PLN-01/03, FIN-01 各一条,
      RPT-01 两条 (退回再批准)."""
    s = demo_run.s
    snapshot = await s.runner.app.aget_state(GraphRunner.config(EID))
    values = fill_defaults(snapshot.values)
    pack = values["procedure_pack"]
    assert {p.id for p in pack if p.phase is Phase.P2_Executing} == P2_SEVEN
    runs = await s.output_store.latest(EID, ProcedureRun)
    used = {p.id for p in pack} | {r.procedure_id for r in runs}
    assert {pid: LIBRARY.load(pid).feature for pid in used if LIBRARY.load(pid).feature is not None} == {}

    fields = x08_fields()
    hints = get_type_hints(EngagementState)
    present = [f for f in fields if f in hints]
    assert {"ownership_graph", "engagement_terms", "audit_plans", "correction_notices"} <= set(present), fields
    assert {f: values[f] for f in present if values[f] != STATE_DEFAULTS[f]} == {}
    assert values["version_snapshot"]["features"] == "critic"

    tasks = await s.output_store.latest(EID, ReviewTask)
    assert [t.id for t in tasks if t.kind is TaskKind.manual] == []
    baseline_gates = {spec.gate for spec in GATES if spec.feature is None}
    decisions = await s.output_store.latest(EID, GateDecision)
    assert all(d.gate in baseline_gates for d in decisions)
    assert Counter(d.gate.value for d in decisions) == {"ACC-01": 1, "PLN-01/03": 1, "FIN-01": 1, "RPT-01": 2}


@shared_loop
async def test_numbat_runs_on_default_dataset_traits(demo_run: Any) -> None:
    """验收 5: Numbat (data/demo) 没有 dataset_traits.json, CSV 载入器取缺省的 DatasetTraits; 演示中每次执行的参数
    快照里, 引用数据集声明 (dataset. 的 ref) 的参数都取缺省值 (如 CSH-02 的冲回格式为空, 不剔除作废对)."""
    assert not (REPO / "data" / "demo" / "dataset_traits.json").exists()
    assert CsvDatasetLoader(REPO / "data").dataset_traits("demo") == DatasetTraits()
    defaults = DatasetTraits()
    checked = []
    for run in await demo_run.s.output_store.latest(EID, ProcedureRun):
        for name, spec in LIBRARY.load(run.procedure_id).params.items():
            if not (isinstance(spec, dict) and str(spec.get("ref", "")).startswith("dataset.")):
                continue
            value: Any = defaults
            for part in spec["ref"].removeprefix("dataset.").split("."):
                value = getattr(value, part) if value is not None else None
            assert run.params_snapshot[name] == value, (run.id, name)
            checked.append((run.procedure_id, name))
    assert ("CSH-02", "reversal_reference_pattern") in checked and ("PLN-07", "prior_period_start") in checked
