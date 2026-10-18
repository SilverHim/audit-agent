"""Xero 演示数据集的补造目录 (R70): 载入与取数前核对 manifest 与快照, 补造画像与计划预置, 测试副本与已载入项目.

用本机的 Xero 快照与入库的补造目录 data/xero_demo/; 没有快照时跳过. 篡改只在 tmp 副本上做, 不改原目录. 不连库.
"""

import json
import shutil
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from backend.adapters.xero_supplements import SupplementMismatch
from backend.contracts.models import ClientProfile
from backend.persistence import stores

REPO = Path(__file__).resolve().parents[1]
SNAPSHOTS = REPO / "data" / "external" / "xero" / "api"
SUPPLEMENTS = REPO / "data" / "xero_demo"
PLANNING = REPO / "src" / "backend" / "knowledge" / "planning"
DATASET = "xero/20261017-q1"
NO_DB: Any = object()  # 本文件不连库
pytestmark = pytest.mark.skipif(not (SNAPSHOTS / "20261017-q1").is_dir(), reason="没有本地 Xero 快照")


def loader(api: Path = SNAPSHOTS, supplements: Path | None = SUPPLEMENTS, **options: Any) -> stores.XeroDatasetLoader:
    return stores.XeroDatasetLoader(api, PLANNING, NO_DB, NO_DB, supplements=supplements, **options)


def copies(tmp_path: Path) -> tuple[Path, Path]:
    """快照与补造目录复制到 tmp, 返回两者的上级目录 (api, xero_demo)."""
    api, demo = tmp_path / "api", tmp_path / "xero_demo"
    shutil.copytree(SNAPSHOTS / "20261017-q1", api / "20261017-q1")
    shutil.copytree(SUPPLEMENTS / "20261017-q1", demo / "20261017-q1")
    return api, demo


def rewrite_manifest(api: Path, demo: Path, injected: str | None = None) -> None:
    """改过补造文件后按副本重写 manifest (同生成脚本), 用来单独测试哈希之外的核对."""
    sys.path.insert(0, str(REPO / "data"))
    try:
        import generate_xero_demo
    finally:
        sys.path.remove(str(REPO / "data"))
    generate_xero_demo.write_manifest(demo / "20261017-q1", api / "20261017-q1", injected=injected)


def mismatch(target: stores.XeroDatasetLoader) -> str:
    with pytest.raises(SupplementMismatch) as raised:
        target.client_profile(DATASET)
    return str(raised.value)


def test_profile_and_planning_from_supplements() -> None:
    profile = loader().client_profile(DATASET)
    supplied = ClientProfile.model_validate_json((SUPPLEMENTS / "20261017-q1" / "client_profile.json").read_bytes())
    assert profile == supplied
    assert (profile.id, profile.version, profile.legal_name, profile.abn) == (
        "CLIENT-XERO-3C1332BB-Q1",
        1,
        "Demo Company (AU) Pty Ltd",
        "25900043021",
    )
    objectives, risks = loader().planning_presets(DATASET)
    assert len(objectives) == 3 and len(risks) == 3
    occurrence = next(r for r in risks if r.id == "RISK-REV-OCC")
    assert occurrence.description.startswith("Presumed fraud risk (ASA 240 para 27). Revenue of AUD 32,431")


def test_without_supplements_falls_back_to_snapshot() -> None:
    """supplements 为空, 或补造目录下没有该快照 (如 20261017-mock) 时照现状: 快照画像与通用模板."""
    generic = stores.read_planning(PLANNING)
    for target, dataset in ((loader(supplements=None), DATASET), (loader(), "xero/20261017-mock")):
        if not (SNAPSHOTS / dataset.removeprefix("xero/")).is_dir():
            continue
        profile = target.client_profile(dataset)
        assert (profile.id, profile.version) == ("CLIENT-XERO-3C1332BB", 1)
        assert target.planning_presets(dataset) == generic


def test_tampered_files_are_rejected(tmp_path: Path) -> None:
    api, demo = copies(tmp_path)
    target = loader(api, demo)
    assert target.client_profile(DATASET).id == "CLIENT-XERO-3C1332BB-Q1"
    profile = demo / "20261017-q1" / "client_profile.json"
    original = profile.read_bytes()
    profile.write_bytes(original + b" ")
    assert mismatch(target) == "补造文件与快照不匹配: client_profile.json"
    profile.write_bytes(original)
    journals = api / "20261017-q1" / "journals.json"
    journals.write_bytes(journals.read_bytes() + b" ")
    assert mismatch(target) == "补造文件与快照不匹配: 快照 journals.json"
    with pytest.raises(SupplementMismatch, match=r"快照 journals\.json"):
        target.planning_presets(DATASET)
    shutil.copy(SNAPSHOTS / "20261017-q1" / "journals.json", journals)
    (demo / "20261017-q1" / "notes.txt").write_text("x", encoding="utf-8")
    assert mismatch(target) == "补造文件与快照不匹配: notes.txt"
    (demo / "20261017-q1" / "notes.txt").unlink()
    (api / "20261017-q1" / "contacts.json").write_bytes(b"[]")  # 不在清单, 只改它照常
    assert target.client_profile(DATASET).id == "CLIENT-XERO-3C1332BB-Q1"
    (demo / "20261017-q1" / "manual" / "rpt07_reply.json").unlink()
    (api / "20261017-q1" / "payments.json").unlink()
    assert mismatch(target) == "补造文件与快照不匹配: manual/rpt07_reply.json, 快照 payments.json"


