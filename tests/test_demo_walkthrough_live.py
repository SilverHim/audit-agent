"""需求 R311: 演示走通用例 (R287, tests/test_demo_walkthrough.py) 的真实大模型变体. 同样只经 HTTP 接口照页面的做法走:
新建项目, 每个停点按概览建议导入演示资料, 点 "数据已齐", 过各道闸门 (总体审计计划先退回一次再批准, 需求 R317),
复核逐条决定, RPT-01 先退回再批准, 签字归档;
全部大模型程序走真实模型 (组装根 LLM_MODE live), 功能集为演示配置, 存储为真实 Postgres (同 R287 的连库变体).

驱动, 汇总与检查在 tests/live_walk (浏览器走查共用, 需求 R353). 真实模型的输出不固定, 所以驱动 (drive) 不按停点的
固定顺序走, 每次停下按当时的状态办: 有待决任务先逐条决定, 概览建议导入就导入, 等数据就绪就确认数据已齐, 停在闸门
就按闸门决定. 审计师的决定以演示决定表为准, 按决定表期望的结论下,
不看模型的建议动作 (decision_table): BA-091 与 XJ-499 认定错报 (草稿同 R287), XJ-575 推翻为接受, 其余确认; 争议任务能
维持原判断就维持, 否则 "两者都不采用" 给出期望的结论; 无解释的例外按同一张表认定错报或推翻; failed 与 unattributed
任务 (判断失败, 后者带规则例外, 需求 R347) 重跑, 每个对象至多一次, unattributed 重跑后仍失败才按决定表处理.
FIN-01 按汇总表的候选意见决定 (错报闭环接入前为保留意见), RPT-03 由模型按所选意见起草.

终验 (需求 R263) 在硬性之上另查 (assert_final): 重要性 32431 / 16216 / 1622, 汇总表为 BA-091 与 XJ-499 两笔, 签字报告的
意见类型与汇总表候选一致; 各大模型程序 (判断, 合同与回函抽取, 上传分类) 的结果只在人给了结论的对象上偏离固定输出
(判断只看是否认定错报, accept 与 more_evidence 之间的不同只告警, 同 R251);
签字后再提交决定报 ENGAGEMENT_LOCKED; 留痕的 token 都大于 0.

断言分两级. 硬性: 走到归档, 后台 run_error 始终为空; 每个判断对象有模型自己的结论 (一次通过, 或 failed, unattributed
后重跑一次; 重跑后仍是 unattributed 的, 驱动按决定表推翻后照常走完, 但该对象缺模型结论, 硬性检查不过); 全部证据
指针经存储解析通过; 签字的报告意见类型与 FIN-01 决定一致, 意见段标题与之相符.
软性 (只告警): 判断的建议动作与固定输出 (演示运行副本) 不同的逐条列出. 汇总写 var/eval/demo-live-<UTC>.json.

工作语言 (需求 R343): 环境变量 LIVE_WORKING_LANGUAGE 为 zh 时建中文项目 (缺省 en), 各执行记 zh, 另查判断, 计划叙述与报告
两段都有中文 (live_walk.language_problem, 硬性; 查了的段数须大于 0), 报告标题为中文的; 疑似整段英文与引号内含中文只
告警 (english_run, translated_quote). mock 不另备中文固定输出, 执行都记 en; 检查本身在不联网自测里把执行改记 zh 来核.

配置经 tests.external.live_llm_settings 取 (环境变量, LLM_ENV_FILE 或仓库 .env), 不写进任何文件; 没配时跳过. 不联网的
自测用同一驱动与检查走演示运行副本的固定输出: 离线假存储一遍 (另有先失败再重跑的几种: 一个判断对象, 计划的两个
修订, 报告的两稿; 与带规则例外的对象失败一种), 连库一遍 (同真实模型用例的路径, 只把大模型换成组装根的 mock);
决定的请求体另有合成数据的单元用例.
"""

import os
from pathlib import Path
from typing import Any, cast, get_args

import pytest

