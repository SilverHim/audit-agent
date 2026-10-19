"""R88 盲判过滤接口 (contracts/ports.BlindFilter, 接口文档第 4, 10 节).

空实现输出不变 (只多 hidden_fields); 覆盖守卫: 测试替身隐藏 J-REV-02-1-SI-2026-0612 与其模型调用留痕时, 全部 GET 接口
与对话工具都不出现该判断 fixture 的结论与模型原文; 守卫二: 返回判断或留痕的接口与对话工具都须登记在本文件的清单中.
"""

import json
import re
from dataclasses import replace
from typing import Any, cast

import pytest

from backend.agents.concierge_chat.rule_model import RuleChatModel
from backend.agents.concierge_chat.service import ConciergeChat
from backend.agents.concierge_chat.tools import TOOLS, ToolBox, ToolEnv
from backend.contracts.conversation import ConciergeQuestion, ToolCall
from backend.contracts.data import TableKind
from backend.contracts.models import (
    AuditException,
    AuditReportVersion,
    Judgment,
    JudgmentRef,
    OpinionType,
    ReportSection,
    ReviewStatus,
    TrailEntry,
    TrailKind,
)
from backend.contracts.views import JudgmentView, TrailEntryView
from backend.core.config import get_settings
from backend.main import create_app
from backend.orchestration.query import GraphStateQuery
from backend.services.blind import PassThroughBlindFilter
from backend.services.review_queue import task_amount
from tests.chat_fakes import StaticSource
from tests.fakes import (
    ENGAGEMENT,
    NUMBAT_AUDITOR,
    NUMBAT_TRIAL_BALANCE,
    api_client,
    numbat_contracts,
    wait_idle,
)
from tests.test_decisions import Started
from tests.test_e2e_mock import start_demo
from tests.test_flow import REVIEWS

EID = ENGAGEMENT.id
HIDDEN = "J-REV-02-1-SI-2026-0612"  # 待复核的 REV-02 判断 (Numbat 停在 await_events 时)
FIXTURE = json.loads((get_settings().LLM_FIXTURES_ROOT / "REV-02" / "SI-2026-0612.json").read_text(encoding="utf-8"))
# 只出现在该判断与其模型调用留痕 (payload 的 raw) 里的原文: 结论 (取前 150 字, 对话工具截到 200 字), 反方论证
SECRETS = [FIXTURE["conclusion"][:150], FIXTURE["counter_argument"][:150]]
HIDDEN_FIELDS = [
    "conclusion",
    "reasoning_chain",
    "counter_argument",
    "misstatement_draft",
    *(name for name in JudgmentView.model_fields if name.startswith("critic_")),
]

# 守卫二的清单: 响应含判断或留痕的 GET 接口, 输出判断或留痕的对话工具. 新增的须登记 (并经 deps.blind_filter 输出)
BLIND_ROUTES = {
    "/api/v1/engagements/{eid}/review-tasks",
    "/api/v1/engagements/{eid}/procedures/{pid}",
    "/api/v1/engagements/{eid}/judgments/{jid}",
    "/api/v1/engagements/{eid}/trail",
    "/api/v1/engagements/{eid}/trail/{trail_id}",
}
BLIND_TOOLS = {
    "list_open_items",
    "list_judgments",
    "get_judgment",
    "get_procedure",
    "get_misstatements",
    "get_report",
    "search_trail",
}
# 不输出判断与留痕的对话工具; 两张表合起来须恰为全部工具
PLAIN_TOOLS = {"get_overview", "get_gate", "list_exceptions", "get_evidence", "get_contract", "get_standard"}
# 输出判断原文或留痕明细的 (接口, 工具): 空实现下其响应含 SECRETS, 隐藏后不含 (证明经过滤)
CONTENT_ROUTES = BLIND_ROUTES
CONTENT_TOOLS = {"list_open_items", "list_judgments", "get_judgment", "get_procedure"}
# 不调的 GET 接口 (不含判断与留痕): 证据原件返回原件文件 (EvidenceFile, 假证据存储不出文件); 大模型设置页与项目无关.
# 只能登记返回文件 (非 JSON) 或不属于项目的接口 (test_skipped_routes_are_files_or_outside_engagements)
SKIPPED_ROUTES = {"/api/v1/engagements/{eid}/evidence/{document_id}", "/api/v1/settings/llm"}
# 调了而不是 200 的接口 (场景决定): 图停在 await_events, 不在闸门, gate 为 404 NOT_AT_GATE; 没配 Xero 应用, connect 为
# 503 XERO_NOT_CONFIGURED; 没连接 Xero, tenants 为 409 XERO_NOT_CONNECTED; 授权回调不带 state, 302 回前端 (需求 R279)
NOT_OK_ROUTES = {
    "/api/v1/engagements/{eid}/gate": 404,
    "/api/v1/engagements/{eid}/data/xero/connect": 503,
    "/api/v1/engagements/{eid}/data/xero/tenants": 409,
    "/api/v1/integrations/xero/callback": 302,
}
TOOL_ARGS: dict[str, dict[str, Any]] = {
    "list_judgments": {"procedure_id": "REV-02"},
    "get_judgment": {"judgment_id": HIDDEN},
    "get_procedure": {"procedure_id": "REV-02"},
    "list_exceptions": {"procedure_id": "REV-08"},
    "get_evidence": {"source_type": "sales_invoice", "source_id": "SI-2026-0612"},
    "get_standard": {"standard_id": "AASB15-P38"},
    "search_trail": {"kind": "llm_call", "limit": 50},
}


