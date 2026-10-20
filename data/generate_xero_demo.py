"""Xero 演示数据集的补造文件: 读生成规格 data/xero_demo_spec.json 与快照, 写 data/xero_demo/<快照>/ 与 manifest.

用法: uv run python data/generate_xero_demo.py [--snapshot 20261017-q1 或快照目录] [--out 目录] [--inject 场景]

- 快照只读, 且只解析白名单内的文件 (READABLE); 其余文件只读字节算哈希, contacts.json 一律不读; 不访问网页导出 raw/.
- 全部合成内容 (人名, 日期, 条款, 号码, 正文) 写在规格里, 脚本不现编; 两次生成逐字节相同.
- --inject 只用于仓库外的测试副本: 在副本目录内按副本的登记 JSON 重绘登记 PDF, 按副本快照重写 manifest 并标 injected.
- 普通生成只许写 data/xero_demo/<快照名> 或仓库外的目录; 已有的输出目录里有符号链接或硬链接时拒绝写.
- 收入合同以 CUAD v1 的合同为底稿改写, 正文与底稿记录 (base) 都在规格里; 生成不读 CUAD, 结果与有无 CUAD 无关.
  生成后的校验在有 data/external/cuad/CUAD_v1 时核对底稿, 没有时打印提示与取数命令 (CUAD_FETCH).
设计见 docs/Xero演示数据集设计.md 第 9 节.
"""

