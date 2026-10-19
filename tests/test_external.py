"""需求 R114: 外部数据定位, 缺失时跳过与真实大模型配置 (tests/external.py). 全部离线: 假仓库, 假快照与假 .env 都在
tmp_path 里造; 只有 "本仓库实测" 两条读仓库自带的快照 (只读)."""

import hashlib
import json
import logging
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import tests.external as ext
from tests import test_e2e_live, test_llm_live, test_sheet_intake
from tests.external import (
    MISMATCH,
    NO_LLM,
    NOT_FOUND,
    external_dir,
    live_llm_settings,
    llm_env_files,
    main_checkout,
    require_external,
    require_xero_snapshot,
    require_xero_supplements,
)
from tests.fakes import XERO_SNAPSHOT, xero_snapshot

FAKE_KEY = "sk-test-0123456789"  # 假密钥: 断言它不出现在跳过原因, 异常文本与日志里


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """不受外面的环境影响: 去掉外部数据与大模型的环境变量, 以及 git 钩子里可能带的 GIT_* (会把 git 指到别的仓库)."""
    for key in ("EXTERNAL_DATA_ROOT", "EXTERNAL_STRICT", "LLM_ENV_FILE", "LLM_MODEL", "LLM_API_KEY", "LLM_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    for key in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(key)


def git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def main_and_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """主检出 main (一个提交) 与它的 worktree wt; 外部数据只放在主检出 (不入库)."""
    main = tmp_path / "main"
    main.mkdir()
    git(main, "init", "-q")
    (main / "README.md").write_text("x", encoding="utf-8")
    git(main, "add", "README.md")
    git(main, "commit", "-q", "-m", "init")
    git(main, "worktree", "add", "-q", str(tmp_path / "wt"))
    return main, tmp_path / "wt"


def outcome(call: Any, *args: Any) -> tuple[str, Any]:
    """调 call: 返回 ("ok", 返回值), ("skip", 原因) 或 ("fail", 原因). 跳过与失败在这里接住, 不让意外的跳过把
    用例变成 "已跳过" 而漏掉错误."""
    try:
        return "ok", call(*args)
    except pytest.skip.Exception as e:
        return "skip", str(e)
    except pytest.fail.Exception as e:
        return "fail", str(e)


def ok(call: Any, *args: Any) -> Any:
    kind, value = outcome(call, *args)
    assert kind == "ok", (kind, value)
    return value


def skip_reason(call: Any, *args: Any) -> str:
    kind, value = outcome(call, *args)
    assert kind == "skip", (kind, value)
    return str(value)


def fail_reason(call: Any, *args: Any) -> str:
    kind, value = outcome(call, *args)
    assert kind == "fail", (kind, value)
    return str(value)


# ── 验收 1: external_dir 的查找顺序 ──


def test_external_dir_prefers_environment_then_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """EXTERNAL_DATA_ROOT/<name> 在时取它, 不在时取仓库的 data/external/<name>; 都没有为 None."""
    repo = tmp_path / "repo"
    (repo / "data" / "external" / "cuad").mkdir(parents=True)
    assert external_dir("cuad", repo) == repo / "data" / "external" / "cuad"
    assert external_dir("xero", repo) is None
    (tmp_path / "elsewhere" / "cuad").mkdir(parents=True)
    monkeypatch.setenv("EXTERNAL_DATA_ROOT", str(tmp_path / "elsewhere"))
    assert external_dir("cuad", repo) == tmp_path / "elsewhere" / "cuad"
    monkeypatch.setenv("EXTERNAL_DATA_ROOT", str(tmp_path / "nothing"))  # 环境变量指的目录下没有这个数据集
    assert external_dir("cuad", repo) == repo / "data" / "external" / "cuad"


def test_external_lookup_goes_by_the_wanted_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """按要找的路径逐个来源查 (第 1 轮审核 R114-03): EXTERNAL_DATA_ROOT 下有 xero/ 与 cuad/ 却没有所要的快照与文件时,
    取仓库里有的那份, 不被整个目录挡住; 环境变量的来源也有时取它."""
    repo = tmp_path / "repo"
    snapshot = fake_pair(repo, "S5")
    (repo / "data" / "external" / "cuad").mkdir(parents=True)
    (repo / "data" / "external" / "cuad" / "master_clauses.csv").write_text("x", encoding="utf-8")
    shared = tmp_path / "shared"
    (shared / "xero" / "api" / "other").mkdir(parents=True)
    (shared / "cuad").mkdir(parents=True)
    monkeypatch.setenv("EXTERNAL_DATA_ROOT", str(shared))
    assert ok(require_xero_snapshot, "S5", repo) == snapshot
    assert ok(require_external, "cuad", ["master_clauses.csv"], repo) == repo / "data" / "external" / "cuad"
    assert external_dir("cuad", repo) == shared / "cuad"  # 不指定路径时取第一个存在的
    (shared / "cuad" / "master_clauses.csv").write_text("y", encoding="utf-8")
    assert ok(require_external, "cuad", ["master_clauses.csv"], repo) == shared / "cuad"


def test_external_dir_falls_back_to_main_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """worktree 里没有外部数据时取主工作副本的 (git worktree list --porcelain 的第一项); worktree 自己有时取自己的;
    git 不可用时略过主工作副本这一项."""
    main, wt = main_and_worktree(tmp_path)
    (main / "data" / "external" / "cuad").mkdir(parents=True)
    assert external_dir("cuad", wt) == main / "data" / "external" / "cuad"
    (wt / "data" / "external" / "cuad").mkdir(parents=True)
    assert external_dir("cuad", wt) == wt / "data" / "external" / "cuad"
    (wt / "data" / "external" / "cuad").rmdir()

    def no_git(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError("git")

    monkeypatch.setattr(ext.subprocess, "run", no_git)
    assert external_dir("cuad", wt) is None


def test_no_main_checkout_outside_own_repository_or_for_bare(tmp_path: Path) -> None:
    """不往项目外找 (第 1 轮审核 R114-02): 代码目录本身不是 git 仓库, 只是放在别的仓库里时没有主工作副本, 不取外层
    仓库的 data/external 与 .env; bare 仓库加 worktree 的布局也没有 (不取仓库目录的上级); 普通 worktree 取主检出."""
    outer = tmp_path / "other-project"
    outer.mkdir()
    git(outer, "init", "-q")
    (outer / "data" / "external" / "cuad").mkdir(parents=True)
    (outer / ".env").write_text(f"LLM_MODEL=foreign\nLLM_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    inner = outer / "downloads" / "audit-agent"
    inner.mkdir(parents=True)
    assert main_checkout(inner) is None and external_dir("cuad", inner) is None
    assert [label for label, _ in llm_env_files(inner)] == ["仓库 .env"]
    assert NO_LLM in skip_reason(live_llm_settings, llm_env_files(inner))

    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    (seed / "README.md").write_text("x", encoding="utf-8")
    git(seed, "add", "README.md")
    git(seed, "commit", "-q", "-m", "init")
    bare = tmp_path / "repos" / "audit.git"
    git(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))
    git(bare, "worktree", "add", "-q", str(tmp_path / "repos" / "wt"), "main")
    (tmp_path / "repos" / "data" / "external" / "cuad").mkdir(parents=True)  # 仓库目录的上级
    assert main_checkout(tmp_path / "repos" / "wt") is None
    assert external_dir("cuad", tmp_path / "repos" / "wt") is None

    (tmp_path / "plain").mkdir()
    main, wt = main_and_worktree(tmp_path / "plain")
    assert main_checkout(wt) == main and main_checkout(main) == main


# ── 验收 2: require_external ──


def test_require_external_skips_with_what_is_missing(tmp_path: Path) -> None:
    """缺目录或缺文件时跳过, 原因含 data/external/<name> 与缺的文件名; 都在时返回目录."""
    repo = tmp_path / "repo"
    assert "data/external/cuad" in skip_reason(require_external, "cuad", ["master_clauses.csv"], repo)
    root = repo / "data" / "external" / "cuad"
    root.mkdir(parents=True)
    (root / "README.md").write_text("x", encoding="utf-8")
    reason = skip_reason(require_external, "cuad", ["README.md", "master_clauses.csv"], repo)
    assert "data/external/cuad" in reason and "master_clauses.csv" in reason and "README.md" not in reason
    assert ok(require_external, "cuad", ["README.md"], repo) == root


# ── 验收 3: 快照与补造的哈希核对 ──


def fake_pair(repo: Path, name: str = "S1") -> Path:
    """假快照 data/external/xero/api/<name> (含 contacts.json) 与补造 data/xero_demo/<name>/manifest.json, 后者的
    快照哈希表列 organisation.json 与 accounts.json (不列 contacts.json); 返回快照目录."""
    snapshot = repo / "data" / "external" / "xero" / "api" / name
    snapshot.mkdir(parents=True)
    files = {"organisation.json": b'{"Organisations": []}', "accounts.json": b'{"Accounts": []}'}
    for rel, data in files.items():
        (snapshot / rel).write_bytes(data)
    (snapshot / "contacts.json").write_bytes(b"{}")
    table = [{"path": rel, "sha256": hashlib.sha256(data).hexdigest()} for rel, data in files.items()]
    supplements = repo / "data" / "xero_demo" / name
    supplements.mkdir(parents=True)
    manifest = {"snapshot": {"name": f"xero/{name}", "files": table}}
    (supplements / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return snapshot


def test_snapshot_hashes_checked_against_supplements(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """快照与补造都在且哈希相符时返回各自目录, 不打开 contacts.json; 改快照一个字节即跳过 ("快照与补造不匹配,
    预期值失效"), 设 EXTERNAL_STRICT=1 时改为失败."""
    repo = tmp_path / "repo"
    snapshot = fake_pair(repo)
    opened: list[str] = []
    real = ext.sha256
    monkeypatch.setattr(ext, "sha256", lambda path: opened.append(path.name) or real(path))
    assert ok(require_xero_snapshot, "S1", repo) == snapshot
    assert ok(require_xero_supplements, "S1", repo) == repo / "data" / "xero_demo" / "S1"
    assert "contacts.json" not in opened and sorted(set(opened)) == ["accounts.json", "organisation.json"]

    (snapshot / "accounts.json").write_bytes(b'{"Accounts": [ ]}')
    for check in (require_xero_snapshot, require_xero_supplements):
        reason = skip_reason(check, "S1", repo)
        assert MISMATCH in reason and "accounts.json" in reason
    monkeypatch.setenv("EXTERNAL_STRICT", "1")
    assert MISMATCH in fail_reason(require_xero_snapshot, "S1", repo)


def test_snapshot_without_supplements_is_not_checked(tmp_path: Path) -> None:
    """没有同名补造目录 (如 20261017-mock) 时不核对, 照常返回; 两样都没有时跳过."""
    repo = tmp_path / "repo"
    snapshot = fake_pair(repo, "S2")
    (repo / "data" / "xero_demo" / "S2" / "manifest.json").unlink()
    (snapshot / "accounts.json").write_bytes(b"changed")
    assert ok(require_xero_snapshot, "S2", repo) == snapshot
    assert "data/external/xero/api/S3" in skip_reason(require_xero_snapshot, "S3", repo)
    assert "data/xero_demo/S3" in skip_reason(require_xero_supplements, "S3", repo)


# ── 验收 4: 仓库跟踪了却找不到时失败 ──


def test_tracked_snapshot_that_is_missing_fails(tmp_path: Path) -> None:
    """git ls-files 列出了快照文件, 工作区里却没有: 失败 ("仓库跟踪了快照却找不到"), 不静默跳过."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    snapshot = repo / "data" / "external" / "xero" / "api" / "S4"
    snapshot.mkdir(parents=True)
    (snapshot / "journals.json").write_text("{}", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "snapshot")
    # 跟踪的文件都在, 却没有 organisation.json (快照不完整): 同样失败, 写明缺 organisation.json
    assert "S4/organisation.json" in fail_reason(require_xero_snapshot, "S4", repo)
    (snapshot / "journals.json").unlink()
    assert "仓库跟踪了快照却找不到" in fail_reason(require_xero_snapshot, "S4", repo)


def test_partly_missing_tracked_snapshot_fails(tmp_path: Path) -> None:
    """仓库跟踪了快照, 工作区只少了一个文件 (organisation.json 还在) 时失败, 不落到 "快照与补造不匹配" 的跳过
    (第 1 轮审核 R114-01); 没有同名补造时同样失败. 有补造时只取补造目录的同样失败 (R294: 补造一侧核对快照哈希前
    也先看快照跟踪的文件)."""
    for name, with_supplements in (("S6", True), ("S7", False)):
        repo = tmp_path / name
        repo.mkdir()
        git(repo, "init", "-q")
        snapshot = fake_pair(repo, name)
        (snapshot / "journals.json").write_text("{}", encoding="utf-8")
        if not with_supplements:
            (repo / "data" / "xero_demo" / name / "manifest.json").unlink()
        git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", "snapshot")
        (snapshot / "journals.json").unlink()
        reason = fail_reason(require_xero_snapshot, name, repo)
        assert NOT_FOUND in reason and f"data/external/xero/api/{name}/journals.json" in reason
        if with_supplements:
            reason = fail_reason(require_xero_supplements, name, repo)
            assert NOT_FOUND in reason and f"data/external/xero/api/{name}/journals.json" in reason


def test_tracked_supplements_that_are_missing_fail(tmp_path: Path) -> None:
    """补造目录被跟踪却找不到时失败, 原因 "仓库跟踪了补造目录却找不到" (第 1 轮审核 R114-05)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    fake_pair(repo, "S8")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "supplements")
    (repo / "data" / "xero_demo" / "S8" / "manifest.json").unlink()
    reason = fail_reason(require_xero_supplements, "S8", repo)
    assert "仓库跟踪了补造目录却找不到" in reason and "data/xero_demo/S8/manifest.json" in reason


def test_tracked_raw_exports_must_all_be_present(tmp_path: Path) -> None:
    """Xero 网页导出 (R294): 跟踪了的都在时返回目录; 缺一个即失败 ("仓库跟踪了 Xero 网页导出却找不到", 写明文件);
    没跟踪又没有时跳过."""
    repo = tmp_path / "repo"
    raw = repo / "data" / "external" / "xero" / "raw"
    raw.mkdir(parents=True)
    git(repo, "init", "-q")
    for name in ("a.csv", "b.xlsx"):
        (raw / name).write_bytes(b"x")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "raw")
    assert ok(ext.require_xero_raw, repo) == raw
    (raw / "b.xlsx").unlink()
    reason = fail_reason(ext.require_xero_raw, repo)
    assert "仓库跟踪了 Xero 网页导出却找不到" in reason and "data/external/xero/raw/b.xlsx" in reason
    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init", "-q")
    assert "没有 Xero 网页导出" in skip_reason(ext.require_xero_raw, empty)


def test_optional_takes_only_missing_data_as_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """optional 只把 "没带数据" 换成 None: 快照与补造哈希不符照常跳过 (EXTERNAL_STRICT=1 时失败), 不当成没带数据而
    少断言; hashes=False 不核对哈希, 照常返回 (校验数据本身的用例用)."""
    repo = tmp_path / "repo"
    snapshot = fake_pair(repo)
    supplements = repo / "data" / "xero_demo" / "S1"
    assert ext.optional(require_xero_supplements, "S1", repo) == supplements
    assert ext.optional(require_xero_supplements, "S9", repo) is None  # 没带
    (snapshot / "accounts.json").write_bytes(b"changed")
    for check in (require_xero_snapshot, require_xero_supplements):
        assert MISMATCH in skip_reason(ext.optional, check, "S1", repo)
        assert ok(check, "S1", repo, False) in (snapshot, supplements)
    monkeypatch.setenv("EXTERNAL_STRICT", "1")
    assert MISMATCH in fail_reason(ext.optional, require_xero_supplements, "S1", repo)


def test_session_cache_is_keyed_by_location_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """会话缓存的键带上 EXTERNAL_DATA_ROOT 等: 两个来源各放一份同名快照 (名字不在仓库里), 先后在两种环境下取, 各得
    各的位置, 与先后顺序无关 (R294). 不读仓库的数据."""
    roots = [tmp_path / "a", tmp_path / "b"]
    for root in roots:
        (root / "xero" / "api" / "S-cache").mkdir(parents=True)
        (root / "xero" / "api" / "S-cache" / "organisation.json").write_text("{}", encoding="utf-8")
    for order in (roots, roots[::-1]):
        found = []
        for root in order:
            monkeypatch.setenv("EXTERNAL_DATA_ROOT", str(root))
            found.append(ok(ext.snapshot_dir, "S-cache"))
        assert found == [root / "xero" / "api" / "S-cache" for root in order]


@pytest.mark.external
def test_repository_snapshots_are_found_and_match() -> None:
    """本仓库实测: 跟踪了 q1 快照的 journals.json, require_xero_snapshot 返回目录且与补造的哈希表相符 (不跳过);
    补造目录同样; fakes.xero_snapshot() 取 20261017-mock (验收 7). 不是 git 检出或不带快照的检出跳过 (R114-07)."""
    listed = ext.tracked("data/external/xero/api/20261017-q1/journals.json")
    if listed != ["data/external/xero/api/20261017-q1/journals.json"]:  # 不是 git 检出, 或这个检出不带快照
        pytest.skip("本检出没有跟踪 Xero 快照 20261017-q1")
    q1 = ok(require_xero_snapshot, "20261017-q1")
    assert (q1 / "journals.json").is_file()
    assert (ok(require_xero_supplements, "20261017-q1") / "manifest.json").is_file()
    assert ok(xero_snapshot).root == ok(require_xero_snapshot, XERO_SNAPSHOT)
    assert ok(xero_snapshot).root.name == "20261017-mock"


# ── 验收 5: 真实大模型的配置 ──


def test_live_settings_from_environment_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """只设环境变量, 没有 .env: 取到配置 (LLM_MODE live, LLM_BASE_URL 可不设), 进程环境里的 mock 强制不变."""
    monkeypatch.setenv("LLM_MODEL", "probe-model")
    monkeypatch.setenv("LLM_API_KEY", FAKE_KEY)
    settings = ok(live_llm_settings, [("仓库 .env", tmp_path / "missing.env")])
    assert (settings.LLM_MODE, settings.LLM_MODEL, settings.LLM_BASE_URL) == ("live", "probe-model", "")
    assert settings.LLM_API_KEY == FAKE_KEY
    assert os.environ["LLM_MODE"] == "mock"


def test_live_settings_from_first_env_file_with_both_keys(tmp_path: Path) -> None:
    """环境变量不全时依次读 .env: 只有模型名的跳过, 取第一个两项都有的, 连同同一文件的 LLM_BASE_URL."""
    partial, full = tmp_path / "partial.env", tmp_path / "full.env"
    partial.write_text("LLM_MODEL=only-model\n", encoding="utf-8")
    full.write_text(f"LLM_MODEL=file-model\nLLM_API_KEY={FAKE_KEY}\nLLM_BASE_URL=https://llm.example/v1\n", "utf-8")
    settings = ok(live_llm_settings, [("a", tmp_path / "missing.env"), ("b", partial), ("c", full)])
    assert (settings.LLM_MODE, settings.LLM_MODEL, settings.LLM_BASE_URL) == (
        "live",
        "file-model",
        "https://llm.example/v1",
    )


def test_live_settings_skip_without_leaking_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """环境变量只有密钥, .env 里只有模型名: 来源之间不合并, 跳过 (第 1 轮审核 R114-06); 原因为 "未配置 LLM_MODEL 与
    LLM_API_KEY" 并点名只配了一部分的来源 (只写键名), 原因与日志都不含密钥."""
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("LLM_API_KEY", FAKE_KEY)
    env = tmp_path / ".env"
    env.write_text("LLM_MODEL=file-model\n", encoding="utf-8")
    reason = skip_reason(live_llm_settings, [("仓库 .env", env)])
    assert reason == f"{NO_LLM}; 环境变量只配了 LLM_API_KEY; 仓库 .env 只配了 LLM_MODEL"
    assert FAKE_KEY not in reason and "file-model" not in reason and FAKE_KEY not in caplog.text


def test_live_settings_skip_with_only_the_model_in_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量只设了 LLM_MODEL, 没有 .env 或 .env 里缺项: 跳过, 不带空密钥去跑 (第 1 轮审核 R114-05); 显式指定的
    LLM_ENV_FILE 不存在时也写明."""
    monkeypatch.setenv("LLM_MODEL", "probe-model")
    assert skip_reason(live_llm_settings, []) == f"{NO_LLM}; 环境变量只配了 LLM_MODEL"
    env = tmp_path / ".env"
    env.write_text("LLM_BASE_URL=https://llm.example/v1\n", encoding="utf-8")
    assert skip_reason(live_llm_settings, [("仓库 .env", env)]) == f"{NO_LLM}; 环境变量只配了 LLM_MODEL"
    missing = [("LLM_ENV_FILE", tmp_path / "nowhere.env")]
    assert skip_reason(live_llm_settings, missing) == f"{NO_LLM}; LLM_ENV_FILE 指的文件不存在; 环境变量只配了 LLM_MODEL"


def test_llm_env_files_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """配置文件只有 LLM_ENV_FILE (显式指定) 与本仓库的 .env, 不读主工作副本的 .env (第 1 轮审核 R114-02): worktree
    里只在主检出放了 .env 时跳过."""
    main, wt = main_and_worktree(tmp_path)
    monkeypatch.setenv("LLM_ENV_FILE", str(tmp_path / "chosen.env"))
    assert llm_env_files(wt) == [("LLM_ENV_FILE", tmp_path / "chosen.env"), ("仓库 .env", wt / ".env")]
    monkeypatch.delenv("LLM_ENV_FILE")
    assert llm_env_files(wt) == [("仓库 .env", wt / ".env")]
    (main / ".env").write_text(f"LLM_MODEL=main-model\nLLM_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    assert skip_reason(live_llm_settings, llm_env_files(wt)) == NO_LLM


# ── 验收 6: live 须同时 slow (另须写上限, 需求 R115), 标记已登记 ──


def marked(name: str, *markers: str, fixtures: tuple[str, ...] = ("live_budget",), **kwargs: Any) -> Any:
    """假的收集项: 带 markers 这几个标记, 标记参数都是 kwargs, 取了 fixtures 这几个夹具."""
    found = {m: SimpleNamespace(kwargs=kwargs) for m in markers}
    return SimpleNamespace(nodeid=name, get_closest_marker=found.get, fixturenames=list(fixtures))


@pytest.mark.parametrize(
    "bad",
    [
        None,
        marked("t::live_only", "live", max_calls=1),  # 没标 slow: 默认测试会联网
        marked("t::no_limit", "live", "slow"),  # 没写 max_calls: 跑起来没有预算
        marked("t::no_fixture", "live", "slow", fixtures=(), max_calls=1),  # 没取 live_budget: 上限不起作用
    ],
)
def test_live_markers_are_checked_at_collection(bad: Any) -> None:
    """标了 live 的测试须同时标 slow, 写 max_calls 并取 live_budget 夹具, 否则收集阶段报错 (报出违规的测试); 合规的
    live 测试, 只标 slow 的与不带标记的照常."""
    from tests.conftest import pytest_collection_modifyitems

    items = [marked("t::both", "live", "slow", max_calls=3), marked("t::slow", "slow"), marked("t::bare")]
    if bad is None:
        pytest_collection_modifyitems(None, items)
        return
    with pytest.raises(pytest.UsageError, match=bad.nodeid):
        pytest_collection_modifyitems(None, [*items, bad])


def test_markers_registered(pytestconfig: pytest.Config) -> None:
    names = {line.split(":")[0] for line in pytestconfig.getini("markers")}
    assert {"slow", "external", "live"} <= names


# ── 验收 8: 联网用例的标记 ──


def marks(module: Any, func: Any) -> set[str]:
    own = getattr(module, "pytestmark", [])
    return {m.name for m in [*(own if isinstance(own, list) else [own]), *getattr(func, "pytestmark", [])]}


@pytest.mark.parametrize(
    ("module", "name"),
    [
        (test_e2e_live, "test_live_llm_full_flow"),
        (test_llm_live, "test_live_doc06_extracts_ba01_reply"),
        (test_sheet_intake, "test_live_recognizes_samples"),
    ],
)
def test_live_tests_are_marked_slow_and_live(module: Any, name: str) -> None:
    assert {"slow", "live"} <= marks(module, getattr(module, name))
