"""Xero 演示数据集补造文件的校验: manifest 与快照哈希, 合成标志, 模型严格校验, 登记与号码, 补造表与 PDF 内容,
补造表与快照的核对, 登录账号联系人扫描与确定性.

用法: uv run python data/check_xero_demo.py [--snapshot 20261017-q1 或快照目录] [--dir 补造目录]

- 问题全部收集后一次抛 CheckError, 每行一项, 只写文件名; 登录账号联系人的姓名与邮箱只在内存里比对, 不打印, 不写文件.
- 没有本地快照时跳过快照哈希, 画像 ID 前缀, 补造表与快照的核对, 联系人扫描与确定性, 补造文件本身照常核对;
  快照在而 contacts.json 缺失或无法解析时报错, 不静默跳过联系人扫描.
设计见 docs/Xero演示数据集设计.md 第 9.4 节.
"""

import argparse
import csv
import hashlib
import io
import json
import re
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal

import generate_xero_demo as gen
import pdfplumber
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from backend.contracts.models import (
    AbnRecord,
    AsicCompanyRecord,
    AuditObjective,
    ClientProfile,
    ReceiptChannel,
    RiskItem,
)

REPO = gen.ROOT.parent
CHANNELS = frozenset(c.value for c in ReceiptChannel)
NO_SNAPSHOT = "没有本地 Xero 快照"
PROFILE_NOTES_PREFIX = "Synthetic – for demonstration only"
TRAIT_KEYS = frozenset(
    {
        "dataset",
        "data_as_of",
        "prior_period_start",
        "entry_timestamps_reliable",
        "entry_timestamps_note",
        "reversal_reference_pattern",
        "reversal_entry_no_template",
        "counterparty_prefixes",
        "audit_adjustment_entry_prefix",
        "payroll_accounts",
        "payroll_register",
    }
)  # 第 5.2 节的 11 个键
FIXTURES = REPO / "tests" / "fixtures"
SCAN_PATHS = (
    gen.SPEC,
    REPO / "docs" / "Xero演示数据集设计.md",
    FIXTURES / "llm_xero",
    FIXTURES / "llm_xero_only17",
    FIXTURES / "xero_injections",
    FIXTURES / "llm_injected",
)  # 补造目录之外也要扫描登录账号联系人的位置, 不存在的跳过
MODELS: dict[str, TypeAdapter[Any]] = {
    "client_profile.json": TypeAdapter(ClientProfile),
    "planning/audit_objectives.json": TypeAdapter(list[AuditObjective]),
    "planning/risk_register.json": TypeAdapter(list[RiskItem]),
}  # 按领域模型严格校验的结构化文件 (extra="forbid", 不能加合成标志字段)


class CheckError(Exception):
    """校验不通过. 信息每行一项问题, 只写文件名, 不写联系人原值."""


def check(
    directory: Path, snapshot_dir: Path, *, determinism: bool = True, scan: Iterable[Path] = SCAN_PATHS
) -> list[str]:
    """校验一套补造文件, 返回说明 (如跳过了哪些检查); 任一项不过抛 CheckError. scan 为补造目录之外要扫描联系人的位置.
    manifest 缺失或结构不符时只报这一项; 其余各项互不影响, 某项意外中断时记一行 (只写异常类名) 后继续."""
    manifest_path = directory / "manifest.json"
    manifest = read_json(manifest_path)
    if manifest is None:
        raise CheckError(f"{rel(manifest_path)}: 没有 manifest.json 或不是合法的 JSON")
    try:
        Manifest.model_validate(manifest, strict=True)
    except ValidationError as error:
        raise CheckError(f"{rel(manifest_path)}: manifest 结构不符 ({error.error_count()} 处)") from None
    header = gen.load_spec()["header"]
    problems = [
        *guarded(manifest_path, check_manifest, directory, manifest),
        *guarded(directory, check_synthetic, directory, manifest, header),
        *guarded(directory, check_models, directory),
        *guarded(directory / "registry", check_registry, directory),
        *guarded(directory, check_numbers, directory),
        *guarded(directory / "delivery_notes.csv", check_delivery_notes, directory),
        *guarded(directory / "contract_links.csv", check_contract_links, directory),
        *guarded(directory / "confirmations", check_receipts, directory),
        *guarded(directory / "confirmations", check_confirmation_text, directory),
        *guarded(directory, check_pdf_content, directory, manifest),
    ]
    notes: list[str] = []
    if snapshot_dir.is_dir():
        problems += guarded(snapshot_dir, check_snapshot, directory, manifest, snapshot_dir)
        problems += guarded(snapshot_dir, check_against_snapshot, directory, snapshot_dir)
        if not snapshot_complete(snapshot_dir):
            missing = [f"{n}.json" for n in SNAPSHOT_TABLE_FILES if not (snapshot_dir / f"{n}.json").is_file()]
            notes.append(f"快照缺 {', '.join(missing)}: 跳过补造表与快照的核对")
        problems += guarded(snapshot_dir / "contacts.json", scan_login_contact, snapshot_dir, [directory, *scan])
        if determinism:
            problems += guarded(directory, check_determinism, directory, snapshot_dir)
    else:
        notes.append(
            f"{NO_SNAPSHOT} ({rel(snapshot_dir)}): 跳过快照哈希, 画像 ID 前缀, 补造表与快照的核对 (服务完成记录, "
            "合同关联, 开户行, xero_refs, 回函余额与应计利息), 登录账号联系人扫描与确定性检查"
        )
    if problems:
        raise CheckError("\n".join(problems))
    return notes


