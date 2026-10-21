"""Xero 程序级真实大模型评测: 现有程序部分 (需求 R250; 设计稿 v2.1 第 5.1, 6.1, 6.2 节) 与分录测试部分 (需求 R251;
第 5.3, 6.3 节). 两部分为两组评测 (Suite), 走同一测试台与检查.

走法: FV-51 测试台 (tests/xero_harness) 连库载入 T0 副本 (db_deps: 演示数据导入两阶段, 合同在上传时由 DOC-04 抽取),
再按依赖分层跑 REV-02 与 CSH-02 的闭包 (run_programs: 先 DOC-04, PLN-07, PLN-01 定重要性, 再 CSH-04, REV-03 等; 每层
先自动确认上游判断, 所以 REV-03 的判断确认后才跑 REV-02); 某程序出了 failed 任务即照 rerun 决定重跑一次. 只有预期
文件里的五个程序 (DOC-04, DOC-06, REV-03, REV-02, CSH-02) 交真实模型 (RoutedLLMClient 按程序分流, 经预算), 其余调用
(上传分类, A7 复核) 读副本的运行副本.

预期在 tests/fixtures/xero/expected_existing.json: hard 不符即失败, soft 只告警; 对象为两段之并 (三份合同, 两户回函,
三份合同的收入评价, 六张发票, BA-091 的函证判断, 共 15 个), 按 (程序, 对象) 取观察值 (observe) 逐字段比 (not_cited
列不得引用的来源). 硬性另有: 每个对象都有通过四级校验的结果 (failed 或 unattributed (带规则例外的对象判断失败, 需求
R347) 后重跑一次仍没有, 算不过); 每个对象都真的交给了模型; 没有失败的执行 (Agent 抛出的异常由节点转成失败的执行); 全部
证据指针经真实存储解析通过. 汇总写 var/eval/xero-existing-<UTC>.json.

分录测试部分 (NEW, 预期 tests/fixtures/xero/expected_new.json): 功能取演示配置 (DEMO_FEATURES, 含 journals), 测试台
跑 GEN-02 的闭包: GEN-05 的 7 条例外由规则引擎产出 (连库时为真实 SQL), 只有 GEN-02 (7 笔凭证) 交真实模型, 预算 21
次 (各三次尝试). hard: XJ-499 为错报 (75000, Existence, overstated, 科目含 710 或 Office equipment), XJ-397, 405, 430,
449, 601 都不是错报; 另须没有未处理的例外 (GEN-05 的每条例外都由 GEN-02 的判断解释); soft: XJ-575 为 more_evidence.
汇总写 var/eval/xero-new-<UTC>.json.

配置经 tests.external.live_llm_settings 取, 没配时跳过. 不联网的自测把 "真实模型" 换成运行副本的固定输出: 连库一遍
(同真实模型的路径), 离线一遍 (一个对象先失败再重跑, 回函抽错, 分录判断漏报, 误报与科目写错, 一笔凭证重跑后仍失败
留下没有解释的例外时硬性检查报出).
"""

import json
import warnings
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from backend.agents.ingest import extracted_from
from backend.contracts.models import (
    AuditException,
    Confirmation,
    ExceptionDisposition,
    Judgment,
    Procedure,
    ProcedureRun,
    ProposedAction,
    ReviewTask,
    RunStatus,
    TaskKind,
    TaskStatus,
    TrailKind,
)
from backend.contracts.ports import LLMClient
from backend.services.features import BASELINE
from backend.services.judgment_engine import MAX_ATTEMPTS
from tests.live_walk import latest_model_judgments, unresolved_pointers
from tests.test_xero_injections import Build, offline_build
from tests.test_xero_injections_db import live_client
from tests.tools.live_budget import EVAL_DIR, Budget, summary, write_summary
from tests.xero_fakes import DEMO_FEATURES, XERO_RULES
from tests.xero_flow import offline_runner
from tests.xero_harness import FlakyLLM, LayeredMockLLMClient, RoutedLLMClient, XeroHarness, db_deps, run_programs
from tests.xero_injection import XeroCase

pytestmark = pytest.mark.external  # 都读 Xero 副本

Fields = dict[str, dict[str, dict[str, Any]]]  # 程序 → 对象 → 字段 → 值