import argparse
import csv
import hashlib
import json
import os
import shutil
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
LLM_FIXTURES = REPO / "tests" / "fixtures" / "llm_xero"  # Xero 大模型 fixture 正本: <程序>/<对象>.json, 各程序的需求写
# 上传处理的固定输出 (Numbat 与 Xero 共用一份), 其中 UPLOAD_PROGRAMS 的也复制进运行副本: 账表识别按上传的文件名取,
# 与数据集无关, Xero 演示项目里上传 Xero 网页导出时用得到 (需求 R277)
UPLOAD_FIXTURES = REPO / "tests" / "fixtures" / "llm_variants" / "upload"
UPLOAD_PROGRAMS = ("DOC-SHEET",)
# 只开现有 17 条路径的固定输出 (需求 R117), 作运行副本的补位来源 (需求 R287): 两层按程序取, 正本 LLM_FIXTURES 在先,
# 正本有该程序的 fixture 文件 (<程序>/*.json) 就整个取正本的, 没有的程序才整个取这里的; 不按文件混用. 应用里的演示项目
# 只开现有程序时靠它补上正本还没有的程序; 正本有了该程序 (如全接入的报告两稿) 即以正本为准, 不必改这里.
# 测试里 R119 的分层客户端反过来把它排在运行副本之前 (tests/test_e2e_xero17)
ONLY17_FIXTURES = REPO / "tests" / "fixtures" / "llm_xero_only17"
LLM_README = (
    "# Synthetic – for demonstration only\n\n"
    "Runtime copy of tests/fixtures/llm_xero for LLM_MODE=mock (Xero datasets read llm/<procedure>/<key>.json here).\n"
    "DOC-SHEET/ is copied from tests/fixtures/llm_variants/upload/DOC-SHEET (upload fixtures, keyed by file name).\n"
    "Procedures with no fixtures in tests/fixtures/llm_xero are copied whole from tests/fixtures/llm_xero_only17\n"
    "(the path with only the existing 17 procedures).\n"
    "Written by data/generate_xero_demo.py; edit the fixtures under tests/fixtures and regenerate.\n"
)
CUAD_DIR = ROOT / "external" / "cuad" / "CUAD_v1"  # CUAD v1 (不入库), 只在校验底稿时读
CUAD_FETCH = "uv run python -m tests.tools.cuad_fetch"  # 取 CUAD 的命令 (R245)
CUAD_ATTRIBUTION = "Contract Understanding Atticus Dataset (CUAD) v1, The Atticus Project, CC BY 4.0"
CUAD_RECORD_URL = "https://zenodo.org/records/4595826"
CUAD_LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"
CUAD_DISCLAIMER = (  # CUAD_v1_README.txt 的 LICENSE 段原句; 以上四项与 tests/tools/cuad_fetch.py 的同名常量相同
    "We make no representations or warranties regarding the license status of the underlying contracts, "
    "which are publicly available and downloadable from EDGAR."
)
CUAD_TERMS = (  # CUAD_v1_README.txt 的使用条款原句 (Atticus 的隐私政策与免责声明链接)
    "The use of CUAD is subject to our privacy policy https://www.atticusprojectai.org/privacy-policy "
    "and disclaimer https://www.atticusprojectai.org/disclaimer."
)
CUAD_CREDIT = [  # 每份收入合同第 3 页签字之后的出处行 (不写底稿公司名)
    "General terms adapted from a contract in CUAD v1 (The Atticus Project, CC BY 4.0); modified",
    "for this synthetic document.",
    f"Licence: {CUAD_LICENSE_URL}",
]
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
    """按规格写全部补造文件与 manifest: 数据集声明, 客户画像, 计划预置, 登记 mock, 合同关联, 服务完成记录,
    开户行, 回函渠道, 工资册, 更正分录, 规格写有正文的 PDF (承接, 合同, 回函, 关联方与证据文件, 财报, 声明书),
    登记 PDF, documents/index.csv, 财报 fs_v1.json, 人工队列提交物 (manual/), 大模型 fixture 的运行副本 (llm/)
    与 README.md (含合同底稿的署名与许可)."""
    if not snapshot_dir.is_dir():
        raise SystemExit(f"没有快照目录 {snapshot_dir}")
    spec = load_spec()
    prefix = profile_prefix(Snapshot(snapshot_dir))
    if prefix is None:
        raise SystemExit(f"快照 {snapshot_dir} 缺组织信息 (organisation.json 或 OrganisationID)")
    if not spec["profile"]["id"].startswith(prefix):
        raise SystemExit(f"画像 ID {spec['profile']['id']} 不以 {prefix} 开头")
    refs = [ref for rel in [*(d["path"] for d in spec["documents"]), FS_JSON] for ref in file_refs(spec, rel)]
    missing = RefIndex(Snapshot(snapshot_dir)).missing(refs) if refs else []
    if missing:
        raise SystemExit(f"xero_refs 在快照中找不到: {', '.join(sorted(set(missing)))}")
    out.mkdir(parents=True, exist_ok=True)
    write_json(out, "dataset_traits.json", spec["traits"])
    write_json(out, "client_profile.json", spec["profile"])
    write_json(out, "planning/audit_objectives.json", spec["planning"]["audit_objectives"])
    write_json(out, "planning/risk_register.json", spec["planning"]["risk_register"])
    write_registry(out, spec["registry"])
    write_csv(out, "contract_links.csv", ["invoice_no", "contract_id"], spec["contract_links"])
    write_csv(
        out,
        "delivery_notes.csv",
        ["id", "delivery_no", "invoice_no", "shipped_date", "signed_date", "signed_by"],
        spec["delivery_notes"],
    )
    write_csv(
        out, "bank_accounts.csv", ["id", "bank_name", "bsb", "account_no", "gl_account_code"], spec["bank_accounts"]
    )
    if spec["receipts"]:
        write_json(out, "confirmations/receipts.json", spec["receipts"])
    write_csv(out, "payroll/employees.csv", PAYROLL_COLUMNS, spec["payroll"])
    write_csv(out, "adjustments/journal_entries.csv", JOURNAL_COLUMNS, [csv_row(r) for r in spec["adjustments"]])
    for doc in spec["documents"]:
        if "pages" in doc:
            write_pdf(out / doc["path"], ["\n".join(page) for page in doc["pages"]], doc["footer"], spec["header"])
    render_registry_pdfs(out)
    write_index(out, spec["documents"])
    if spec["financial_statements"]:
        write_financial_statements(out, spec["financial_statements"])
    for name, submission in spec["manual"].items():
        write_json(out, f"manual/{name}", submission)
    write_llm_fixtures(out)
    (out / "README.md").write_text(readme_text(spec), encoding="utf-8")
    write_manifest(out, snapshot_dir)
    return out


