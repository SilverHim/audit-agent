"""CUAD 合同抽取与条款评价的真实大模型评测 (需求 R249; docs/云端开发/design/external/cuad.md 第 3 节).

走法: 金标准 10 份合同各登记一份文件 (文件 ID 与合同 ID 用别名), 证据库为测试内的只读实现 (CuadPages: 页文本由
pdfplumber 从本地 PDF 抽出, 同 FileEvidenceStore.pages). DOC-04 经 ingest.extract_contract (IngestAgent 与上传处理
共用的抽取: 生产的提示词, JudgmentEngine.call_llm 与四级校验), 被审计单位名取金标准的 client_name; 通过校验的货物
合同 (CUAD-01 至 06) 再经 A6 的 REV-03 (FieldworkAgent 的判断公共流程; 功能取基线, 不开 critic, A7 不复核). 评分与
检查见 tests/tools/cuad_eval (score_contract, aggregate): hard 不符即失败, soft 只告警. 每份合同的输出, 各次尝试的
校验结果与评分, 汇总与用量写 var/eval/cuad-<UTC>.json 并打印. 用量超出预算时停发, 失败原因为 "超预算, 评测未完成",
与质量失败分开. CUAD_EXTENDED=1 时另跑扩展集 30 份 (只跑 DOC-04, 只比 CSV 自动真值, 只看趋势).

真实模型的配置经 tests.external.live_llm_settings 取, 没配时跳过; 没有本地 CUAD 时跳过. 不联网的自测把 "真实模型"
换成脚本回复, 跑同一路径. 合同文本出自 CUAD v1 (The Atticus Project, CC BY 4.0), 署名见 tests/fixtures/cuad/README.md.
"""

import functools
import json
import os
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from backend.adapters.llm import SwitchableLLMClient, env_provider, make_live_client
from backend.agents.base import gather_all
from backend.agents.fieldwork import FieldworkAgent
from backend.agents.ingest import extract_contract
from backend.contracts.models import Contract, DocumentRef, Judgment, TermType, TrailKind
from backend.contracts.ports import LLMClient
from tests.external import live_llm_settings, require_external
from tests.fakes import (
    ENGAGEMENT,
    NUMBAT_PROFILE,
    FakeFinancialStore,
    FakeRegistry,
    FakeTrail,
    ScriptedLLM,
    make_ctx,
    make_deps,
    make_run,
)
from tests.tools.cuad_eval import (
    LLM,
    GoldContract,
    aggregate,
    contract_key,
    csv_rows,
    csv_truth,
    load_extended,
    load_gold,
    pdf_pages,
    score_contract,
    termination_truth,
)
from tests.tools.live_budget import EVAL_DIR, Budget, BudgetExceeded, install_budget, summary, write_summary

pytestmark = pytest.mark.external  # 都读本地 CUAD
TIMEOUT = httpx.Timeout(300.0, connect=15.0)  # 测试内 LiveLLMClient 的超时 (长合同一次调用可能较久)
GOLD_LIMITS = {"max_calls": 48, "max_input_tokens": 550_000, "max_output_tokens": 150_000}  # 16 次调用各三次尝试
EXTENDED_LIMITS = {"max_calls": 90, "max_input_tokens": 1_000_000}
UNFINISHED = "超预算, 评测未完成"


def local_cuad() -> Path:
    """本地 CUAD 的 CUAD_v1/ 目录, 没有时跳过."""
    return require_external("cuad", ["CUAD_v1/master_clauses.csv", "CUAD_v1/full_contract_pdf"]) / "CUAD_v1"


@functools.cache
def pages_of(path: Path) -> list[str]:
    """PDF 的逐页文本 (同 FileEvidenceStore.pages), 每份只抽一次."""
    return pdf_pages(path)


class CuadPages:
    """只读证据库: pages 按文件 ID (别名) 取本地 PDF 的逐页文本; 不写入."""

    def __init__(self, root: Path, sources: Mapping[str, str]) -> None:
        self.root = root  # CUAD_v1/full_contract_pdf
        self.sources = dict(sources)  # 别名 → 相对 root 的 PDF 路径

    async def pages(self, eid: str, document_id: str) -> list[str]:
        return pages_of(self.root / self.sources[document_id])


