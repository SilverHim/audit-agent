"""A5 GEN-08 股权穿透与关系图 (需求 R137, R138): 查询顺序与留痕, 人员检索对象, 登记 PDF 存档, 关系图, R01 例外;
按留痕幂等重建, 失败路径, R02 穿透中断, 重跑与过期重跑, 进程中断后的恢复 (R138, 见文末一节).

Xero 用例的登记取补造目录 registry/ (xero_registry, 只读补造文件, 不改任何文件); 向上穿透, 没有 ACN 的法人股东与
按名称比对申报清单另用合成的假登记 (FakeRegistry).
"""

import asyncio
import json
from dataclasses import replace
from datetime import date
from typing import Any

import pytest

from backend.agents.base import RunContext
from backend.agents.evidence import EvidenceAgent
from backend.agents.ownership import declared
from backend.agents.registry import build_agents
from backend.contracts.data import DataChange, DataInput
from backend.contracts.errors import RegistryUnavailable
from backend.contracts.models import (
    AbnRecord,
    AgentId,
    AsicExtract,
    ClientProfile,
    Decision,
    DeclaredHolding,
    DeclaredParty,
    DocumentType,
    Engagement,
    EvidenceType,
    Officeholder,
    OwnershipGraph,
    PersonRole,
    PersonSearch,
    Phase,
    ProcedureStatus,
    Reliability,
    RunStatus,
    Shareholding,
    TaskKind,
    TrailEntry,
    TrailKind,
    organisation_node_id,
)
from backend.contracts.state import StatePatch, fill_defaults
from backend.orchestration.merge import check_patch
from backend.orchestration.nodes import GraphNodes, apply_patch
from backend.orchestration.runner import GraphRunner
from backend.services.staleness import current_results, stale_procedures
from tests.external import needs
from tests.fakes import (
    ENGAGEMENT,
    NUMBAT_ABN,
    NUMBAT_ASIC,
    NUMBAT_FEATURES,
    NUMBAT_PROFILE,
    FakeDataStore,
    FakeEngagementStore,
    FakeFinancialStore,
    FakeRegistry,
    FakeTrail,
    make_ctx,
    make_deps,
    make_procedure,
    make_run,
    wait_idle,
)
from tests.interrupts import kill
from tests.test_e2e_mock import start_demo
from tests.test_e2e_restart import restart
from tests.xero_fakes import (
    XERO_DATASET,
    XeroFakeEvidenceStore,
    manifest_documents,
    supplement,
    xero_profile,
    xero_registry,
)

FEATURES = frozenset({"critic", "related_parties"})
EID = ENGAGEMENT.id
ALEX = "PER-MORGAN-ALEX-19790412"
DEMO, VPA, ADVISORY, SMART = "ACN-900043021", "ACN-900043030", "ACN-900043049", "ACN-900043076"


async def run_gen08(
    registry: FakeRegistry,
    profile: ClientProfile,
    params: dict[str, Any] | None = None,
    *,
    xero: bool = False,
    profiles: list[ClientProfile] | None = None,
    trail: FakeTrail | None = None,
    financial: FakeFinancialStore | None = None,
    run_no: int = 1,
    state: dict[str, Any] | None = None,
) -> tuple[StatePatch, RunContext, FakeTrail, Any]:
    """以 profile 为客户的项目 (采用 profile 那一版; 存储中的画像为 profiles, 缺省只有它) 执行 GEN-08 第 run_no 次
    (功能 related_parties 打开); 参数快照另按 params 改, 状态另按 state 改 (如带上之前执行的关系图与例外); 片段经
    check_patch 校验 (A5, 本程序的 outputs). xero 时项目带 Xero 数据集, 证据存储用 XeroFakeEvidenceStore (补造 PDF 的
    页数取 manifest). trail 与 financial 给出时沿用 (之前执行的留痕与已登记的文件; 执行抛错时也能查). 返回片段,
    上下文, 留痕与依赖."""
    trail = trail if trail is not None else FakeTrail()
    engagement: Engagement = ENGAGEMENT.model_copy(
        update={
            "client_id": profile.id,
            "client_profile_version": profile.version,
            **({"dataset": XERO_DATASET} if xero else {}),
        }
    )
    financial = financial if financial is not None else FakeFinancialStore()
    store = FakeEngagementStore(profiles or [profile], [engagement])
    deps = make_deps(registry, trail, store, financial_store=financial, features=FEATURES)
    if xero:
        deps = replace(deps, evidence_store=XeroFakeEvidenceStore(financial))
    ctx = make_ctx(deps, "GEN-08", engagement)
    run = make_run("GEN-08", run_no, {**ctx.run.params_snapshot, **(params or {})})
    ctx = RunContext(ctx.engagement, ctx.procedure, ctx.template, run, {**ctx.state, **(state or {})})  # type: ignore[typeddict-item]
    patch = await EvidenceAgent(deps).run(ctx)
    check_patch(
        patch, writer=AgentId.A5, procedure_id="GEN-08", run_id=run.id, state=ctx.state, outputs=ctx.template.outputs
    )
    return patch, ctx, trail, deps


def calls(trail: FakeTrail) -> list[Any]:
    return [e for e in trail.entries if e.kind is TrailKind.external_call]


def links_of(patch: StatePatch) -> list[tuple[str, str, str, int | None, float | None]]:
    return [(x.from_id, x.to_id, x.relation, x.shares, x.pct) for x in patch["ownership_graph"].links]


# ── 程序定义与分派 ──


def test_gen08_template_and_dispatch() -> None:
    """验收 1, 8: GEN-08.yaml 的各项; 只在功能 related_parties 打开时启用, 映射到 EvidenceAgent (CSH-04 照旧);
    EvidenceAgent 负责 CSH-04 与 GEN-08."""
    deps = make_deps(FakeRegistry(), FakeTrail(), features=FEATURES)
    t = deps.library.load("GEN-08")
    assert (t.owner, t.engine, t.autonomy_level, t.cycle, t.fixed, t.feature) == (
        AgentId.A5,
        "rule",
        "L4",
        "Pervasive",
        True,
        "related_parties",
    )
    assert t.outputs == ["ownership_graph", "exceptions", "documents"]
    assert t.document_types == [DocumentType.registry_extract]
    assert t.params == {
        "max_depth": 3,
        "person_hops": 1,
        "significant_pct": 0.2,
        "entity_suffixes": ["pty ltd", "pty limited", "pty", "ltd", "limited"],
    }
    assert (t.standards, t.rules) == (["ASA550-P15"], [])
    assert {"CSH-04", "GEN-08"} <= EvidenceAgent.procedures  # 集合包含: A5 另有 GEN-07 (需求 R177)
    agents = build_agents(deps)
    assert isinstance(agents["GEN-08"], EvidenceAgent) and agents["GEN-08"] is agents["CSH-04"]
    assert "GEN-08" in {x.id for x in deps.library.fixed()}
    baseline = make_deps(FakeRegistry(), FakeTrail())
    assert not baseline.library.enabled("GEN-08") and "GEN-08" not in build_agents(baseline)


# ── Xero 演示数据 (补造目录的登记) ──


