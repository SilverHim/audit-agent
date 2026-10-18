"""Xero 演示数据集的补造目录 (data/xero_demo/<快照目录名>/, 由 data/generate_xero_demo.py 生成): 读 manifest, 核对
补造文件与快照, 读客户画像与计划预置. 格式见 docs/Xero演示数据集设计.md 第 9.2, 9.5 节.

补造目录与快照目录同名. 读任何补造文件之前先 verify:
- manifest 的 dataset 与 snapshot.name 都等于数据集名;
- manifest.files 的每个文件都在, sha256 相符, 目录里没有清单外的文件 (manifest.json 除外);
- 快照目录的文件去掉 contacts.json (测试副本与派生快照里它是过滤版, 不记哈希) 恰为 snapshot.files, sha256 相符;
- 测试副本 (manifest 带 injected) 只在 allow_injected 下放行.
不符抛 SupplementMismatch, 消息只列文件名, 不带文件内容.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from backend.contracts.models import AuditObjective, ClientProfile, RiskItem

MISMATCH = "补造文件与快照不匹配"
UNHASHED = frozenset({"contacts.json"})  # 快照中不记哈希的文件


class SupplementMismatch(ValueError):
    """补造文件与快照不匹配, 或测试副本未经允许载入. 建项目接口按 INVALID_VALUE 返回."""


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


def hashes(items: Any) -> dict[str, Any]:
    """manifest 的 files 段 → 路径: sha256; 结构不对的项按缺失处理 (由 verify 报出文件名)."""
    if not isinstance(items, list):
        return {}
    return {str(item["path"]): item.get("sha256") for item in items if isinstance(item, dict) and "path" in item}