class FileItem(BaseModel):
    """manifest files 段的一项."""

    model_config = ConfigDict(extra="forbid")
    path: str
    sha256: str
    synthetic: bool
    generator: str
    xero_refs: list[str]


class DocumentItem(BaseModel):
    """manifest documents 段的一项; 登记信息各字段由后续需求核对, 这里只要求定位与哈希."""

    model_config = ConfigDict(extra="allow")
    path: str
    sha256: str


class SnapshotFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    sha256: str


class SnapshotSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    files: list[SnapshotFile]


class Manifest(BaseModel):
    """manifest.json 的结构 (设计文档第 9.5 节); injected 只出现在测试副本."""

    model_config = ConfigDict(extra="forbid")
    dataset: str
    generator: str
    spec_sha256: str
    snapshot: SnapshotSection
    files: list[FileItem]
    documents: list[DocumentItem]
    injected: str | None = None


def guarded(where: Path, step: Callable[..., list[str]], *args: Any) -> list[str]:
    """跑一项检查; 意外异常记一行 "{文件}: 校验中断 ({异常类名})", 不带异常消息, 免得带出文件内容."""
    try:
        return step(*args)
    except CheckError as error:
        return [str(error)]
    except Exception as error:
        return [f"{rel(where)}: 校验中断 ({type(error).__name__})"]


def rel(path: Path) -> str:
    """报错用的文件名: 仓库内写相对路径, 仓库外 (测试临时目录) 写原路径."""
    resolved = path.resolve()
    return resolved.relative_to(REPO).as_posix() if resolved.is_relative_to(REPO) else str(path)


def read_json(path: Path) -> Any:
    """读 JSON; 文件缺失, 无法解码或不是合法 JSON 时返回 None (问题由调用方按需报告)."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


PDF_TEXTS: dict[str, list[str] | None] = {}  # 按文件内容的 sha256 缓存文本层, 同一份 PDF 只解析一次


def pdf_pages(path: Path) -> list[str] | None:
    """PDF 各页的文本层; 读不到或无法解析时为 None."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    digest = hashlib.sha256(data).hexdigest()
    if digest not in PDF_TEXTS:
        try:
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                PDF_TEXTS[digest] = [page.extract_text() or "" for page in pdf.pages]
        except Exception:
            PDF_TEXTS[digest] = None
    texts = PDF_TEXTS[digest]
    return None if texts is None else list(texts)


def files_of(directory: Path) -> Iterator[Path]:
    return (p for p in sorted(directory.rglob("*")) if p.is_file())


def check_manifest(directory: Path, manifest: dict[str, Any]) -> list[str]:
    """重算各文件哈希; 目录里有而清单没有, 或清单有而目录没有的文件都报错; spec_sha256 须等于当前规格."""
    problems: list[str] = []
    listed = {item["path"]: item for item in manifest["files"]}
    present = {p.relative_to(directory).as_posix() for p in files_of(directory)} - {"manifest.json"}
    problems += [f"{rel(directory / name)}: 不在 manifest 清单中" for name in sorted(present - listed.keys())]
    problems += [f"{rel(directory / name)}: manifest 列出但文件不存在" for name in sorted(listed.keys() - present)]
    for name in sorted(present & listed.keys()):
        if gen.sha256(directory / name) != listed[name]["sha256"]:
            problems.append(f"{rel(directory / name)}: sha256 与 manifest 不符")
    for doc in manifest["documents"]:
        if doc["path"] in listed and doc["sha256"] != listed[doc["path"]]["sha256"]:
            problems.append(f"{rel(directory / doc['path'])}: documents 段的 sha256 与 files 段不符")
    pdfs = {name for name in listed if name.endswith(".pdf")}
    documented = {doc["path"] for doc in manifest["documents"]}
    problems += [f"{rel(directory / name)}: PDF 在 manifest documents 段没有登记" for name in sorted(pdfs - documented)]
    if manifest["spec_sha256"] != gen.sha256(gen.SPEC):
        problems.append(f"{rel(directory / 'manifest.json')}: spec_sha256 与 {rel(gen.SPEC)} 不符, 需重新生成")
    if "injected" in manifest and gen.inside(directory, gen.OUT_ROOT):
        problems.append(f"{rel(directory / 'manifest.json')}: 正式目录的 manifest 不得有 injected 键")
    return problems


def check_synthetic(directory: Path, manifest: dict[str, Any], header: str) -> list[str]:
    """合成标志: 清单每个文件 synthetic 为真且 generator 为本脚本; 每份 PDF 每页文本层含页眉原文; 画像 notes 的开头."""
    problems: list[str] = []
    for item in [*manifest["files"], *manifest["documents"]]:
        if item.get("synthetic") is not True:
            problems.append(f"{rel(directory / item['path'])}: manifest 中 synthetic 不为 true")
    for item in manifest["files"]:
        if item["generator"] != gen.GENERATOR:
            problems.append(f"{rel(directory / item['path'])}: manifest 中 generator 不是 {gen.GENERATOR}")
    for path in files_of(directory):
        if path.suffix == ".pdf":
            texts = pdf_pages(path)
            if texts is None:
                problems.append(f"{rel(path)}: 无法解析 PDF")
                continue
            pages = [n for n, text in enumerate(texts, 1) if header not in text]
            if pages:
                problems.append(f"{rel(path)}: 第 {', '.join(map(str, pages))} 页文本层没有合成页眉")
    profile = directory / "client_profile.json"
    data = read_json(profile)
    if isinstance(data, dict):  # 文件缺失或格式不对由 check_models 报
        notes = data.get("notes")
        if not isinstance(notes, str) or not notes.startswith(PROFILE_NOTES_PREFIX):
            problems.append(f"{rel(profile)}: notes 不以 {PROFILE_NOTES_PREFIX!r} 开头")
    return problems


