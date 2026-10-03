from datetime import date

import pytest
from jinja2 import Environment, FileSystemLoader

from backend.contracts.models import AuditObjective, Phase, Procedure, RiskLevel
from backend.contracts.state import initial_state
from backend.core.config import get_settings
from backend.orchestration.runner import fixed_procedures
from backend.orchestration.scheduler import build_schedule
from backend.services.procedure_library import ProcedureLibrary, highest_risk
from tests.fakes import ENGAGEMENT
from tests.fakes import NUMBAT_OBJECTIVES as OBJECTIVES

ROOT = get_settings().KNOWLEDGE_ROOT
P2 = {"REV-08", "REV-03", "REV-02", "CSH-04", "DOC-06", "CSH-05", "CSH-02"}


def test_all_procedures_load() -> None:
    assert len(ProcedureLibrary(ROOT).all()) == 17


def test_standards_referenced_are_registered() -> None:
    lib = ProcedureLibrary(ROOT)
    missing = {s for t in lib.all() for s in t.standards if lib.fragment(s) is None}
    assert not missing


def test_assembly_and_layers() -> None:
    lib = ProcedureLibrary(ROOT)
    matched = {t.id for o in OBJECTIVES for t in lib.match(o)}
    assembled = {t.id for t in lib.closure(matched) if t.phase is Phase.P2_Executing}
    assert assembled == P2

    pack = fixed_procedures(lib) + [
        Procedure(id=t.id, phase=t.phase, template_commit=lib.commit, autonomy_level=t.autonomy_level)
        for t in lib.all()
        if t.id in P2
    ]
    layers: dict[int, set[str]] = {}
    for row in build_schedule(pack, lib, cap=4):
        layers.setdefault(row.layer, set()).update(row.procedure_ids)
    assert layers == {0: {"REV-08", "REV-03", "CSH-04", "CSH-05"}, 1: {"REV-02", "DOC-06"}, 2: {"CSH-02"}}


def test_prompts_parse() -> None:
    env = Environment(loader=FileSystemLoader(ROOT / "prompts"))
    for name in env.list_templates():
        env.get_template(name)


# ── R01 参数解析: 风险等级取最高, by_risk 与 ref 取不到值时报错 ──


@pytest.mark.parametrize(
    ("lower", "higher"),
    [("low", "medium"), ("medium", "high"), ("high", "significant"), ("low", "significant")],
)
def test_highest_risk_adjacent_levels(lower: str, higher: str) -> None:
    """每对相邻等级都比一次, 两种排列顺序结果相同: 顺序写错任何一处都会失败."""
    objectives = [
        AuditObjective(id="O-1", account="Revenue", assertion="Occurrence", risk_level=lower, is_material=True),
        AuditObjective(id="O-2", account="Revenue", assertion="Cutoff", risk_level=higher, is_material=True),
    ]

    assert highest_risk(objectives, ["O-1", "O-2"]) == higher
    assert highest_risk(objectives, ["O-2", "O-1"]) == higher


def test_highest_risk_takes_max_of_linked_objectives() -> None:
    """按风险高低比, 不按字母序 (字母序下 medium 最大); 未关联的特别风险目标不算."""
    objectives = [
        AuditObjective(id="O-LOW", account="Revenue", assertion="Occurrence", risk_level="low", is_material=False),
        AuditObjective(id="O-HIGH", account="Revenue", assertion="Cutoff", risk_level="high", is_material=True),
        AuditObjective(id="O-MED", account="Cash", assertion="Existence", risk_level="medium", is_material=True),
        AuditObjective(id="O-SIG", account="Cash", assertion="Existence", risk_level="significant", is_material=True),
    ]

    assert highest_risk(objectives, ["O-LOW", "O-HIGH", "O-MED"]) is RiskLevel.high
    assert highest_risk(objectives, []) is None
    with pytest.raises(LookupError, match="O-MISSING"):
        highest_risk(objectives, ["O-LOW", "O-MISSING"])


def test_resolve_params_by_risk_needs_risk_level() -> None:
    """REV-08 的 window_days 按风险取值, 没有风险等级就报错; CSH-04 没有 by_risk 参数, 不需要风险等级."""
    lib = ProcedureLibrary(ROOT)
    state = initial_state(ENGAGEMENT, [], 1)

    with pytest.raises(ValueError, match=r"REV-08\.window_days"):
        lib.resolve_params(lib.load("REV-08"), ENGAGEMENT, state, None)
    assert lib.resolve_params(lib.load("CSH-04"), ENGAGEMENT, state, None) == {"as_of": date(2026, 6, 30)}


def test_resolve_params_by_risk_without_that_level() -> None:
    """REV-02 只给了 medium 以上的取值, 低风险时报错而不是 KeyError."""
    lib = ProcedureLibrary(ROOT)

    with pytest.raises(ValueError, match="low"):
        lib.resolve_params(lib.load("REV-02"), ENGAGEMENT, initial_state(ENGAGEMENT, [], 1), RiskLevel.low)


def test_resolve_params_state_ref_not_set() -> None:
    """重要性还没经闸门确定时执行 FIN-04: 报错, 不以 None 作参数."""
    lib = ProcedureLibrary(ROOT)

    with pytest.raises(ValueError, match="materiality_cents"):
        lib.resolve_params(lib.load("FIN-04"), ENGAGEMENT, initial_state(ENGAGEMENT, [], 1), None)


def test_resolve_params_literals_are_copies() -> None:
    """改解析结果里的列表与字典, 模板不受影响."""
    lib = ProcedureLibrary(ROOT)

    out = lib.resolve_params(lib.load("PLN-01"), ENGAGEMENT, initial_state(ENGAGEMENT, [], 1), None)
    out["candidates"][0]["pct_low"] = 0
    out["candidates"].append({"basis": "x"})

    candidates = lib.load("PLN-01").params["candidates"]
    assert len(candidates) == 3
    assert candidates[0]["pct_low"] == 0.005