from backend.adapters.llm import SwitchableLLMClient
from backend.api.schemas import ReviewRecordIn
from backend.contracts.models import (
    AuditException,
    ExceptionDisposition,
    ProcedureRun,
    TrailEntry,
    TrailKind,
    WorkingLanguage,
)
from backend.orchestration.runner import GraphRunner
from backend.services.judgment_engine import MAX_ATTEMPTS
from tests.external import LLM_KEYS, live_llm_settings
from tests.fakes import api_client
from tests.fixture_sets import XERO
from tests.live_walk import (
    FIXED_CALLS,
    JUDGED,
    JUDGING,
    MAX_CALLS,
    FinPolicy,
    LiveWalk,
    assert_final,
    assert_hard,
    assert_locked,
    candidate_opinion,
    chinese_checks,
    decision_table,
    drive,
    english_run,
    evaluate,
    language_problem,
    review_body,
    translated_quote,
    verdict,
    warn_soft,
)
from tests.test_e2e_mock import real_storage  # 连库装配 (R56 起多个连库用例共用, 搬它不在需求 R353)
from tests.tools.live_budget import EVAL_DIR, Budget, install_budget, write_summary
from tests.xero_fakes import DEMO_FEATURES, ORC1026, XERO17_INVOICES, XERO_DATASET, XERO_PERIOD
from tests.xero_flow import demo_offline, xero_link
from tests.xero_harness import FlakyLLM, LayeredMockLLMClient

GATES = ["ACC-01", "PLN-01/03", "PLN-15", "PLN-15", "FIN-01", "RPT-01", "RPT-01"]  # 依次决定的闸门


def live_language() -> WorkingLanguage:
    """真实模型用例的项目工作语言: 环境变量 LIVE_WORKING_LANGUAGE (zh 或 en), 缺省 en; 别的值报错."""
    value = os.environ.get("LIVE_WORKING_LANGUAGE", "en")
    if value not in get_args(WorkingLanguage):
        raise ValueError(f"LIVE_WORKING_LANGUAGE 须为 zh 或 en: {value!r}")
    return cast(WorkingLanguage, value)


async def real_walk(
    tmp_path: Path,
    budget: Budget,
    name: str,
    fin: FinPolicy = candidate_opinion,
    root: Path = EVAL_DIR,
    language: WorkingLanguage = "en",
    **settings: Any,
) -> tuple[LiveWalk, dict[str, Any], list[TrailEntry]]:
    """组装根的真实依赖 (真实 Postgres 存储与检查点, 规则 SQL, 数据集载入, 登记与函证 mock; 功能集为演示配置, 大模型按
    settings) 上照页面走法走到归档, 项目的工作语言为 language, 外面包上预算; 汇总写 root (缺省 var/eval) 下的
    <name>-<UTC>.json (中途失败也写出已走到的部分).
    返回走法记录, 汇总与该项目的 llm_call 留痕. 真实模型的用例与连库自测共用."""
    async with real_storage(
        tmp_path,
        dataset=XERO_DATASET,
        period=XERO_PERIOD,
        load_dataset=False,
        fresh_profile=False,
        FEATURES=",".join(sorted(DEMO_FEATURES)),
        **settings,
    ) as run:
        install_budget(run.deps, budget)
        service = run.data_service()
        runner = run.s.runner
        walk = LiveWalk()
        try:
            async with api_client(runner, run.owner, deps=run.deps, xero=xero_link(run.deps, service)) as client:
                await drive(client, runner, service, walk, fin=fin, budget=budget, language=language)
                await assert_locked(client, walk)
        finally:
            data = await evaluate(runner, walk, budget)
            budget.reported = True
            write_summary(name, data, root=root)
        calls = await run.deps.trail.list(walk.eid or "", TrailKind.llm_call, limit=10_000)
        return walk, data, calls


@pytest.mark.slow
@pytest.mark.external
@pytest.mark.live(max_calls=MAX_CALLS)  # 用量上限 (需求 R115), LIVE_MAX_CALLS 等可覆盖
async def test_demo_walkthrough_live(tmp_path: Path, live_budget: Budget) -> None:
    """验收 1-4: 真实大模型下照页面走法从新建项目走到归档 (real_walk, LLM_MODE live); 硬性与软性检查见 assert_hard,
    warn_soft; 闸门依次为 ACC-01, PLN-01/03, PLN-15 两次, FIN-01, RPT-01 两次; 留痕的模型为配置的; 汇总写
    var/eval/demo-live-<UTC>.json. LIVE_WORKING_LANGUAGE=zh 时为中文项目 (需求 R343 验收 2, 汇总为 demo-live-zh-*):
    各执行都记 zh, 中文执行写出的文字都有中文 (查了的段数大于 0), 报告标题为中文的 (assert_hard)."""
    configured = live_llm_settings()  # 没配时跳过
    language = live_language()
    XERO.ready()
    llm = {key: getattr(configured, key) for key in LLM_KEYS}
    name = "demo-live" if language == "en" else f"demo-live-{language}"
    walk, data, calls = await real_walk(tmp_path, live_budget, name, language=language, LLM_MODE="live", **llm)
    warn_soft(data)
    assert_hard(data)
    assert_final(data)  # 需求 R263: 重要性, 汇总表, 意见与候选一致, 只在人给了结论的对象上偏离
    assert walk.gates == GATES
    assert (data["language"], list(data["run_languages"])) == (language, [language])  # live 照项目的工作语言记
    assert (data["chinese_checked"] > 0) == (language == "zh")  # 中文项目确有要查的中文执行, 不空转
    assert calls and all(e.model and e.model.startswith(configured.LLM_MODEL) for e in calls)
    assert all(e.input_tokens and e.output_tokens for e in calls)  # 需求 R263 验收 5