@dataclass(frozen=True)
class Suite:
    """一组评测: 汇总文件名, 预期 (hard, soft 两段), 测试台要跑的程序, 证据指针数的下限, 测试台的功能集, 预算 (调用次数
    上限), 例外须都有判断解释的程序 (findings_from). 对象为两段之并, 交真实模型的程序即预期里出现的程序."""

    name: str
    expected: dict[str, Fields]
    programs: list[str]
    min_pointers: int
    features: frozenset[str]
    max_calls: int  # 各对象三次尝试
    findings_from: tuple[str, ...] = ()

    @property
    def subjects(self) -> list[tuple[str, str]]:
        return sorted({(pid, s) for part in self.expected.values() for pid, objects in part.items() for s in objects})

    @property
    def live_programs(self) -> list[str]:
        return sorted({pid for pid, _ in self.subjects})

    def db_build(self) -> Build:
        """连库测试台, 功能集为该组的."""

        async def build(case: XeroCase, llm: LLMClient) -> XeroHarness:
            return await db_deps(case, llm=llm, features=self.features)

        return build


def expected(name: str) -> dict[str, Fields]:
    value: dict[str, Fields] = json.loads(
        (Path(__file__).parent / "fixtures" / "xero" / f"expected_{name}.json").read_text(encoding="utf-8")
    )
    return value


# 现有程序: REV-02 与 CSH-02 的闭包 (DOC-04 先跑, DOC-06, REV-03), 五个程序交真实模型; 15 个对象
EXISTING = Suite("xero-existing", expected("existing"), ["REV-02", "CSH-02"], 30, BASELINE, max_calls=45)
# 分录测试: GEN-02 的闭包 (GEN-05), 只有 GEN-02 交真实模型; 7 笔凭证; 功能取演示配置 (含 journals); GEN-05 的例外须都
# 由 GEN-02 的判断解释 (R251 验收 4 "无未处理异常")
NEW = Suite("xero-new", expected("new"), ["GEN-02"], 7, DEMO_FEATURES, max_calls=21, findings_from=("GEN-05",))
SUITES = [EXISTING, NEW]


async def observe(h: XeroHarness) -> Fields:
    """存储中各对象通过校验的结果, 取预期比对的字段: DOC-04 为合同 (条款类型排序), DOC-06 为当前函证中由回函抽取的
    (证据指向回函, 同 ingest.extracted_from), 判断程序为模型自己给出的最近一次判断 (latest_model_judgments, 重跑过的
    取重跑的), cited 为证据与推理链引用的来源. 没有结果的对象不在其中."""
    eid, store = h.engagement.id, h.deps.output_store
    found: Fields = {"DOC-04": {}, "DOC-06": {}}
    for c in await h.deps.financial_store.contracts(eid):
        found["DOC-04"][c.id] = c.model_dump(mode="json", include={"counterparty_abn", "amount_cents", "sign_date"})
        found["DOC-04"][c.id]["term_types"] = sorted(t.term_type.value for t in c.terms)
    for c in await store.latest(eid, Confirmation):
        if extracted_from(c, c.reply_document_id):
            fields = {"confirmed_balance_cents", "balance_date", "accrued_interest_cents"}
            found["DOC-06"][c.bank_account_id] = c.model_dump(mode="json", include=fields)
    for (pid, subject), j in latest_model_judgments(await store.latest(eid, Judgment)).items():
        draft = j.misstatement_draft
        found.setdefault(pid, {})[subject] = {
            "judgment_id": j.id,
            "proposed_action": j.proposed_action.value,
            "misstatement": j.proposed_action is ProposedAction.misstatement,
            "amount_cents": draft.amount_cents if draft else None,
            "direction": draft.direction.value if draft else None,
            "assertion": draft.assertion.value if draft else None,
            "account": f"{draft.account} {draft.account_code or ''}".strip() if draft else None,
            "cited": sorted({p.source_id for p in [*j.evidence, *(p for s in j.reasoning_chain for p in s.evidence)]}),
        }
    return found


