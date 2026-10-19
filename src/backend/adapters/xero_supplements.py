"""Xero 演示数据集的补造目录 (data/xero_demo/<快照目录名>/, 由 data/generate_xero_demo.py 生成): 读 manifest, 核对
补造文件与快照, 读客户画像与计划预置. 格式见 docs/Xero演示数据集设计.md 第 9.2, 9.5 节.

补造目录与快照目录同名. 读任何补造文件之前先 verify:
- manifest 的 dataset 与 snapshot.name 都等于数据集名;
- manifest.files 的每个文件都在, sha256 相符, 目录里没有清单外的文件 (manifest.json 除外);
- 快照目录的文件去掉 contacts.json (测试副本与派生快照里它是过滤版, 不记哈希) 恰为 snapshot.files, sha256 相符;
- 测试副本 (manifest 带 injected) 只在 allow_injected 下放行.
不符抛 SupplementMismatch, 消息只列文件名, 不带文件内容.

apply 把补造表并入快照映射出的财务表 (设计第 3.1, 3.3 节, 第 9.3 节第 6-9 项): 合同关联填
sales_invoices.contract_id, 服务完成记录写成 delivery_notes (发票号换成 SI- ID), 开户行覆盖 bank_accounts.bank_name;
合同 PDF 由 contracts() 列出. 发票号在快照中找不到或不唯一, 或开户行表的账号与快照不符, 抛 SupplementMismatch
(消息含发票号或账户 ID), 不载入. 现在数据集载入仍并入补造表并登记合同; 演示数据导入 (R269) 接手后改为只载快照.

演示导入清单 (plan, 需求 R268): 不连库列出演示数据导入要写的补造账表 (demo_tables) 与要上传的文件 (uploads),
供 R269 按阶段写入与上传.
"""

import csv
import hashlib
import json
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ValidationError

from backend.adapters.xero_dataset import Snapshot, build_tables
from backend.contracts.data import TableKind
from backend.contracts.models import AuditObjective, ClientProfile, DocumentType, Model, Reliability, RiskItem
from backend.contracts.records import BankAccount, DatasetTraits, DeliveryNote, Employee, SalesInvoice

MISMATCH = "补造文件与快照不匹配"
UNHASHED = frozenset({"contacts.json"})  # 快照中不记哈希的文件
DemoStage = Literal["acceptance", "fieldwork", "statements", "manual"]  # 演示数据导入的阶段 (设计第 9.5 节)
STAGES: tuple[DemoStage, ...] = ("acceptance", "fieldwork", "statements", "manual")  # 清单按此顺序
ARCHIVED = frozenset({"registry", "confirmation"})  # 程序执行时由登记 mock 与函证 mock 存档, 不在清单里
STAGE_REQUESTS: dict[str, str] = {"statements": "REQ-financial_statements"}  # 按补交请求上传的阶段 → 请求 ID
# 补造覆盖的账表 → 来源文件 (相对补造目录), 按 TableKind 顺序; 工资册的文件取数据集声明的 payroll_register
COVERED: tuple[tuple[TableKind, str], ...] = (
    (TableKind.sales_invoices, "contract_links.csv"),
    (TableKind.bank_accounts, "bank_accounts.csv"),
    (TableKind.delivery_notes, "delivery_notes.csv"),
)


class SupplementMismatch(ValueError):
    """补造文件与快照不匹配, 或测试副本未经允许载入. 建项目接口按 INVALID_VALUE 返回."""


@dataclass(frozen=True)
class DemoTable:
    """演示导入要写的一张补造账表: 种类, 来源文件 (相对补造目录) 与整表的行 (写入时整表替换)."""

    kind: TableKind
    file_name: str
    rows: list[BaseModel]


class DemoUpload(Model):
    """演示导入要上传的一份文件 (manifest.documents 的一项), 上传后按这里的值补登记."""

    path: str  # 相对补造目录, 如 contracts/CON-X01.pdf
    file_name: str  # 上传时的文件名 (path 的最后一段)
    stage: DemoStage  # 上传的阶段
    document_type: DocumentType
    reliability: Reliability
    document_date: date | None
    related_ids: list[str]
    request_id: str | None  # 按补交请求上传的 (statements) 为请求 ID, 其余为空
    document_id: str  # 上传后的文件 ID (按内容, 同 EvidenceStore.save)


@dataclass(frozen=True)
class DemoPlan:
    """演示导入清单: 补造账表 (按 TableKind 顺序) 与上传文件 (按阶段, 再按文件名)."""

    dataset: str
    tables: list[DemoTable]
    uploads: list[DemoUpload]


class SupplementLoader(Protocol):
    """plan 用到的载入器部分 (XeroDatasetLoader): 按数据集取已核对的补造目录."""

    def supplement(self, dataset: str) -> "Supplements | None": ...