@needs("q1_supplements")
async def test_xero_penetration_order_and_trail() -> None:
    """验收 2, 3: 查询依次为 ABN, 摘录 900043021, 人员 Morgan Alex 1979-04-12, 摘录 900043030, 900043049, 每次以 eid
    为首参; external_call 5 行, 幂等键依次如下; payload 为查询参数, 结构化结果 (与补造目录的 JSON 逐键相同, 不含 PDF
    原件) 与文件 ID (ABN 查询没有). Sam Whitfield (15%, 不任职, 无出生日期) 不检索. 摘要不带出生日期 (ISO 与
    yyyymmdd 两种写法都查) 与检索对象的姓名: 人员检索那行只写检索序号 (审核意见 R137-01)."""
    registry = xero_registry()
    _, _, trail, _ = await run_gen08(registry, xero_profile(), xero=True)

    assert registry.queries == [
        ("abn", "25900043021"),
        ("extract", "900043021"),
        ("person", "Morgan Alex 1979-04-12"),
        ("extract", "900043030"),
        ("extract", "900043049"),
    ]
    assert set(registry.eids) == {EID}
    rows = calls(trail)
    assert [r.idempotency_key for r in rows] == [
        f"{EID}:GEN-08:abn:25900043021",
        f"{EID}:GEN-08:extract:900043021",
        f"{EID}:GEN-08:person:MORGAN-ALEX-19790412",
        f"{EID}:GEN-08:extract:900043030",
        f"{EID}:GEN-08:extract:900043049",
    ]
    assert {(r.actor, r.procedure_id, r.run_id) for r in rows} == {(AgentId.A5, "GEN-08", "RUN-GEN-08-1")}
    assert rows[2].summary == "ASIC person search #1"
    personal = ("1979-04-12", "19790412", "morgan", "alex")  # 出生日期两种写法与姓名 (不分大小写)
    assert not [r.summary for r in rows if any(p in r.summary.lower() for p in personal)]
    root = supplement().root / "registry"
    abn = rows[0].payload
    assert abn == {"abn": "25900043021", "result": json.loads((root / "abn.json").read_text())["25900043021"]}
    documents = {d["path"]: i for i, d in manifest_documents().items() if d["registration"] == "registry"}
    for row, path in zip(
        rows[1:],
        [
            "extracts/900043021",
            "persons/MORGAN-ALEX-19790412",
            *(f"extracts/{acn}" for acn in ("900043030", "900043049")),
        ],
        strict=True,
    ):
        source = json.loads((root / f"{path}.json").read_text())
        result = row.payload["result"]
        assert {k: result[k] for k in source} == source and "file_bytes" not in result
        assert row.payload["document_id"] == documents[f"registry/{path}.pdf"]
    assert {k: v for k, v in rows[2].payload.items() if k not in ("result", "document_id")} == {
        "family_name": "Morgan",
        "given_name": "Alex",
        "birth_date": "1979-04-12",
    }
    assert rows[1].payload["acn"] == "900043021"


@needs("q1_supplements")
async def test_xero_archives_four_registry_documents() -> None:
    """验收 4: 3 份摘录与 1 份人员检索经 EvidenceStore.save 存档 (registry_extract, external_original), 文件 ID 即
    manifest.documents 中登记途径为 registry 的 4 份; 片段 documents 4 条, 状态 extracted, 渠道 registry (程序向
    登记机构查询后存档, 需求 R306), 没有上传人."""
    patch, _, _, deps = await run_gen08(xero_registry(), xero_profile(), xero=True)
    expected = {i for i, d in manifest_documents().items() if d["registration"] == "registry"}
    assert {d.id for d in patch["documents"]} == expected and len(patch["documents"]) == 4
    assert {(d.type, d.reliability, d.status, d.channel, d.uploaded_by) for d in patch["documents"]} == {
        (DocumentType.registry_extract, Reliability.external_original, "extracted", "registry", None)
    }
    assert {(t, r) for _, _, t, r in deps.evidence_store.save_args} == {
        (DocumentType.registry_extract, Reliability.external_original)
    }


@needs("q1_supplements")
async def test_xero_ownership_graph() -> None:
    """验收 5: OWNERSHIP v1 (RUN-GEN-08-1, 报告主体 ACN-900043021, 带画像 ABN): 节点 6 (按 ID), 关系 10 (只取公司
    摘录; Dana 的股东行并入其高管节点 PER-MORGAN-DANA-19811103); 每条关系的证据为该摘录的 document 指针 (高管第 1 页,
    股东第 2 页); 只有 Alex 检索过, 三家公司都取得了摘录."""
    patch, _, _, _ = await run_gen08(xero_registry(), xero_profile(), xero=True)
    graph = patch["ownership_graph"]
    assert (graph.id, graph.version, graph.run_id, graph.reporting_entity_id) == ("OWNERSHIP", 1, "RUN-GEN-08-1", DEMO)
    dana, sam = "PER-MORGAN-DANA-19811103", "PER-WHITFIELD-SAM-NODOB"
    assert [n.id for n in graph.nodes] == [DEMO, VPA, ADVISORY, ALEX, dana, sam]
    nodes = {n.id: n for n in graph.nodes}
    assert {i for i, n in nodes.items() if n.searched} == {DEMO, VPA, ADVISORY, ALEX}
    assert (nodes[DEMO].abn, nodes[VPA].abn, nodes[VPA].name) == (
        "25900043021",
        None,
        "Victoria Property Agency Pty Ltd",
    )
    assert (nodes[dana].birth_date, nodes[sam].birth_date) == (date(1981, 11, 3), None)
    assert links_of(patch) == [
        (ALEX, DEMO, "director", None, None),
        (ALEX, DEMO, "secretary", None, None),
        (ALEX, DEMO, "shareholder", 85, 0.85),
        (sam, DEMO, "shareholder", 15, 0.15),
        (dana, VPA, "director", None, None),
        (ALEX, VPA, "director", None, None),
        (dana, VPA, "shareholder", 50, 0.5),
        (ALEX, VPA, "shareholder", 50, 0.5),
        (ALEX, ADVISORY, "director", None, None),
        (ALEX, ADVISORY, "shareholder", 100, 1.0),
    ]
    by_path = {d["path"]: i for i, d in manifest_documents().items()}
    vpa_doc = by_path["registry/extracts/900043030.pdf"]
    assert [(x.evidence.source_id, x.evidence.page, x.evidence.locator) for x in graph.links[4:8]] == [
        (vpa_doc, 1, "officeholders[0]"),
        (vpa_doc, 1, "officeholders[1]"),
        (vpa_doc, 2, "shareholders[0]"),
        (vpa_doc, 2, "shareholders[1]"),
    ]
    assert {(x.evidence.type, x.evidence.source_type, x.evidence.reliability) for x in graph.links} == {
        (EvidenceType.external, "document", Reliability.external_original)
    }
    assert graph.links[0].appointed == date(2018, 9, 3)


@needs("q1_supplements")
async def test_xero_undeclared_related_company() -> None:
    """验收 6: 只有 Victoria Property Agency 出 R01 (high, detail 如下, 证据为人员检索第 1 页与 VPA 摘录第 1 页);
    Morgan Advisory 按申报清单的 acn 匹配 (清单只给 abn 时按其后 9 位同样匹配), 不出例外; 执行成功, 不建任务."""
    patch, _, _, _ = await run_gen08(xero_registry(), xero_profile(), xero=True)
    [found] = patch["exceptions"]
    assert (found.id, found.rule_id, found.severity, found.source_ids, found.amount_cents) == (
        "X-GEN-08-1-R01-1",
        "R01",
        "high",
        [VPA],
        None,
    )
    assert found.detail == {
        "name": "Victoria Property Agency Pty Ltd",
        "acn": "900043030",
        "via": ALEX,
        "via_relations": ["director", "shareholder 0.5"],
        "declared": False,
        "profile_version": 1,
    }
    by_path = {d["path"]: i for i, d in manifest_documents().items()}
    assert [(e.source_id, e.page) for e in found.evidence] == [
        (by_path["registry/persons/MORGAN-ALEX-19790412.pdf"], 1),
        (by_path["registry/extracts/900043030.pdf"], 1),
    ]
    [run] = patch["procedure_runs"]
    assert run.status is RunStatus.succeeded and "review_tasks" not in patch

    profile = xero_profile()
    by_abn = [p.model_copy(update={"acn": None}) if p.kind == "company" else p for p in profile.related_parties]
    patch, _, _, _ = await run_gen08(xero_registry(), profile.model_copy(update={"related_parties": by_abn}), xero=True)
    assert [x.source_ids for x in patch["exceptions"]] == [[VPA]]