def differences(observed: Fields, expected: Fields) -> list[str]:
    """observed 与一段预期 (hard 或 soft) 不符之处, 每处一句; not_cited 为不得引用的来源, account_any 为科目 (名称与
    代码) 须含其中之一. 没有结果的对象不在这里报 (见 evaluate 的 missing)."""
    found = []
    for pid, objects in sorted(expected.items()):
        for subject, fields in sorted(objects.items()):
            got = observed.get(pid, {}).get(subject)
            if got is None:
                continue
            for name, want in fields.items():
                if name == "not_cited":
                    cited = sorted(set(want) & set(got["cited"]))
                    found += [f"{pid} {subject} 引用了上下文外的 {', '.join(cited)}"] if cited else []
                elif name == "account_any":
                    if not any(w in (got["account"] or "") for w in want):
                        found.append(f"{pid} {subject} account: {got['account']!r}, 须含 {' 或 '.join(want)}")
                elif got[name] != want:
                    found.append(f"{pid} {subject} {name}: {got[name]!r}, 预期 {want!r}")
    return found


async def evaluate(h: XeroHarness, routed: RoutedLLMClient, budget: Budget, suite: Suite) -> dict[str, Any]:
    """汇总 (R250 验收 4, R251 验收 4) 与两级检查的材料, 都取存储 (中途出错时也能汇总已写入的): 用量 (调用与 token
    取预算, 尝试与失败级别取交真实模型的程序的 llm_call 留痕), 各对象交真实模型的次数与观察值, 重跑的程序与对象,
    失败的执行, 待决的非 normal 任务, 该组 findings_from 中没有判断解释的例外 (不在任一判断的 exception_ids 中, 或已
    置 unattributed; 判断待复核时例外仍为 open, 所以按 exception_ids 看), 缺结果与没交给模型的对象, hard 与 soft 的
    不符之处, 证据指针数与解析不了的."""
    eid, store = h.engagement.id, h.deps.output_store
    observed = await observe(h)
    live = Counter((r.procedure_id, r.fixture_key) for route, r in routed.requests if route == "live")
    entries = await h.deps.trail.list(eid, TrailKind.llm_call, limit=10_000)
    runs = await store.latest(eid, ProcedureRun)
    times = Counter(r.procedure_id for r in runs)
    pack = {p.id: p for p in await store.latest(eid, Procedure)}
    judgments = await store.latest(eid, Judgment)
    explained = {x for j in judgments for x in j.exception_ids}
    data: dict[str, Any] = {
        "engagement_id": eid,
        "usage": summary([e for e in entries if e.procedure_id in suite.live_programs], budget),
        "live_calls": {f"{pid} {s}": live[pid, s] for pid, s in suite.subjects},
        "observed": {f"{pid} {s}": observed[pid][s] for pid, s in suite.subjects if s in observed.get(pid, {})},
        "reruns": {pid: pack[pid].rerun_subject_ids for pid, n in sorted(times.items()) if n > 1},  # 空列表为整条重跑
        "failed_runs": sorted(r.id for r in runs if r.status is RunStatus.failed),
        "open_tasks": sorted(
            f"{t.id} {t.kind.value}"
            for t in await store.latest(eid, ReviewTask)
            if t.status is TaskStatus.pending and t.kind is not TaskKind.normal
        ),
        "unexplained": sorted(
            x.id
            for x in await store.latest(eid, AuditException)
            if x.procedure_id in suite.findings_from
            and (x.id not in explained or x.disposition is ExceptionDisposition.unattributed)
        ),
        "missing": [f"{pid} {s}" for pid, s in suite.subjects if s not in observed.get(pid, {})],
        "unrouted": [f"{pid} {s}" for pid, s in suite.subjects if not live[pid, s]],
        "hard": differences(observed, suite.expected["hard"]),
        "soft": differences(observed, suite.expected["soft"]),
        "min_pointers": suite.min_pointers,
    }
    data["pointers"], data["unresolved_pointers"] = await unresolved_pointers(offline_runner(h.deps), eid)
    return data


def assert_hard(data: dict[str, Any]) -> None:
    """硬性检查 (R250 验收 2, 4, R251 验收 2, 4): 每个对象都有结果且都交给了模型, 没有失败的执行, 没有未解释的例外,
    hard 段全部相符, 证据指针全部解析通过 (且多于该组的下限)."""
    keys = ("missing", "unrouted", "failed_runs", "unexplained", "hard", "unresolved_pointers")
    problems = {key: data[key] for key in keys}
    assert not any(problems.values()), problems
    assert data["pointers"] > data["min_pointers"]