def document(contract: GoldContract) -> DocumentRef:
    """别名为文件 ID 的合同 PDF 登记 (CUAD 是公开的复印件, 可靠性按 external_copy)."""
    return DocumentRef(
        id=contract.alias,
        type="contract",
        file_name=f"{contract.alias}.pdf",
        sha256="",
        page_count=contract.page_count,
        reliability="external_copy",
        status="received",
        uploaded_at=datetime.now(UTC),
    )


def assert_finished(data: Mapping[str, Any]) -> None:
    """用量超出预算 (调用被拒, 没评完) 时失败, 原因为 UNFINISHED, 与质量失败分开."""
    if not data["finished"]:
        pytest.fail(f"{UNFINISHED}: {data['budget']}")


def check(data: Mapping[str, Any]) -> None:
    """软性不符逐条告警, 硬性不符即失败 (先查是否评完)."""
    assert_finished(data)
    for line in data["aggregate"]["soft"]:
        warnings.warn(line, stacklevel=1)
    assert data["aggregate"]["hard"] == []


async def evaluate(
    name: str,
    cuad: Path,
    contracts: Sequence[GoldContract],
    llm: LLMClient,
    model: str,
    budget: Budget,
    *,
    rev03: bool,
    root: Path = EVAL_DIR,
) -> dict[str, Any]:
    """对 contracts 跑 DOC-04 (并发), rev03 为真时再对通过的货物合同跑 REV-03; 调用都经 budget, 超限即停发 (已
    出的结果照常评分). 评分, 汇总与用量写 root/<name>-<UTC>.json (中途超限也写), 返回写出的数据."""
    pdfs = cuad / "full_contract_pdf"
    rows = csv_rows(cuad)
    client = SwitchableLLMClient(llm, model)
    trail = FakeTrail()
    documents = {c.alias: document(c) for c in contracts}
    evidence = CuadPages(pdfs, {c.alias: c.pdf for c in contracts})

    def deps_with(store: FakeFinancialStore) -> Any:
        deps = make_deps(FakeRegistry(), trail, financial_store=store, llm=client)
        return replace(deps, evidence_store=evidence)

    deps = deps_with(FakeFinancialStore(documents=list(documents.values())))
    install_budget(deps, budget)
    extracted: dict[str, Contract | None] = {}
    judgments: dict[str, Judgment] = {}
    template, run = deps.library.load("DOC-04"), make_run("DOC-04")

    async def extract(contract: GoldContract) -> None:
        profile = NUMBAT_PROFILE.model_copy(update={"legal_name": contract.client_name})
        doc = documents[contract.alias]
        extracted[contract.alias] = await extract_contract(deps, ENGAGEMENT.id, template, run, doc, profile)

    finished = True
    try:
        await gather_all(extract(c) for c in contracts)
        goods = [c.alias for c in contracts if c.rev03 is not None and extracted.get(c.alias) is not None]
        if rev03 and goods:
            store = FakeFinancialStore([documents[a] for a in goods], contracts=[extracted[a] for a in goods])
            rev_deps = deps_with(store)
            ctx = make_ctx(rev_deps, "REV-03")
            params = {"period_end": ENGAGEMENT.period_end}  # 同 build_run 的解析结果 (派生引用取项目期末)
            ctx = replace(ctx, run=ctx.run.model_copy(update={"params_snapshot": params}))
            patch = await FieldworkAgent(rev_deps).run(ctx)
            judgments = {j.subject_id: j for j in patch.get("judgments", [])}
    except BudgetExceeded:
        finished = False
    entries = await trail.list(ENGAGEMENT.id, TrailKind.llm_call, limit=10_000)
    scores = []
    for c in contracts:
        pages = pages_of(pdfs / c.pdf)
        truth = termination_truth(rows[contract_key(c.csv_filename)], pages)
        scores.append(score_contract(c, extracted.get(c.alias), pages, truth))
    actions = {
        c.alias: (judgments[c.alias].proposed_action.value if c.alias in judgments else None, c.rev03)
        for c in contracts
        if c.rev03 is not None and rev03
    }
    calls = [e for e in entries if e.procedure_id == "DOC-04"]
    data: dict[str, Any] = {
        "model": model,
        "finished": finished,
        "budget": budget.message() if not finished else None,
        "contracts": {
            c.alias: {
                "score": score,
                "levels": [e.payload["level_failed"] or "passed" for e in calls if e.payload["fixture_key"] == c.alias],
                "output": None if extracted.get(c.alias) is None else extracted[c.alias].model_dump(mode="json"),
                "rev03": None if c.alias not in judgments else judgments[c.alias].model_dump(mode="json"),
            }
            for c, score in zip(contracts, scores, strict=True)
        },
        "rev03": actions,
        "aggregate": aggregate(scores, actions),
        "usage": summary(entries, budget),
    }
    budget.reported = True
    write_summary(name, data, root=root)
    return data