def readme_text(spec: dict[str, Any]) -> str:
    """补造目录的 README.md: 生成方式, 以及收入合同的 CUAD 底稿, 署名, 许可与改写方式 (底稿取自规格的 base)."""
    bases = "".join(f"  - {d['path']}: {d['base']['file']}\n" for d in spec["documents"] if "base" in d)
    return (
        "# Synthetic – for demonstration only\n\n"
        f"本目录是 Xero 演示数据集 {spec['dataset']} 的补造文件, 全部合成, 由 {GENERATOR} 按 data/xero_demo_spec.json "
        "生成, 不要手改. 各文件的哈希与合成标志见 manifest.json, 设计见 docs/Xero演示数据集设计.md.\n\n"
        "## 收入合同的底稿 (contracts/)\n\n"
        "3 份收入合同各以下面这个数据集中的一份合同为底稿, 已改写:\n\n"
        f"{CUAD_ATTRIBUTION}.\n\n"
        f"- 来源: Zenodo 记录 4595826, {CUAD_RECORD_URL}, 版本 v1.\n"
        f"- 许可: Creative Commons Attribution 4.0, {CUAD_LICENSE_URL}\n"
        f'- CUAD 的免责句: "{CUAD_DISCLAIMER}"\n'
        f'- CUAD 的使用条款: "{CUAD_TERMS}"\n'
        "- 改写方式: 当事方, 合同编号, 金额, 日期, 完工与控制权转移, 开票与付款, 贷项与终止条款按设计稿第 4.3 节"
        "编写; 合同结构与一般条款 (服务方式, 双方配合, 保密, 责任限制, 不可抗力, 转让, 变更与适用法律等) 取自底稿, "
        "删改后改写为 Demo Company (AU) 与其客户之间适用新南威尔士州法律的合同. "
        "每份合同第 3 页末注明出处, 已修改与许可链接; 取用的底稿原句记在生成规格各合同的 base.excerpts.\n"
        "- 底稿 (CUAD master_clauses.csv 的文件名, manifest 的 files 段同记):\n"
        f"{bases}"
        f"- 合同 PDF 已入库, 使用时不需要 CUAD. 核对底稿要先取 CUAD 到 data/external/cuad/ (不入库): `{CUAD_FETCH}`.\n"
    )


def file_base(spec: dict[str, Any], rel: str) -> dict[str, str] | None:
    """manifest files 段的底稿来源: 规格 documents 段该文件有 base 时为 {dataset, file}, 否则为 None."""
    for doc in spec["documents"]:
        if doc["path"] == rel and "base" in doc:
            return {"dataset": doc["base"]["dataset"], "file": doc["base"]["file"]}
    return None


def write_llm_fixtures(
    out: Path,
    source: Path = LLM_FIXTURES,
    uploads: Path = UPLOAD_FIXTURES,
    programs: tuple[str, ...] = UPLOAD_PROGRAMS,
    fill: Path = ONLY17_FIXTURES,
) -> None:
    """大模型 fixture 的运行副本: 清空 out/llm 后写 README.md, 再把 source 下的 <程序>/<对象>.json, fill 下 source
    里没有 fixture 文件的程序的 <程序>/<对象>.json (按程序补位, 不按文件) 与 uploads 下 programs 各程序的
    <程序>/<键>.json 原样复制过去 (正本删掉的文件不留副本). 不存在的目录跳过 (都不存在时只写 README). 正本根目录,
    程序目录或文件是符号链接时拒绝 (只复制仓库里的文件), 在清空之前查, 被拒时 out/llm 保持原样."""
    for root in (source, uploads, fill):
        if root.is_symlink():
            raise SystemExit(f"大模型 fixture 正本 {root} 是符号链接")
    files = sorted(source.glob("*/*.json")) if source.is_dir() else []
    have = {p.parent.name for p in files}
    files += [p for p in sorted(fill.glob("*/*.json")) if p.parent.name not in have] if fill.is_dir() else []
    files += [p for program in programs for p in sorted((uploads / program).glob("*.json"))] if uploads.is_dir() else []
    linked = [p.relative_to(p.parents[1]).as_posix() for p in files if p.is_symlink() or p.parent.is_symlink()]
    if linked:
        raise SystemExit(f"大模型 fixture 是符号链接: {', '.join(linked)}")
    target = out / "llm"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    (target / "README.md").write_text(LLM_README, encoding="utf-8")
    for path in files:
        dest = target / path.parent.name / path.name
        dest.parent.mkdir(exist_ok=True)
        shutil.copyfile(path, dest)