def warn_soft(data: dict[str, Any]) -> None:
    """软性检查 (验收 3): soft 段的不符之处逐条告警."""
    for line in data["soft"]:
        warnings.warn(line, stacklevel=1)


async def evaluated(
    build: Build, case: XeroCase, live: LLMClient, budget: Budget, suite: Suite = EXISTING, root: Path = EVAL_DIR
) -> dict[str, Any]:
    """在 case 上装配测试台 (build), 该组交真实模型的程序的调用交 live (经 budget), 其余读副本的运行副本; 跑该组程序
    的闭包 (出了 failed 任务的程序重跑一次), 预算已超限即失败. 汇总写 root (缺省 var/eval) 下的 <组名>-<UTC>.json,
    中途出错也写出已走到的部分 (装配就出错时只有 live_budget 夹具收尾写的用量)."""
    mock = LayeredMockLLMClient([case.supplements / "llm"])
    routed = RoutedLLMClient(mock, live, (), budget, programs=suite.live_programs)
    h = await build(case, routed)
    try:
        await run_programs(h, suite.programs, rerun=True)
        budget.check()
    finally:
        data = await evaluate(h, routed, budget, suite)
        budget.reported = True
        write_summary(suite.name, data, root=root)
    return data


@pytest.mark.slow
@pytest.mark.live(max_calls=EXISTING.max_calls)  # 用量上限 (需求 R115), LIVE_MAX_CALLS 等可覆盖
async def test_xero_existing_programs_live(xero_case: Any, live_budget: Budget) -> None:
    """验收 1-4: 连库测试台在 T0 副本上, DOC-04, DOC-06, REV-03, REV-02, CSH-02 交真实模型 (预算 45 次); 硬性与软性
    检查见 assert_hard, warn_soft; 汇总写 var/eval/xero-existing-<UTC>.json."""
    live = live_client()  # 先核对数据与测试库, 没配模型时跳过
    data = await evaluated(EXISTING.db_build(), xero_case("T0"), live, live_budget)
    warn_soft(data)
    assert_hard(data)


@pytest.mark.slow
@pytest.mark.parametrize("suite", SUITES, ids=lambda s: s.name)
async def test_live_path_on_postgres_with_mock_llm(xero_case: Any, tmp_path: Path, suite: Suite) -> None:
    """连库自测 (两组): 真实模型用例的同一路径 (分录测试组的 GEN-05 跑真实 SQL), "真实模型" 换成运行副本的固定输出.
    硬性检查都过, soft 没有不符, 没有重跑与非 normal 任务; 各对象各交一次 (现有程序 15 个, 分录测试 7 笔), 预算计同样
    次数; 汇总写在临时目录."""
    case = xero_case("T0")
    budget = Budget(max_calls=suite.max_calls)
    mock = LayeredMockLLMClient([case.supplements / "llm"])
    data = await evaluated(suite.db_build(), case, mock, budget, suite, tmp_path)
    assert_hard(data)
    assert (data["soft"], data["reruns"], data["open_tasks"]) == ([], {}, [])
    assert set(data["live_calls"].values()) == {1} and data["usage"]["calls"] == len(suite.subjects)
    assert len(list(tmp_path.glob(f"{suite.name}-*.json"))) == 1