def check_models(directory: Path) -> list[str]:
    """结构化文件按领域模型严格校验; dataset_traits.json 的键集合等于第 5.2 节."""
    problems: list[str] = []
    for name, adapter in MODELS.items():
        path = directory / name
        if not path.is_file():
            problems.append(f"{rel(path)}: 文件不存在")
            continue
        try:
            adapter.validate_json(path.read_bytes(), strict=True)
        except ValidationError as error:
            problems.append(f"{rel(path)}: 不符合领域模型 ({error.error_count()} 处)")
    traits = directory / "dataset_traits.json"
    data = read_json(traits)
    if not traits.is_file():
        problems.append(f"{rel(traits)}: 文件不存在")
    elif not isinstance(data, dict):
        problems.append(f"{rel(traits)}: 不是合法的 JSON 对象")
    elif (keys := set(data)) != TRAIT_KEYS:
        extra, lack = sorted(keys - TRAIT_KEYS), sorted(TRAIT_KEYS - keys)
        problems.append(f"{rel(traits)}: 键与第 5.2 节不符 (多 {extra}, 缺 {lack})")
    return problems


def abn_valid(abn: str) -> bool:
    """ABN 校验位: 首位减 1, 按权重 10, 1, 3, ..., 19 加权求和, 能被 89 整除."""
    if not (len(abn) == 11 and abn.isdigit()):
        return False
    digits = [int(c) for c in abn]
    digits[0] -= 1
    return sum(d * w for d, w in zip(digits, (10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19), strict=True)) % 89 == 0


def acn_valid(acn: str) -> bool:
    """ACN 校验位: 前 8 位按权重 8 至 1 加权求和, (10 - 和 mod 10) mod 10 等于第 9 位."""
    if not (len(acn) == 9 and acn.isdigit()):
        return False
    total = sum(int(c) * w for c, w in zip(acn[:8], range(8, 0, -1), strict=True))
    return (10 - total % 10) % 10 == int(acn[8])