def hidden_trail(entry: TrailEntry) -> bool:
    """该判断的模型调用留痕: REV-02 对 SI-2026-0612 的 llm_call (含各次重试)."""
    return (
        entry.kind is TrailKind.llm_call
        and entry.procedure_id == "REV-02"
        and entry.payload.get("fixture_key") == "SI-2026-0612"
    )


class HideOne:
    """测试替身: 隐藏 HIDDEN 的结论, 推理链, 反方论证, 错报草稿与 critic_ 字段 (置空并列入 hidden_fields), 及其模型调用
    留痕的 payload. 其余原样 (同空实现)."""

    async def judgment(self, eid: str, j: Judgment) -> JudgmentView:
        view = await PassThroughBlindFilter().judgment(eid, j)
        if j.id != HIDDEN:
            return view
        return view.model_copy(update={**dict.fromkeys(HIDDEN_FIELDS), "hidden_fields": HIDDEN_FIELDS})

    async def trail(self, eid: str, entry: TrailEntry) -> TrailEntryView:
        view = await PassThroughBlindFilter().trail(eid, entry)
        if not hidden_trail(entry):
            return view
        return view.model_copy(update={"payload": None, "hidden_fields": ["payload"]})


class Recording:
    """记录调用的替身: 包住 inner, 记下 judgment 与 trail 被调用的次数 (守卫二用: 清单中的接口与工具确实经过过滤)."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.calls = 0

    async def judgment(self, eid: str, j: Judgment) -> JudgmentView:
        self.calls += 1
        return cast(JudgmentView, await self.inner.judgment(eid, j))

    async def trail(self, eid: str, entry: TrailEntry) -> TrailEntryView:
        self.calls += 1
        return cast(TrailEntryView, await self.inner.trail(eid, entry))


# ── 验收 1: 空实现与视图字段 ──


def test_views_mirror_store_models() -> None:
    """视图的字段与存储模型一一对应 (同序), 只多 hidden_fields; Judgment 或 TrailEntry 加字段时这里失败, 提醒同步."""
    assert list(JudgmentView.model_fields) == [*Judgment.model_fields, "hidden_fields"]
    assert list(TrailEntryView.model_fields) == [*TrailEntry.model_fields, "hidden_fields"]


async def test_pass_through_keeps_everything(demo: Started) -> None:
    """空实现: 每条判断与留痕转成视图后, 除 hidden_fields: [] 外与原对象逐字段相同."""
    store, trail = demo.runner.deps.output_store, demo.runner.deps.trail
    blind = PassThroughBlindFilter()
    judgments = await store.latest(EID, Judgment)
    entries = await trail.list(EID, None, limit=500)
    assert judgments and entries
    for j in judgments:
        assert (await blind.judgment(EID, j)).model_dump() == {**j.model_dump(), "hidden_fields": []}
    for e in entries:
        assert (await blind.trail(EID, e)).model_dump() == {**e.model_dump(), "hidden_fields": []}


# ── 验收 2: 覆盖守卫 ──


@pytest.fixture
async def demo() -> Started:
    """Numbat 用真实 Agent 与 mock 大模型跑进 P2, 按第 7 节提交复核, 直到 REV-02 的该判断待复核 (停在 await_events)."""
    s = await start_demo()
    await wait_idle(s.runner, EID)
    for decision in REVIEWS:
        assert await s.next_nodes() == ("await_events",), s.runner.run_error(EID)
        if await s.runner.deps.output_store.get(EID, Judgment, HIDDEN) is not None:
            break
        await s.submit.submit(EID, decision)
        await wait_idle(s.runner, EID)
    judgment = await s.runner.deps.output_store.get(EID, Judgment, HIDDEN)
    assert judgment is not None and judgment.review_status is ReviewStatus.pending
    return s


async def get_paths(s: Started) -> dict[str, str]:
    """全部 GET 接口 (OpenAPI 中的路径模板) → 实际请求的路径: 路径参数取能取到该判断与留痕的值."""
    trail = s.runner.deps.trail
    [entry, *_] = [e for e in await trail.list(EID, TrailKind.llm_call, limit=500) if hidden_trail(e)]
    documents = await s.runner.deps.financial_store.documents(EID)
    values = {
        "eid": EID,
        "pid": "REV-02",
        "jid": HIDDEN,
        "trail_id": str(entry.id),
        "document_id": documents[0].id,
        "source_type": "sales_invoice",
        "source_id": "SI-2026-0612",
        "fragment_id": "AASB15-P38",
        # 数据管理与合同修改的接口 (不含判断与留痕, 只要求不出 500)
        "kind": TableKind.journal_entries.value,
        "code": NUMBAT_TRIAL_BALANCE[0].account_code,
        "contract_id": numbat_contracts()[0].id,  # 合同 ID (CON-001), 不是合同文件的登记 ID
    }
    paths = {}
    for template, operations in create_app().openapi()["paths"].items():
        if "get" in operations and template not in SKIPPED_ROUTES:
            paths[template] = re.sub(r"\{(\w+)\}", lambda m: values[m[1]], template)
    return paths


async def responses(s: Started, deps: Any) -> dict[str, str]:
    """每个 GET 接口的响应文本 (判断另取第 1 版). 先向审计助理 (规则模型, 用同一依赖) 问一句待办, 使对话接口有内容."""
    found: dict[str, str] = {}
    chat = ConciergeChat(deps, GraphStateQuery(s.runner.app, deps, s.runner), StaticSource(RuleChatModel()), None)
    await chat.ask(EID, NUMBAT_AUDITOR.id, ConciergeQuestion(question="还有哪些待处理的事项?", lang="zh"))
    async with api_client(s.runner, deps=deps, concierge=chat) as client:
        for template, path in (await get_paths(s)).items():
            response = await client.get(path)
            assert response.status_code == NOT_OK_ROUTES.get(template, 200), (template, response.text)
            found[template] = response.text
        found["judgments/{jid}?version=1"] = (
            await client.get(f"/api/v1/engagements/{EID}/judgments/{HIDDEN}?version=1")
        ).text
    return found


async def tool_outputs(s: Started, deps: Any) -> dict[str, str]:
    """每个对话工具的输出 (给模型的 JSON 文本)."""
    env = ToolEnv(EID, deps, GraphStateQuery(s.runner.app, deps, s.runner), 1_000_000)
    found: dict[str, str] = {}
    for tool in TOOLS:
        outcome = await ToolBox().call(
            env, ToolCall(id="c", name=tool.name, arguments=json.dumps(TOOL_ARGS.get(tool.name, {})))
        )
        assert outcome.ok, (tool.name, outcome.content)
        found[tool.name] = outcome.content
    return found


def leaks(outputs: dict[str, str]) -> list[str]:
    return [name for name, text in outputs.items() if any(secret in text for secret in SECRETS)]


async def test_hidden_judgment_and_trail_never_leak(demo: Started) -> None:
    """隐藏该判断与其模型调用留痕时, 全部 GET 接口与全部对话工具都不出现其结论与模型原文 (失败时列出接口或工具名).
    空实现下, 输出判断原文或留痕明细的接口与工具都含这些原文: 去掉其中任一处的过滤, 就会泄露."""
    hidden = replace(demo.runner.deps, blind_filter=HideOne())
    assert leaks(await tool_outputs(demo, hidden)) == []
    assert leaks(await responses(demo, hidden)) == []  # 含对话: 审计助理答一句待办后的对话历史与留痕
    plain = demo.runner.deps
    assert set(leaks(await tool_outputs(demo, plain))) == CONTENT_TOOLS
    assert set(leaks(await responses(demo, plain))) == {*CONTENT_ROUTES, "judgments/{jid}?version=1"}


async def test_hidden_fields_are_reported(demo: Started) -> None:
    """接口与工具如实给出 hidden_fields: 判断详情与各版本列出被隐藏的字段, 留痕详情列出 payload."""
    hidden = replace(demo.runner.deps, blind_filter=HideOne())
    paths = await get_paths(demo)
    async with api_client(demo.runner, deps=hidden) as client:
        detail = (await client.get(paths["/api/v1/engagements/{eid}/judgments/{jid}"])).json()
        entry = (await client.get(paths["/api/v1/engagements/{eid}/trail/{trail_id}"])).json()
    assert detail["judgment"]["hidden_fields"] == HIDDEN_FIELDS and detail["judgment"]["conclusion"] is None
    assert all(v["hidden_fields"] == HIDDEN_FIELDS for v in detail["versions"])
    assert (entry["payload"], entry["hidden_fields"]) == (None, ["payload"])
    env = ToolEnv(EID, hidden, GraphStateQuery(demo.runner.app, hidden, demo.runner), 1_000_000)
    outcome = await ToolBox().call(
        env, ToolCall(id="c", name="get_judgment", arguments=json.dumps({"judgment_id": HIDDEN}))
    )
    data = json.loads(outcome.content)
    assert data["hidden_fields"] == HIDDEN_FIELDS and data["reasoning_chain"] is None and data["conclusion"] is None


async def test_review_task_amount_and_tier_follow_the_view(demo: Started) -> None:
    """review-tasks 的派生字段按过滤后的视图算: 隐藏该判断的错报草稿后, amount_cents 不再是草稿金额 (改按例外或交易行
    取), 档位与仲裁选项也按视图算, 被隐藏的内容不经派生字段漏出; 空实现下仍为草稿金额."""
    # 测试内把草稿金额改成与例外金额不同的合成值 (只改这份内存存储), 好区分金额取自草稿还是例外
    store = demo.runner.deps.output_store
    current = await store.get(EID, Judgment, HIDDEN)
    assert current is not None and current.misstatement_draft is not None
    draft = 99_900_000
    stored = current.model_copy(
        update={"misstatement_draft": current.misstatement_draft.model_copy(update={"amount_cents": draft})}
    )
    rows = store.rows_of(EID)
    rows[rows.index(current)] = stored

    async def task_view(deps: Any) -> dict[str, Any]:
        async with api_client(demo.runner, deps=deps) as client:
            rows = (await client.get(f"/api/v1/engagements/{EID}/review-tasks?status=pending")).json()
        [row] = [r for r in rows if r["target"]["id"] == HIDDEN]
        return cast(dict[str, Any], row)

    plain = await task_view(demo.runner.deps)
    hidden = await task_view(replace(demo.runner.deps, blind_filter=HideOne()))
    assert plain["amount_cents"] == draft
    assert hidden["target"]["misstatement_draft"] is None and hidden["amount_cents"] != draft
    exceptions = {x.id: x for x in await demo.runner.deps.output_store.latest(EID, AuditException)}
    view = await HideOne().judgment(EID, stored)
    assert hidden["amount_cents"] == task_amount(view, exceptions)


# ── 验收 3: 守卫二 ──


def schema_refs(schema: Any, components: dict[str, Any], seen: set[str]) -> set[str]:
    """schema 里直接与间接引用的组件名."""
    found: set[str] = set()
    if isinstance(schema, dict):
        ref = schema.get("$ref")
        if isinstance(ref, str):
            name = ref.rsplit("/", 1)[-1]
            if name not in seen:
                seen.add(name)
                found |= {name} | schema_refs(components[name], components, seen)
        for value in schema.values():
            found |= schema_refs(value, components, seen)
    elif isinstance(schema, list):
        for value in schema:
            found |= schema_refs(value, components, seen)
    return found


def routes_with_judgments_or_trail(spec: dict[str, Any]) -> set[str]:
    """OpenAPI 中响应模型含 Judgment, JudgmentView, TrailEntry 或 TrailEntryView 的 GET 路由."""
    components = spec["components"]["schemas"]
    watched = {"Judgment", "JudgmentView", "TrailEntry", "TrailEntryView"}
    return {
        path
        for path, operations in spec["paths"].items()
        if "get" in operations and schema_refs(operations["get"]["responses"], components, set()) & watched
    }


def test_routes_and_tools_returning_judgments_are_registered() -> None:
    """响应含判断或留痕的 GET 路由恰为 BLIND_ROUTES; 对话工具都在 BLIND_TOOLS 或 PLAIN_TOOLS 中 (不重叠).
    自测: 多一个直接返回 Judgment 的路由时报出它."""
    spec = create_app().openapi()
    assert routes_with_judgments_or_trail(spec) == BLIND_ROUTES
    names = [tool.name for tool in TOOLS]
    assert set(names) == BLIND_TOOLS | PLAIN_TOOLS and not BLIND_TOOLS & PLAIN_TOOLS, sorted(
        set(names) ^ (BLIND_TOOLS | PLAIN_TOOLS)
    )
    extra = json.loads(json.dumps(spec))
    extra["paths"]["/api/v1/engagements/{eid}/raw-judgments"] = {
        "get": {"responses": {"200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/X"}}}}}}
    }
    extra["components"]["schemas"]["X"] = {"properties": {"items": {"$ref": "#/components/schemas/JudgmentView"}}}
    assert routes_with_judgments_or_trail(extra) - BLIND_ROUTES == {"/api/v1/engagements/{eid}/raw-judgments"}


async def test_registered_routes_and_tools_call_the_filter(demo: Started) -> None:
    """清单有实际含义: BLIND_ROUTES 的接口与 BLIND_TOOLS 的工具各至少调用一次过滤器, 其余接口与 PLAIN_TOOLS 的工具
    都不调用. 工具在两表间挪动, 接口漏登记或多登记, 去掉某处的过滤调用 (含只作引用的 get_misstatements, get_report,
    search_trail) 都会失败."""
    # 停在 await_events 时还没有报告: 测试内放一份引用该判断的合成报告草稿 (只改这份内存存储), get_report 才有判断可引
    synthetic = AuditReportVersion(
        id="REPORT-1",
        revision=1,
        opinion_type=OpinionType.qualified,
        sections={ReportSection.opinion: "合成的报告草稿"},
        judgment_refs=[JudgmentRef(id=HIDDEN, version=1)],
    )
    demo.runner.deps.output_store.rows_of(EID).append(synthetic)
    recorder = Recording(HideOne())
    deps = replace(demo.runner.deps, blind_filter=recorder)
    chat = ConciergeChat(deps, GraphStateQuery(demo.runner.app, deps, demo.runner), StaticSource(RuleChatModel()), None)
    routes: dict[str, int] = {}
    async with api_client(demo.runner, deps=deps, concierge=chat) as client:
        for template, path in (await get_paths(demo)).items():
            before = recorder.calls
            await client.get(path)
            routes[template] = recorder.calls - before
    tools: dict[str, int] = {}
    env = ToolEnv(EID, deps, GraphStateQuery(demo.runner.app, deps, demo.runner), 1_000_000)
    for tool in TOOLS:
        before = recorder.calls
        arguments = json.dumps(TOOL_ARGS.get(tool.name, {}))
        assert (await ToolBox().call(env, ToolCall(id="c", name=tool.name, arguments=arguments))).ok, tool.name
        tools[tool.name] = recorder.calls - before
    assert {name for name, n in routes.items() if n} == BLIND_ROUTES, routes
    assert {name for name, n in tools.items() if n} == BLIND_TOOLS, tools


def test_skipped_routes_are_files_or_outside_engagements() -> None:
    """跳过的接口须是现有的 GET 路由, 不返回判断与留痕, 且只能是返回文件 (非 JSON) 或不属于项目的接口: 普通的项目
    JSON 接口登记进来就会退出覆盖守卫, 这里报出."""
    spec = create_app().openapi()
    gets = {path: operations["get"] for path, operations in spec["paths"].items() if "get" in operations}
    assert set(gets) >= SKIPPED_ROUTES
    assert not SKIPPED_ROUTES & routes_with_judgments_or_trail(spec)
    for path in SKIPPED_ROUTES:
        content = gets[path]["responses"]["200"].get("content", {})
        assert "application/json" not in content or not path.startswith("/api/v1/engagements/{eid}"), path