async def test_eval_reruns_once_and_catches_a_wrong_extraction(xero_case: Any, tmp_path: Path) -> None:
    """离线自测 (离线测试台, 规则预置 XERO_RULES): REV-03 对 CON-X02 的前 3 次调用输出不是 JSON, 成为 failed 任务, 重跑
    一次后得出 accept (第 2 次执行的判断), REV-02 照常判断; DOC-06 把 BA-090 的余额日抽成回函的落款日. 硬性检查恰报出
    这一处, 外加三份合同没交给模型 (离线测试台的合同在载入时按固定输出抽取, DOC-04 不调模型); soft 没有不符."""
    case = xero_case("T0")
    runtime, wrong = case.supplements / "llm", tmp_path / "wrong"
    reply = json.loads((runtime / "DOC-06" / "BA-090.json").read_text(encoding="utf-8"))
    (wrong / "DOC-06").mkdir(parents=True)
    (wrong / "DOC-06" / "BA-090.json").write_text(json.dumps(reply | {"balance_date": "2026-10-08"}))
    live = FlakyLLM(LayeredMockLLMClient([wrong, runtime]), {("REV-03", "CON-X02"): 3})
    data = await evaluated(offline_build(XERO_RULES), case, live, Budget(max_calls=EXISTING.max_calls), root=tmp_path)
    assert data["hard"] == ["DOC-06 BA-090 balance_date: '2026-10-08', 预期 '2026-09-30'"]
    assert data["unrouted"] == ["DOC-04 CON-X01", "DOC-04 CON-X02", "DOC-04 CON-X03"]
    assert (data["missing"], data["failed_runs"], data["soft"]) == ([], [], [])
    assert data["reruns"] == {"REV-03": ["CON-X02"]} and data["live_calls"]["REV-03 CON-X02"] == 4
    assert data["observed"]["REV-03 CON-X02"]["judgment_id"] == "J-REV-03-2-CON-X02"
    with pytest.raises(AssertionError, match="BA-090"):
        assert_hard(data)


async def test_eval_reruns_an_unattributed_object_once(xero_case: Any, tmp_path: Path) -> None:
    """离线自测 rerun_failed 的 unattributed 一路 (需求 R347, 第 1 轮审核 R347-06): CSH-02 对 BA-091 的前 MAX_ATTEMPTS
    次调用输出不是 JSON, 该账户带本次新建的 D04, 判断失败转 unattributed 任务; 测试台照 rerun 决定重跑一次, 第 2 次
    执行得出判断 (沿用第 1 次的 D04), 不再有待决的非 normal 任务; hard 只报出判断 ID 是第 2 次执行的 (预期按不重跑写),
    soft 没有不符."""
    case = xero_case("T0")
    live = FlakyLLM(LayeredMockLLMClient([case.supplements / "llm"]), {("CSH-02", "BA-091"): MAX_ATTEMPTS})
    data = await evaluated(offline_build(XERO_RULES), case, live, Budget(max_calls=EXISTING.max_calls), root=tmp_path)
    assert data["reruns"] == {"CSH-02": ["BA-091"]} and data["live_calls"]["CSH-02 BA-091"] == MAX_ATTEMPTS + 1
    assert data["observed"]["CSH-02 BA-091"]["judgment_id"] == "J-CSH-02-2-BA-091"
    assert (data["missing"], data["failed_runs"], data["open_tasks"]) == ([], [], [])
    assert [line.split(":")[0] for line in data["hard"]] == ["CSH-02 BA-091 judgment_id"]  # 只差判断 ID
    assert data["soft"] == []


# ── 分录测试 (需求 R251) ──


@pytest.mark.slow
@pytest.mark.live(max_calls=NEW.max_calls)  # 用量上限 (需求 R115), LIVE_MAX_CALLS 等可覆盖
async def test_xero_journal_programs_live(xero_case: Any, live_budget: Budget) -> None:
    """R251 验收 1-4 (按 Q32 改写, 只做 GEN-02): 连库测试台在 T0 副本上 (演示配置的功能), GEN-05 的 7 条例外由真实 SQL
    产出, GEN-02 的 7 笔凭证交真实模型 (预算 21 次); 硬性与软性检查见 assert_hard, warn_soft; 汇总写
    var/eval/xero-new-<UTC>.json."""
    live = live_client()  # 先核对数据与测试库, 没配模型时跳过
    data = await evaluated(NEW.db_build(), xero_case("T0"), live, live_budget, NEW)
    warn_soft(data)
    assert_hard(data)


