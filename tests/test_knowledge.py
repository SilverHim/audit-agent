from jinja2 import Environment, FileSystemLoader

from backend.contracts.models import AuditObjective, Phase, Procedure
from backend.core.config import get_settings
from backend.orchestration.runner import fixed_procedures
from backend.orchestration.scheduler import build_schedule
from backend.services.procedure_library import ProcedureLibrary

ROOT = get_settings().KNOWLEDGE_ROOT
OBJECTIVES = [
    AuditObjective(
        id="OBJ-REV-OCC", account="Revenue", assertion="Occurrence", risk_level="significant", is_material=True
    ),
    AuditObjective(id="OBJ-REV-CUT", account="Revenue", assertion="Cutoff", risk_level="significant", is_material=True),
    AuditObjective(id="OBJ-CASH-EXI", account="Cash", assertion="Existence", risk_level="high", is_material=True),
]
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
