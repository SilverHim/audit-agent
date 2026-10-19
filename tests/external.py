"""外部数据与真实大模型配置的定位 (需求 R114): 依赖 data/external, data/xero_demo 的测试与真实大模型测试共用.

- external_dir: 外部数据集目录, 按要找的路径在三个来源 (EXTERNAL_DATA_ROOT, 仓库, 主工作副本) 里逐个查;
  require_external: 缺了就跳过.
- require_xero_snapshot, require_xero_supplements: Xero 快照与补造目录. 仓库跟踪了的, git ls-files 列出的每个文件都
  得在, 缺一个就失败, 不静默跳过; 两者都在时按补造 manifest 记的快照哈希逐文件核对 (contacts.json 不在表内, 不打开),
  不符时跳过 (EXTERNAL_STRICT=1 时失败).
- live_llm_settings: 真实大模型的配置, 只取环境变量, LLM_ENV_FILE (显式指定的文件) 与本仓库的 .env, 不去别的目录
  找; 来源之间不合并; 没配全就跳过. 跳过原因与报错里不带取到的值.
"""

import hashlib
import json
import os
import subprocess
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import pytest
from dotenv import dotenv_values

from backend.core.config import Settings

REPO = Path(__file__).resolve().parents[1]
LLM_KEYS = ("LLM_MODEL", "LLM_API_KEY", "LLM_BASE_URL")  # 真实大模型取的配置; LLM_BASE_URL 可选
REQUIRED = ("LLM_MODEL", "LLM_API_KEY")  # 两项须在同一来源配全
NO_LLM = "未配置 LLM_MODEL 与 LLM_API_KEY"  # 没配真实大模型时的跳过原因 (之后可另附只配了一部分的来源)
MISMATCH = "快照与补造不匹配, 预期值失效"
NOT_FOUND = "仓库跟踪了快照却找不到"


def git_lines(repo: Path, *args: str) -> list[str] | None:
    """在 repo 下跑 git, 返回输出的各行; git 不可用或 repo 不在 git 仓库里时为 None."""
    try:
        out = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.splitlines()


def main_checkout(repo: Path = REPO) -> Path | None:
    """repo 所在仓库的主工作副本: `git worktree list --porcelain` 的第一项. repo 须是列出的工作树之一 (repo 本身不是
    git 仓库, 只是放在别的仓库里时为 None); 主工作副本是 bare 仓库时为 None (不往仓库目录的上级找); git 不可用时
    为 None."""
    lines = git_lines(repo, "worktree", "list", "--porcelain")
    if not lines:
        return None
    trees: list[tuple[Path, bool]] = []  # (路径, 是否 bare)
    for line in lines:
        if line.startswith("worktree "):
            trees.append((Path(line.removeprefix("worktree ")), False))
        elif line == "bare" and trees:
            trees[-1] = (trees[-1][0], True)
    if not trees or repo.resolve() not in {path.resolve() for path, _ in trees}:
        return None
    main, bare = trees[0]
    return None if bare else main


def external_roots(name: str, repo: Path = REPO) -> list[Path]:
    """外部数据集 name 的候选目录, 按优先顺序 (不论存在与否): EXTERNAL_DATA_ROOT/<name>, 仓库的 data/external/<name>,
    主工作副本的 data/external/<name> (worktree 里没带外部数据时用; 与仓库相同时不重复)."""
    roots = [Path(os.environ["EXTERNAL_DATA_ROOT"]) / name] if os.getenv("EXTERNAL_DATA_ROOT") else []
    roots.append(repo / "data" / "external" / name)
    main = main_checkout(repo)
    if main is not None and main.resolve() != repo.resolve():
        roots.append(main / "data" / "external" / name)
    return roots