@needs("q1_supplements")
async def test_t6_isomorphic_case_with_fake_registry() -> None:
    """验收 7 (同构于注入场景 T6, 不改任何文件): 假登记加 SMART Agency Pty Ltd (ACN 900043076, Alex 任董事并持 40,
    Casey Turner 持 60), Alex 的人员检索多两项: 节点 8, 关系 13, external_call 6, registry_extract 5 份, R01 多一条."""
    registry = xero_registry()
    smart = AsicExtract.model_validate(
        {
            **registry.extracts["900043049"].model_dump(by_alias=True),
            "acn": "900043076",
            "name": "SMART Agency Pty Ltd",
            "registered_date": date(2021, 2, 1),
            "officeholders": [
                Officeholder(
                    name="Alex Morgan", birth_date=date(1979, 4, 12), role="director", appointed=date(2021, 2, 1)
                )
            ],
            "shareholders": [
                Shareholding(name="Alex Morgan", kind="person", acn=None, share_class="ORD", shares=40, pct=0.4),
                Shareholding(name="Casey Turner", kind="person", acn=None, share_class="ORD", shares=60, pct=0.6),
            ],
            "file_name": "900043076.pdf",
            "file_bytes": b"%PDF-1.4 synthetic SMART Agency extract",
        }
    )
    registry.extracts["900043076"] = smart
    [alex] = registry.persons
    extra = [
        PersonRole(acn="900043076", company_name="SMART Agency Pty Ltd", role=r, appointed=a, shares=s)
        for r, a, s in (("director", date(2021, 2, 1), None), ("shareholder", None, 40))
    ]
    registry.persons = [alex.model_copy(update={"roles": [*alex.roles, *extra]})]

    patch, _, trail, _ = await run_gen08(registry, xero_profile(), xero=True)

    graph = patch["ownership_graph"]
    assert len(graph.nodes) == 8 and {SMART, "PER-TURNER-CASEY-NODOB"} <= {n.id for n in graph.nodes}
    assert len(graph.links) == 13
    assert links_of(patch)[10:] == [
        (ALEX, SMART, "director", None, None),
        (ALEX, SMART, "shareholder", 40, 0.4),
        ("PER-TURNER-CASEY-NODOB", SMART, "shareholder", 60, 0.6),
    ]
    assert len(calls(trail)) == 6 and calls(trail)[-1].idempotency_key == f"{EID}:GEN-08:extract:900043076"
    assert len(patch["documents"]) == 5
    assert [x.source_ids for x in patch["exceptions"]] == [[VPA], [SMART]]
    assert patch["exceptions"][1].id == "X-GEN-08-1-R01-2"
    assert patch["exceptions"][1].detail["via_relations"] == ["director", "shareholder 0.4"]


# ── 合成的假登记: 向上穿透, 没有 ACN 的法人股东, 按名称比对申报清单 ──

ENTITY_ACN, PARENT_ACN, GRAND_ACN = "900000017", "900000026", "900000035"
PDF = b"%PDF-1.4 synthetic extract "


def extract(acn: str, name: str, officers: list[Officeholder], holders: list[Shareholding]) -> AsicExtract:
    return AsicExtract.model_validate(
        {
            "acn": acn,
            "name": name,
            "type": "APTY",
            "class": "LMSH",
            "subclass": "PROP",
            "status": "Registered",
            "registered_date": date(2015, 1, 5),
            "officeholders": officers,
            "shareholders": holders,
            "paid_up_cents": None,
            "file_name": f"{acn}.pdf",
            "file_bytes": PDF + acn.encode(),
        }
    )


def chain_registry() -> FakeRegistry:
    """报告主体 Bilby Tools Pty Ltd: 母公司 Quoll Holdings Pty Ltd 持 60%, 外国法人 Kiwi Holdings Ltd (没有 ACN) 持
    40%; 母公司的董事 Riley Shaw (有出生日期), 其上又有 Galah Group Pty Ltd 全资持股. 都是合成的名称与号码."""
    riley = Officeholder(name="Riley Shaw", birth_date=date(1970, 5, 6), role="director", appointed=date(2015, 1, 5))
    entity = extract(
        ENTITY_ACN,
        "Bilby Tools Pty Ltd",
        [Officeholder(name="Jo Park", birth_date=None, role="secretary", appointed=date(2015, 1, 5))],
        [
            Shareholding(
                name="Quoll Holdings Pty Ltd", kind="company", acn=PARENT_ACN, share_class="ORD", shares=60, pct=0.6
            ),
            Shareholding(name="Kiwi Holdings Ltd", kind="company", acn=None, share_class="ORD", shares=40, pct=0.4),
        ],
    )
    parent = extract(
        PARENT_ACN,
        "Quoll Holdings Pty Ltd",
        [riley],
        [Shareholding(name="Galah Group Pty Ltd", kind="company", acn=GRAND_ACN, share_class="ORD", shares=1, pct=1.0)],
    )
    return FakeRegistry(
        {
            NUMBAT_PROFILE.abn: AbnRecord(
                abn=NUMBAT_PROFILE.abn,
                acn=ENTITY_ACN,
                entity_name="Bilby Tools Pty Ltd",
                status="Active",
                entity_type="Australian Private Company",
            )
        },
        extracts={ENTITY_ACN: entity, PARENT_ACN: parent},
    )


async def test_upward_penetration_and_name_matching() -> None:
    """向上穿透 (max_depth 改为 1): 查询为 ABN, 主体摘录, 母公司摘录, 再检索母公司链上有出生日期的高管 Riley Shaw
    (查不到); 秘书 Jo Park 没有出生日期不检索. 没有 ACN 的 Kiwi Holdings 与超过 max_depth 的 Galah Group 建节点
    (searched 为假), 不查询. 申报清单: 母公司按 abn 后 9 位匹配, Kiwi 按名称匹配 (party_name_key 去后缀,
    "Kiwi Holdings Limited" 与 "Kiwi Holdings Ltd" 同键); 只有 Galah Group 出 R01, via 为母公司, 关系为母公司的
    股东行. R02 (需求 R138): Kiwi 没有 ACN, Galah 达到 max_depth, 各一条; 检索查不到的个人不出 R02."""
    registry = chain_registry()
    kiwi = organisation_node_id("Kiwi Holdings Ltd")
    profile = NUMBAT_PROFILE.model_copy(
        update={
            "related_parties": [
                DeclaredParty(name="Quoll Holdings", kind="company", relationship="parent", abn=f"11{PARENT_ACN}")
            ],
            "ownership": [DeclaredHolding(holder="Kiwi Holdings Limited", kind="company", shares=40, pct=0.4)],
        }
    )
    patch, ctx, trail, _ = await run_gen08(registry, profile, {"max_depth": 1})

    assert registry.queries == [
        ("abn", NUMBAT_PROFILE.abn),
        ("extract", ENTITY_ACN),
        ("extract", PARENT_ACN),
        ("person", "Shaw Riley 1970-05-06"),
    ]
    assert [r.payload["result"] for r in calls(trail)][-1] is None
    graph = patch["ownership_graph"]
    nodes = {n.id: n for n in graph.nodes}
    grand = f"ACN-{GRAND_ACN}"
    assert kiwi == "ORG-KIWIHOLDINGSLTD"
    assert {i: n.searched for i, n in nodes.items() if n.kind == "company"} == {
        f"ACN-{ENTITY_ACN}": True,
        f"ACN-{PARENT_ACN}": True,
        kiwi: False,
        grand: False,
    }
    assert nodes["PER-SHAW-RILEY-19700506"].searched is False and nodes[kiwi].acn is None
    assert (kiwi, f"ACN-{ENTITY_ACN}", "shareholder", 40, 0.4) in links_of(patch)
    assert (grand, f"ACN-{PARENT_ACN}", "shareholder", 1, 1.0) in links_of(patch)
    [found] = [x for x in patch["exceptions"] if x.rule_id == "R01"]
    assert [x.rule_id for x in patch["exceptions"]] == ["R01", "R02", "R02"]  # R02 排在 R01 之后
    assert [
        (x.id, x.severity, x.source_ids, x.detail["reason"]) for x in patch["exceptions"] if x.rule_id == "R02"
    ] == [
        ("X-GEN-08-1-R02-1", "medium", [kiwi], "没有 ACN"),
        ("X-GEN-08-1-R02-2", "medium", [grand], "达到 max_depth"),
    ]
    kiwi_r02 = patch["exceptions"][1]
    assert kiwi_r02.detail == {
        "name": "Kiwi Holdings Ltd",
        "acn": None,
        "via": f"ACN-{ENTITY_ACN}",
        "reason": "没有 ACN",
    }
    assert patch["exceptions"][2].detail == {
        "name": "Galah Group Pty Ltd",
        "acn": GRAND_ACN,
        "via": f"ACN-{PARENT_ACN}",
        "reason": "达到 max_depth",
    }
    assert found.source_ids == [grand]
    assert {k: found.detail[k] for k in ("acn", "via", "via_relations")} == {
        "acn": GRAND_ACN,
        "via": f"ACN-{PARENT_ACN}",
        "via_relations": ["shareholder 1"],
    }
    parent_doc = next(d for d in patch["documents"] if d.file_name == f"{PARENT_ACN}.pdf")
    assert [(e.source_id, e.page, e.locator) for e in found.evidence] == [(parent_doc.id, 2, "shareholders[0]")]
    assert len(patch["documents"]) == 2 and ctx.run.params_snapshot["max_depth"] == 1


