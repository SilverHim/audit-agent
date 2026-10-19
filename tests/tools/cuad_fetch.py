"""取 CUAD v1 (Zenodo 记录 4595826) 到 data/external/cuad/, 校验, 解压并写来源与许可说明.

用法: uv run python -m tests.tools.cuad_fetch [--dest data/external/cuad]

- 目标目录被 .gitignore 的 /data/* 忽略, 不入库.
- 走环境里已配置的 HTTPS 代理与 CA (httpx 的 trust_env), 不关 TLS 校验.
- 边下载边算 md5, 与 MD5 不符即报错, 不留目标目录, 也不留临时文件.
- 先解压到 dest 下新建的空临时目录, 拒绝绝对路径与含 .. 的条目, 再把压缩包里的 CUAD_v1/ 整体改名为 dest/CUAD_v1.
  解压出的文件只当数据读, 不执行, 不导入.
- 写 dest/README.md: 来源, 版本, 署名, 许可, CUAD README 的免责句, 下载日期与 md5.
- 目标目录已在, 且 README 记下的 md5 与 MD5 相符时, 不再下载 (不发请求).
- 默认测试从不调下载: 只有显式运行本命令, 或慢测试在 CUAD_FETCH=1 时调 ensure_cuad().
"""

import argparse
import hashlib
import re
import shutil
import sys
import tempfile
import zipfile
from datetime import date
from pathlib import Path, PurePosixPath

import httpx

REPO = Path(__file__).resolve().parents[2]
DEFAULT_DEST = REPO / "data" / "external" / "cuad"
RECORD_URL = "https://zenodo.org/records/4595826"
URL = f"{RECORD_URL}/files/CUAD_v1.zip"
MD5 = "c38f490a984420b8a62600db401fafd5"
TARGET = "CUAD_v1"  # 压缩包的顶层目录, 也是 dest 下的目标目录名
ATTRIBUTION = "Contract Understanding Atticus Dataset (CUAD) v1, The Atticus Project, CC BY 4.0"
LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"
DISCLAIMER = (  # CUAD_v1_README.txt 的 LICENSE 段原句
    "We make no representations or warranties regarding the license status of the underlying contracts, "
    "which are publicly available and downloadable from EDGAR."
)
RECORDED = re.compile(r"CUAD_v1\.zip 的 md5 为 ([0-9a-f]{32})")


class CuadFetchError(RuntimeError):
    """下载, 校验或解压失败; 失败时目标目录与临时文件都已清理."""


def readme(md5: str, downloaded: date) -> str:
    """dest/README.md 的全文."""
    return (
        "# CUAD v1\n\n"
        f"- 来源: Zenodo 记录 4595826, {RECORD_URL}, 版本 v1. 由 `uv run python -m tests.tools.cuad_fetch` 取得.\n"
        f"- 署名: {ATTRIBUTION}.\n"
        f"- 许可: Creative Commons Attribution 4.0, {LICENSE_URL}\n"
        f'- 免责 (CUAD README 原句): "{DISCLAIMER}"\n'
        f"- 下载: {downloaded.isoformat()}, CUAD_v1.zip 的 md5 为 {md5}.\n"
        "- 解压: 先解到同级新建的空临时目录 (拒绝绝对路径与含 .. 的条目), 再整体改名为 `CUAD_v1/`; "
        "解压出的文件只当数据读, 不执行, 不导入.\n"
        "- 本目录被 `.gitignore` 的 `/data/*` 忽略, 不入库.\n"
    )


def recorded_md5(dest: Path) -> str | None:
    """dest/README.md 记下的 md5; 没有 README 或没有这一句时为 None."""
    try:
        text = (dest / "README.md").read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    found = RECORDED.search(text)
    return found.group(1) if found else None


def make_client() -> httpx.Client:
    """真实下载用的客户端: 跟随跳转, 读环境里的代理与 CA, 校验 TLS."""
    return httpx.Client(follow_redirects=True, timeout=httpx.Timeout(120.0, connect=30.0), trust_env=True)