def live_client() -> tuple[LLMClient, str]:
    """真实模型的客户端 (超时 TIMEOUT) 与模型名; 没配时跳过."""
    settings = live_llm_settings()
    provider = env_provider(settings)
    llm = make_live_client(provider, settings.LLM_BASE_URL, settings.LLM_MODEL, settings.LLM_API_KEY, timeout=TIMEOUT)
    return llm, settings.LLM_MODEL


@pytest.mark.slow
@pytest.mark.live(**GOLD_LIMITS)  # 用量上限 (需求 R115), LIVE_MAX_CALLS 等可覆盖
async def test_cuad_gold_live(live_budget: Budget) -> None:
    """验收 1-5, 7: 金标准 10 份跑 DOC-04, 通过的货物合同跑 REV-03 (预算 48 次调用, 输入 550k, 输出 150k token);
    硬性: 至少 9 份通过四级校验, ABN 都为 null, 反编造检查通过率 >= 95%, 无未处理异常; 软性只告警. CUAD-03 的
    印刷页码 (11-14) 由引用级校验拦下, 不计入页码命中 (没通过的输出不评分). 汇总写 var/eval/cuad-<UTC>.json."""
    cuad = local_cuad()
    llm, model = live_client()
    data = await evaluate("cuad", cuad, list(load_gold().values()), llm, model, live_budget, rev03=True)
    check(data)


@pytest.mark.slow
@pytest.mark.live(**EXTENDED_LIMITS)
async def test_cuad_extended_live(live_budget: Budget) -> None:
    """验收 6: CUAD_EXTENDED=1 时跑扩展集 30 份 (extended.json), 只跑 DOC-04, 真值全由 CSV 生成 (csv_truth); 上限
    90 次调用, 输入 1,000k token; 结果只看趋势 (不判硬性), 只查评完. 汇总写 var/eval/cuad-extended-<UTC>.json."""
    if os.getenv("CUAD_EXTENDED") != "1":
        pytest.skip("扩展集默认不跑, 设 CUAD_EXTENDED=1")
    cuad = local_cuad()
    llm, model = live_client()
    rows = csv_rows(cuad)
    truths = [
        csv_truth(e, rows[contract_key(e.csv_filename)], pages_of(cuad / "full_contract_pdf" / e.pdf))
        for e in load_extended()
    ]
    data = await evaluate("cuad-extended", cuad, truths, llm, model, live_budget, rev03=False)
    assert_finished(data)


# ── 不联网的自测: 脚本回复走同一路径 ──


def doc04_reply(alias: str, **term_changes: Any) -> str:
    """离线 mock 的 DOC-04 输出 (tests/fixtures/cuad/llm, CUAD-01 与 03 才有); term_changes 改第 3 条条款的字段."""
    value = json.loads((LLM / "DOC-04" / f"{alias}.json").read_text(encoding="utf-8"))
    value["terms"][2] |= term_changes
    return json.dumps(value)