async def test_client_without_acn_fails_after_recording_the_lookup() -> None:
    """客户 ABN 查不到, 或登记没有 ACN (不是公司): 写完 ABN 那行留痕后抛 LookupError, 不再查询 (A1 按执行失败处理,
    见 test_client_not_penetrable_becomes_failed_run_and_rerun_task). 留痕先写 (审核意见 R137-02): 那一行的 result
    为查到的记录或空."""
    no_acn = chain_registry().abn_records[NUMBAT_PROFILE.abn].model_copy(update={"acn": None})
    not_found = {"result": None, "success": False}  # 查不到记 success 为假, 之后的执行再查 (需求 R138)
    for records, result in (
        ({}, not_found),
        ({NUMBAT_PROFILE.abn: no_acn}, {"result": no_acn.model_dump(mode="json")}),
    ):
        registry, trail = FakeRegistry(records), FakeTrail()
        with pytest.raises(LookupError, match="无从穿透"):
            await run_gen08(registry, NUMBAT_PROFILE, trail=trail)
        assert registry.queries == [("abn", NUMBAT_PROFILE.abn)]
        [row] = calls(trail)
        assert row.payload == {"abn": NUMBAT_PROFILE.abn, **result}
        assert row.idempotency_key == f"{EID}:GEN-08:abn:{NUMBAT_PROFILE.abn}"


SUFFIXES = ["pty ltd", "pty limited", "pty", "ltd", "limited"]  # GEN-08.yaml 的 entity_suffixes


@pytest.mark.parametrize(
    ("holder", "name", "matched"),
    [
        ("有限会社", "株式会社", False),  # 只有非拉丁字母: 两边都没有键
        ("Pty Ltd", "Limited", False),  # 只剩公司后缀
        ("有限会社 Pty Ltd", "株式会社 Pty Ltd", False),  # 非拉丁名加后缀, 去后缀后为空
        ("Kiwi Holdings Ltd", "KIWI HOLDINGS PTY LTD", True),  # 有键的照常比, 去后缀
        ("Café Holdings Pty Ltd", "CAFE HOLDINGS PTY LTD", True),  # 去附加符后相同
    ],
)
def test_names_without_a_key_never_match(holder: str, name: str, matched: bool) -> None:
    """按名称比对申报清单时, 规范化后为空的名称 (party_name_key 为 None: 只有非拉丁字母, 或只剩公司后缀) 不参与
    比对, 两个这样的名称不算相等; 有键的照常比 (去附加符, 去后缀). 匹配时返回那一项, 否则为 None."""
    item = DeclaredHolding(holder=holder, kind="company", shares=1, pct=0.1)
    profile = NUMBAT_PROFILE.model_copy(update={"ownership": [item]})
    assert declared(profile, name, "900000044", SUFFIXES) == (item if matched else None)


@pytest.mark.parametrize(
    ("party", "matched"),
    [
        (DeclaredParty(name="Other Name", kind="company", relationship="sister", acn="900000044"), True),  # 只给 acn
        (DeclaredParty(name="Same Name Pty Ltd", kind="company", relationship="sister", acn="900000053"), False),
        (DeclaredParty(name="Same Name Pty Ltd", kind="company", relationship="sister", abn="11900000053"), False),
    ],
    ids=["acn_only", "acn_differs_same_name", "abn_differs_same_name"],
)
def test_declared_numbers_take_priority_over_names(party: DeclaredParty, matched: bool) -> None:
    """审核意见 R137-02: 申报项只给 acn 时按 acn 匹配 (名称不同也算); 申报项有 acn 或 abn 而号码不同时不再比名称,
    同名也算未申报. 匹配时返回那一项 (GEN-03 复用, 需求 R139)."""
    profile = NUMBAT_PROFILE.model_copy(update={"related_parties": [party], "ownership": []})
    assert declared(profile, "Same Name Pty Ltd", "900000044", SUFFIXES) == (party if matched else None)


@needs("q1_supplements")
async def test_declared_check_uses_the_adopted_profile_version() -> None:
    """审核意见 R137-02 (验收 6): 申报比对用本项目采用的那一版画像 (v1), 不用之后的 v2 (v2 申报了 VPA): VPA 仍出
    R01, profile_version 为 1."""
    v1 = xero_profile()
    vpa = DeclaredParty(name="Victoria Property Agency Pty Ltd", kind="company", relationship="x", acn="900043030")
    v2 = v1.model_copy(update={"version": 2, "related_parties": [*v1.related_parties, vpa]})
    patch, ctx, _, _ = await run_gen08(xero_registry(), v1, xero=True, profiles=[v1, v2])
    assert ctx.state["client_profile_version"] == 1
    assert [(x.source_ids, x.detail["profile_version"]) for x in patch["exceptions"]] == [([VPA], 1)]


async def test_secretary_with_birth_date_is_searched() -> None:
    """审核意见 R137-02 (验收 3 的高管含秘书): 报告主体的秘书有出生日期时同样检索."""
    registry = chain_registry()
    entity = registry.extracts[ENTITY_ACN]
    jo = Officeholder(name="Jo Park", birth_date=date(1988, 8, 8), role="secretary", appointed=date(2015, 1, 5))
    registry.extracts[ENTITY_ACN] = entity.model_copy(update={"officeholders": [jo]})
    await run_gen08(registry, NUMBAT_PROFILE, {"max_depth": 1})
    assert ("person", "Park Jo 1988-08-08") in registry.queries


