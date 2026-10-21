"""需求 R340: 大模型程序的提示词按执行记录的语言要求输出语言 (提示词末尾的共用片段 _output_language.md.j2).

守卫逐个渲染 knowledge/prompts 下的程序提示词 (收集时列出, 新模板自动有用例), 截出末尾的输出语言一节核对; 抽取类
另核对同一份 mock 输出在两种语言下抽出的字段相同, fixture_key 不随语言变 (校验见 tests/test_output_validator.py).
"""

import re
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any, get_args

import pytest
from jinja2 import ChainableUndefined

from backend.agents.base import AgentDeps
from backend.agents.ingest import DocumentIntake, extract_contract, upload_run
from backend.agents.sheets import SheetIntake
from backend.contracts.models import DocumentType, Reliability, TrailEntry, TrailKind, WorkingLanguage
from backend.core.config import get_settings
from backend.services.judgment_engine import JudgmentEngine, OutputValidator
from backend.services.procedure_library import prompt_env
from tests.external import REPO
from tests.fakes import (
    ENGAGEMENT,
    NUMBAT_CONTRACTS,
    NUMBAT_PROFILE,
    NUMBAT_REPLIES,
    UPLOAD_FIXTURES,
    FakeEvidenceStore,
    FakeRegistry,
    FakeTrail,
    LayeredMockLLM,
    make_deps,
)

EID = ENGAGEMENT.id
PROMPTS = get_settings().KNOWLEDGE_ROOT / "prompts"
TEMPLATES = sorted(p.name for p in PROMPTS.glob("*.md.j2") if not p.name.startswith("_"))  # 共用片段以 _ 开头
# 抽取类 → 用工作语言写的说明性字段 (其余字段是抽出的值, 照原文)
EXTRACTION = {
    "ACC-08-extract.md.j2": [],
    "DOC-04.md.j2": [],
    "DOC-06.md.j2": [],
    "DOC-REGISTRY.md.j2": [],
    "DOC-CLASSIFY.md.j2": ["reason"],
    "DOC-SHEET.md.j2": ["reason"],
}
LATER: dict[str, str] = {}  # 暂不带输出语言一节的模板 → 归哪条需求 (到时加上这一节, 从这里去掉; RPT-03 已由 R341 加上)
SECTION = "\n## Output language\n"
NAMES: dict[WorkingLanguage, str] = {"zh": "Simplified Chinese", "en": "Australian English"}
# 口径 → 关键词: 输出语言一节须有一行同时含这几个词 (不分大小写), {name} 换成语言名
LITERALS = ("instructions above give in quotation marks", "exactly as given", "do not translate them")  # 引号固定值照抄
NARRATIVE = {
    "文字字段用工作语言": ("write every free-text field", "conclusions", "reasoning", "in {name}"),
    "引文不翻译, 照原文放在引号里": ("do not translate quotations", "quotation marks", "original language"),
    "客户等名称, 科目名与准则原文照输入": ("names of the client", "account names", "wording of standards"),
    "提示词给的引号固定值照抄": LITERALS,
    "JSON 键, 枚举, ID 与指针字段照规定": ("json keys", "enum values", "`locator`"),
}
EXTRACTED = {
    "抽出的值照原文 (模板规则要求的格式除外), 不翻译": (
        "copy every value",
        "exactly as written",
        "original language",
        "apart from the formatting the rules above require",
        "do not translate",
    ),
    "提示词给的引号固定值照抄": LITERALS,
    "JSON 键, 枚举, ID 与指针字段照规定": ("json keys", "enum values", "`locator`"),
}


class Blank(ChainableUndefined):
    """守卫渲染时缺的变量: 取属性, 下标都得到它自己, 迭代为空, 转字符串为空串, 比较为假, 运算得到它自己 (只为走到
    末尾的输出语言一节, 不核对其余内容; 其余内容按头注释的变量渲染另见 test_knowledge)."""

    __slots__ = ()

    def _same(self, *_: object) -> "Blank":
        return self

    def _false(self, _: object) -> bool:
        return False

    __add__ = __radd__ = __sub__ = __rsub__ = __mul__ = __rmul__ = __truediv__ = __rtruediv__ = _same
    __lt__ = __le__ = __gt__ = __ge__ = _false


def rendered(template: str, language: WorkingLanguage) -> str:
    """按 output_language 渲染一次, 其余变量取 Blank (tojson 遇到它写 null)."""
    env = prompt_env(PROMPTS)
    env.undefined = Blank
    env.policies["json.dumps_kwargs"] = {"sort_keys": True, "default": lambda _: None}
    return env.get_template(template).render(output_language=language)


