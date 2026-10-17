"""Xero 演示数据集的补造文件: 读生成规格 data/xero_demo_spec.json 与快照, 写 data/xero_demo/<快照>/ 与 manifest.

用法: uv run python data/generate_xero_demo.py [--snapshot 20261017-q1 或快照目录] [--out 目录] [--inject 场景]

- 快照只读, 且只解析白名单内的文件 (READABLE); 其余文件只读字节算哈希, contacts.json 一律不读; 不访问网页导出 raw/.
- 全部合成内容 (人名, 日期, 条款, 号码, 正文) 写在规格里, 脚本不现编; 两次生成逐字节相同.
- --inject 只用于仓库外的测试副本: 在副本目录内按副本的登记 JSON 重绘登记 PDF, 按副本快照重写 manifest 并标 injected.
- 普通生成只许写 data/xero_demo/<快照名> 或仓库外的目录; 已有的输出目录里有符号链接或硬链接时拒绝写.
设计见 docs/Xero演示数据集设计.md 第 9 节.
"""

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from reportlab.pdfgen.canvas import Canvas

ROOT = Path(__file__).resolve().parent  # data/
REPO = ROOT.parent
SPEC = ROOT / "xero_demo_spec.json"
SNAPSHOTS = ROOT / "external" / "xero" / "api"  # 快照的上级目录
OUT_ROOT = ROOT / "xero_demo"  # 补造目录的上级, 一个快照一套补造
GENERATOR = "data/generate_xero_demo.py"
READABLE = frozenset({"accounts", "invoices", "credit_notes", "journals", "bank_statements", "organisation", "dataset"})
NOT_HASHED = frozenset({"contacts.json"})  # 快照中不读也不进哈希清单的文件 (含登录账号联系人)
PAGE_LINES = 56  # 每页正文最多行数 (页眉与空行之外), 再多就压到页脚
LINE_WIDTH = 95  # 每行最多字符数 (Courier 9pt 每字 5.4pt, 左右各留 40pt)