async def test_shareholder_rows_merge_by_normalised_name() -> None:
    """审核意见 R137-02 (验收 5): 同一摘录的股东行按规范化姓名并入高管: "MORGAN, Riley" 并入 "Riley Morgan"."""
    registry = chain_registry()
    entity = registry.extracts[ENTITY_ACN]
    riley = Officeholder(name="Riley Morgan", birth_date=date(1970, 5, 6), role="director", appointed=date(2015, 1, 5))
    holder = Shareholding(name="MORGAN, Riley", kind="person", acn=None, share_class="ORD", shares=10, pct=0.1)
    registry.extracts[ENTITY_ACN] = entity.model_copy(
        update={"officeholders": [riley], "shareholders": [*entity.shareholders, holder]}
    )
    patch, _, _, _ = await run_gen08(registry, NUMBAT_PROFILE, {"max_depth": 1})
    assert ("PER-MORGAN-RILEY-19700506", f"ACN-{ENTITY_ACN}", "shareholder", 10, 0.1) in links_of(patch)
    assert not [n.id for n in patch["ownership_graph"].nodes if n.id.endswith("-NODOB")]


async def test_undeclared_shareholder_without_acn_gets_r01() -> None:
    """审核意见 R137-02: 母公司链上没有 ACN 的法人股东 (Kiwi, 40%) 未申报时也出 R01, acn 为空, 证据只有发现它的
    股东行 (它没有摘录)."""
    profile = NUMBAT_PROFILE.model_copy(update={"related_parties": [], "ownership": []})
    patch, _, _, _ = await run_gen08(chain_registry(), profile, {"max_depth": 1})
    [kiwi] = [x for x in patch["exceptions"] if x.source_ids == ["ORG-KIWIHOLDINGSLTD"] and x.rule_id == "R01"]
    assert (kiwi.detail["acn"], kiwi.detail["via"], kiwi.detail["via_relations"]) == (
        None,
        f"ACN-{ENTITY_ACN}",
        ["shareholder 0.4"],
    )
    assert [(e.page, e.locator) for e in kiwi.evidence] == [(2, "shareholders[1]")]


def wombat(registry: FakeRegistry) -> FakeRegistry:
    """Xero 假登记加 Wombat Ventures Pty Ltd (合成, ACN 900043085): Alex 只持 30% 不任职, 董事 Pat Lee 持 70%; Alex
    的人员检索多一项持股."""
    registry.extracts["900043085"] = extract(
        "900043085",
        "Wombat Ventures Pty Ltd",
        [Officeholder(name="Pat Lee", birth_date=date(1975, 1, 2), role="director", appointed=date(2020, 1, 1))],
        [
            Shareholding(name="Alex Morgan", kind="person", acn=None, share_class="ORD", shares=30, pct=0.3),
            Shareholding(name="Pat Lee", kind="person", acn=None, share_class="ORD", shares=70, pct=0.7),
        ],
    )
    [alex] = registry.persons
    role = PersonRole(
        acn="900043085", company_name="Wombat Ventures Pty Ltd", role="shareholder", appointed=None, shares=30
    )
    registry.persons = [alex.model_copy(update={"roles": [*alex.roles, role]})]
    return registry


@needs("q1_supplements")
async def test_shareholder_only_role_stays_on_the_searched_person() -> None:
    """审核意见 R137-03: 检索出的个人在新发现的公司只持股不任职时, 该公司摘录里同名的股东行并入检索对象的节点 (不另建
    NODOB 节点); 这家公司的 R01 的 via_relations 随之为 ["shareholder 0.3"]. 同名而不是检索对象的照旧 (Pat Lee 是
    Wombat 的高管, 按高管栏合并)."""
    patch, _, _, _ = await run_gen08(wombat(xero_registry()), xero_profile(), xero=True)
    graph = patch["ownership_graph"]
    assert "PER-MORGAN-ALEX-NODOB" not in {n.id for n in graph.nodes}
    assert (ALEX, "ACN-900043085", "shareholder", 30, 0.3) in links_of(patch)
    assert ("PER-LEE-PAT-19750102", "ACN-900043085", "shareholder", 70, 0.7) in links_of(patch)
    [found] = [x for x in patch["exceptions"] if x.source_ids == ["ACN-900043085"]]
    assert (found.detail["via"], found.detail["via_relations"]) == (ALEX, ["shareholder 0.3"])


async def test_spaced_shareholder_acn_is_normalised() -> None:
    """审核意见 R137-04: 股东栏的 ACN 带空格 ("900 000 026") 时, 查询, 幂等键与节点都用去空白的号码, 照常穿透."""
    registry = chain_registry()
    entity = registry.extracts[ENTITY_ACN]
    holders = [h.model_copy(update={"acn": "900 000 026"}) if h.acn else h for h in entity.shareholders]
    registry.extracts[ENTITY_ACN] = entity.model_copy(update={"shareholders": holders})
    patch, _, trail, _ = await run_gen08(registry, NUMBAT_PROFILE, {"max_depth": 1})
    assert ("extract", PARENT_ACN) in registry.queries and ("extract", "900 000 026") not in registry.queries
    assert f"{EID}:GEN-08:extract:{PARENT_ACN}" in [r.idempotency_key for r in calls(trail)]
    nodes = {n.id: n for n in patch["ownership_graph"].nodes}
    assert nodes[f"ACN-{PARENT_ACN}"].acn == PARENT_ACN and nodes[f"ACN-{PARENT_ACN}"].searched


async def test_small_corporate_shareholder_is_not_penetrated() -> None:
    """审核意见 R137-05, 06 (协调者裁定): 母公司链只含持股不低于 significant_pct 的法人股东. 报告主体另有持 5% 的
    法人股东 (有 ACN, 其摘录里有有出生日期的董事): 不查其摘录, 不检索其高管, 不出 R01; 节点 (searched 为假) 与股东
    关系照建. 持 60% 的母公司照旧穿透."""
    registry = chain_registry()
    entity = registry.extracts[ENTITY_ACN]
    small = Shareholding(
        name="Emu Capital Pty Ltd", kind="company", acn="900000062", share_class="ORD", shares=5, pct=0.05
    )
    registry.extracts[ENTITY_ACN] = entity.model_copy(update={"shareholders": [*entity.shareholders, small]})
    pat = Officeholder(name="Pat Lee", birth_date=date(1975, 1, 2), role="director", appointed=date(2020, 1, 1))
    registry.extracts["900000062"] = extract("900000062", "Emu Capital Pty Ltd", [pat], [])
    profile = NUMBAT_PROFILE.model_copy(update={"related_parties": [], "ownership": []})
    patch, _, _, _ = await run_gen08(registry, profile, {"max_depth": 1})
    assert ("extract", "900000062") not in registry.queries and ("extract", PARENT_ACN) in registry.queries
    assert not [q for q in registry.queries if q == ("person", "Lee Pat 1975-01-02")]
    nodes = {n.id: n for n in patch["ownership_graph"].nodes}
    assert nodes["ACN-900000062"].searched is False and nodes["ACN-900000062"].acn == "900000062"
    assert ("ACN-900000062", f"ACN-{ENTITY_ACN}", "shareholder", 5, 0.05) in links_of(patch)
    assert "PER-LEE-PAT-19750102" not in nodes
    assert ["ACN-900000062"] not in [x.source_ids for x in patch["exceptions"]]


# ── 需求 R138: 按留痕幂等重建, 失败路径, R02 与重跑 ──
# 人员检索的查询与幂等键含出生日期: 下面的用例只断言查询类别与公司号码, 不写出个人的出生日期.