@pytest.mark.slow
@pytest.mark.external
async def test_live_path_on_real_storage_with_mock_llm(tmp_path: Path) -> None:
    """连库自测: 真实模型用例的同一条路径 (real_walk), 大模型换成组装根的 mock (按项目数据集读演示运行副本); FIN-01 按
    汇总表的候选意见 (保留) 决定, 报告读保留意见的两稿 (需求 R260). 走到归档, 硬性检查都过, 没有软性差异与重跑, 调用
    FIXED_CALLS 次 (同 R287 的离线走法)."""
    XERO.ready()
    budget = Budget(max_calls=MAX_CALLS)
    walk, data, calls = await real_walk(
        tmp_path / "run",
        budget,
        "demo-live-selftest",
        root=tmp_path,
        LLM_MODE="mock",
    )
    assert_hard(data)
    assert_final(data)
    assert (data["differences"], data["reruns"], data["troubled_tasks"]) == ([], {}, [])
    assert (data["usage"]["calls"], len(calls), walk.gates) == (FIXED_CALLS, FIXED_CALLS, GATES)


async def offline_walk(
    tmp_path: Path, failing: dict[tuple[str, str], int]
) -> tuple[LiveWalk, dict[str, Any], list[AuditException], GraphRunner]:
    """不联网走一遍: 同一驱动与检查, 大模型换成演示运行副本的固定输出 (离线假存储, 规则引擎为 XERO_RULES, 同 R287 的
    离线走法; failing 中的对象先失败几次, 见 FlakyLLM), 外面照样包上预算. FIN-01 按汇总表的候选意见 (保留) 决定, 报告
    读保留意见的两稿 (需求 R260; 真实模型同样按所选意见起草). 汇总写在临时目录.
    返回走法记录, 汇总, 走到归档时存储中各例外的最新版本与 runner (存储照常可读)."""
    XERO.ready()
    llm = SwitchableLLMClient(FlakyLLM(LayeredMockLLMClient([XERO.runtime]), failing), "mock")
    budget = Budget(max_calls=MAX_CALLS)
    walk = LiveWalk()
    async with demo_offline(llm) as (runner, service, client):
        install_budget(runner.deps, budget)
        await drive(client, runner, service, walk, budget=budget)
        await assert_locked(client, walk)
    data = await evaluate(runner, walk, budget)
    write_summary("demo-live-selftest", data, root=tmp_path)
    return walk, data, await runner.deps.output_store.latest(walk.eid or "", AuditException), runner


def stranded(exceptions: list[AuditException]) -> list[str]:
    """走到归档时仍为 unattributed 的例外: 没有待决任务再指向它们 (归档时已没有待决任务), 也没有判断解释它们."""
    return sorted(x.id for x in exceptions if x.disposition is ExceptionDisposition.unattributed)