def external_dir(name: str, repo: Path = REPO, has: Iterable[str] = ()) -> Path | None:
    """外部数据集 name 的目录: external_roots 中第一个存在且含 has 里全部路径 (相对该目录) 的; 都没有为 None.
    按要找的路径逐个来源查, 一个来源有这个数据集的目录却没有所要的文件时, 不挡住后面的来源."""
    wanted = list(has)
    return next((r for r in external_roots(name, repo) if r.is_dir() and all((r / f).exists() for f in wanted)), None)


def require_external(name: str, files: Iterable[str] = (), repo: Path = REPO) -> Path:
    """外部数据集 name 的目录, 且 files (相对该目录) 都在 (按 external_dir 逐个来源查); 没有时 pytest.skip, 原因写明
    缺什么: 哪个来源都没有这个数据集, 或第一个有它的来源缺哪些文件."""
    wanted = list(files)
    root = external_dir(name, repo, wanted)
    if root is not None:
        return root
    first = next((r for r in external_roots(name, repo) if r.is_dir()), None)
    if first is None:
        pytest.skip(f"没有外部数据 data/external/{name} (见该目录的 README 取数)")
    missing = [f for f in wanted if not (first / f).exists()]
    pytest.skip(f"外部数据 data/external/{name} 缺文件: {', '.join(missing)}")


def tracked(path: str, repo: Path = REPO) -> list[str]:
    """仓库跟踪的 path 下的文件 (相对仓库根); 没跟踪, git 不可用或 repo 不在 git 仓库里时为空."""
    return [line for line in git_lines(repo, "ls-files", "--", path) or [] if line]


def missing_tracked(path: str, repo: Path = REPO) -> list[str]:
    """仓库跟踪了却在工作区里找不到的 path 下的文件."""
    return [f for f in tracked(path, repo) if not (repo / f).exists()]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot_mismatches(snapshot: Path, supplements: Path) -> list[str]:
    """补造 manifest 的快照哈希表 (snapshot.files) 中与 snapshot 下实际文件不符或缺的文件名; 只读表里列的文件
    (contacts.json 不在表内, 不打开)."""
    manifest: dict[str, Any] = json.loads((supplements / "manifest.json").read_text(encoding="utf-8"))
    bad = []
    for entry in manifest["snapshot"]["files"]:
        path = snapshot / entry["path"]
        if not path.is_file() or sha256(path) != entry["sha256"]:
            bad.append(entry["path"])
    return bad


def check_pair(snapshot: Path | None, supplements: Path) -> None:
    """快照与补造都在时核对快照哈希; 不符时跳过 (预期值按补造写, 换了快照就不成立), EXTERNAL_STRICT=1 时失败."""
    if snapshot is None or not (supplements / "manifest.json").is_file():
        return
    bad = snapshot_mismatches(snapshot, supplements)
    if bad:
        reason = f"{MISMATCH}: {snapshot.name} 的 {', '.join(bad)}"
        if os.getenv("EXTERNAL_STRICT") == "1":
            pytest.fail(reason)
        pytest.skip(reason)


def snapshot_path(name: str) -> str:
    return f"api/{name}/organisation.json"


def require_xero_snapshot(name: str, repo: Path = REPO) -> Path:
    """Xero 快照 data/external/xero/api/<name>: 三个来源里第一个有这个快照 (organisation.json) 的. 仓库跟踪了这个
    快照时, git ls-files 列出的每个文件都得在, 缺一个即失败 ("仓库跟踪了快照却找不到"), 不静默跳过; 没跟踪又找不到
    时跳过. 同名补造目录 data/xero_demo/<name> 也在时核对快照哈希 (check_pair)."""
    rel = f"data/external/xero/api/{name}"
    lost = missing_tracked(rel, repo)
    if lost:
        pytest.fail(f"{NOT_FOUND}: {', '.join(lost)}")
    xero = external_dir("xero", repo, [snapshot_path(name)])
    if xero is None:
        if tracked(rel, repo):
            pytest.fail(f"{NOT_FOUND}: {rel}/organisation.json")
        pytest.skip(f"没有 Xero 快照 {rel} (见 data/external/xero/README.md)")
    root = xero / "api" / name
    check_pair(root, repo / "data" / "xero_demo" / name)
    return root