def test_gen08_is_stale_after_registry_extracts_change() -> None:
    """10-20 补充: GEN-08 读 registry_extracts; 执行之后审计师上传或修改了 ASIC 摘录 (数据变动记 registry_extracts),
    GEN-08 即过期, 可过期重跑出关系图的下一版 (主流程见 test_stale_rerun_keeps_current_exceptions_under_the_new_run);
    另读登记 PDF 的文件登记 (审核意见 R138-07: 关系的证据可靠性取它, 重建也按它取文件), 只看 registry_extract 一类
    (R304 的写法); 别的数据变动或别类文件的登记变动不让它过期."""
    deps = make_deps(FakeRegistry(), FakeTrail(), features=FEATURES)
    template = deps.library.load("GEN-08")
    assert (template.reads, template.document_reads) == (
        [DataInput.registry_extracts, DataInput.documents],
        [DocumentType.registry_extract],
    )
    pack = [make_procedure("GEN-08", Phase.P2_Executing, status=ProcedureStatus.completed)]
    run = make_run("GEN-08").model_copy(update={"status": RunStatus.succeeded, "data_version": 1})

    def change(data_input: DataInput, types: list[DocumentType] | None = None) -> list[DataChange]:
        return [DataChange(data_version=2, input=data_input, document_types=types)]

    assert stale_procedures(pack, [run], change(DataInput.registry_extracts), deps.library) == ["GEN-08"]
    registered = change(DataInput.documents, [DocumentType.registry_extract])
    assert stale_procedures(pack, [run], registered, deps.library) == ["GEN-08"]
    for other in (change(DataInput.trial_balance), change(DataInput.documents, [DocumentType.contract])):
        assert stale_procedures(pack, [run], other, deps.library) == []


def kinds(registry: FakeRegistry) -> list[tuple[str, str]]:
    """登记查询的 (类别, 编号): 人员检索只留类别, 编号 (姓名与出生日期) 记为 "-"."""
    return [(kind, "-" if kind == "person" else number) for kind, number in registry.queries]


def same_graph(a: StatePatch, b: StatePatch) -> bool:
    """两次执行的关系图除版本与执行外相同 (节点, 关系与报告主体)."""
    x, y = a["ownership_graph"], b["ownership_graph"]
    return (x.nodes, x.links, x.reporting_entity_id) == (y.nodes, y.links, y.reporting_entity_id)


@needs("q1_supplements")
async def test_preset_trail_rebuilds_without_queries() -> None:
    """验收 1: 留痕已有 5 行成功查询时再执行, RegistrySource 0 次, EvidenceStore.save 0 次, 图, 例外与文件同首次
    (逐字段相等); 只留前 2 行时只补查后 3 项 (人员检索与两份摘录), 结果同首次, 留痕补写 3 行."""
    first, _, trail, deps = await run_gen08(xero_registry(), xero_profile(), xero=True)
    financial = deps.financial_store
    again = xero_registry()
    second, _, _, rebuilt = await run_gen08(again, xero_profile(), xero=True, trail=trail, financial=financial)
    assert again.queries == [] and rebuilt.evidence_store.saves == [] and len(calls(trail)) == 5
    assert second["ownership_graph"] == first["ownership_graph"]
    assert (second["exceptions"], second["documents"]) == (first["exceptions"], first["documents"])

    partial_trail = FakeTrail()
    partial_trail.entries = calls(trail)[:2]
    rest = xero_registry()
    third, _, _, _ = await run_gen08(rest, xero_profile(), xero=True, trail=partial_trail, financial=financial)
    assert kinds(rest) == [("person", "-"), ("extract", "900043030"), ("extract", "900043049")]
    assert third["ownership_graph"] == first["ownership_graph"] and third["exceptions"] == first["exceptions"]
    assert [r.idempotency_key for r in calls(partial_trail)] == [r.idempotency_key for r in calls(trail)]


async def test_person_search_numbers_continue_after_recovery() -> None:
    """人员检索摘要的序号按本次执行已检索的人数往下数, 查不到的也算 (审核意见 R138-08): 第 1 人查不到, 第 2 人查到时
    为 #1, #2; 两人都查到时写完第 1 人那行后恢复, 补写的第 2 人那行仍为 #2."""
    people = [("Shaw", "Riley", date(1970, 5, 6)), ("Lee", "Pat", date(1975, 1, 2))]
    officers = [
        Officeholder(name=f"{given} {family}", birth_date=born, role="director", appointed=date(2015, 1, 5))
        for family, given, born in people
    ]
    entity = extract(ENTITY_ACN, "Bilby Tools Pty Ltd", officers, [])

    def registry(searchable: list[tuple[str, str, date]]) -> FakeRegistry:
        persons = [
            PersonSearch(
                family_name=family,
                given_name=given,
                birth_date=born,
                roles=[],
                file_name=f"{family}.pdf",
                file_bytes=PDF + family.encode(),
            )
            for family, given, born in searchable
        ]
        return FakeRegistry(chain_registry().abn_records, extracts={ENTITY_ACN: entity}, persons=persons)

    def searches(trail: FakeTrail) -> list[str]:
        return [r.summary for r in calls(trail) if r.idempotency_key.startswith(f"{EID}:GEN-08:person:")]

    _, _, partly, _ = await run_gen08(registry(people[1:]), NUMBAT_PROFILE, {"max_depth": 0})
    assert searches(partly) == ["ASIC person search #1", "ASIC person search #2"]
    _, _, clean, deps = await run_gen08(registry(people), NUMBAT_PROFILE, {"max_depth": 0})
    cut = FakeTrail()
    cut.entries = calls(clean)[:3]  # ABN, 报告主体摘录, 第 1 人的检索
    await run_gen08(registry(people), NUMBAT_PROFILE, {"max_depth": 0}, trail=cut, financial=deps.financial_store)
    assert searches(cut) == searches(clean) == ["ASIC person search #1", "ASIC person search #2"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda p: {k: v for k, v in p.items() if k != "result"}, "缺结构化结果"),
        (lambda p: {k: v for k, v in p.items() if k != "document_id"}, "缺 document_id"),
        (None, "未登记"),
        ("retyped", "未登记"),
    ],
    ids=["no_result", "no_document_id", "unregistered", "retyped"],
)
async def test_rebuild_rejects_incomplete_payload(change: Any, message: str) -> None:
    """验收 2: 重建只读 payload 与已登记的 registry_extract 文件: 报告主体那行摘录留痕的 payload 缺 result, 缺
    document_id, 文件未登记 (换一个空的文件登记), 或文件仍登记而类型已不是 registry_extract (审核意见 R138-04) 时抛
    LookupError, 消息含幂等键."""
    _, _, trail, deps = await run_gen08(chain_registry(), NUMBAT_PROFILE, {"max_depth": 1})
    key = f"{EID}:GEN-08:extract:{ENTITY_ACN}"
    financial = deps.financial_store if change is not None else FakeFinancialStore()
    if change == "retyped":
        financial.document_refs = [d.model_copy(update={"type": DocumentType.other}) for d in financial.document_refs]
    elif change is not None:
        trail.entries = [
            e.model_copy(update={"payload": change(e.payload)}) if e.idempotency_key == key else e
            for e in trail.entries
        ]
    with pytest.raises(LookupError, match=rf"{key}: .*{message}"):
        await run_gen08(chain_registry(), NUMBAT_PROFILE, {"max_depth": 1}, trail=trail, financial=financial)


async def test_client_not_penetrable_becomes_failed_run_and_rerun_task() -> None:
    """验收 3: 客户 ABN 查不到, 或登记没有 ACN (不是公司): EvidenceAgent 抛 LookupError, 节点由 A1 代写
    RUN-GEN-08-1 failed 与 T-RUN-GEN-08-1-v1 (failed, 只能 rerun)."""
    no_acn = chain_registry().abn_records[NUMBAT_PROFILE.abn].model_copy(update={"acn": None})
    for records in ({}, {NUMBAT_PROFILE.abn: no_acn}):
        deps = make_deps(FakeRegistry(records), FakeTrail(), features=FEATURES)
        agent, ctx = EvidenceAgent(deps), make_ctx(deps, "GEN-08")
        patch, writer, _ = await GraphNodes(deps, parallel_cap=2).run_agent(agent, ctx)
        [run] = patch["procedure_runs"]
        [task] = patch["review_tasks"]
        assert writer is AgentId.A1 and (run.id, run.status) == ("RUN-GEN-08-1", RunStatus.failed)
        assert "无从穿透" in (run.failure_reason or "")
        assert (task.id, task.kind, task.target_id) == ("T-RUN-GEN-08-1-v1", TaskKind.failed, run.id)
        assert deps.queue.allowed(task) == {Decision.rerun}


