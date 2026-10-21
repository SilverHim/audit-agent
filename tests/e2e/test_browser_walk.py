"""需求 R345: 浏览器端到端走查. 模拟审计师在页面上走完 Xero 演示: 注册登录, 新建演示项目, 按数据管理页的提示分批
导入演示资料并确认数据已齐, 各闸门在表单视图按决定表决定, 复核任务逐条在表单视图处理, 签字归档. 驱动 (walk.mjs)
只经页面操作推进, 读数经接口; 每步截图, 记控制台报错与失败请求, 超时记 "卡住"; 报告写 var/e2e/<UTC>/report.md.

决定同 R311 (tests/test_demo_walkthrough_live.py 的 decision_table: BA-091 与 XJ-499 认定错报, XJ-575 推翻为接受,
其余确认; 争议能维持原判断就维持, 否则 "两者都不采用"; failed 与 unattributed 任务每个对象至多重跑一次, 后者重跑后
仍失败才按决定表处理, 需求 R347, R355), PLN-15 先退回一次再批准 (同 R317), RPT-01 先退回一次再签字. 错报闭环
(需求 R356, Q34): 在人工任务页登记 FIN-08 第 1 轮管理层回复 (补造的 manual/fin08_round1.json: 两条都同意更正), 等更正时
在总览的更正卡片上提交更正通知 (该轮的更正分录 AJE-X01, AJE-X05), FIN-01 按候选意见为无保留. 功能集取 .env.example 的
FEATURES (演示配置). 后端连测试库
(TEST_DATABASE_URL), 存储与检查点都是真实的; mock 模式读演示运行副本的固定输出, live 模式调真实模型 (配置同 R311,
经 tests.external.live_llm_settings 取, 没配时跳过). 两个用例都标 slow, 默认测试不跑.
"""

import os
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from backend.services.judgment_engine import MAX_ATTEMPTS
from tests.e2e.harness import backend, frontend, out_dir, require_tools, run_walk, write_report
from tests.external import LLM_KEYS, live_llm_settings
from tests.fakes import PLAN_REJECT_REASON
from tests.fixture_sets import XERO
from tests.live_walk import (
    CONFIRM_CONCLUSION,
    CORRECTED_AMOUNTS,
    MAX_CALLS,
    RERUN_REASON,
    RERUNNABLE,
    REVISED_BY,
    LiveWalk,
    assert_hard,
    decision_table,
    evaluate,
    warn_soft,
)
from tests.tools.live_budget import Budget, install_budget
from tests.xero_fakes import DEMO_FEATURES, ORC1026, REPO, XERO17_ANNOTATION, XERO17_MATERIALITY, round_file
from tests.xero_flow import FIN08_CONCLUSION, FIN08_REASON, READY_REASON, SYDNEY
from tests.xero_harness import FlakyLLM

pytestmark = [pytest.mark.slow, pytest.mark.external]
# 各段的超时 (毫秒): 页面上的一个操作, 打开页面 (开发服务首次打开要预构建依赖), 后台一段执行 (live 时模型调用多,
# 放宽); 驱动整体 (秒)
TIMEOUTS = {
    "mock": {"action_ms": 30_000, "nav_ms": 120_000, "idle_ms": 180_000},
    "live": {"action_ms": 60_000, "nav_ms": 120_000, "idle_ms": 1_800_000},
}
WALK_TIMEOUT_S = {"mock": 1_800, "live": 7_200}
# 驱动认的页面文字 (英文界面, src/frontend/src/locales/en.ts)
LABELS = {
    "register": "Create account",
    "newEngagement": "New engagement",
    "create": "Create and open",
    "demoTitle": "Demo data",
    "demoStages": {
        "acceptance": "acceptance files",
        "fieldwork": "fieldwork ledgers and files",
        "manual": "representation letter",
    },
    "ready": "Data complete, continue planning",
    "readyConfirm": "Confirm data complete",
    "record": "Record decision",
    "section": "Section",
    "comment": "Comment",
    "noExpert": "No auditor's expert is needed",
    "signed": "Signed and archived",
    "manual": {  # 人工任务页与 FIN-08 登记表
        "submit": "Record result",
        "communicatedAt": "Communicated at",
        "communicatedTo": "Communicated to",
        "respondedAt": "Responded at",
        "response": "Response (verbatim)",
        "agrees": "Agrees",
        "disagrees": "Does not agree",
        "performedOn": "Date performed",
        "conclusion": "Conclusion",
        "reason": "Reason (optional)",
    },
    "correction": {  # 总览的更正卡片
        "title": "Waiting for client correction",
        "posted": "Client has posted adjustments",
        "submit": "Submit",
    },
}