@pytest.mark.external
async def test_live_driver_on_mock_outputs(tmp_path: Path) -> None:
    """不联网的自测 (offline_walk): 走到归档, 硬性检查都过, 没有软性差异 (固定输出与自己比), 没有重跑与非 normal 任务;
    闸门依次为 ACC-01, PLN-01/03, PLN-15 两次, FIN-01, RPT-01 两次; 调用 FIXED_CALLS 次 (同 R287 的离线走法), 17 个
    判断对象, 决定同演示决定表. 执行都记 en, 没有要查的中文执行; 把各执行改记 zh 再查 (chinese_checks, 审核 R343-02):
    英文的固定输出 (判断, 计划, 报告) 每段都查出 "没有中文"."""
    walk, data, exceptions, runner = await offline_walk(tmp_path, {})
    assert_hard(data)
    assert (data["language"], list(data["run_languages"]), data["report_language"]) == ("en", ["en"], ["en"])
    assert data["chinese_checked"] == 0
    store, eid = runner.deps.output_store, walk.eid or ""
    as_zh = {r.id: r.model_copy(update={"language": "zh"}) for r in await store.latest(eid, ProcedureRun)}
    checks = await chinese_checks(runner, eid, as_zh)
    assert checks["chinese_checked"] > 50 and len(checks["not_in_language"]) == checks["chinese_checked"]
    assert {line.rsplit(": ", 1)[1] for line in checks["not_in_language"]} == {"没有中文"}
    assert_final(data)  # 终验的各项 (需求 R263) 在固定输出上成立: 候选与签字报告都是保留意见
    assert (data["schedule"]["candidate_opinion"], data["report_opinion"]) == ("qualified", ["qualified"])
    # 比对了的对象: CSH-02 只有 BA-091 一条判断, 它与 GEN-02 的 XJ-499 由审计师认定错报, XJ-575 推翻, 都不比
    expected = {"DOC-04": 3, "DOC-06": 2, "DOC-CLASSIFY": 21, "GEN-02": 5, "REV-02": 6, "REV-03": 3}
    assert data["compared"] == expected
    assert stranded(exceptions) == []
    assert (data["differences"], data["reruns"], data["troubled_tasks"]) == ([], {}, [])
    assert (walk.gates, walk.candidate, data["usage"]["calls"]) == (GATES, "qualified", FIXED_CALLS)
    actions = {(r["procedure_id"], r["subject_id"]): r["proposed_action"] for r in data["judgments"]}
    assert (
        len(actions) == JUDGED and (actions["CSH-02", "BA-091"], actions["GEN-02", "XJ-499"]) == ("misstatement",) * 2
    )
    assert data["decisions"] == {"confirm": 14, "misstatement": 2, "override": 1}
    assert len(list(tmp_path.glob("demo-live-selftest-*.json"))) == 1


ORC1021 = XERO17_INVOICES[0]  # REV-02 判断对象里没有规则例外的一张 (有例外的 ORC1026 失败时转 unattributed)


D04 = ["X-CSH-02-1-D04-1"]  # CSH-02 的例外: 各组都只有第 1 次执行为 BA-091 新建的这一条 D04


@pytest.mark.parametrize(
    ("failing", "kind"),
    [
        ([("REV-02", ORC1021)], "failed"),
        # 带 REV-08 的 R03-1, 判断失败转 unattributed 任务, 驱动同 failed 先重跑一次 (需求 R347 验收 3)
        ([("REV-02", ORC1026)], "unattributed"),
        # 第一条规则例外是本次新建的 D04 (v1 即 unattributed); 重跑沿用它, 不另建 (第 1 轮审核 R347-02)
        ([("CSH-02", "BA-091")], "unattributed"),
        # 计划两个修订与报告两稿各整条失败一次 (需求 R317)
        ([("PLN-15", "r1"), ("PLN-15", "r2"), ("RPT-03", "qualified-r1"), ("RPT-03", "qualified-r2")], "failed"),
    ],
    ids=["rev02", "rev02_findings", "csh02", "revisions"],
)
@pytest.mark.external
async def test_live_driver_reruns_a_failed_object_once(
    tmp_path: Path, failing: list[tuple[str, str]], kind: str
) -> None:
    """自测重跑一路: 各对象 (REV-02 的 ORC1021 与带规则例外的 ORC1026, CSH-02 的 BA-091, 计划或报告的每个修订) 前
    MAX_ATTEMPTS 次调用输出不是 JSON, 成为 kind 类任务 (failed; 带规则例外的为 unattributed); 驱动对每个各重跑一次,
    第 2 次执行给出结果, 照常走到归档, 硬性检查都过 (没有缺结论的对象); 汇总记各重跑一次与这几条任务, 每个多调
    MAX_ATTEMPTS 次. 重跑出的判断与固定输出的建议动作相同; 走到归档时没有仍为 unattributed 的例外 (重跑前置的那一版
    由重跑的判断解释, 随复核决定归因)."""
    _, data, exceptions, _ = await offline_walk(tmp_path, dict.fromkeys(failing, MAX_ATTEMPTS))
    assert_hard(data)
    # 失败按 (程序, fixture_key) 注入; 驱动对整条失败的报告按修订计重跑 (rerun_key 的 r1, r2), 不带意见类型
    counted = [(pid, key.removeprefix("qualified-")) for pid, key in failing]
    assert data["reruns"] == {f"{pid} {subject}": 1 for pid, subject in counted}
    assert sorted(data["troubled_tasks"]) == sorted([pid, subject, kind] for pid, subject in counted)
    assert data["missing_subjects"] == []
    assert data["usage"]["calls"] == FIXED_CALLS + MAX_ATTEMPTS * len(failing)
    assert data["decisions"]["rerun"] == len(failing)
    rows = {(r["procedure_id"], r["subject_id"]): r for r in data["judgments"]}
    for key in (k for k in failing if k[0] in JUDGING):  # 判断对象 (与固定输出的建议动作相同)
        assert rows[key]["same"], rows[key]
    assert stranded(exceptions) == []
    assert sorted(x.id for x in exceptions if x.procedure_id == "CSH-02") == D04  # 重跑不另建 D04