class FlakyRegistry(FakeRegistry):
    """第一次人员检索抛错 (登记不可达), 之后照常; 查询照常记进 queries."""

    def __init__(self, base: FakeRegistry) -> None:
        super().__init__(base.abn_records, base.asic_records, base.extracts, base.persons)
        self.failed = False

    async def asic_person(self, eid: str, family_name: str, given_name: str, birth_date: date) -> Any:
        person = await super().asic_person(eid, family_name, given_name, birth_date)
        if not self.failed:
            self.failed = True
            raise RegistryUnavailable("ASIC 不可达", "unreachable")
        return person


@needs("q1_supplements")
async def test_third_query_fails_then_rerun_queries_only_the_rest() -> None:
    """验收 4: 第 3 次查询 (人员检索) 抛错: 整次失败, 留痕只有前 2 行. rerun (第 2 次执行) 沿用前 2 行, 只重查失败的
    人员检索与其后的两份摘录; external_call 合计 5 行, 幂等键与不中断时相同 (跨执行共用); 图与不中断时相同."""
    registry, trail, financial = FlakyRegistry(xero_registry()), FakeTrail(), FakeFinancialStore()
    with pytest.raises(RegistryUnavailable):
        await run_gen08(registry, xero_profile(), xero=True, trail=trail, financial=financial)
    assert [k for k, _ in kinds(registry)] == ["abn", "extract", "person"] and len(calls(trail)) == 2
    registry.queries.clear()
    patch, _, _, _ = await run_gen08(registry, xero_profile(), xero=True, trail=trail, financial=financial, run_no=2)
    assert kinds(registry) == [("person", "-"), ("extract", "900043030"), ("extract", "900043049")]
    clean, _, clean_trail, _ = await run_gen08(xero_registry(), xero_profile(), xero=True)
    assert [r.idempotency_key for r in calls(trail)] == [r.idempotency_key for r in calls(clean_trail)]
    assert [r.run_id for r in calls(trail)] == ["RUN-GEN-08-1"] * 2 + ["RUN-GEN-08-2"] * 3
    assert same_graph(patch, clean) and patch["ownership_graph"].run_id == "RUN-GEN-08-2"


async def test_missing_extract_gives_r02_and_is_queried_again() -> None:
    """验收 5: 母公司的摘录查不到: 出 R02 (medium, source_ids 为母公司节点, reason 查不到摘录, 证据为报告主体摘录里的
    股东行), 母公司之后的关系不入图 (没有指向它的任职与持股, 它之上的 Galah 不出现), 其余照常; 那行留痕记 success 为假.
    之后的执行 (摘录有了) 照常再查, 取得摘录, 不再出 R02."""
    registry = chain_registry()
    parent = registry.extracts.pop(PARENT_ACN)
    profile = NUMBAT_PROFILE.model_copy(update={"related_parties": [], "ownership": []})
    patch, _, trail, deps = await run_gen08(registry, profile, {"max_depth": 1})
    quoll = f"ACN-{PARENT_ACN}"
    r02 = [x for x in patch["exceptions"] if x.rule_id == "R02" and x.source_ids == [quoll]]
    assert [(x.severity, x.detail["reason"], [(e.page, e.locator) for e in x.evidence]) for x in r02] == [
        ("medium", "查不到摘录", [(2, "shareholders[0]")])
    ]
    graph = patch["ownership_graph"]
    assert {x.to_id for x in graph.links} == {f"ACN-{ENTITY_ACN}"} and f"ACN-{GRAND_ACN}" not in {
        n.id for n in graph.nodes
    }
    [row] = [r for r in calls(trail) if r.idempotency_key.endswith(f"extract:{PARENT_ACN}")]
    assert row.payload == {"acn": PARENT_ACN, "result": None, "document_id": None, "success": False}

    registry.extracts[PARENT_ACN] = parent
    # 第 2 次执行的状态 (审核意见 R138-04): 第 1 次的例外去掉母公司那条 R01 (只留它的 R02, 看去重认不认 rule_id),
    # 另加一条别的程序对 Galah 的同 (R01, source_ids) 例外 (看去重是否只限本程序)
    grand = f"ACN-{GRAND_ACN}"
    kept = [x for x in patch["exceptions"] if not (x.rule_id == "R01" and x.source_ids == [quoll])]
    foreign = kept[0].model_copy(update={"id": "X-GEN-03-1-C01-1", "procedure_id": "GEN-03", "source_ids": [grand]})
    state = {"exceptions": [*kept, foreign], "ownership_graph": graph}
    again, _, _, _ = await run_gen08(
        registry, profile, {"max_depth": 1}, trail=trail, financial=deps.financial_store, run_no=2, state=state
    )
    assert ("extract", PARENT_ACN) in registry.queries[-3:]
    assert quoll in {x.to_id for x in again["ownership_graph"].links}
    # 新出的只数本次的: 母公司的 R01 (状态里只有它的 R02), Galah 的 R01 (状态里那条是别的程序的) 与 R02 (达到 max_depth)
    assert [(x.id, x.source_ids) for x in again["exceptions"]] == [
        ("X-GEN-08-2-R01-1", [quoll]),
        ("X-GEN-08-2-R01-2", [grand]),
        ("X-GEN-08-2-R02-1", [grand]),
    ]


async def test_entity_extract_not_found_gives_r02_without_evidence() -> None:
    """报告主体的摘录查不到: 主体节点出 R02 (reason 查不到摘录, 没有可指的股东行, 证据为空), 不再往上穿透, 执行成功."""
    registry = chain_registry()
    registry.extracts.pop(ENTITY_ACN)
    patch, _, _, _ = await run_gen08(registry, NUMBAT_PROFILE, {"max_depth": 1})
    r02 = [x for x in patch["exceptions"] if x.rule_id == "R02"]
    assert [(x.source_ids, x.detail["reason"], x.evidence) for x in r02] == [([f"ACN-{ENTITY_ACN}"], "查不到摘录", [])]
    assert ("extract", PARENT_ACN) not in registry.queries
    assert patch["procedure_runs"][0].status is RunStatus.succeeded


@needs("q1_supplements")
async def test_rerun_outputs_v2_without_repeating_exceptions() -> None:
    """验收 6: 重跑 (第 2 次执行, 状态带第 1 次的关系图与例外): OWNERSHIP 出 v2 (状态中的版本 +1, run_id
    RUN-GEN-08-2), 内容同 v1; 查询全部按留痕重建 (0 次); 状态中已有的 X-GEN-08-1-R01-1 不再出, 片段的例外为空, 状态
    中仍只有它."""
    first, _, trail, deps = await run_gen08(xero_registry(), xero_profile(), xero=True)
    state = {"exceptions": first["exceptions"], "ownership_graph": first["ownership_graph"]}
    again = xero_registry()
    second, ctx2, _, _ = await run_gen08(
        again, xero_profile(), xero=True, trail=trail, financial=deps.financial_store, run_no=2, state=state
    )
    graph = second["ownership_graph"]
    assert (graph.id, graph.version, graph.run_id) == ("OWNERSHIP", 2, "RUN-GEN-08-2") and same_graph(first, second)
    assert again.queries == [] and second["exceptions"] == []
    merged = apply_patch(ctx2.state, second)
    assert [x.id for x in merged["exceptions"]] == ["X-GEN-08-1-R01-1"]