# 依次决定的闸门: PLN-15 先退回再批准, RPT-01 先退回再签字
GATES = ["ACC-01", "PLN-01/03", "PLN-15", "PLN-15", "FIN-01", "RPT-01", "RPT-01"]
# 复核与人工任务的决定计数: 演示决定表的 17 条 (BA-091, XJ-499 认定错报, XJ-575 推翻, 其余确认), FIN-03 与 FIN-09 各两条
# 确认, FIN-08 人工结果一条
DECISIONS = {"confirm": 18, "manual_result": 1, "misstatement": 2, "override": 1}


def manual_config() -> dict[str, Any]:
    """人工任务的做法 (只有 FIN-08 第 1 轮, 同 tests.xero_flow.fin08_record 的登记): 沟通与回复的时刻, 对象与原文取
    补造的沟通记录, 两条错报都同意更正, 执行日为回复当天 (悉尼), 结论与理由同 HTTP 驱动."""
    record = round_file(1)
    return {
        "FIN-08": {
            "rows": len(record["misstatement_sources"]),
            "communicated_at": record["communicated_at"],
            "communicated_to": record["recipient"],
            "responded_at": record["responded_at"],
            "response": record["response"],
            "agrees": True,
            "performed_on": datetime.fromisoformat(record["responded_at"]).astimezone(SYDNEY).date().isoformat(),
            "conclusion": FIN08_CONCLUSION,
            "reason": FIN08_REASON,
        }
    }


def walk_config(out: Path, base: str, mode: str, fin_policy: str) -> dict[str, Any]:
    """驱动的配置: 页面地址, 注册用的账号 (随机用户名与口令; 姓名同报告签字块), 决定表与各闸门的决定, 人工任务与更正
    通知的做法, 超时."""
    table = {
        subject: {"decision": e.decision, "reason": e.reason, "draft": e.draft, "conclusion": e.conclusion}
        for subject, e in decision_table("").items()
    }
    return {
        "base": base,
        "locale": "en-AU",
        "out_dir": str(out),
        "shots_dir": str(out / "shots"),
        "user": {"full_name": "Jordan Ellis", "username": f"e2e-{uuid4().hex[:12]}", "password": uuid4().hex},
        "client_name": "Demo Company (AU)",
        "labels": LABELS,
        "table": table,
        "materiality": XERO17_MATERIALITY.model_dump(),
        "fin_policy": fin_policy,
        "fin_reason": "Opinion follows the schedule of uncorrected misstatements (candidate {candidate}).",
        "rpt_reject": {
            "section": XERO17_ANNOTATION.section.value,
            "comment": XERO17_ANNOTATION.comment,
            "reason": "The basis for opinion paragraph must name the financial reporting framework.",
        },
        "plan_reject_reason": PLAN_REJECT_REASON,
        "ready_reason": READY_REASON,
        "rerun_reason": RERUN_REASON,
        "rerunnable": sorted(RERUNNABLE),
        "revised_by": REVISED_BY,
        "confirm_conclusion": CONFIRM_CONCLUSION,
        "manual": manual_config(),
        "correction": {"entries": round_file(1)["adjustment_entry_nos"]},
        "timeouts": TIMEOUTS[mode],
        "max_steps": 200,
    }


def walk_of(results: dict[str, Any]) -> LiveWalk:
    """驱动结果转成 R311 汇总 (evaluate) 用的走法记录."""
    return LiveWalk(
        eid=results["eid"],
        owner=results["owner"],
        gates=results["gates"],
        fin=results["fin"],
        candidate=results["candidate"],
        failures=Counter({(pid, subject): n for pid, subject, n in results["failures"]}),
        troubled={task: (pid, subject, kind) for task, (pid, subject, kind) in results["troubled"].items()},
        decisions=[(task, decision) for task, decision in results["decisions"]],
    )