def language_section(template: str, language: WorkingLanguage) -> str | None:
    """渲染一次 (rendered), 截出末尾的输出语言一节; 没有为 None."""
    parts = rendered(template, language).split(SECTION)
    assert len(parts) <= 2, f"{template} 有不止一节输出语言"
    return parts[1] if len(parts) == 2 else None


def missing_rules(section: str, rules: dict[str, tuple[str, ...]], name: str) -> list[str]:
    """找不到对应行 (同时含全部关键词, 不分大小写) 的口径."""
    lines = [line.casefold() for line in section.splitlines()]
    words = {rule: [w.format(name=name).casefold() for w in ws] for rule, ws in rules.items()}
    return [rule for rule, ws in words.items() if not any(all(w in line for w in ws) for line in lines)]


def test_registered_templates_exist() -> None:
    """登记的抽取类与暂缓的模板都在 prompts 目录里 (改名或删去时这里先失败); 目录里列得出程序提示词."""
    assert TEMPLATES and set(EXTRACTION) | set(LATER) <= set(TEMPLATES)


@pytest.mark.parametrize("language", get_args(WorkingLanguage))
@pytest.mark.parametrize("template", TEMPLATES)
def test_every_prompt_states_the_output_language(template: str, language: WorkingLanguage) -> None:
    """验收 1: 每个程序提示词末尾有一节输出语言 (共用片段): 写明工作语言, 只出现这一种语言名; 写文字的程序要求文字
    字段用工作语言, 引文, 客户等名称, 科目名与准则原文不翻译; 抽取类要求抽出的值照原文, 只有登记的说明性字段用工作
    语言. 新模板缺这一节时失败 (暂缓的见 LATER)."""
    section = language_section(template, language)
    if template in LATER:
        assert section is None, f"{template} 已有输出语言一节, 从 LATER 去掉"
        return
    assert section is not None, f"{template} 缺输出语言一节: 末尾引入 _output_language.md.j2"
    assert "\n## " not in section, f"{template} 的输出语言一节须在末尾"
    name, other = NAMES[language], NAMES["en" if language == "zh" else "zh"]
    assert f"\nWorking language: {name}.\n" in section and other not in section
    if template not in EXTRACTION:
        assert missing_rules(section, NARRATIVE, name) == []
        assert "explanatory field" not in section
        return
    assert missing_rules(section, EXTRACTED, name) == []
    [line] = [line for line in section.splitlines() if "explanatory field" in line]
    if EXTRACTION[template]:  # 说明性字段与登记的整个相等 (个数与顺序), 写在一行里
        assert re.findall(r"`(\w+)`", line) == EXTRACTION[template] and line.endswith(f" in {name}.")
    else:
        assert "no explanatory field" in line and f"in {name}" not in section


@pytest.mark.parametrize(
    ("template", "literals"),
    [
        ("CSH-02.md.j2", ['`account` "Cash"', '"Interest income"', '"Interest expense"']),
        ("REV-02.md.j2", ['`account` "Revenue"']),
    ],
)
def test_literal_account_names_stay_as_given(template: str, literals: list[str]) -> None:
    """审核 R340-01: 提示词写死的科目名 (不在输入里; 下游 misstatement_objective 按它配审计目标) 在 zh 的提示词里原样在,
    输出语言一节写明引号给出的固定值照抄, 不翻译."""
    head, section = rendered(template, "zh").split(SECTION)  # 字面值只在正文里找: 输出语言一节自带示例 (审核 R340-07)
    assert [literal for literal in literals if literal not in head] == []
    assert missing_rules(section, {"提示词给的引号固定值照抄": LITERALS}, NAMES["zh"]) == []


# ── 抽取类: 抽出的值不随语言变 (验收 3, 4) ──

CONTRACT = next(d for d in NUMBAT_CONTRACTS if d.file_name == "CON-001.pdf")
REPLY = next(d for d in NUMBAT_REPLIES if d.file_name == "12345601.pdf")
REGISTRY_DOCUMENT = "DOC-d7305364b2ae"  # 900043030.pdf 的文件 ID (固定输出的指针指向它)
ACCOUNTS = REPO / "data" / "demo" / "accounts.csv"  # Numbat 合成的科目表


async def saved(deps: AgentDeps, file_name: str, data: bytes, document_id: str, pages: int = 2) -> Any:
    """把一份文件存进假证据存储 (取固定输出指针指向的文件 ID 与页数), 返回登记."""
    evidence = deps.evidence_store
    assert isinstance(evidence, FakeEvidenceStore)
    evidence.ids[file_name] = document_id
    evidence.page_counts[file_name] = pages
    media = "text/csv" if file_name.endswith(".csv") else "application/pdf"
    return await evidence.save(
        EID, file_name, data, type=DocumentType.other, reliability=Reliability.internal_copy, media_type=media
    )