def test_manifest_must_name_this_dataset(tmp_path: Path) -> None:
    api, demo = copies(tmp_path)
    manifest = demo / "20261017-q1" / "manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    manifest.write_text(json.dumps({**data, "dataset": "xero/20261017-mock"}), encoding="utf-8")
    assert mismatch(loader(api, demo)) == "补造文件与快照不匹配: manifest.json"
    manifest.write_text("{", encoding="utf-8")
    assert mismatch(loader(api, demo)) == "补造文件与快照不匹配: manifest.json"


def test_injected_copy_needs_allow_injected(tmp_path: Path) -> None:
    api, demo = copies(tmp_path)
    rewrite_manifest(api, demo, injected="T1")
    assert mismatch(loader(api, demo)) == "测试副本 (injected=T1) 只能在 allow_injected 下载入"
    assert loader(api, demo, allow_injected=True).client_profile(DATASET).id == "CLIENT-XERO-3C1332BB-Q1"


@pytest.mark.parametrize("bad_id", ["CLIENT-XERO-3C1332BB", "CLIENT-001"])
def test_profile_id_must_carry_snapshot_prefix(tmp_path: Path, bad_id: str) -> None:
    """补造画像 ID 须以快照组织 ID 推出的前缀开头, 免得与快照画像 CLIENT-XERO-3C1332BB v1 同 ID 而内容不同."""
    api, demo = copies(tmp_path)
    path = demo / "20261017-q1" / "client_profile.json"
    path.write_text(json.dumps({**json.loads(path.read_text(encoding="utf-8")), "id": bad_id}), encoding="utf-8")
    rewrite_manifest(api, demo)
    message = mismatch(loader(api, demo))
    assert message.startswith("补造文件与快照不匹配") and "CLIENT-XERO-3C1332BB-" in message


@asynccontextmanager
async def no_transaction(*args: Any, **kwargs: Any) -> Any:
    yield None


async def test_load_verifies_before_building(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未载入时先核对再读快照建表; 不匹配时抛 SupplementMismatch, 不写任何表."""
    api, demo = copies(tmp_path)
    target = loader(api, demo)
    (demo / "20261017-q1" / "client_profile.json").write_bytes(b"{}")
    built: list[str] = []

    async def writable(conn: Any, eid: str) -> None:
        return None

    async def rows(eid: str, statement: Any) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(stores, "transaction", no_transaction)
    monkeypatch.setattr(stores, "writable", writable)
    monkeypatch.setattr(target, "rows", rows)
    monkeypatch.setattr(stores, "build_tables", lambda snapshot: built.append("built"))
    with pytest.raises(SupplementMismatch, match=r"client_profile\.json"):
        await target.load("E", DATASET)
    assert built == []


async def test_already_loaded_does_not_read_supplements(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """已载入的项目再 load 直接返回: 补造目录与快照被移走也不报错."""
    target = loader(tmp_path / "api", tmp_path / "xero_demo")  # 两个目录都不存在

    async def writable(conn: Any, eid: str) -> None:
        return None

    async def rows(eid: str, statement: Any) -> list[dict[str, Any]]:
        return [{"dataset": DATASET, "data_version": 1}]

    monkeypatch.setattr(stores, "transaction", no_transaction)
    monkeypatch.setattr(stores, "writable", writable)
    monkeypatch.setattr(target, "rows", rows)
    await target.load("E", DATASET)


def test_supplements_without_profile_or_planning(tmp_path: Path) -> None:
    """补造目录没有 client_profile.json 或 planning/ 时, 分别取快照画像与通用模板 (其余补造文件照常核对)."""
    api, demo = copies(tmp_path)
    (demo / "20261017-q1" / "client_profile.json").unlink()
    shutil.rmtree(demo / "20261017-q1" / "planning")
    rewrite_manifest(api, demo)
    target = loader(api, demo)
    assert target.client_profile(DATASET).id == "CLIENT-XERO-3C1332BB"
    assert target.planning_presets(DATASET) == stores.read_planning(PLANNING)


def test_tampered_organisation_is_a_mismatch(tmp_path: Path) -> None:
    """快照 organisation.json 被改坏时先报不匹配 (不报数据集不存在或 JSON 解析错误)."""
    api, demo = copies(tmp_path)
    organisation = api / "20261017-q1" / "organisation.json"
    organisation.write_text("[{}]", encoding="utf-8")  # 去掉 OrganisationID
    assert mismatch(loader(api, demo)) == "补造文件与快照不匹配: 快照 organisation.json"
    organisation.write_text("{", encoding="utf-8")
    assert mismatch(loader(api, demo)) == "补造文件与快照不匹配: 快照 organisation.json"


def test_invalid_supplied_models_do_not_echo_content(tmp_path: Path) -> None:
    """补造画像或计划哈希相符而不合模型时, 消息只列文件名, 不带出文件里的值."""
    api, demo = copies(tmp_path)
    path = demo / "20261017-q1" / "client_profile.json"
    path.write_text(json.dumps({**json.loads(path.read_text(encoding="utf-8")), "extra": "MARKER-VALUE"}))
    risks = demo / "20261017-q1" / "planning" / "risk_register.json"
    risks.write_text(json.dumps([{"id": "MARKER-VALUE"}]), encoding="utf-8")
    rewrite_manifest(api, demo)
    message = mismatch(loader(api, demo))
    assert message == "补造文件与快照不匹配: client_profile.json 不合模型" and "MARKER" not in message
    with pytest.raises(SupplementMismatch) as raised:
        loader(api, demo).planning_presets(DATASET)
    assert str(raised.value) == "补造文件与快照不匹配: planning/ 缺文件或不合模型"