FS_JSON = "financial_statements/fs_v1.json"
FS_PDF = "financial_statements/fs_v1.pdf"


def write_financial_statements(out: Path, fs: dict[str, Any]) -> None:
    """待审财报 fs_v1.json (第 9.5 节, 字段同需求清单 R216 的 FinancialStatements): 规格的报表加 document_id,
    即同目录 fs_v1.pdf 的文件 ID (DOC- 加 sha256 前 12 位, 同 manifest), 所以须在 PDF 写出之后调用."""
    head = {k: fs[k] for k in ("id", "version", "prepared_at", "directors_declaration_date")}
    rest = {k: fs[k] for k in ("comparative_restatement_entry_nos", "lines")}
    write_json(out, FS_JSON, {**head, "document_id": "DOC-" + sha256(out / FS_PDF)[:12], **rest})


PAYROLL_COLUMNS = [
    "employee_id",
    "name",
    "start_date",
    "employment_type",
    "pay_period_days",
    "gross_per_period_cents",
]  # 工资册 (第 9.1 节; 金额按 _cents 约定)
JOURNAL_COLUMNS = [
    "id",
    "entry_no",
    "posting_date",
    "created_at",
    "created_by",
    "approved_by",
    "account_code",
    "debit_cents",
    "credit_cents",
    "description",
    "source_doc",
    "is_adjustment",
]  # 表头同 data/demo/journal_entries.csv (JournalEntry 的字段顺序)


def csv_row(row: dict[str, Any]) -> dict[str, Any]:
    """CSV 单元格写法同 data/demo: 布尔写 true/false, 空值写空串."""
    return {k: ("true" if v else "false") if isinstance(v, bool) else ("" if v is None else v) for k, v in row.items()}


def write_csv(out: Path, name: str, columns: list[str], rows: list[dict[str, Any]]) -> None:
    """规格里的一段表写成 CSV (换行 \\n, 列顺序按 columns; 表头同 data/demo 的同名表); 没有行时不写."""
    if not rows:
        return
    path = out / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\n", extrasaction="raise")
        writer.writeheader()
        writer.writerows(rows)


def write_registry(out: Path, registry: dict[str, Any]) -> None:
    """登记 mock: registry/abn.json, asic.json, extracts/{acn}.json 与 persons/{姓-名-yyyymmdd}.json (第 2.5 节)."""
    if not registry:
        return
    write_json(out, "registry/abn.json", registry["abn"])
    write_json(out, "registry/asic.json", registry["asic"])
    for extract in registry["extracts"]:
        write_json(out, f"registry/extracts/{extract['acn']}.json", extract)
    for person in registry["persons"]:
        write_json(out, f"registry/persons/{person_key(person)}.json", person)


def person_key(person: dict[str, Any]) -> str:
    """人员检索的文件名主干 {姓}-{名}-{yyyymmdd}, 大写, 如 MORGAN-ALEX-19790412."""
    return f"{person['family_name']}-{person['given_name']}-{person['birth_date'].replace('-', '')}".upper()


def write_index(out: Path, documents: list[dict[str, Any]]) -> None:
    """documents/index.csv: 规格中 index 为真的文件, 列 path, document_type, reliability, document_date, related_ids
    (以 ";" 分隔); 载入器据此登记证据文件 (第 9.5 节)."""
    rows = [doc for doc in documents if doc.get("index")]
    if not rows:
        return
    path = out / "documents" / "index.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(["path", "document_type", "reliability", "document_date", "related_ids"])
        for doc in rows:
            writer.writerow(
                [doc["path"], doc["type"], doc["reliability"], doc["document_date"], ";".join(doc["related_ids"])]
            )


MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]
COMPANY_CODES = {
    "APTY": "Australian Proprietary Company",
    "APUB": "Australian Public Company",
    "LMSH": "Limited by Shares",
    "PROP": "Proprietary Company",
}  # 登记摘录里类型代码的说明, 只用于版式


