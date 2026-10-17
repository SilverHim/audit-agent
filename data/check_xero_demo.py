"""Xero 演示数据集补造文件的校验: manifest 哈希, 快照哈希, 合成标志, 模型严格校验, 登录账号联系人扫描与确定性.

用法: uv run python data/check_xero_demo.py [--snapshot 20261017-q1 或快照目录] [--dir 补造目录]

- 问题全部收集后一次抛 CheckError, 每行一项, 只写文件名; 登录账号联系人的姓名与邮箱只在内存里比对, 不打印, 不写文件.
- 没有本地快照时跳过快照哈希, 画像 ID 前缀, 联系人扫描与确定性, 补造文件本身照常核对; 快照在而 contacts.json
  缺失或无法解析时报错, 不静默跳过联系人扫描.
设计见 docs/Xero演示数据集设计.md 第 9.4 节.
"""

import argparse
import json
import re
import sys
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import generate_xero_demo as gen
import pdfplumber
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from backend.contracts.models import AuditObjective, ClientProfile, RiskItem

REPO = gen.ROOT.parent
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
    ]
    notes: list[str] = []
    if snapshot_dir.is_dir():
        problems += guarded(snapshot_dir, check_snapshot, directory, manifest, snapshot_dir)
        problems += guarded(snapshot_dir / "contacts.json", scan_login_contact, snapshot_dir, [directory, *scan])
        if determinism:
            problems += guarded(directory, check_determinism, directory, snapshot_dir)
    else:
        notes.append(f"{NO_SNAPSHOT} ({rel(snapshot_dir)}): 跳过快照哈希, 画像 ID 前缀, 登录账号联系人扫描与确定性检查")
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


def pdf_pages(path: Path) -> list[str] | None:
    """PDF 各页的文本层; 无法解析时为 None."""
    try:
        with pdfplumber.open(path) as pdf:
            return [page.extract_text() or "" for page in pdf.pages]
    except Exception:
        return None


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