async def classify(deps: AgentDeps, language: WorkingLanguage) -> Any:
    document = await saved(deps, CONTRACT.file_name, b"%PDF CON-001", CONTRACT.id, CONTRACT.page_count)
    return await DocumentIntake(deps).classify(EID, document, language)


async def contract(deps: AgentDeps, language: WorkingLanguage) -> Any:
    document = await saved(deps, CONTRACT.file_name, b"%PDF CON-001", CONTRACT.id, CONTRACT.page_count)
    typed = document.model_copy(update={"type": DocumentType.contract, "reliability": Reliability.external_copy})
    run = upload_run("DOC-04", typed, language)
    return await extract_contract(deps, EID, deps.library.load("DOC-04"), run, typed, NUMBAT_PROFILE)


async def reply(deps: AgentDeps, language: WorkingLanguage) -> Any:
    document = await saved(deps, REPLY.file_name, b"%PDF reply", REPLY.id, REPLY.page_count)
    typed = document.model_copy(update={"type": DocumentType.confirmation, "reliability": Reliability.external_copy})
    return await DocumentIntake(deps).extract_reply(EID, typed, language)


async def registry(deps: AgentDeps, language: WorkingLanguage) -> Any:
    document = await saved(deps, "900043030.pdf", b"%PDF registry extract", REGISTRY_DOCUMENT)
    typed = document.model_copy(
        update={"type": DocumentType.registry_extract, "reliability": Reliability.external_copy}
    )
    return await DocumentIntake(deps).extract_registry(EID, typed, language)


async def sheet(deps: AgentDeps, language: WorkingLanguage) -> Any:
    data = ACCOUNTS.read_bytes()
    document = await saved(deps, ACCOUNTS.name, data, "DOC-ACCOUNTS0000", 1)
    return await SheetIntake(deps).recognize(EID, document, data, language)


Extract = Callable[[AgentDeps, WorkingLanguage], Awaitable[Any]]


async def extracted(extract: Extract, language: WorkingLanguage) -> tuple[Any, list[TrailEntry]]:
    """按上传处理的方式抽取一次 (mock: 上传固定输出, 再找主固定输出), 返回结果与 llm_call 留痕. 项目存储里的项目是
    en: 语言只取传入的执行语言, 不读项目."""
    trail = FakeTrail()
    deps = make_deps(FakeRegistry(), trail, llm=LayeredMockLLM(UPLOAD_FIXTURES, UPLOAD_FIXTURES.parents[1] / "llm"))
    validator = OutputValidator(deps.financial_store, deps.library, deps.trail)
    deps = replace(deps, judgments=JudgmentEngine(deps.llm, validator, deps.trail, deps.library))
    result = await extract(deps, language)
    return result, [e for e in trail.entries if e.kind is TrailKind.llm_call]


@pytest.mark.parametrize(
    ("procedure", "extract"),
    [
        ("DOC-CLASSIFY", classify),
        ("DOC-04", contract),
        ("DOC-06", reply),
        ("DOC-REGISTRY", registry),
        ("DOC-SHEET", sheet),
    ],
)
async def test_extracted_values_do_not_depend_on_the_language(procedure: str, extract: Extract) -> None:
    """验收 3, 4: 同一份 mock 输出在 zh 与 en 下抽出的结果相同 (校验都通过); 请求的 fixture_key 相同, 读的固定输出
    不变; 两次提示词只差末尾的输出语言一节, 分别写明中文与英文. ACC-08 的抽取还没接进程序 (只有模板), 不在此列."""
    (zh, zh_calls), (en, en_calls) = [await extracted(extract, language) for language in ("zh", "en")]
    assert zh is not None and zh == en
    assert [e.procedure_id for e in zh_calls] == [procedure] and len(en_calls) == 1
    assert zh_calls[0].payload["level_failed"] is None
    assert zh_calls[0].payload["fixture_key"] == en_calls[0].payload["fixture_key"]
    zh_prompt, en_prompt = (calls[0].payload["messages"][0]["content"] for calls in (zh_calls, en_calls))
    (zh_head, zh_section), (en_head, en_section) = zh_prompt.split(SECTION), en_prompt.split(SECTION)
    assert zh_head == en_head
    assert (
        "Working language: Simplified Chinese." in zh_section and "Working language: Australian English." in en_section
    )