def plan(loader: SupplementLoader, dataset: str) -> DemoPlan:
    """数据集的演示导入清单, 不连库. 先核对补造目录 (loader.supplement, 不符抛 SupplementMismatch); 没有补造目录抛
    FileNotFoundError. 清单内容不合规 (类型或登记途径不认识, index.csv 与 manifest 不符) 抛 ValueError, 见 uploads."""
    found = loader.supplement(dataset)
    if found is None:
        raise FileNotFoundError(f"{dataset} 没有补造目录")
    return DemoPlan(dataset=dataset, tables=found.demo_tables(), uploads=found.uploads())


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class Supplements:
    """一个快照的补造目录: root 为补造目录, snapshot 为同名快照目录, dataset 为数据集名 "xero/<快照目录名>"."""

    root: Path
    snapshot: Path
    dataset: str
    allow_injected: bool = False

    def verify(self) -> dict[str, Any]:
        """核对补造目录与快照 (见模块说明), 通过时返回 manifest."""
        try:
            manifest: dict[str, Any] = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SupplementMismatch(f"{MISMATCH}: manifest.json") from exc
        if not isinstance(manifest, dict):
            raise SupplementMismatch(f"{MISMATCH}: manifest.json")
        injected = manifest.get("injected")
        if injected is not None and not self.allow_injected:
            raise SupplementMismatch(f"测试副本 (injected={injected}) 只能在 allow_injected 下载入")
        section = manifest.get("snapshot")
        section = section if isinstance(section, dict) else {}
        problems: list[str] = []
        if manifest.get("dataset") != self.dataset or section.get("name") != self.dataset:
            problems.append("manifest.json")
        listed = hashes(manifest.get("files"))
        present = {p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file()} - {"manifest.json"}
        problems += sorted(present ^ listed.keys())
        problems += [name for name in sorted(present & listed.keys()) if sha256(self.root / name) != listed[name]]
        recorded = hashes(section.get("files"))
        actual = {p.name for p in self.snapshot.iterdir() if p.is_file()} - UNHASHED
        changed = sorted(actual ^ recorded.keys()) + [
            name for name in sorted(actual & recorded.keys()) if sha256(self.snapshot / name) != recorded[name]
        ]
        problems += [f"快照 {name}" for name in changed]
        if problems:
            raise SupplementMismatch(f"{MISMATCH}: {', '.join(problems)}")
        return manifest

    def client_profile(self, prefix: str) -> ClientProfile | None:
        """补造的客户画像 (client_profile.json); 没有时为 None. id 须以 prefix (快照组织 ID 推出的
        "CLIENT-XERO-{短 ID}-") 开头, 免得与快照画像或别的客户同 ID 同版本而内容不同. 调用前须已 verify."""
        path = self.root / "client_profile.json"
        if not path.is_file():
            return None
        try:
            profile = ClientProfile.model_validate_json(path.read_bytes())
        except ValidationError as exc:  # 不带出校验器的原文 (含文件里的值)
            raise SupplementMismatch(f"{MISMATCH}: client_profile.json 不合模型") from exc
        if not profile.id.startswith(prefix):
            raise SupplementMismatch(f"{MISMATCH}: client_profile.json 的 id 应以 {prefix} 开头")
        return profile

    def planning_presets(self) -> tuple[list[AuditObjective], list[RiskItem]] | None:
        """补造的计划预置 (planning/ 下的 audit_objectives.json 与 risk_register.json); 没有 planning/ 时为 None.
        调用前须已 verify."""
        directory = self.root / "planning"
        if not directory.is_dir():
            return None
        try:
            objectives = json.loads((directory / "audit_objectives.json").read_text(encoding="utf-8"))
            risks = json.loads((directory / "risk_register.json").read_text(encoding="utf-8"))
            return (
                [AuditObjective.model_validate(r) for r in objectives],
                [RiskItem.model_validate(r) for r in risks],
            )
        except (OSError, ValueError, TypeError) as exc:  # 缺文件或不合模型; 不带出文件内容
            raise SupplementMismatch(f"{MISMATCH}: planning/ 缺文件或不合模型") from exc

    def dataset_traits(self) -> DatasetTraits | None:
        """补造的数据集声明 (dataset_traits.json, 需求 R96); 没有时为 None. 不合模型 (含未知键) 或 dataset 与数据集名
        不符抛 SupplementMismatch (ValueError 子类), 不带出文件内容. 调用前须已 verify."""
        path = self.root / "dataset_traits.json"
        if not path.is_file():
            return None
        try:
            traits = DatasetTraits.model_validate_json(path.read_bytes())
        except ValidationError as exc:
            raise SupplementMismatch(f"{MISMATCH}: dataset_traits.json 不合模型") from exc
        if traits.dataset != self.dataset:
            raise SupplementMismatch(f"{MISMATCH}: dataset_traits.json 的 dataset 应为 {self.dataset}")
        return traits

    def employees(self) -> list[Employee]:
        """数据集声明的工资册 (payroll_register, 相对补造目录), 按文件中的顺序, 供演示数据导入写入 (需求 R96). 声明里
        没有工资册或文件不存在抛 FileNotFoundError; 缺列或格式不对抛 SupplementMismatch. 调用前须已 verify."""
        traits = self.dataset_traits()
        register = traits.payroll_register if traits is not None else None
        if register is None:
            raise FileNotFoundError(f"{self.dataset} 的数据集声明没有工资册 (payroll_register)")
        if not (self.root / register).resolve().is_relative_to(self.root.resolve()):
            raise SupplementMismatch(f"{MISMATCH}: dataset_traits.json 的 payroll_register 不在补造目录内")
        if not (self.root / register).is_file():
            raise FileNotFoundError(f"没有工资册 {register}")
        with malformed(register):
            return [
                Employee.model_validate({k: (v if v != "" else None) for k, v in row.items()})
                for row in self.rows(register)
            ]

    def demo_tables(self) -> list[DemoTable]:
        """补造覆盖的账表 (按 TableKind 顺序, 补造目录里有来源文件的才列): 合同关联并入后的整张 sales_invoices,
        开户行覆盖后的 bank_accounts, 服务完成记录 delivery_notes, 以及数据集声明了工资册时的 employees. 行同 apply
        对快照映射出的表并入的结果. 调用前须已 verify."""
        merged = self.apply(build_tables(Snapshot(self.snapshot)))
        found = [DemoTable(kind, name, list(merged[kind])) for kind, name in COVERED if (self.root / name).is_file()]
        traits = self.dataset_traits()
        if traits is not None and traits.payroll_register is not None:
            found.append(DemoTable(TableKind.employees, traits.payroll_register, list(self.employees())))
        return found

    def uploads(self) -> list[DemoUpload]:
        """manifest.documents 中由演示导入上传的文件 (登记途径为 STAGES 之一), 按阶段再按文件名排序; 登记 mock 与
        函证 mock 存档的 (ARCHIVED) 不在其中. 某项缺 path (消息含第几项) 或同一 path 出现两次, 类型不是 DocumentType
        取值, 登记途径不认识, 缺项或可靠性, 日期不合格式, 或 index.csv 的行与 manifest 不符 (没有这份文件, 或类型,
        可靠性, 文件日期, 关联对象不同) 抛 ValueError, 消息含该文件的 path. 调用前须已 verify."""
        manifest = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
        documents: dict[str, dict[str, Any]] = {}
        for n, item in enumerate(manifest.get("documents", []), start=1):
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                raise ValueError(f"manifest.documents 第 {n} 项缺 path")
            if item["path"] in documents:
                raise ValueError(f"manifest.documents 的 {item['path']} 重复")
            documents[item["path"]] = item
        for path, document in documents.items():
            if document.get("type") not in set(DocumentType):
                raise ValueError(f"manifest.documents 的 {path}: 类型 {document.get('type')} 不是 DocumentType 取值")
            if document.get("registration") not in {*STAGES, *ARCHIVED}:
                raise ValueError(f"manifest.documents 的 {path}: 登记途径 {document.get('registration')} 不认识")
        for row in self.rows("documents/index.csv"):
            path = row["path"]
            entry = documents.get(path)
            listed = (row["document_type"], row["reliability"], row["document_date"] or None, row["related_ids"])
            recorded = None
            if entry is not None:
                related = ";".join(entry.get("related_ids", []))
                recorded = (entry.get("type"), entry.get("reliability"), entry.get("document_date"), related)
            if listed != recorded:
                raise ValueError(f"documents/index.csv 的 {path} 与 manifest.documents 不符")
        found: list[DemoUpload] = []
        for path, document in documents.items():
            stage = document["registration"]
            if stage in ARCHIVED:
                continue
            try:
                found.append(
                    DemoUpload(
                        path=path,
                        file_name=PurePosixPath(path).name,
                        stage=stage,
                        document_type=document["type"],
                        reliability=document["reliability"],
                        document_date=document.get("document_date"),
                        related_ids=document.get("related_ids", []),
                        request_id=STAGE_REQUESTS.get(stage),
                        document_id=document["document_id"],
                    )
                )
            except (KeyError, ValidationError) as exc:  # 不带出校验器的原文
                raise ValueError(f"manifest.documents 的 {path} 缺项或格式不对") from exc
        return sorted(found, key=lambda u: (STAGES.index(u.stage), u.file_name))

    def contracts(self) -> list[Path]:
        """补造的合同 PDF (contracts/*.pdf), 按文件名排序; 合同编号取文件名主干 (CON-X01 等)."""
        return sorted((self.root / "contracts").glob("*.pdf"))

    def rows(self, name: str) -> list[dict[str, str]]:
        """补造 CSV 的行; 文件不存在时为空."""
        path = self.root / name
        if not path.is_file():
            return []
        with path.open(newline="", encoding="utf-8") as stream:
            return list(csv.DictReader(stream))

    def apply(self, tables: Mapping[str, Sequence[Any]]) -> dict[str, list[Any]]:
        """补造表并入财务表 (见模块说明), 返回新的各表; 其余表原样. 调用前须已 verify. 补造 CSV 缺列, 日期格式不对,
        行重复, 合同关联指向没有 PDF 的合同, 也抛 SupplementMismatch, 消息只列文件名与编号, 不带出文件内容."""
        invoices: list[SalesInvoice] = list(tables["sales_invoices"])
        counts = Counter(i.invoice_no for i in invoices)
        ids = {i.invoice_no: i.id for i in invoices}

        def invoice_id(name: str, number: str) -> str:
            if counts[number] != 1:
                raise SupplementMismatch(f"{MISMATCH}: {name} 的发票号 {number} 在快照中找不到或不唯一")
            return ids[number]

        contracts = {p.stem for p in self.contracts()}
        links: dict[str, str] = {}
        with malformed("contract_links.csv"):
            for row in self.rows("contract_links.csv"):
                number, contract = row["invoice_no"], row["contract_id"]
                if invoice_id("contract_links.csv", number) in links:
                    raise SupplementMismatch(f"{MISMATCH}: contract_links.csv 的发票号 {number} 重复")
                if contract not in contracts:
                    raise SupplementMismatch(f"{MISMATCH}: contract_links.csv 的合同 {contract} 没有 PDF")
                links[ids[number]] = contract
        notes: list[DeliveryNote] = []
        with malformed("delivery_notes.csv"):
            for row in self.rows("delivery_notes.csv"):
                notes.append(
                    DeliveryNote(
                        id=row["id"],
                        delivery_no=row["delivery_no"],
                        invoice_id=invoice_id("delivery_notes.csv", row["invoice_no"]),
                        shipped_date=iso_date(row["shipped_date"]),
                        signed_date=iso_date(row["signed_date"]),
                        signed_by=row["signed_by"] or None,
                    )
                )
        repeated = [key for key, n in Counter(x.id for x in notes).items() if n > 1]
        repeated += [key for key, n in Counter(x.invoice_id for x in notes).items() if n > 1]
        if repeated:
            raise SupplementMismatch(f"{MISMATCH}: delivery_notes.csv 的 {', '.join(repeated)} 重复")
        banks: dict[str, BankAccount] = {a.id: a for a in tables["bank_accounts"]}
        names: dict[str, str] = {}
        with malformed("bank_accounts.csv"):
            for row in self.rows("bank_accounts.csv"):
                account = banks.get(row["id"])
                if row["id"] in names:
                    raise SupplementMismatch(f"{MISMATCH}: bank_accounts.csv 的 {row['id']} 重复")
                if account is None or (row["bsb"], row["account_no"], row["gl_account_code"]) != (
                    account.bsb,
                    account.account_no,
                    account.gl_account_code,
                ):
                    raise SupplementMismatch(f"{MISMATCH}: bank_accounts.csv 的 {row['id']} 与快照的银行账户不符")
                names[row["id"]] = row["bank_name"]
        return {
            **{name: list(rows) for name, rows in tables.items()},
            "sales_invoices": [i.model_copy(update={"contract_id": links.get(i.id, i.contract_id)}) for i in invoices],
            "delivery_notes": notes if notes else list(tables["delivery_notes"]),
            "bank_accounts": [
                a.model_copy(update={"bank_name": names[a.id]}) if a.id in names else a for a in banks.values()
            ],
        }


def iso_date(value: str) -> date | None:
    return date.fromisoformat(value) if value else None


@contextmanager
def malformed(name: str) -> Iterator[None]:
    """补造 CSV 的缺列与格式错误 (KeyError, 日期或模型校验失败) 改抛 SupplementMismatch, 只写文件名;
    已是 SupplementMismatch 的照原样抛出."""
    try:
        yield
    except SupplementMismatch:
        raise
    except (KeyError, ValueError, TypeError) as exc:
        raise SupplementMismatch(f"{MISMATCH}: {name} 缺列或格式不对") from exc


def hashes(items: Any) -> dict[str, Any]:
    """manifest 的 files 段 → 路径: sha256; 结构不对的项按缺失处理 (由 verify 报出文件名)."""
    if not isinstance(items, list):
        return {}
    return {str(item["path"]): item.get("sha256") for item in items if isinstance(item, dict) and "path" in item}