MISSTATED_499 = {"type": "factual", "assertion": "Existence", "amount_cents": 75000, "direction": "overstated"}
WRONG_JOURNALS = [  # GEN-02 固定输出的改动 (凭证 → 覆盖的字段), 硬性与软性应报出的
    pytest.param(
        {"XJ-499": {"proposed_action": "accept", "misstatement_draft": None}},
        [
            "GEN-02 XJ-499 proposed_action: 'accept', 预期 'misstatement'",
            "GEN-02 XJ-499 amount_cents: None, 预期 75000",
            "GEN-02 XJ-499 direction: None, 预期 'overstated'",
            "GEN-02 XJ-499 assertion: None, 预期 'Existence'",
            "GEN-02 XJ-499 account: None, 须含 710 或 Office equipment",
        ],
        [],
        id="missed",
    ),
    pytest.param(
        {
            "XJ-499": {"misstatement_draft": MISSTATED_499 | {"account": "Depreciation", "account_code": None}},
            "XJ-397": {  # 草稿金额取它自己 840 贷方行, 能过引用级校验
                "proposed_action": "misstatement",
                "misstatement_draft": MISSTATED_499
                | {"account": "Historical Adjustment", "account_code": "840", "amount_cents": 1921221},
            },
            "XJ-575": {"proposed_action": "accept"},
        },
        [
            "GEN-02 XJ-397 misstatement: True, 预期 False",
            "GEN-02 XJ-499 account: 'Depreciation', 须含 710 或 Office equipment",
        ],
        ["GEN-02 XJ-575 proposed_action: 'accept', 预期 'more_evidence'"],
        id="false-positive-and-wrong-account",
    ),
]


@pytest.mark.parametrize(("changes", "hard", "soft"), WRONG_JOURNALS)
async def test_journal_eval_catches_wrong_judgments(
    xero_case: Any, tmp_path: Path, changes: dict[str, dict[str, Any]], hard: list[str], soft: list[str]
) -> None:
    """R251 离线自测 (离线测试台, 规则预置 XERO_RULES 的 GEN-05 七条例外, 演示配置的功能): GEN-02 的固定输出按 changes
    改动 (漏报 XJ-499; 或 XJ-499 科目写错, XJ-397 误报, XJ-575 判成 accept) 时, 硬性与软性恰报出改动处; 7 笔凭证都有
    判断且都交给了模型, 例外都有判断解释, 没有失败的执行."""
    case = xero_case("T0")
    runtime, wrong = case.supplements / "llm", tmp_path / "wrong"
    (wrong / "GEN-02").mkdir(parents=True)
    for subject, update in changes.items():
        output = json.loads((runtime / "GEN-02" / f"{subject}.json").read_text(encoding="utf-8"))
        (wrong / "GEN-02" / f"{subject}.json").write_text(json.dumps(output | update), encoding="utf-8")
    build = offline_build(XERO_RULES, NEW.features)
    budget = Budget(max_calls=NEW.max_calls)
    data = await evaluated(build, case, LayeredMockLLMClient([wrong, runtime]), budget, NEW, tmp_path)
    assert (data["hard"], data["soft"]) == (hard, soft)
    assert (data["missing"], data["unrouted"], data["failed_runs"], data["unexplained"]) == ([], [], [], [])
    assert set(data["live_calls"].values()) == {1}
    with pytest.raises(AssertionError, match="XJ-499"):
        assert_hard(data)


async def test_journal_eval_reports_an_unexplained_exception(xero_case: Any, tmp_path: Path) -> None:
    """R251 离线自测 "无未处理异常" 一项: GEN-02 对 XJ-499 的调用两次执行都输出不是 JSON (照 rerun 重跑一次仍失败), 它
    的例外 (R09-2) 置 unattributed, 没有判断解释: unexplained 恰报出它, 该凭证缺结果, 留一个待决的 unattributed 任务."""
    case = xero_case("T0")
    live = FlakyLLM(LayeredMockLLMClient([case.supplements / "llm"]), {("GEN-02", "XJ-499"): 2 * MAX_ATTEMPTS})
    build = offline_build(XERO_RULES, NEW.features)
    data = await evaluated(build, case, live, Budget(max_calls=NEW.max_calls), NEW, tmp_path)
    assert (data["unexplained"], data["missing"]) == (["X-GEN-05-1-R09-2"], ["GEN-02 XJ-499"])
    assert data["reruns"] == {"GEN-02": ["XJ-499"]} and data["open_tasks"] == ["T-X-GEN-05-1-R09-2-v3 unattributed"]
    with pytest.raises(AssertionError, match="X-GEN-05-1-R09-2"):
        assert_hard(data)