def long_date(value: str | None) -> str:
    """2018-09-03 → 3 September 2018; 空为 "-"."""
    if not value:
        return "-"
    year, month, day = (int(x) for x in value.split("-"))
    return f"{day} {MONTHS[month - 1]} {year}"


def spaced_acn(acn: str) -> str:
    """900043021 → 900 043 021."""
    return f"{acn[:3]} {acn[3:6]} {acn[6:]}"


def extract_pages(data: dict[str, Any]) -> list[str]:
    """公司摘录 2 页: 第 1 页公司与高管, 第 2 页股本与股东; 每页都写名称, ACN 与注册日."""
    acn, name, registered = spaced_acn(data["acn"]), data["name"], long_date(data["registered_date"])
    holders = [
        f"{h['name']:<22}{long_date(h['birth_date']):<20}{h['role'].title():<12}{long_date(h['appointed'])}"
        for h in data["officeholders"]
    ]
    classes: dict[str, int] = {}
    for holder in data["shareholders"]:
        classes[holder["share_class"]] = classes.get(holder["share_class"], 0) + holder["shares"]
    paid = "not shown" if data["paid_up_cents"] is None else f"AUD {data['paid_up_cents'] / 100:,.2f}"
    members = [
        f"{m['name']:<22}{m['kind']:<9}{spaced_acn(m['acn']) if m['acn'] else '-':<13}{m['share_class']:<7}"
        f"{m['shares']:<8}{m['pct']:.0%}"
        for m in data["shareholders"]
    ]
    page1 = [
        "COMPANY EXTRACT (MOCK REGISTRY SEARCH)",
        "",
        "Organisation details",
        f"Name:               {name}",
        f"ACN:                {acn}",
        f"Type:               {data['type']} {COMPANY_CODES.get(data['type'], '')}".rstrip(),
        f"Class:              {data['class']} {COMPANY_CODES.get(data['class'], '')}".rstrip(),
        f"Subclass:           {data['subclass']} {COMPANY_CODES.get(data['subclass'], '')}".rstrip(),
        f"Status:             {data['status']}",
        f"Registration date:  {registered}",
        "",
        "Office holders",
        f"{'Name':<22}{'Date of birth':<20}{'Role':<12}Appointed",
        *holders,
    ]
    page2 = [
        "COMPANY EXTRACT (MOCK REGISTRY SEARCH) - SHARES",
        "",
        f"Company: {name}",
        f"ACN {acn}, registered {registered}",
        "",
        "Share structure",
        f"{'Class':<7}{'Number issued':<16}Total amount paid",
        *(f"{share_class:<7}{count:<16}{paid}" for share_class, count in classes.items()),
        "",
        "Members",
        f"{'Name':<22}{'Kind':<9}{'ACN':<13}{'Class':<7}{'Shares':<8}Percentage",
        *members,
    ]
    return ["\n".join(page1), "\n".join(page2)]


def person_pages(data: dict[str, Any]) -> list[str]:
    """人员检索 1 页: 姓名, 出生日期与各公司的职务和持股."""
    roles = [
        f"{spaced_acn(r['acn']):<13}{r['company_name']:<34}{r['role'].title():<13}{long_date(r['appointed']):<20}"
        f"{r['shares'] if r['shares'] is not None else '-'}"
        for r in data["roles"]
    ]
    page = [
        "PERSON SEARCH (MOCK REGISTRY SEARCH)",
        "",
        f"Name:           {data['family_name'].upper()}, {data['given_name']}",
        f"Date of birth:  {long_date(data['birth_date'])}",
        "",
        "Roles and shareholdings",
        f"{'ACN':<13}{'Company':<34}{'Role':<13}{'Appointed':<20}Shares",
        *roles,
    ]
    return ["\n".join(page)]