APES110 = "APES 110 Code of Ethics for Professional Accountants (including Independence Standards)"  # 9 个词, 照原文


@pytest.mark.parametrize(
    ("text", "problem", "run"),
    [
        ("应收账款高估 $75,000, 依 ASA 450 第 5 段累计.", None, False),
        (
            "合同第 5.1 条写明 “title to and risk in the goods pass to the customer when it signs the delivery note”.",
            None,
            False,
        ),
        (
            '合同第 5.1 条写明 "title to and risk in the goods pass to the customer when it signs the delivery note".',
            None,
            False,
        ),
        ("Victoria Property Agency Pty Ltd 为关联方, 科目 Interest income 少记.", None, False),
        ("结论: one two three four five six seven.", None, False),
        ("结论: one two three four five six seven eight.", None, True),
        (f"我们按照 {APES110} 的职业道德要求独立于公司.", None, True),
        (
            "ASA 505 第 14 段: The auditor shall investigate exceptions to determine whether or not they are "
            "indicative of misstatements.",
            None,
            True,
        ),
        ("The accrued interest confirmed by the bank was not recorded in the general ledger.", "没有中文", True),
    ],
    ids=[
        "chinese",
        "quoted_clause",
        "ascii_quotes",
        "names",
        "seven_words",
        "eight_words",
        "apes110",
        "standard",
        "english",
    ],
)
def test_language_problem(text: str, problem: str | None, run: bool) -> None:
    """需求 R343 验收 2 的检查自身 (审核 R343-01, 04): 硬性只看有没有中文 (language_problem); 引号外 8 个以上相连的
    英文词只告警 (english_run). 中文正文, 引号 (全角或半角双引号) 里的英文原文, 公司名与科目名, 7 个词都不告警; 8 个词,
    照原文写的 APES 110 全名与准则原文告警而不判失败; 全英文判 "没有中文"."""
    assert (language_problem(text), english_run(text) is not None) == (problem, run)


@pytest.mark.parametrize(
    ("text", "translated"),
    [
        ("合同写明 “title passes to the customer on delivery”.", False),
        ("合同写明 “客户签收送货单时货物所有权转移”.", True),
    ],
    ids=["original", "translated"],
)
def test_translated_quote(text: str, translated: bool) -> None:
    """审核 R343-03: 引号里出现中文的引文告警 (Xero 的合同与回函原文都是英文, 多半是把原文译掉了)."""
    assert (translated_quote(text) is not None) == translated


@pytest.mark.parametrize(
    ("actual", "fixture", "deviates"),
    [
        ("more_evidence", "accept", False),  # 真实模型 en 一次的 GEN-02 XJ-397, 449, 601: 只告警
        ("accept", "more_evidence", False),
        ("misstatement", "accept", True),
        ("accept", "misstatement", True),
        ("misstatement", "misstatement", False),
    ],
)
def test_judgment_deviates_only_on_misstatement(actual: str, fixture: str, deviates: bool) -> None:
    """R263 验收 4 的判断类口径 (同 R251): 只有是否认定错报不同才算偏离."""
    assert (verdict(actual) != verdict(fixture)) == deviates


def view_of(kind: str, subject: str, **extra: Any) -> dict[str, Any]:
    """合成的复核队列一项 (只填驱动用到的字段)."""
    task = {
        "id": f"T-{subject}",
        "kind": kind,
        "procedure_id": "GEN-02",
        "run_id": "RUN-GEN-02-1",
        "target_version": 1,
        "subject_id": None,
    }
    target: dict[str, Any] = {"subject_id": subject} if kind != "unattributed" else {"detail": {"entry_no": subject}}
    return {"task": task, "target": target, "allowed_resolutions": [], "draft_rules": {}} | extra


