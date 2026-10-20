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
    registry: FakeRegistry,
    profile: ClientProfile,
    params: dict[str, Any] | None = None,
    *,
    xero: bool = False,
    profiles: list[ClientProfile] | None = None,
    trail: FakeTrail | None = None,
) -> tuple[StatePatch, RunContext, FakeTrail, Any]:
    """以 profile 为客户的项目 (采用 profile 那一版; 存储中的画像为 profiles, 缺省只有它) 执行 GEN-08 第 1 次 (功能
    related_parties 打开); 参数快照另按 params 改; 片段经 check_patch 校验 (A5, 本程序的 outputs). xero 时项目带 Xero
    数据集, 证据存储用 XeroFakeEvidenceStore (补造 PDF 的页数取 manifest). trail 给出时留痕写进它 (执行抛错时也能查).
    返回片段, 上下文, 留痕与依赖."""
    trail = trail or FakeTrail()
    engagement: Engagement = ENGAGEMENT.model_copy(
        update={
            "client_id": profile.id,
            "client_profile_version": profile.version,
            **({"dataset": XERO_DATASET} if xero else {}),
        }
    )
    financial = FakeFinancialStore()
    store = FakeEngagementStore(profiles or [profile], [engagement])
    deps = make_deps(registry, trail, store, financial_store=financial, features=FEATURES)
    if xero:
        deps = replace(deps, evidence_store=XeroFakeEvidenceStore(financial))
    ctx = make_ctx(deps, "GEN-08", engagement)
    run = ctx.run.model_copy(update={"params_snapshot": {**ctx.run.params_snapshot, **(params or {})}})
    ctx = RunContext(ctx.engagement, ctx.procedure, ctx.template, run, ctx.state)
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
    manifest.documents 中登记途径为 registry 的 4 份; 片段 documents 4 条, 状态 extracted."""
    patch, _, _, deps = await run_gen08(xero_registry(), xero_profile(), xero=True)
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


async def test_client_without_acn_fails_after_recording_the_lookup() -> None:
    """客户 ABN 查不到, 或登记没有 ACN (不是公司): 写完 ABN 那行留痕后抛 LookupError, 不再查询 (A1 按执行失败处理;
    失败路径的其余部分由需求 R138 做). 留痕先写 (审核意见 R137-02): 那一行的 result 为查到的记录或空."""
    no_acn = chain_registry().abn_records[NUMBAT_PROFILE.abn].model_copy(update={"acn": None})
    for records, result in (({}, None), ({NUMBAT_PROFILE.abn: no_acn}, no_acn.model_dump(mode="json"))):
        registry, trail = FakeRegistry(records), FakeTrail()
        with pytest.raises(LookupError, match="无从穿透"):
            await run_gen08(registry, NUMBAT_PROFILE, trail=trail)
        assert registry.queries == [("abn", NUMBAT_PROFILE.abn)]
        [row] = calls(trail)
        assert row.payload == {"abn": NUMBAT_PROFILE.abn, "result": result}
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
    [kiwi] = [x for x in patch["exceptions"] if x.source_ids == ["ORG-KIWIHOLDINGSLTD"]]
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