def unsafe_entry(name: str) -> bool:
    """压缩包条目是绝对路径 (含盘符与反斜杠写法) 或含 .. 时为真."""
    path = PurePosixPath(name.replace("\\", "/"))
    return path.is_absolute() or ".." in path.parts or re.match(r"^[A-Za-z]:", name) is not None


def download(client: httpx.Client, archive: Path) -> str:
    """把 URL 流式写到 archive, 返回其 md5."""
    digest = hashlib.md5(usedforsecurity=False)
    with client.stream("GET", URL) as response:
        if response.status_code != 200:
            raise CuadFetchError(f"下载 {URL} 失败: HTTP {response.status_code}")
        with archive.open("wb") as out:
            for chunk in response.iter_bytes():
                digest.update(chunk)
                out.write(chunk)
    return digest.hexdigest()


def extract(archive: Path, into: Path) -> Path:
    """先查全部条目再解压到空目录 into, 返回其中的 CUAD_v1 目录."""
    try:
        with zipfile.ZipFile(archive) as zf:
            bad = [info.filename for info in zf.infolist() if unsafe_entry(info.filename)]
            if bad:
                raise CuadFetchError(f"压缩包含不安全的条目, 拒绝解压: {bad[0]}")
            zf.extractall(into)
    except zipfile.BadZipFile as exc:
        raise CuadFetchError(f"{URL} 不是有效的 zip") from exc
    inner = into / TARGET
    if not inner.is_dir():
        raise CuadFetchError(f"压缩包里没有 {TARGET}/ 目录")
    return inner


def ensure_cuad(
    dest: Path = DEFAULT_DEST,
    *,
    client: httpx.Client | None = None,
    today: date | None = None,
    expected_md5: str = MD5,
) -> Path:
    """确保 dest/CUAD_v1 在且来源记录相符, 返回该目录 (见模块说明). 失败抛 CuadFetchError."""
    target = dest / TARGET
    if target.is_dir() and recorded_md5(dest) == expected_md5:
        return target
    if target.exists():
        raise CuadFetchError(f"{target} 已存在, 但 {dest / 'README.md'} 没有记下相符的 md5; 核对后删掉它再取")
    created = not dest.exists()
    dest.mkdir(parents=True, exist_ok=True)
    download_dir = Path(tempfile.mkdtemp(dir=dest, prefix=".cuad-download-"))
    extract_dir = Path(tempfile.mkdtemp(dir=dest, prefix=".cuad-extract-"))
    done = False
    try:
        archive = download_dir / "CUAD_v1.zip"
        own = client is None
        http = make_client() if client is None else client
        try:
            got = download(http, archive)
        finally:
            if own:
                http.close()
        if got != expected_md5:
            raise CuadFetchError(f"CUAD_v1.zip 的 md5 为 {got}, 应为 {expected_md5}; 已删除下载的文件")
        extract(archive, extract_dir).rename(target)
        (dest / "README.md").write_text(readme(expected_md5, today or date.today()), encoding="utf-8")
        done = True
    except httpx.HTTPError as exc:
        raise CuadFetchError(f"下载 {URL} 失败: {exc}") from exc
    finally:
        shutil.rmtree(download_dir, ignore_errors=True)
        shutil.rmtree(extract_dir, ignore_errors=True)
        if not done:
            shutil.rmtree(target, ignore_errors=True)
            if created:
                shutil.rmtree(dest, ignore_errors=True)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="取 CUAD v1 到 data/external/cuad/ (不入库)")
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST, help="目标目录, 默认 data/external/cuad")
    args = parser.parse_args(argv)
    dest: Path = args.dest.resolve()
    before = (dest / TARGET).is_dir() and recorded_md5(dest) == MD5
    try:
        target = ensure_cuad(dest, expected_md5=MD5)
    except CuadFetchError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"已有 CUAD (md5 相符), 不再下载: {target}" if before else f"已下载, 校验并解压到 {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