def test_review_bodies_follow_the_decision_table() -> None:
    """各类任务的决定都合复核接口的请求体 (ReviewRecordIn), 且按演示决定表: 争议任务原判断与期望一致时维持, 否则
    "两者都不采用" 给出期望的结论, 期望错报时附草稿; 无解释的例外先重跑一次, 仍失败再认定错报或推翻; failed 同一
    对象第二次失败即判失败."""
    table, walk = decision_table("USR-1"), LiveWalk()
    both = {"allowed_resolutions": ["a6_upheld", "a7_upheld", "neither"]}
    cases = {
        "a6 认定错报, 期望错报": (
            view_of("dispute", "XJ-499", **both, draft_rules={"a6_upheld": "required"}),
            "a6_upheld",
        ),
        "a6 接受, 期望错报": (view_of("dispute", "XJ-499", **both, draft_rules={"a6_upheld": "hidden"}), "neither"),
        "a6 认定错报, 期望确认": (
            view_of("dispute", "XJ-397", **both, draft_rules={"a6_upheld": "required"}),
            "neither",
        ),
        "a6 接受, 期望确认": (view_of("dispute", "XJ-397", **both, draft_rules={"a6_upheld": "hidden"}), "a6_upheld"),
        "期望推翻": (view_of("dispute", "XJ-575", **both, draft_rules={"a6_upheld": "hidden"}), "neither"),
    }
    for name, (view, resolution) in cases.items():
        body = review_body(view, table, walk)
        record = ReviewRecordIn.model_validate(body)
        assert record.dispute_resolution == resolution, name
        assert (record.misstatement_draft is not None) == ("期望错报" in name), name
    # unattributed (需求 R347): 第一次先重跑, 重跑后仍失败才按决定表认定错报或推翻
    for subject, then in (("XJ-499", "misstatement"), ("XJ-601", "override")):
        unattributed = view_of("unattributed", subject)
        assert ReviewRecordIn.model_validate(review_body(unattributed, table, walk)).decision == "rerun", subject
        assert ReviewRecordIn.model_validate(review_body(unattributed, table, walk)).decision == then, subject
    failed = view_of("failed", "XJ-430")
    failed["task"]["subject_id"] = "XJ-430"
    assert ReviewRecordIn.model_validate(review_body(failed, table, walk)).decision == "rerun"
    with pytest.raises(pytest.fail.Exception, match="重跑后又失败"):
        review_body(failed, table, walk)


def test_rerun_counts_the_whole_group() -> None:
    """一轮失败算一组 (第 1 轮审核 R347-03): 对一条可重跑任务下 rerun 时, 同程序同一次执行其余待决的可重跑任务随之
    重跑 (DecisionSubmit 一并关闭), 驱动一并计数. 同轮 XJ-430 failed 与 XJ-601 unattributed, 对 XJ-430 rerun 后两者
    各记一次; 同程序的 normal 任务, 别的执行的与别的程序的失败任务不计 (第 2 轮审核 R347-14). XJ-601 第 2 轮仍是
    unattributed 时已重跑过, 按决定表推翻."""
    table, walk = decision_table("USR-1"), LiveWalk()
    failed, unattributed, normal = (
        view_of("failed", "XJ-430"),
        view_of("unattributed", "XJ-601"),
        view_of("normal", "XJ-397"),
    )
    failed["task"]["subject_id"] = "XJ-430"
    elsewhere = view_of("failed", "BA-091")
    elsewhere["task"] |= {"procedure_id": "CSH-02", "subject_id": "BA-091"}
    earlier = view_of("failed", "XJ-575")
    earlier["task"] |= {"subject_id": "XJ-575", "run_id": "RUN-GEN-02-0"}

    pending = [failed, unattributed, normal, elsewhere, earlier]
    assert review_body(failed, table, walk, pending)["decision"] == "rerun"
    assert walk.failures == {("GEN-02", "XJ-430"): 1, ("GEN-02", "XJ-601"): 1}
    assert review_body(unattributed, table, walk, [unattributed])["decision"] == "override"
    assert walk.failures == {("GEN-02", "XJ-430"): 1, ("GEN-02", "XJ-601"): 1}