async def test_uploaded_extract_is_read_again() -> None:
    """新建的客户的摘录来自审计师上传 (结果带 document_id): 这种查询不按留痕复用, 下一次执行重读 (审计师修改后读到新的
    抽取结果); 登记来源的摘录照常复用."""
    registry = chain_registry()
    registry.extracts[ENTITY_ACN] = registry.extracts[ENTITY_ACN].model_copy(update={"document_id": "DOC-UPLOADED"})
    _, _, trail, deps = await run_gen08(registry, NUMBAT_PROFILE, {"max_depth": 1})
    registry.queries.clear()
    await run_gen08(registry, NUMBAT_PROFILE, {"max_depth": 1}, trail=trail, financial=deps.financial_store, run_no=2)
    assert ("extract", ENTITY_ACN) in registry.queries and ("extract", PARENT_ACN) not in registry.queries


class NoPersonSource(FakeRegistry):
    """新建的客户的登记来源: 人员检索没有来源 (no_source); reason 给别的类别时模拟不可达."""

    def __init__(self, base: FakeRegistry, reason: str = "no_source") -> None:
        super().__init__(base.abn_records, base.asic_records, base.extracts, base.persons)
        self.reason = reason

    async def asic_person(self, eid: str, family_name: str, given_name: str, birth_date: date) -> Any:
        raise RegistryUnavailable("没有人员检索来源", self.reason)  # type: ignore[arg-type]


async def test_no_person_search_source_gives_r02_not_failure() -> None:
    """10-20 补充: 新建的客户高管有出生日期而没有人员检索来源 (RegistryUnavailable no_source) 时只出 R02, 不整次失败:
    该高管所在的母公司出 R02 (reason 没有人员检索来源, 证据另含其摘录的高管页), 不写人员检索留痕, 该人节点 searched
    为假, 执行成功. 其他类别 (不可达) 照常抛出, 整次失败."""
    patch, _, trail, _ = await run_gen08(NoPersonSource(chain_registry()), NUMBAT_PROFILE, {"max_depth": 1})
    quoll = f"ACN-{PARENT_ACN}"
    [r02] = [x for x in patch["exceptions"] if x.rule_id == "R02" and x.source_ids == [quoll]]
    assert r02.detail["reason"] == "没有人员检索来源, 高管未检索"
    assert [(e.page, e.locator) for e in r02.evidence] == [(2, "shareholders[0]"), (1, "officeholders")]
    assert not [r for r in calls(trail) if ":person:" in r.idempotency_key]
    people = [n for n in patch["ownership_graph"].nodes if n.kind == "person" and n.birth_date is not None]
    assert people and not [n for n in people if n.searched]
    assert patch["procedure_runs"][0].status is RunStatus.succeeded
    with pytest.raises(RegistryUnavailable):
        await run_gen08(NoPersonSource(chain_registry(), "unreachable"), NUMBAT_PROFILE, {"max_depth": 1})


def numbat_chain_registry() -> FakeRegistry:
    """Numbat 的登记记录 (ACC-04 照旧) 加上 chain_registry 的穿透数据: 报告主体摘录改挂在 Numbat 的 ACN 上. GEN-08
    依次查 ABN, 主体摘录, 母公司摘录, Galah 摘录 (查不到), 母公司董事的人员检索 (查不到), 共 5 次."""
    base = chain_registry()
    entity = base.extracts.pop(ENTITY_ACN).model_copy(update={"acn": NUMBAT_ABN.acn, "name": NUMBAT_ABN.entity_name})
    return FakeRegistry(
        {NUMBAT_ABN.abn: NUMBAT_ABN}, {NUMBAT_ASIC.acn: NUMBAT_ASIC}, {NUMBAT_ABN.acn: entity, **base.extracts}
    )


class PausingTrail(FakeTrail):
    """GEN-08 写完第 3 行 external_call 后停住 (只停一次), 模拟此时进程被杀."""

    def __init__(self) -> None:
        super().__init__()
        self.paused = asyncio.Event()

    async def record(self, entry: TrailEntry) -> str:
        entry_id = await super().record(entry)
        gen08 = entry.procedure_id == "GEN-08" and entry.kind is TrailKind.external_call
        rows = [e for e in self.entries if e.procedure_id == "GEN-08" and e.kind is TrailKind.external_call]
        if gen08 and len(rows) == 3 and not self.paused.is_set():
            self.paused.set()
            await asyncio.Event().wait()
        return entry_id


async def test_restart_after_third_query_recovers_without_requery() -> None:
    """验收 7: Numbat 主流程 (功能 related_parties 打开) 中 GEN-08 写完第 3 行查询留痕后进程被杀; 用同一检查点与存储
    新建 runner recover 后跑到等复核: 登记查询的次数与顺序同不中断时 (GEN-08 合计 5 次, 前 3 项不重查), GEN-08 的
    external_call 5 行, 关系图同不中断时."""
    features = NUMBAT_FEATURES | {"related_parties"}
    clean_registry = numbat_chain_registry()
    clean = await start_demo(features=features, registry=clean_registry)
    await wait_idle(clean.runner, EID)
    [expected] = await clean.output_store.latest(EID, OwnershipGraph)

    registry, trail = numbat_chain_registry(), PausingTrail()
    s = await start_demo(trail=trail, features=features, registry=registry)
    await asyncio.wait_for(trail.paused.wait(), timeout=10)
    await kill(s.runner, EID)
    s = await restart(s)
    await wait_idle(s.runner, EID)
    assert s.runner.run_error(EID) is None and await s.next_nodes() == ("await_events",)
    assert kinds(registry) == kinds(clean_registry)
    assert len([k for k, _ in kinds(registry) if k in ("extract", "person")]) == 4  # 加上 ABN 共 5 次
    rows = [e for e in trail.entries if e.procedure_id == "GEN-08" and e.kind is TrailKind.external_call]
    assert len(rows) == 5 and len({r.idempotency_key for r in rows}) == 5
    [graph] = await s.output_store.latest(EID, OwnershipGraph)
    assert graph == expected


async def test_stale_rerun_keeps_current_exceptions_under_the_new_run() -> None:
    """审核意见 R138-01: Numbat 主流程 (功能 related_parties 打开) 跑到等复核后记一次 registry_extracts 变动, 过期重跑
    GEN-08: Store 中关系图出 v2 (RUN-GEN-08-2); 第 1 次执行被取代, 其例外不再是当前结果, 新执行重新出同样的
    (规则, source_ids) 例外, 当前结果中的 GEN-08 例外与重跑前一致, 都属新执行."""
    s = await start_demo(features=NUMBAT_FEATURES | {"related_parties"}, registry=numbat_chain_registry())
    await wait_idle(s.runner, EID)
    assert await s.next_nodes() == ("await_events",), s.runner.run_error(EID)

    async def current() -> list[tuple[str, tuple[str, ...], str]]:
        values = fill_defaults((await s.runner.app.aget_state(GraphRunner.config(EID))).values)
        xs = [x for x in current_results(values).exceptions if x.procedure_id == "GEN-08"]
        return sorted((x.rule_id, tuple(x.source_ids), x.run_id) for x in xs)

    before = await current()
    assert {"R01", "R02"} <= {rule for rule, _, _ in before}
    data = s.runner.deps.data
    assert isinstance(data, FakeDataStore)
    data.record(DataInput.registry_extracts)
    request = await s.submit.rerun_stale(EID, "RR-1", ["GEN-08"], requested_by="U-1")
    await wait_idle(s.runner, EID)
    assert s.runner.run_error(EID) is None and "RUN-GEN-08-1" in request.superseded_run_ids
    graphs = sorted((r for r in s.output_store.rows if isinstance(r, OwnershipGraph)), key=lambda g: g.version)
    assert [(g.version, g.run_id) for g in graphs] == [(1, "RUN-GEN-08-1"), (2, "RUN-GEN-08-2")]
    after = await current()
    assert [(rule, ids) for rule, ids, _ in after] == [(rule, ids) for rule, ids, _ in before]
    assert {run for _, _, run in after} == {"RUN-GEN-08-2"}