def pdf_sources(directory: Path) -> dict[str, list[str]]:
    """各 PDF 的来源正文 (按页): 规格写有正文的取规格, 登记 PDF 取目录内同名 JSON 的渲染结果. 供校验核对文本层.
    读不出或缺字段的登记 JSON 跳过 (格式问题由校验的登记核对报出)."""
    sources = {
        doc["path"]: ["\n".join(page) for page in doc["pages"]] for doc in load_spec()["documents"] if "pages" in doc
    }
    for folder, render in (("extracts", extract_pages), ("persons", person_pages)):
        for path in sorted((directory / "registry" / folder).glob("*.json")):
            try:
                pages = render(read_spec_json(path))
            except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError):
                continue
            sources[path.with_suffix(".pdf").relative_to(directory).as_posix()] = pages
    return sources


REGISTRY_PDFS = ("registry/extracts/", "registry/persons/")  # 登记 PDF 的目录, 登记信息按规则推出


def document_meta(spec: dict[str, Any], rel: str) -> dict[str, Any] | None:
    """PDF 的登记信息: 规格 documents 段有就取它; 登记 PDF (含测试副本新增的) 按规则推出: registry_extract,
    external_original, registration registry, 文件日期为检索日 (数据集声明的 data_as_of); 都不是时为 None."""
    for doc in spec["documents"]:
        if doc["path"] == rel:
            return dict(doc)
    if rel.startswith(REGISTRY_PDFS):
        return {
            "path": rel,
            "type": "registry_extract",
            "reliability": "external_original",
            "document_date": spec["traits"]["data_as_of"],
            "related_ids": [],
            "registration": "registry",
        }
    return None


def read_spec_json(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def render_registry_pdfs(directory: Path) -> list[Path]:
    """按目录内的登记 JSON (registry/extracts/*.json, registry/persons/*.json) 渲染同名 PDF, 内容只取自 JSON
    (页眉取规格). 测试副本改了登记 JSON 后调用它重绘. 返回写出的 PDF."""
    header = load_spec()["header"]
    written: list[Path] = []
    for path in sorted((directory / "registry" / "extracts").glob("*.json")):
        data = read_spec_json(path)
        write_pdf(
            path.with_suffix(".pdf"),
            extract_pages(data),
            f"Mock registry extract | ACN {spaced_acn(data['acn'])}",
            header,
        )
        written.append(path.with_suffix(".pdf"))
    for path in sorted((directory / "registry" / "persons").glob("*.json")):
        data = read_spec_json(path)
        footer = f"Mock person search | {data['family_name'].upper()}, {data['given_name']}"
        write_pdf(path.with_suffix(".pdf"), person_pages(data), footer, header)
        written.append(path.with_suffix(".pdf"))
    return written


def file_refs(spec: dict[str, Any], rel: str) -> list[str]:
    """文件引用的 Xero 对象 (manifest 的 xero_refs): 文件在规格 documents 段时取其 xero_refs; fs_v1.json 取比较数
    重述所依据的凭证号; 其余文件的规则由后续需求补上, 暂为空."""
    for doc in spec["documents"]:
        if doc["path"] == rel:
            return list(doc.get("xero_refs", []))
    if rel == FS_JSON and spec["financial_statements"]:
        return list(spec["financial_statements"]["comparative_restatement_entry_nos"])
    return []


def write_manifest(directory: Path, snapshot_dir: Path, injected: str | None = None) -> dict[str, Any]:
    """重算补造目录的 manifest: 各文件哈希与合成标志, PDF 的登记信息, 快照各文件哈希 (不含 contacts.json).
    测试副本传 injected (场景名). xero_refs 是否都在快照中由 generate 核对."""
    if not snapshot_dir.is_dir():
        raise SystemExit(f"没有快照目录 {snapshot_dir}")
    spec = load_spec()
    snapshot = Snapshot(snapshot_dir)
    files: list[dict[str, Any]] = []
    documents: list[dict[str, Any]] = []
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
        base = file_base(spec, rel)
        if base is not None:
            item["base"] = base
        files.append(item)
        if path.suffix == ".pdf":
            doc = document_meta(spec, rel)
            if doc is None:
                raise SystemExit(f"PDF {rel} 在规格 documents 段没有登记信息")
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
        for note in check(out, snapshot_dir, cuad=CUAD_DIR):
            print(note)
    except CheckError as error:
        print(error, file=sys.stderr)
        return 1
    print(f"补造文件已生成并通过校验: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