def rev03_reply(alias: str, term_id: str, page: int) -> str:
    """REV-03 的一份 accept 判断 (事实指向控制权转移条款, 准则 AASB15-P38, 推论收尾)."""
    term = {
        "type": "contract",
        "source_type": "document",
        "source_id": alias,
        "locator": term_id,
        "page": page,
        "reliability": "external_copy",
    }
    standard = {
        "type": "standard",
        "source_type": "standard",
        "source_id": "AASB15-P38",
        "locator": "para 38",
        "page": None,
        "reliability": None,
    }
    chain = [
        {"kind": "fact", "text": f"{term_id} sets when title and risk pass.", "evidence": [term], "from_steps": []},
        {
            "kind": "standard",
            "text": "Indicators of control (AASB 15 para 38).",
            "evidence": [standard],
            "from_steps": [],
        },
        {"kind": "inference", "text": "Control passes at that point.", "evidence": [], "from_steps": [0, 1]},
    ]
    return json.dumps(
        {
            "conclusion": f"Control passes to the customer at the point set by {term_id}.",
            "reasoning_chain": chain,
            "evidence": [term, standard],
            "counter_argument": "A side agreement moving title would change the transfer point.",
            "proposed_action": "accept",
            "misstatement_draft": None,
            "self_confidence": 0.8,
        }
    )


def scripted() -> ScriptedLLM:
    """自测的脚本回复: CUAD-01 一次通过, CUAD-03 先把一条条款的页码写成印刷页码 14 (引用级失败) 再通过, 两份都再
    给 REV-03 的 accept; 其余 8 份三次都不是 JSON (DOC-04 失败, 不跑 REV-03)."""
    replies = {a: ["not json"] * 3 for a in load_gold()}
    replies["CUAD-01"] = [doc04_reply("CUAD-01"), rev03_reply("CUAD-01", "CUAD-01-T5", 3)]
    replies["CUAD-03"] = [
        doc04_reply("CUAD-03", page=14),
        doc04_reply("CUAD-03"),
        rev03_reply("CUAD-03", "CUAD-03-T1", 3),
    ]
    return ScriptedLLM(replies)


async def test_evaluation_scores_scripted_replies(tmp_path: Path) -> None:
    """自测 (本地 CUAD, 不联网): 只有 CUAD-01, 03 通过四级校验, 硬性检查报出 "通过四级校验 2/10"; CUAD-03 第 1 次
    因印刷页码 14 判引用级失败, 第 2 次通过; 两份的当事方, 签订日都对, 条款都不算编造, 控制权转移条款都对上金标准;
    REV-03 只判这两份, 都为 accept (DOC-04 没通过的货物合同记为没有结果); 用量 29 次 (DOC-04 1 + 2 + 8 x 3,
    REV-03 2); 汇总写在临时目录."""
    gold = list(load_gold().values())
    data = await evaluate(
        "cuad", local_cuad(), gold, scripted(), "scripted", Budget(max_calls=48), rev03=True, root=tmp_path
    )
    assert data["finished"] and data["usage"]["calls"] == 29
    contracts = data["contracts"]
    assert contracts["CUAD-03"]["levels"] == ["reference", "passed"]
    assert contracts["CUAD-02"]["levels"] == ["syntax"] * 3
    for alias in ("CUAD-01", "CUAD-03"):
        score = contracts[alias]["score"]
        assert (score["counterparty"], score["sign_date"], score["abn_null"], score["fabricated"]) == (
            True,
            True,
            True,
            [],
        )
        assert score["found"][TermType.control_transfer.value]
    assert {a: got for a, (got, _) in data["rev03"].items()} == {
        "CUAD-01": "accept",
        "CUAD-02": None,
        "CUAD-03": "accept",
        "CUAD-04": None,
        "CUAD-05": None,
        "CUAD-06": None,
    }
    assert data["aggregate"]["hard"] == ["通过四级校验 2/10, 门槛 9"]
    assert len(list(tmp_path.glob("cuad-*.json"))) == 1


async def test_evaluation_over_budget_is_unfinished(tmp_path: Path) -> None:
    """自测: 预算只够 5 次调用时停发, 汇总照写, 标为没评完; assert_finished 以 "超预算, 评测未完成" 失败."""
    gold = list(load_gold().values())
    data = await evaluate(
        "cuad", local_cuad(), gold, scripted(), "scripted", Budget(max_calls=5), rev03=True, root=tmp_path
    )
    assert (data["finished"], data["usage"]["calls"]) == (False, 5)
    assert len(list(tmp_path.glob("cuad-*.json"))) == 1
    with pytest.raises(pytest.fail.Exception, match=UNFINISHED):
        assert_finished(data)