async def browser_walk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    budget: Budget,
    mode: str,
    fin_policy: str,
    failing: dict[tuple[str, str], int] | None = None,
    **llm: str,
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    """起后端与前端, 跑驱动, 写报告; 返回 (驱动结果, R311 口径的汇总, 报告路径). 没有测试库或走查工具时跳过.
    failing: (程序, fixture_key) → 先失败几次 (主模型换成 FlakyLLM 包装, 见 tests.xero_harness), 自测驱动的重跑一路."""
    require_tools()
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("需设 TEST_DATABASE_URL: 已迁移并建好检查点表的测试库")
    XERO.ready()  # 演示运行副本 (mock 的固定输出) 齐备
    out = out_dir(datetime.now(UTC))
    env = {
        "DATABASE_URL": url,
        "CHECKPOINT_BACKEND": "postgres",
        "DATASET_ROOT": str(REPO / "data"),
        "EVIDENCE_ROOT": str(tmp_path / "evidence"),
        "LLM_MODE": mode,
        "FEATURES": ",".join(sorted(DEMO_FEATURES)),
        "SETTINGS_SECRET_KEY": "",  # 不读库里存的模型设置, 只用这里给的
        "XERO_CLIENT_ID": "",
        "XERO_CLIENT_SECRET": "",
        **llm,
    }
    async with backend(monkeypatch, env) as (api, app):
        if failing:  # 组装根的主模型与判断引擎共用这一个客户端
            client = app.state.deps.llm
            client.reload(FlakyLLM(client.inner, failing), client.model_name)
        install_budget(app.state.deps, budget)
        async with frontend(out, api) as base:
            results = await run_walk(walk_config(out, base, mode, fin_policy), out, WALK_TIMEOUT_S[mode])
        data = await evaluate(app.state.runner, walk_of(results), budget) if results["eid"] else {}
    return results, data, write_report(out, mode, results, data)


def assert_correction_path(results: dict[str, Any], data: dict[str, Any]) -> None:
    """错报闭环 (需求 R356): 等更正处提交了一次更正通知 (两条错报, 更正分录 AJE-X01, AJE-X05), 两条都已更正, 汇总表
    为空, FIN-01 按候选为无保留意见."""
    [(kind, ids, entries)] = results["corrections"]
    assert (kind, len(ids), entries) == ("adjustments_posted", 2, round_file(1)["adjustment_entry_nos"])
    expected = {"amounts": [], "corrected": CORRECTED_AMOUNTS, "total_cents": 0, "candidate_opinion": "unqualified"}
    assert data["schedule"] == expected
    assert results["fin"] == "unqualified"


async def test_browser_walk_mock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """mock 模式在本机走一遍: 走到归档, 没有问题; 闸门序列为 GATES; R311 的硬性检查都过 (每个判断对象有结论, 指针都能
    解析, 报告意见与 FIN-01 一致); 决定同演示决定表, 没有重跑; 走 FIN-08 登记与更正通知两步, FIN-01 按汇总表的候选意见
    为无保留 (需求 R356)."""
    results, data, report = await browser_walk(tmp_path, monkeypatch, Budget(max_calls=MAX_CALLS), "mock", "candidate")
    assert results["issues"] == [], report
    assert results["reached_archive"] and results["gates"] == GATES, report
    assert_hard(data)
    assert (data["reruns"], data["troubled_tasks"]) == ({}, []), report
    assert data["decisions"] == DECISIONS, report
    assert_correction_path(results, data)


async def test_browser_walk_mock_reruns_unattributed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """需求 R355: ORC1026 (REV-02 的判断对象, 带 REV-08 的 R03-1) 前 MAX_ATTEMPTS 次输出不是 JSON, 判断失败转
    unattributed 任务; 驱动在页面上先重跑一次 (同 R347 的 HTTP 驱动), 第 2 次执行给出结果, 照常走到归档, 没有问题,
    硬性检查都过; 汇总记重跑一次与这条任务, 其余决定同 mock 走查."""
    failing = {("REV-02", ORC1026): MAX_ATTEMPTS}
    results, data, report = await browser_walk(
        tmp_path, monkeypatch, Budget(max_calls=MAX_CALLS), "mock", "candidate", failing
    )
    assert results["issues"] == [], report
    assert results["reached_archive"] and results["gates"] == GATES, report
    assert_hard(data)
    assert (data["reruns"], data["troubled_tasks"]) == ({f"REV-02 {ORC1026}": 1}, [["REV-02", ORC1026, "unattributed"]])
    assert data["decisions"] == DECISIONS | {"rerun": 1}, report
    assert_correction_path(results, data)


@pytest.mark.live(max_calls=MAX_CALLS)
async def test_browser_walk_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live_budget: Budget) -> None:
    """live 模式 (真实模型): 同一驱动走到归档, R311 的硬性检查都过; 走查发现的问题与建议动作的差异写进报告, 不在这里
    判失败 (由协调者看报告另立需求). FIN-01 按汇总表的候选意见决定."""
    configured = live_llm_settings()  # 没配时跳过
    llm = {key: str(getattr(configured, key)) for key in LLM_KEYS}
    results, data, report = await browser_walk(tmp_path, monkeypatch, live_budget, "live", "candidate", **llm)
    live_budget.reported = True
    warn_soft(data)
    assert results["reached_archive"], report
    assert_hard(data)
