"""A5 GEN-08 股权穿透与关系图 (需求 R137): 查询顺序与留痕, 人员检索对象, 登记 PDF 存档, 关系图, R01 例外.

Xero 用例的登记取补造目录 registry/ (xero_registry, 只读补造文件, 不改任何文件); 向上穿透, 没有 ACN 的法人股东与
按名称比对申报清单另用合成的假登记 (FakeRegistry). 按留痕重建, 失败路径与 R02 由需求 R138 做.
"""

import json
from dataclasses import replace
from datetime import date
from typing import Any

import pytest

from backend.agents.base import RunContext
from backend.agents.evidence import EvidenceAgent
from backend.agents.ownership import declared
from backend.agents.registry import build_agents
from backend.contracts.models import (
    AbnRecord,
    AgentId,
    AsicExtract,
    ClientProfile,
    DeclaredHolding,
    DeclaredParty,
    DocumentType,
    Engagement,
    EvidenceType,
    Officeholder,
    PersonRole,
    Reliability,
    RunStatus,
    Shareholding,
    TrailKind,
    organisation_node_id,
)
from backend.contracts.state import StatePatch
from backend.orchestration.merge import check_patch
from tests.external import needs
from tests.fakes import (
    ENGAGEMENT,
    NUMBAT_PROFILE,
    FakeEngagementStore,
    FakeFinancialStore,
    FakeRegistry,
    FakeTrail,
    make_ctx,
    make_deps,
)
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
    registry: FakeRegistry, profile: ClientProfile, xero: bool = True
) -> tuple[StatePatch, RunContext, FakeTrail, Any]:
    """以 profile 为客户的项目执行 GEN-08 第 1 次 (功能 related_parties 打开); 片段经 check_patch 校验 (A5, 本程序的
    outputs). xero 时证据存储用 XeroFakeEvidenceStore (补造 PDF 的页数取 manifest). 返回片段, 上下文, 留痕与依赖."""
    trail = FakeTrail()
    engagement: Engagement = ENGAGEMENT.model_copy(
        update={"client_id": profile.id, "client_profile_version": profile.version, "dataset": XERO_DATASET}
    )
    financial = FakeFinancialStore()
    deps = make_deps(
        registry, trail, FakeEngagementStore([profile], [engagement]), financial_store=financial, features=FEATURES
    )
    if xero:
        deps = replace(deps, evidence_store=XeroFakeEvidenceStore(financial))
    ctx = make_ctx(deps, "GEN-08", engagement)
    patch = await EvidenceAgent(deps).run(ctx)
    check_patch(
        patch,
        writer=AgentId.A5,
        procedure_id="GEN-08",
        run_id=ctx.run.id,
        state=ctx.state,
        outputs=ctx.template.outputs,
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
    assert EvidenceAgent.procedures == {"CSH-04", "GEN-08"}
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
    原件) 与文件 ID (ABN 查询没有). Sam Whitfield (15%, 不任职, 无出生日期) 不检索."""
    registry = xero_registry()
    _, _, trail, _ = await run_gen08(registry, xero_profile())

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
    manifest.documents 中登记途径为 registry 的 4 份; 片段 documents 4 条, 状态 extracted."""
    patch, _, _, deps = await run_gen08(xero_registry(), xero_profile())
    expected = {i for i, d in manifest_documents().items() if d["registration"] == "registry"}
    assert {d.id for d in patch["documents"]} == expected and len(patch["documents"]) == 4
    assert {(d.type, d.reliability, d.status) for d in patch["documents"]} == {
        (DocumentType.registry_extract, Reliability.external_original, "extracted")
    }
    assert {(t, r) for _, _, t, r in deps.evidence_store.save_args} == {
        (DocumentType.registry_extract, Reliability.external_original)
    }


@needs("q1_supplements")
async def test_xero_ownership_graph() -> None:
    """验收 5: OWNERSHIP v1 (RUN-GEN-08-1, 报告主体 ACN-900043021, 带画像 ABN): 节点 6 (按 ID), 关系 10 (只取公司
    摘录; Dana 的股东行并入其高管节点 PER-MORGAN-DANA-19811103); 每条关系的证据为该摘录的 document 指针 (高管第 1 页,
    股东第 2 页); 只有 Alex 检索过, 三家公司都取得了摘录."""
    patch, _, _, _ = await run_gen08(xero_registry(), xero_profile())
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
    patch, _, _, _ = await run_gen08(xero_registry(), xero_profile())
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
    patch, _, _, _ = await run_gen08(xero_registry(), profile.model_copy(update={"related_parties": by_abn}))
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

    patch, _, trail, _ = await run_gen08(registry, xero_profile())

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
    股东行."""
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
    patch, ctx, trail, _ = await run_gen08_with_params(registry, profile, {"max_depth": 1})

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
    [found] = patch["exceptions"]
    assert found.source_ids == [grand]
    assert {k: found.detail[k] for k in ("acn", "via", "via_relations")} == {
        "acn": GRAND_ACN,
        "via": f"ACN-{PARENT_ACN}",
        "via_relations": ["shareholder 1"],
    }
    parent_doc = next(d for d in patch["documents"] if d.file_name == f"{PARENT_ACN}.pdf")
    assert [(e.source_id, e.page, e.locator) for e in found.evidence] == [(parent_doc.id, 2, "shareholders[0]")]
    assert len(patch["documents"]) == 2 and ctx.run.params_snapshot["max_depth"] == 1


async def run_gen08_with_params(
    registry: FakeRegistry, profile: ClientProfile, params: dict[str, Any]
) -> tuple[StatePatch, RunContext, FakeTrail, Any]:
    """同 run_gen08 (不用 Xero 证据存储), 参数快照另按 params 改."""
    trail = FakeTrail()
    engagement = ENGAGEMENT.model_copy(update={"client_id": profile.id, "client_profile_version": profile.version})
    deps = make_deps(registry, trail, FakeEngagementStore([profile], [engagement]), features=FEATURES)
    ctx = make_ctx(deps, "GEN-08", engagement)
    run = ctx.run.model_copy(update={"params_snapshot": {**ctx.run.params_snapshot, **params}})
    ctx = RunContext(ctx.engagement, ctx.procedure, ctx.template, run, ctx.state)
    patch = await EvidenceAgent(deps).run(ctx)
    check_patch(
        patch, writer=AgentId.A5, procedure_id="GEN-08", run_id=run.id, state=ctx.state, outputs=ctx.template.outputs
    )
    return patch, ctx, trail, deps


async def test_client_without_acn_fails_after_recording_the_lookup() -> None:
    """客户 ABN 查不到, 或登记没有 ACN (不是公司): 写完 ABN 那行留痕后抛 LookupError, 不再查询 (A1 按执行失败处理;
    失败路径的其余部分由需求 R138 做)."""
    for records in (
        {},
        {NUMBAT_PROFILE.abn: chain_registry().abn_records[NUMBAT_PROFILE.abn].model_copy(update={"acn": None})},
    ):
        registry = FakeRegistry(records)
        with pytest.raises(LookupError, match="无从穿透"):
            await run_gen08_with_params(registry, NUMBAT_PROFILE, {})
        assert registry.queries == [("abn", NUMBAT_PROFILE.abn)]


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
    比对, 两个这样的名称不算相等; 有键的照常比 (去附加符, 去后缀)."""
    profile = NUMBAT_PROFILE.model_copy(
        update={"ownership": [DeclaredHolding(holder=holder, kind="company", shares=1, pct=0.1)]}
    )
    assert declared(profile, name, "900000044", SUFFIXES) is matched