class Snapshot:
    """只读的快照目录, 与载入器的 Snapshot 同接口; load 只许白名单内的文件 (加 trial_balance_*)."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def name(self) -> str:
        return self.root.name

    def load(self, name: str) -> Any:
        if name not in READABLE and not name.startswith("trial_balance_"):
            raise ValueError(f"快照文件 {name} 不在读取白名单内")
        path = self.root / f"{name}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []

    def file_hashes(self) -> list[dict[str, str]]:
        """快照各文件的 sha256 (只读字节, 不解析), 不含 contacts.json; 按文件名排序."""
        return [
            {"path": p.name, "sha256": sha256(p)}
            for p in sorted(self.root.iterdir())
            if p.is_file() and p.name not in NOT_HASHED
        ]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resolve_snapshot(value: str | Path) -> Path:
    """--snapshot 不含路径分隔符时是快照名 (在 data/external/xero/api/ 下), 含分隔符时是快照目录路径."""
    text = str(value)
    return Path(text) if os.sep in text or "/" in text else SNAPSHOTS / text


def inside(path: Path, root: Path) -> bool:
    """path 是否在 root 之下 (含 root 本身). 逐级用 os.path.samefile 比较已存在的祖先目录,
    大小写不敏感的文件系统与符号链接也能识别."""
    resolved = path.resolve()
    return any(p.exists() and os.path.samefile(p, root) for p in (resolved, *resolved.parents))


def same_path(a: Path, b: Path) -> bool:
    """a 与 b 是否同一路径; 尚不存在时比较已存在的上级目录 (samefile) 与名字."""
    if a.exists() and b.exists():
        return os.path.samefile(a, b)
    if a.exists() or b.exists() or a.name != b.name:
        return False
    return same_path(a.resolve().parent, b.resolve().parent)


def out_problem(out: Path, snapshot_dir: Path, inject: bool) -> str | None:
    """输出目录不合规时返回原因: --inject 只许仓库外已存在的副本目录; 普通生成只许 data/xero_demo/<快照名>
    或仓库外的目录. 两种情况下已有的输出目录里都不许有符号链接与硬链接 (防止顺着链接改到别处的文件)."""
    if inject and inside(out, REPO):
        return f"--inject 只能用于仓库外的测试副本, 不能写 {out}"
    if inject and not out.is_dir():
        return f"--inject 的副本目录 {out} 不存在"
    if not inject and inside(out, REPO) and not same_path(out, OUT_ROOT / snapshot_dir.name):
        return f"补造文件只能写到 {OUT_ROOT / snapshot_dir.name} 或仓库外的目录, 不能写 {out}"
    if out.is_dir():
        linked = [p for p in out.rglob("*") if p.is_symlink() or (p.is_file() and p.stat().st_nlink > 1)]
        if linked:
            return f"输出目录中有符号链接或硬链接: {linked[0]}"
    return None


def load_spec() -> dict[str, Any]:
    spec: dict[str, Any] = json.loads(SPEC.read_text(encoding="utf-8"))
    return spec


def write_json(directory: Path, name: str, value: Any) -> None:
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_pdf(path: Path, pages: list[str], footer: str, header: str) -> None:
    """写 PDF (后续各条共用): A4 (595 × 842), Courier 9pt, 每页第一行为页眉原文, 页脚 "{footer} | Page n of N";
    invariant=1 使两次生成逐字节相同. 某页超过 PAGE_LINES 行或某行超过 LINE_WIDTH 字时报错, 由规格自己分页."""
    for n, page in enumerate(pages, 1):
        lines = page.strip("\n").splitlines()
        if len(lines) > PAGE_LINES:
            raise ValueError(f"{path.name} 第 {n} 页有 {len(lines)} 行, 超过 {PAGE_LINES} 行")
        wide = [line for line in [header, *lines] if len(line) > LINE_WIDTH]
        if wide:
            raise ValueError(f"{path.name} 第 {n} 页有 {len(wide)} 行超过 {LINE_WIDTH} 字")
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas = Canvas(str(path), invariant=1, pagesize=(595, 842))
    for n, page in enumerate(pages, 1):
        text = canvas.beginText(40, 795)
        text.setFont("Courier", 9)
        text.setLeading(13)
        for line in [header, "", *page.strip("\n").splitlines()]:
            text.textLine(line)
        canvas.drawText(text)
        canvas.setFont("Helvetica", 8)
        canvas.drawString(40, 35, f"{footer} | Page {n} of {len(pages)}")
        canvas.showPage()
    canvas.save()


class RefIndex:
    """快照中可引用的对象, 用于核对 manifest 的 xero_refs: SI-{InvoiceID}, XJ-{JournalNumber} 及其分录行
    XJ-{JournalNumber}-{n}, BT-{科目}-{序号}, BA-{科目} 与科目代码. 编号与范围沿用载入器 (adapters/xero_dataset)
    的规则 (SI- 只含已生效的销售发票), 只用读白名单文件的函数 (不用 build_tables 与 map_sales_invoices,
    二者会读 contacts.json)."""

    def __init__(self, snapshot: Snapshot) -> None:
        from backend.adapters.xero_dataset import (
            LIVE_DOCUMENTS,
            journal_entries_from_api,
            journal_rows,
            map_bank_transactions,
        )

        accounts = snapshot.load("accounts")
        self.codes = {str(a["Code"]) for a in accounts if a.get("Code")}
        self.bank_accounts = {f"BA-{a['Code']}" for a in accounts if a.get("Type") == "BANK" and a.get("Code")}
        self.invoices = {
            f"SI-{i['InvoiceID']}"
            for i in snapshot.load("invoices")
            if i.get("Type") == "ACCREC" and i.get("Status") in LIVE_DOCUMENTS
        }
        entries = journal_entries_from_api(snapshot)  # type: ignore[arg-type]
        self.journals = {e.entry_no for e in entries} | {row.id for row in journal_rows(entries)}
        self.bank_lines = {t.id for t in map_bank_transactions(snapshot)}  # type: ignore[arg-type]

    def missing(self, refs: Iterable[str]) -> list[str]:
        known = self.codes | self.bank_accounts | self.invoices | self.journals | self.bank_lines
        return [r for r in refs if r not in known]


def profile_prefix(snapshot: Snapshot) -> str | None:
    """补造画像 ID 应有的前缀 CLIENT-XERO-{短 ID}- (由快照组织 ID 推出, 与 20261017-mock 的载入器画像区分);
    快照缺组织信息时为 None."""
    from backend.adapters.xero_dataset import short

    organisations = snapshot.load("organisation")
    org = organisations[0] if isinstance(organisations, list) and organisations else None
    if not isinstance(org, dict) or not isinstance(org.get("OrganisationID"), str):
        return None
    return f"CLIENT-XERO-{short(org['OrganisationID'])}-"


def generate(snapshot_dir: Path, out: Path) -> Path:
    """按规格写全部补造文件与 manifest. 本版写数据集声明, 客户画像与计划预置; 其余各段由后续需求补进."""
    if not snapshot_dir.is_dir():
        raise SystemExit(f"没有快照目录 {snapshot_dir}")
    spec = load_spec()
    prefix = profile_prefix(Snapshot(snapshot_dir))
    if prefix is None:
        raise SystemExit(f"快照 {snapshot_dir} 缺组织信息 (organisation.json 或 OrganisationID)")
    if not spec["profile"]["id"].startswith(prefix):
        raise SystemExit(f"画像 ID {spec['profile']['id']} 不以 {prefix} 开头")
    out.mkdir(parents=True, exist_ok=True)
    write_json(out, "dataset_traits.json", spec["traits"])
    write_json(out, "client_profile.json", spec["profile"])
    write_json(out, "planning/audit_objectives.json", spec["planning"]["audit_objectives"])
    write_json(out, "planning/risk_register.json", spec["planning"]["risk_register"])
    render_registry_pdfs(out)
    write_manifest(out, snapshot_dir)
    return out


def render_registry_pdfs(directory: Path) -> list[Path]:
    """按目录内的登记 JSON (registry/extracts, registry/persons) 渲染同名 PDF, 只读 JSON. 登记 JSON 由后续需求生成,
    没有时不做事. 测试副本改了登记 JSON 后调用它重绘."""
    return []


def file_refs(spec: dict[str, Any], rel: str) -> list[str]:
    """文件引用的 Xero 对象 (manifest 的 xero_refs): 文件在规格 documents 段时取其 xero_refs; 其余文件的规则由
    后续需求补上, 暂为空."""
    for doc in spec["documents"]:
        if doc["path"] == rel:
            return list(doc.get("xero_refs", []))
    return []


def write_manifest(directory: Path, snapshot_dir: Path, injected: str | None = None) -> dict[str, Any]:
    """重算补造目录的 manifest: 各文件哈希与合成标志, PDF 的登记信息, 快照各文件哈希 (不含 contacts.json).
    xero_refs 中有快照里找不到的对象时失败. 测试副本传 injected (场景名)."""
    if not snapshot_dir.is_dir():
        raise SystemExit(f"没有快照目录 {snapshot_dir}")
    spec = load_spec()
    snapshot = Snapshot(snapshot_dir)
    files: list[dict[str, Any]] = []
    documents: list[dict[str, Any]] = []
    meta = {doc["path"]: doc for doc in spec["documents"]}
    refs: list[str] = []
    for path in sorted(directory.rglob("*")):
        rel = path.relative_to(directory).as_posix()
        if not path.is_file() or rel == "manifest.json":
            continue
        item: dict[str, Any] = {
            "path": rel,
            "sha256": sha256(path),
            "synthetic": True,
            "generator": GENERATOR,
            "xero_refs": file_refs(spec, rel),
        }
        refs.extend(item["xero_refs"])
        files.append(item)
        if path.suffix == ".pdf":
            if rel not in meta:
                raise SystemExit(f"PDF {rel} 在规格 documents 段没有登记信息")
            doc = meta[rel]
            documents.append(
                {
                    "path": rel,
                    "sha256": item["sha256"],
                    "document_id": "DOC-" + item["sha256"][:12],
                    "type": doc["type"],
                    "reliability": doc["reliability"],
                    "page_count": page_count(path),
                    "document_date": doc["document_date"],
                    "related_ids": list(doc.get("related_ids", [])),
                    "registration": doc["registration"],
                    "synthetic": True,
                }
            )
    if refs:
        missing = RefIndex(snapshot).missing(refs)
        if missing:
            raise SystemExit(f"xero_refs 在快照中找不到: {', '.join(sorted(set(missing)))}")
    manifest: dict[str, Any] = {
        "dataset": spec["dataset"],
        "generator": GENERATOR,
        "spec_sha256": sha256(SPEC),
        "snapshot": {"name": f"xero/{snapshot.name}", "files": snapshot.file_hashes()},
        "files": files,
        "documents": documents,
    }
    if injected is not None:
        manifest["injected"] = injected
    write_json(directory, "manifest.json", manifest)
    return manifest


def page_count(path: Path) -> int:
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        return len(pdf.pages)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 Xero 演示数据集的补造文件")
    parser.add_argument("--snapshot", default="20261017-q1", help="快照名 (在 data/external/xero/api/ 下) 或快照目录")
    parser.add_argument("--out", type=Path, default=None, help="输出目录, 缺省 data/xero_demo/<快照名>")
    parser.add_argument("--inject", default=None, help="仓库外测试副本的场景名: 只重绘登记 PDF 并重写 manifest")
    args = parser.parse_args(argv)
    snapshot_dir = resolve_snapshot(args.snapshot)
    out: Path = args.out or OUT_ROOT / snapshot_dir.name
    problem = out_problem(out, snapshot_dir, inject=args.inject is not None)
    if problem:
        print(problem, file=sys.stderr)
        return 2
    if args.inject is not None:
        render_registry_pdfs(out)
        write_manifest(out, snapshot_dir, injected=args.inject)
        return 0
    generate(snapshot_dir, out)
    from check_xero_demo import CheckError, check

    try:
        for note in check(out, snapshot_dir):
            print(note)
    except CheckError as error:
        print(error, file=sys.stderr)
        return 1
    print(f"补造文件已生成并通过校验: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