class Officeholder(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    birth_date: date | None
    role: Literal["director", "secretary"]
    appointed: date


class Shareholding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    kind: Literal["person", "company"]
    acn: str | None
    share_class: str
    shares: int
    pct: float


class Extract(BaseModel):
    """公司摘录 JSON (第 9.5 节); 契约模型 AsicExtract 按同名字段建 (JSON 键 class)."""

    model_config = ConfigDict(extra="forbid")
    acn: str
    name: str
    type: str
    company_class: str = Field(alias="class")
    subclass: str
    status: Literal["Registered", "Deregistered"]
    registered_date: date
    officeholders: list[Officeholder]
    shareholders: list[Shareholding]
    paid_up_cents: int | None
    file_name: str


class PersonRole(BaseModel):
    model_config = ConfigDict(extra="forbid")
    acn: str
    company_name: str
    role: Literal["director", "secretary", "shareholder"]
    appointed: date | None
    shares: int | None


class Person(BaseModel):
    """人员检索 JSON (第 9.5 节); 契约模型 PersonSearch 按同名字段建."""

    model_config = ConfigDict(extra="forbid")
    family_name: str
    given_name: str
    birth_date: date
    roles: list[PersonRole]
    file_name: str


def check_registry(directory: Path) -> list[str]:
    """登记 mock: abn.json, asic.json 按 AbnRecord, AsicCompanyRecord 严格校验, 键等于号码; 摘录与人员检索的格式;
    全部 ABN 与 ACN 通过校验位; 摘录的 ACN 与名称与 abn.json, asic.json 一致; 人员检索的职务与持股
    和各摘录的高管, 股东一一对应. 没有 registry/ 时不查."""
    root = directory / "registry"
    if not root.is_dir():
        return []
    problems: list[str] = []
    missing = [name for name in ("abn.json", "asic.json") if not (root / name).is_file()]
    if missing:
        return [f"{rel(root / name)}: 文件不存在" for name in missing]
    try:
        abns = TypeAdapter(dict[str, AbnRecord]).validate_json((root / "abn.json").read_bytes(), strict=True)
        asics = TypeAdapter(dict[str, AsicCompanyRecord]).validate_json((root / "asic.json").read_bytes(), strict=True)
    except ValidationError as error:
        return [f"{rel(root)}: abn.json 或 asic.json 不符合领域模型 ({error.error_count()} 处)"]
    problems += [f"{rel(root / 'abn.json')}: 键 {k} 与行内 ABN 不符" for k, v in abns.items() if k != v.abn]
    problems += [f"{rel(root / 'asic.json')}: 键 {k} 与行内 ACN 不符" for k, v in asics.items() if k != v.acn]
    extracts: dict[str, Extract] = {}
    for path in sorted((root / "extracts").glob("*.json")):
        try:
            extract = Extract.model_validate_json(path.read_bytes(), strict=True)
        except ValidationError as error:
            problems.append(f"{rel(path)}: 不符合摘录格式 ({error.error_count()} 处)")
            continue
        extracts[extract.acn] = extract
        if path.stem != extract.acn or extract.file_name != f"{extract.acn}.pdf":
            problems.append(f"{rel(path)}: 文件名与 acn 或 file_name 不符")
        total = sum(s.shares for s in extract.shareholders)
        if any(abs(s.pct - s.shares / total) > 1e-9 for s in extract.shareholders):
            problems.append(f"{rel(path)}: 股东持股比例与股数不符")
        record = asics.get(extract.acn)
        if record is None or record.name != extract.name or record.registered_date != extract.registered_date:
            problems.append(f"{rel(path)}: 与 asic.json 的名称或注册日不一致")
        if not any(r.acn == extract.acn and r.entity_name == extract.name for r in abns.values()):
            problems.append(f"{rel(path)}: abn.json 中没有同 ACN 同名称的主体")
    persons: list[tuple[Path, Person]] = []
    for path in sorted((root / "persons").glob("*.json")):
        try:
            person = Person.model_validate_json(path.read_bytes(), strict=True)
        except ValidationError as error:
            problems.append(f"{rel(path)}: 不符合人员检索格式 ({error.error_count()} 处)")
            continue
        persons.append((path, person))
        key = gen.person_key(person.model_dump(mode="json"))
        if path.stem != key or person.file_name != f"{key}.pdf":
            problems.append(f"{rel(path)}: 文件名或 file_name 不是 {key}")
        if Counter(role_rows(person)) != Counter(expected_roles(person, extracts.values())):
            problems.append(f"{rel(path)}: 职务与持股和公司摘录不一一对应")
    problems += [
        f"{rel(root / 'abn.json')}: ABN {r.abn} 的后 9 位不是 ACN {r.acn}"
        for r in abns.values()
        if r.acn and r.abn[2:] != r.acn
    ]
    profile = read_json(directory / "client_profile.json")
    if isinstance(profile, dict):
        record = abns.get(str(profile.get("abn")))
        if record is None or record.entity_name != profile.get("legal_name"):
            problems.append(f"{rel(directory / 'client_profile.json')}: 画像的 ABN 与法定名称不在 abn.json 中")
    numbers = [*abns, *(r.abn for r in abns.values())]
    companies = [*asics, *extracts, *(r.acn for r in abns.values() if r.acn)]
    companies += [s.acn for e in extracts.values() for s in e.shareholders if s.acn]
    companies += [r.acn for _, p in persons for r in p.roles]
    problems += [f"{rel(root)}: ABN {n} 校验位不对" for n in sorted(set(numbers)) if not abn_valid(n)]
    problems += [f"{rel(root)}: ACN {n} 校验位不对" for n in sorted(set(companies)) if not acn_valid(n)]
    return problems


ABN_TEXT = re.compile(r"\bABN (\d{2} \d{3} \d{3} \d{3})\b")
ACN_TEXT = re.compile(r"\bACN (\d{3} \d{3} \d{3})\b")


def check_numbers(directory: Path) -> list[str]:
    """登记目录之外的 ABN 与 ACN 也要通过校验位: 画像的 abn, 规格中 PDF 正文写出的 "ABN dd ddd ddd ddd"
    与 "ACN ddd ddd ddd" (合同, 回函, 承接文件等)."""
    problems: list[str] = []
    profile = read_json(directory / "client_profile.json")
    if isinstance(profile, dict) and not abn_valid(str(profile.get("abn", ""))):
        problems.append(f"{rel(directory / 'client_profile.json')}: ABN {profile.get('abn')} 校验位不对")
    for doc in gen.load_spec()["documents"]:
        text = "\n".join("\n".join(page) for page in doc.get("pages", []))
        bad = [n for n in ABN_TEXT.findall(text) if not abn_valid(n.replace(" ", ""))]
        bad += [n for n in ACN_TEXT.findall(text) if not acn_valid(n.replace(" ", ""))]
        if bad:
            problems.append(f"{rel(gen.SPEC)}: {doc['path']} 正文中的号码校验位不对: {', '.join(bad)}")
    return problems


RoleRow = tuple[str, str, str, date | None, int | None]


def role_rows(person: Person) -> list[RoleRow]:
    return [(r.acn, r.company_name, r.role, r.appointed, r.shares) for r in person.roles]


def expected_roles(person: Person, extracts: Iterable[Extract]) -> list[RoleRow]:
    """按摘录推出某人应有的职务 (同名且同生日的高管) 与持股 (同名的个人股东)."""
    name = f"{person.given_name} {person.family_name}"
    rows: list[RoleRow] = []
    for e in extracts:
        rows += [
            (e.acn, e.name, h.role, h.appointed, None)
            for h in e.officeholders
            if h.name == name and h.birth_date == person.birth_date
        ]
        rows += [
            (e.acn, e.name, "shareholder", None, s.shares)
            for s in e.shareholders
            if s.name == name and s.kind == "person"
        ]
    return rows


def check_pdf_content(directory: Path, manifest: dict[str, Any]) -> list[str]:
    """PDF 的页数与内容: manifest 的 page_count 等于实际页数; 规格写有正文的 PDF 与按登记 JSON 渲染的 PDF,
    每页每个非空行都出现在该页文本层 (空白归一); 登记 PDF 每页含名称, ACN 与日期."""
    problems: list[str] = []
    expected = gen.pdf_sources(directory)
    for doc in manifest["documents"]:
        path = directory / doc["path"]
        texts = pdf_pages(path) if path.is_file() else None
        if texts is None:
            continue  # 缺失或无法解析由 check_manifest 与 check_synthetic 报
        if doc.get("page_count") != len(texts):
            problems.append(f"{rel(path)}: manifest 的 page_count 与实际页数 {len(texts)} 不符")
        pages = expected.get(doc["path"])
        if pages is None:
            continue
        if len(pages) != len(texts):
            problems.append(f"{rel(path)}: 页数 {len(texts)} 与来源的 {len(pages)} 页不符")
            continue
        for n, (page, text) in enumerate(zip(pages, texts, strict=True), 1):
            lack = missing_in_order(page, text)
            if lack:
                problems.append(f"{rel(path)}: 第 {n} 页文本层缺 {lack} 行来源内容或顺序不对")
    for path in sorted((directory / "registry" / "extracts").glob("*.pdf")):
        data = read_json(path.with_suffix(".json"))
        texts = pdf_pages(path)
        if isinstance(data, dict) and texts:
            keys = [data["name"], gen.spaced_acn(data["acn"]), gen.long_date(data["registered_date"])]
            pages = [n for n, t in enumerate(texts, 1) if not all(k in normalize_case(t) for k in keys)]
            if len(texts) != 2 or pages:
                problems.append(f"{rel(path)}: 应为 2 页且每页含名称, ACN 与注册日")
    for path in sorted((directory / "registry" / "persons").glob("*.pdf")):
        data = read_json(path.with_suffix(".json"))
        texts = pdf_pages(path)
        if isinstance(data, dict) and texts:
            keys = [data["family_name"].upper(), data["given_name"], gen.long_date(data["birth_date"])]
            keys += [gen.spaced_acn(r["acn"]) for r in data["roles"]]
            if len(texts) != 1 or not all(k in normalize_case(texts[0]) for k in keys):
                problems.append(f"{rel(path)}: 应为 1 页且含姓名, 出生日期与各公司 ACN")
    return problems


def missing_in_order(source: str, text: str) -> int:
    """来源的非空行按顺序在文本层中依次查找 (区分大小写, 空白归一), 返回找不到的行数."""
    have, start, lack = normalize_case(text), 0, 0
    for line in source.splitlines():
        if not line.strip():
            continue
        found = have.find(normalize_case(line).strip(), start)
        if found < 0:
            lack += 1
        else:
            start = found + len(normalize_case(line).strip())
    return lack


def normalize_case(text: str) -> str:
    """只归一空白, 保留大小写."""
    return re.sub(r"\s+", " ", text)


TABLES = {
    "contract_links.csv": ["invoice_no", "contract_id"],
    "delivery_notes.csv": ["id", "delivery_no", "invoice_no", "shipped_date", "signed_date", "signed_by"],
    "bank_accounts.csv": ["id", "bank_name", "bsb", "account_no", "gl_account_code"],
}  # 补造 CSV 的列 (表头同 data/demo 的同名表)
UNSERVED = frozenset({"ORC1003", "ORC1005"})  # 本期没有服务完成记录的发票: 重复开票与已并入 ORC1006 的 (第 3.3 节)
INTEREST = ("BA-091", "BT-091-0002", 15, 30)  # 应计利息 = 期后首次入账利息 × 期内天数 / 计息期天数 (第 2.8 节)


def read_table(directory: Path, name: str, *, quiet: bool = False) -> list[dict[str, str]] | None:
    """读补造 CSV; 文件不存在时为 None. 表头不对时抛 CheckError, quiet 时返回 None (由不带 quiet 的检查报出)."""
    path = directory / name
    if not path.is_file():
        return None
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        rows = list(reader)
        if reader.fieldnames != TABLES[name]:
            if quiet:
                return None
            raise CheckError(f"{rel(path)}: 表头应为 {', '.join(TABLES[name])}")
    return rows


def iso(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def money(cents: int) -> str:
    """回函的金额写法: AUD 10,900.44."""
    return f"AUD {abs(cents) / 100:,.2f}"


def confirmations() -> list[dict[str, Any]]:
    """规格中带 confirmation 事实 (bank_account_id, balance_cents, accrued_interest_cents) 的回函."""
    return [doc for doc in gen.load_spec()["documents"] if "confirmation" in doc]


def check_delivery_notes(directory: Path) -> list[str]:
    """服务完成记录: 每张发票至多一条, id 与 delivery_no 为 SR-{发票号}, 日期合法且开始日不晚于完成日."""
    notes = read_table(directory, "delivery_notes.csv")
    if notes is None:
        return []
    problems: list[str] = []
    where = rel(directory / "delivery_notes.csv")
    repeated = sorted(n for n, c in Counter(r["invoice_no"] for r in notes).items() if c > 1)
    if repeated:
        problems.append(f"{where}: 同一发票有多条记录: {', '.join(repeated)}")
    wrong_id = [r["id"] for r in notes if not r["id"] == r["delivery_no"] == f"SR-{r['invoice_no']}"]
    if wrong_id:
        problems.append(f"{where}: id 与 delivery_no 应为 SR-{{发票号}}: {', '.join(wrong_id)}")
    spans = {r["id"]: (iso(r["shipped_date"]), iso(r["signed_date"])) for r in notes}
    bad = [i for i, (a, b) in spans.items() if a is None or b is None]
    if bad:
        problems.append(f"{where}: 日期格式不对: {', '.join(bad)}")
    late = [i for i, (a, b) in spans.items() if a and b and a > b]
    if late:
        problems.append(f"{where}: 开始日晚于完成日: {', '.join(late)}")
    return problems


def check_contract_links(directory: Path) -> list[str]:
    """合同关联: 发票号不重复, 合同 PDF 存在."""
    links = read_table(directory, "contract_links.csv")
    if links is None:
        return []
    problems: list[str] = []
    where = rel(directory / "contract_links.csv")
    repeated = sorted(n for n, c in Counter(r["invoice_no"] for r in links).items() if c > 1)
    if repeated:
        problems.append(f"{where}: 发票号重复: {', '.join(repeated)}")
    lost = sorted(
        {r["contract_id"] for r in links if not (directory / "contracts" / f"{r['contract_id']}.pdf").is_file()}
    )
    if lost:
        problems.append(f"{where}: 合同 PDF 不存在: {', '.join(lost)}")
    return problems


def check_receipts(directory: Path) -> list[str]:
    """回函渠道: 账号与回函 PDF 一一对应, receipt_channel 是有效渠道."""
    receipts = read_json(directory / "confirmations" / "receipts.json")
    replies = {p.stem for p in (directory / "confirmations").glob("*.pdf")}
    if receipts is None and not replies:
        return []
    where = rel(directory / "confirmations" / "receipts.json")
    if not isinstance(receipts, dict) or set(receipts) != replies:
        return [f"{where}: 账号应与回函 PDF 一一对应"]
    if any(not isinstance(v, dict) or v.get("receipt_channel") not in CHANNELS for v in receipts.values()):
        return [f"{where}: receipt_channel 不是有效的回函渠道"]
    return []


OTHER_ACCOUNT = re.compile(r"Account (\d+) \([^)]*\): (AUD [\d,]+\.\d{2}) \((credit|debit)")


def check_confirmation_text(directory: Path) -> list[str]:
    """回函写法: 第 1 页写出规格中的余额与应计利息; 其他账户一节提到的账号, 金额与方向等于该账号回函的余额."""
    problems: list[str] = []
    docs = confirmations()
    balances = {Path(doc["path"]).stem: doc["confirmation"]["balance_cents"] for doc in docs}
    for doc in docs:
        texts = pdf_pages(directory / doc["path"])
        if not texts:
            continue  # 缺失或无法解析由其他检查报
        facts, first = doc["confirmation"], normalize_case(texts[0])
        side = "debit" if facts["balance_cents"] < 0 else "credit"
        accrued = facts["accrued_interest_cents"]
        wanted = [f"{money(facts['balance_cents'])} ({side}", money(accrued) if accrued else ": nil"]
        if not all(w in first for w in wanted):
            problems.append(f"{rel(directory / doc['path'])}: 第 1 页没有写出规格中的余额或应计利息")
        for number, amount, direction in OTHER_ACCOUNT.findall(normalize_case(" ".join(texts))):
            other = balances.get(number)
            if other is not None and (amount, direction) != (money(other), "debit" if other < 0 else "credit"):
                problems.append(f"{rel(directory / doc['path'])}: 其他账户 {number} 的余额与该账户回函不符")
    return problems


SNAPSHOT_TABLE_FILES = ("dataset", "invoices", "accounts", "bank_statements")  # 补造表与快照核对要读的快照文件


def snapshot_complete(snapshot_dir: Path) -> bool:
    return all((snapshot_dir / f"{name}.json").is_file() for name in SNAPSHOT_TABLE_FILES)


def check_against_snapshot(directory: Path, snapshot_dir: Path) -> list[str]:
    """补造表与快照核对, 各项互不影响: 服务完成记录, 合同关联, 开户行, xero_refs, 回函余额与应计利息.
    快照缺 SNAPSHOT_TABLE_FILES 任一文件时不做 (manifest 记了这些文件, 哈希核对已报出; check 另记说明)."""
    from backend.adapters.xero_dataset import LIVE_DOCUMENTS, xero_date

    if not snapshot_complete(snapshot_dir):
        return []
    snapshot = gen.Snapshot(snapshot_dir)
    dataset = snapshot.load("dataset")
    period = date.fromisoformat(dataset["period_start"]), date.fromisoformat(dataset["period_end"])
    live = [i for i in snapshot.load("invoices") if i.get("Type") == "ACCREC" and i.get("Status") in LIVE_DOCUMENTS]
    in_period = {i["InvoiceNumber"] for i in live if period[0] <= xero_date(i) <= period[1]}
    steps: list[tuple[Path, Callable[..., list[str]], tuple[Any, ...]]] = [
        (directory / "delivery_notes.csv", snapshot_delivery_notes, (directory, in_period)),
        (directory / "contract_links.csv", snapshot_contract_links, (directory, {i["InvoiceNumber"] for i in live})),
        (directory / "bank_accounts.csv", snapshot_bank_accounts, (directory, snapshot)),
        (directory / "manifest.json", snapshot_refs, (directory, snapshot)),
        (gen.SPEC, snapshot_confirmations, (snapshot, period[1])),
    ]
    return [problem for where, step, args in steps for problem in guarded(where, step, *args)]


def snapshot_delivery_notes(directory: Path, in_period: set[str]) -> list[str]:
    """服务完成记录的发票都是本期生效的销售发票, 没有记录的恰好是 UNSERVED."""
    notes = read_table(directory, "delivery_notes.csv", quiet=True)
    if notes is None:
        return []
    where, noted = rel(directory / "delivery_notes.csv"), {r["invoice_no"] for r in notes}
    problems = [f"{where}: 不是本期生效的销售发票: {', '.join(sorted(noted - in_period))}"] if noted - in_period else []
    if in_period - noted != UNSERVED:
        problems.append(f"{where}: 没有记录的本期发票应恰好是 {', '.join(sorted(UNSERVED))}")
    return problems


def snapshot_contract_links(directory: Path, live: set[str]) -> list[str]:
    """合同关联的发票在快照中存在且已生效."""
    links = read_table(directory, "contract_links.csv", quiet=True)
    unknown = sorted({r["invoice_no"] for r in links} - live) if links else []
    return [f"{rel(directory / 'contract_links.csv')}: 快照中没有这些生效发票: {', '.join(unknown)}"] if unknown else []


def snapshot_bank_accounts(directory: Path, snapshot: gen.Snapshot) -> list[str]:
    """开户行覆盖表与快照银行科目一一对应 (id 不重复, 不缺不多), 每行的 bsb, 账号与科目一致."""
    from backend.adapters.xero_dataset import map_bank_accounts

    accounts = read_table(directory, "bank_accounts.csv", quiet=True)
    if accounts is None:
        return []
    where = rel(directory / "bank_accounts.csv")
    expected = {a.id: a for a in map_bank_accounts(snapshot.load("accounts"))}
    ids = Counter(row["id"] for row in accounts)
    problems: list[str] = []
    if set(ids) != set(expected) or max(ids.values(), default=0) > 1:
        problems.append(f"{where}: 行应与快照银行科目 {', '.join(sorted(expected))} 一一对应")
    for row in accounts:
        a = expected.get(row["id"])
        if a is not None and (row["bsb"], row["account_no"], row["gl_account_code"]) != (
            a.bsb,
            a.account_no,
            a.gl_account_code,
        ):
            problems.append(f"{where}: {row['id']} 与快照银行科目的账号不一致")
    return problems


def snapshot_refs(directory: Path, snapshot: gen.Snapshot) -> list[str]:
    """manifest 的 xero_refs 都在快照中 (测试副本经 --inject 重写 manifest 时不在生成脚本核对, 由此兜底)."""
    manifest = read_json(directory / "manifest.json")
    refs = [r for item in manifest["files"] for r in item["xero_refs"]] if isinstance(manifest, dict) else []
    missing = gen.RefIndex(snapshot).missing(refs) if refs else []
    if missing:
        return [f"{rel(directory / 'manifest.json')}: xero_refs 在快照中找不到: {', '.join(sorted(set(missing)))}"]
    return []


def snapshot_confirmations(snapshot: gen.Snapshot, period_end: date) -> list[str]:
    """回函余额等于对账单期末前最后一行的余额; 应计利息按 INTEREST 推算 (四舍五入)."""
    from backend.adapters.xero_dataset import map_bank_transactions

    problems: list[str] = []
    lines = map_bank_transactions(snapshot)  # type: ignore[arg-type]
    account, line_id, days, period_days = INTEREST
    for doc in confirmations():
        facts = doc["confirmation"]
        closing = [t for t in lines if t.bank_account_id == facts["bank_account_id"] and t.txn_date <= period_end]
        if not closing or closing[-1].balance_after_cents != facts["balance_cents"]:
            problems.append(f"{rel(gen.SPEC)}: {doc['path']} 的余额与快照对账单期末余额不符")
        if facts["bank_account_id"] == account:
            amount = next((t.amount_cents for t in lines if t.id == line_id), None)
            accrued = None if amount is None else int((Decimal(amount) * days / period_days).quantize(1, ROUND_HALF_UP))
            if accrued != facts["accrued_interest_cents"]:
                problems.append(
                    f"{rel(gen.SPEC)}: {doc['path']} 的应计利息不等于 {line_id} 按 {days}/{period_days} 折算"
                )
    return problems


def check_snapshot(directory: Path, manifest: dict[str, Any], snapshot_dir: Path) -> list[str]:
    """快照名与各文件哈希 (contacts.json 除外) 须等于 manifest 所记; 画像 ID 以快照组织 ID 推出的前缀开头."""
    problems: list[str] = []
    snapshot = gen.Snapshot(snapshot_dir)
    recorded = manifest["snapshot"]
    if recorded["name"] != f"xero/{snapshot.name}":
        problems.append(f"{rel(directory / 'manifest.json')}: 快照名与 {rel(snapshot_dir)} 不符")
    expected = {f["path"]: f["sha256"] for f in recorded["files"]}
    actual = {f["path"]: f["sha256"] for f in snapshot.file_hashes()}
    for name in sorted(expected.keys() | actual.keys()):
        if expected.get(name) != actual.get(name):
            problems.append(f"{rel(snapshot_dir / name)}: 快照文件与 manifest 所记不符 (补造文件与快照不匹配)")
    profile = directory / "client_profile.json"
    prefix = gen.profile_prefix(snapshot)
    if prefix is None:
        problems.append(f"{rel(snapshot_dir / 'organisation.json')}: 快照缺组织信息, 无从核对画像 ID 前缀")
    elif isinstance(data := read_json(profile), dict) and not str(data.get("id", "")).startswith(prefix):
        problems.append(f"{rel(profile)}: 画像 ID 不以 {prefix} 开头")
    return problems


def normalize(text: str) -> str:
    """不区分大小写, 连续空白视为一个空格 (PDF 文本层与 JSON 字符串都可能换行)."""
    return re.sub(r"\s+", " ", text).casefold()


def login_contact(snapshot_dir: Path) -> tuple[str, str]:
    """快照中登录账号联系人的全名与邮箱 (已规范化), 只在内存中用. 口径: IsCustomer 与 IsSupplier 都为假,
    且有 FirstName 与 EmailAddress, 须恰好 1 条; contacts.json 缺失, 无法解析或条数不对都抛 CheckError (不写原值)."""
    where = rel(snapshot_dir / "contacts.json")
    contacts = read_json(snapshot_dir / "contacts.json")
    if not isinstance(contacts, list):
        raise CheckError(f"{where}: 快照没有 contacts.json 或无法解析, 无从扫描登录账号联系人")
    hits = [
        c
        for c in contacts
        if isinstance(c, dict)
        and not c.get("IsCustomer")
        and not c.get("IsSupplier")
        and c.get("FirstName")
        and c.get("EmailAddress")
    ]
    if len(hits) != 1:
        raise CheckError(f"{where}: 双否且有 FirstName 与 EmailAddress 的联系人应恰好 1 条, 实有 {len(hits)} 条")
    name = f"{hits[0]['FirstName']} {hits[0].get('LastName') or ''}".strip()
    return normalize(name), normalize(str(hits[0]["EmailAddress"]).strip())


def strings(value: Any) -> Iterator[str]:
    """JSON 值里的全部字符串 (含对象的键), 用于扫描转义过的文本 (如 \\n, \\u)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def text_of(path: Path) -> str | None:
    """要扫描的文本: PDF 取文本层 (无法解析为 None); 其余按 UTF-8 读 (无法解码的字节替换掉), JSON 另加解码后的字符串."""
    if path.suffix == ".pdf":
        pages = pdf_pages(path)
        return None if pages is None else "\n".join(pages)
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix == ".json" and (data := read_json(path)) is not None:
        text += "\n" + "\n".join(strings(data))
    return text


def scan_login_contact(snapshot_dir: Path, paths: Iterable[Path]) -> list[str]:
    """登录账号联系人的全名与邮箱 (整串, 不区分大小写) 不得出现在 paths (文件或目录, 不存在的跳过) 中; 只报文件名.
    读不出文本的 PDF 也报出 (无从确认)."""
    values = login_contact(snapshot_dir)
    problems: list[str] = []
    for root in paths:
        targets = [root] if root.is_file() else list(files_of(root)) if root.is_dir() else []
        for path in targets:
            text = text_of(path)
            if text is None:
                problems.append(f"{rel(path)}: 无法读取文本层, 未能扫描登录账号联系人")
            elif any(value in normalize(text) for value in values):
                problems.append(f"{rel(path)}: 含登录账号联系人的姓名或邮箱")
    return problems


def check_determinism(directory: Path, snapshot_dir: Path) -> list[str]:
    """另生成到临时目录, 与 directory 逐文件逐字节比较; 无法重新生成时报原因 (生成脚本的 SystemExit 写消息,
    其余异常只写类名)."""
    with TemporaryDirectory() as temp:
        other = Path(temp)
        try:
            gen.generate(snapshot_dir, other)
        except SystemExit as error:  # 生成脚本自己写的原因 (路径与对象编号)
            return [f"{rel(directory)}: 无法重新生成 ({error})"]
        except Exception as error:  # 其余异常的消息可能带出文件内容, 只写类名
            return [f"{rel(directory)}: 无法重新生成 ({type(error).__name__})"]
        mine = {p.relative_to(directory).as_posix(): p.read_bytes() for p in files_of(directory)}
        fresh = {p.relative_to(other).as_posix(): p.read_bytes() for p in files_of(other)}
    return [
        f"{rel(directory / name)}: 重新生成的结果与现有文件不同"
        for name in sorted(mine.keys() | fresh.keys())
        if mine.get(name) != fresh.get(name)
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验 Xero 演示数据集的补造文件")
    parser.add_argument("--snapshot", default="20261017-q1", help="快照名 (在 data/external/xero/api/ 下) 或快照目录")
    parser.add_argument("--dir", type=Path, default=None, help="补造目录, 缺省 data/xero_demo/<快照名>")
    args = parser.parse_args(argv)
    snapshot_dir = gen.resolve_snapshot(args.snapshot)
    directory: Path = args.dir or gen.OUT_ROOT / snapshot_dir.name
    try:
        for note in check(directory, snapshot_dir):
            print(note)
    except CheckError as error:
        print(error, file=sys.stderr)
        return 1
    print(f"补造文件校验通过: {rel(directory)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