def require_xero_supplements(name: str, repo: Path = REPO) -> Path:
    """Xero 演示的补造目录 data/xero_demo/<name> (随仓库). 仓库跟踪了的, 列出的每个文件都得在, 缺一个即失败
    ("仓库跟踪了补造目录却找不到"); 没有又没跟踪时跳过. 快照也在时核对快照哈希 (check_pair)."""
    rel = f"data/xero_demo/{name}"
    lost = missing_tracked(rel, repo)
    if lost:
        pytest.fail(f"仓库跟踪了补造目录却找不到: {', '.join(lost)}")
    root = repo / rel
    if not (root / "manifest.json").is_file():
        pytest.skip(f"没有 Xero 补造目录 {rel}")
    xero = external_dir("xero", repo, [snapshot_path(name)])
    check_pair(xero / "api" / name if xero is not None else None, root)
    return root


def llm_env_files(repo: Path = REPO) -> list[tuple[str, Path]]:
    """真实大模型配置可放的 .env, 按优先顺序, 带来源名: LLM_ENV_FILE (显式指定), 本仓库根的 .env. 不读主工作副本
    或别的目录的 .env: 在工作副本里跑联网测试的人用 LLM_ENV_FILE 指过去."""
    files = [("LLM_ENV_FILE", Path(os.environ["LLM_ENV_FILE"]))] if os.getenv("LLM_ENV_FILE") else []
    files.append(("仓库 .env", repo / ".env"))
    return files


def partly(source: str, values: dict[str, str]) -> str | None:
    """只配了一部分的来源的说明 (只写键名, 不写值); 都没配或都配了为 None."""
    given = [key for key in REQUIRED if values.get(key)]
    sep = " " if source[-1].isascii() else ""  # "仓库 .env 只配了", "环境变量只配了"
    return f"{source}{sep}只配了 {given[0]}" if len(given) == 1 else None


def live_llm_settings(env_files: Sequence[tuple[str, Path]] | None = None) -> Settings:
    """真实大模型测试的配置 (LLM_MODE live 的 Settings; 不改 conftest 对进程环境的 mock 强制).

    来源按顺序: 环境变量, env_files (缺省 llm_env_files(): LLM_ENV_FILE, 仓库 .env) 中存在的文件; 取第一个
    LLM_MODEL 与 LLM_API_KEY 两项都配了的来源的 LLM_KEYS 三项 (LLM_BASE_URL 可选, 也取同一来源), 来源之间不合并.
    都没配全时 pytest.skip, 原因为 "未配置 LLM_MODEL 与 LLM_API_KEY", 另附只配了一部分的来源 (如 "环境变量只配了
    LLM_MODEL") 与不存在的 LLM_ENV_FILE; 原因与报错都不带取到的值. 其余配置照常 (环境变量与仓库 .env)."""
    sources: list[tuple[str, dict[str, str]]] = [("环境变量", {key: os.getenv(key, "") for key in LLM_KEYS})]
    notes: list[str] = []
    for label, path in llm_env_files() if env_files is None else env_files:
        if path.is_file():
            values = dotenv_values(path)
            sources.append((label, {key: values.get(key) or "" for key in LLM_KEYS}))
        elif label == "LLM_ENV_FILE":
            notes.append("LLM_ENV_FILE 指的文件不存在")
    for label, values in sources:
        if all(values[key] for key in REQUIRED):
            return Settings(LLM_MODE="live", **values)
        note = partly(label, values)
        if note is not None:
            notes.append(note)
    pytest.skip("; ".join([NO_LLM, *notes]))


def live_without_slow(items: Iterable[pytest.Item]) -> list[str]:
    """标了 live 却没标 slow 的测试 (nodeid); conftest 在收集阶段据此报错, 免得默认测试联网."""
    return [i.nodeid for i in items if i.get_closest_marker("live") and not i.get_closest_marker("slow")]
