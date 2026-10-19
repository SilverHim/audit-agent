"""CUAD 下载脚本 (R245): 用 httpx.MockTransport 给假 zip, 不联网; 只有慢测试在 CUAD_FETCH=1 时真下载."""

import hashlib
import io
import os
import shutil
import subprocess
import zipfile
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import httpx
import pytest

from tests.tools import cuad_fetch
from tests.tools.cuad_fetch import CuadFetchError, ensure_cuad

TODAY = date(2026, 10, 19)


def fake_zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buffer.getvalue()


GOOD = fake_zip(
    {
        "CUAD_v1/master_clauses.csv": b"Filename,Document Name\n",
        "CUAD_v1/full_contract_txt/A.txt": b"contract text",
        "CUAD_v1/CUAD_v1_README.txt": b"readme",
    }
)
GOOD_MD5 = hashlib.md5(GOOD, usedforsecurity=False).hexdigest()


class Server:
    """假 Zenodo: 记下请求, 回给定的字节与状态码."""

    def __init__(self, body: bytes = GOOD, status: int = 200) -> None:
        self.body, self.status, self.requests = body, status, list[httpx.Request]()

    def client(self) -> httpx.Client:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(self.status, content=self.body)

        return httpx.Client(transport=httpx.MockTransport(handle))


@pytest.fixture(autouse=True)
def no_real_client(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Iterator[None]:
    """默认测试不得建真实客户端 (只有标 slow 的测试可以)."""
    if request.node.get_closest_marker("slow") is None:

        def forbidden() -> httpx.Client:
            raise AssertionError("默认测试不许联网下载 CUAD")

        monkeypatch.setattr(cuad_fetch, "make_client", forbidden)
    yield


def leftovers(dest: Path) -> list[str]:
    return sorted(p.name for p in dest.iterdir()) if dest.exists() else []


def test_downloads_checks_extracts_and_writes_readme(tmp_path: Path) -> None:
    dest = tmp_path / "cuad"
    server = Server()
    target = ensure_cuad(dest, client=server.client(), today=TODAY, expected_md5=GOOD_MD5)
    assert target == dest / "CUAD_v1"
    assert [str(r.url) for r in server.requests] == ["https://zenodo.org/records/4595826/files/CUAD_v1.zip"]
    assert (target / "full_contract_txt" / "A.txt").read_bytes() == b"contract text"
    assert leftovers(dest) == ["CUAD_v1", "README.md"]  # 下载与解压的临时目录都已删
    readme = (dest / "README.md").read_text(encoding="utf-8")
    assert "Contract Understanding Atticus Dataset (CUAD) v1, The Atticus Project, CC BY 4.0" in readme
    assert "https://zenodo.org/records/4595826" in readme and "https://creativecommons.org/licenses/by/4.0/" in readme
    assert cuad_fetch.DISCLAIMER in readme and "license status of the underlying contracts" in readme
    assert f"2026-10-19, CUAD_v1.zip 的 md5 为 {GOOD_MD5}" in readme
    assert cuad_fetch.recorded_md5(dest) == GOOD_MD5


def test_second_run_sends_no_request(tmp_path: Path) -> None:
    dest = tmp_path / "cuad"
    ensure_cuad(dest, client=Server().client(), today=TODAY, expected_md5=GOOD_MD5)
    again = Server()
    assert ensure_cuad(dest, client=again.client(), expected_md5=GOOD_MD5) == dest / "CUAD_v1"
    assert again.requests == []


def test_md5_mismatch_fails_without_leftovers(tmp_path: Path) -> None:
    dest = tmp_path / "cuad"
    with pytest.raises(CuadFetchError, match=f"md5 为 {GOOD_MD5}, 应为 {cuad_fetch.MD5}"):
        ensure_cuad(dest, client=Server().client(), today=TODAY)  # 假 zip 的 md5 不是正本的
    assert not dest.exists()  # 本次新建的目标目录也删掉


def test_failure_keeps_an_existing_dest_but_leaves_nothing_in_it(tmp_path: Path) -> None:
    dest = tmp_path / "cuad"
    dest.mkdir()
    (dest / "notes.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(CuadFetchError, match="md5"):
        ensure_cuad(dest, client=Server().client(), today=TODAY)
    assert leftovers(dest) == ["notes.txt"]


@pytest.mark.parametrize("entry", ["../x", "CUAD_v1/../../x", "/etc/x", "C:/x", "CUAD_v1\\..\\x"])
def test_unsafe_entries_are_rejected_without_leftovers(tmp_path: Path, entry: str) -> None:
    body = fake_zip({"CUAD_v1/master_clauses.csv": b"x", entry: b"evil"})
    md5 = hashlib.md5(body, usedforsecurity=False).hexdigest()
    dest = tmp_path / "cuad"
    with pytest.raises(CuadFetchError, match="不安全的条目"):
        ensure_cuad(dest, client=Server(body).client(), today=TODAY, expected_md5=md5)
    assert not dest.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == []  # 也没有写到 dest 外面


def test_archive_without_cuad_dir_is_rejected(tmp_path: Path) -> None:
    body = fake_zip({"other/file.txt": b"x"})
    md5 = hashlib.md5(body, usedforsecurity=False).hexdigest()
    with pytest.raises(CuadFetchError, match="没有 CUAD_v1/ 目录"):
        ensure_cuad(tmp_path / "cuad", client=Server(body).client(), today=TODAY, expected_md5=md5)
    assert not (tmp_path / "cuad").exists()


def test_http_error_fails_without_leftovers(tmp_path: Path) -> None:
    with pytest.raises(CuadFetchError, match="HTTP 404"):
        ensure_cuad(tmp_path / "cuad", client=Server(b"", status=404).client(), today=TODAY)
    assert not (tmp_path / "cuad").exists()


def test_existing_target_without_matching_record_is_left_alone(tmp_path: Path) -> None:
    dest = tmp_path / "cuad"
    (dest / "CUAD_v1").mkdir(parents=True)
    (dest / "README.md").write_text("CUAD_v1.zip 的 md5 为 " + "0" * 32, encoding="utf-8")
    server = Server()
    with pytest.raises(CuadFetchError, match="没有记下相符的 md5"):
        ensure_cuad(dest, client=server.client(), expected_md5=GOOD_MD5)
    assert server.requests == [] and leftovers(dest) == ["CUAD_v1", "README.md"]


def test_cli_reports_errors_and_reuses_a_recorded_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    server = Server()
    monkeypatch.setattr(cuad_fetch, "make_client", server.client)
    dest = tmp_path / "cuad"
    assert cuad_fetch.main(["--dest", str(dest)]) == 1  # 假 zip 的 md5 不符
    assert "应为 c38f490a984420b8a62600db401fafd5" in capsys.readouterr().err and not dest.exists()
    monkeypatch.setattr(cuad_fetch, "MD5", GOOD_MD5)
    assert cuad_fetch.main(["--dest", str(dest)]) == 0
    assert "已下载, 校验并解压到" in capsys.readouterr().out
    assert cuad_fetch.main(["--dest", str(dest)]) == 0
    assert "不再下载" in capsys.readouterr().out and len(server.requests) == 2


def test_default_dest_is_git_ignored() -> None:
    if shutil.which("git") is None or not (cuad_fetch.REPO / ".git").exists():
        pytest.skip("不是 git 检出")
    assert cuad_fetch.DEFAULT_DEST == cuad_fetch.REPO / "data" / "external" / "cuad"
    for path in ("data/external/cuad/CUAD_v1", "data/external/cuad/README.md"):
        done = subprocess.run(["git", "check-ignore", "-q", path], cwd=cuad_fetch.REPO, check=False)
        assert done.returncode == 0, f"{path} 没有被 .gitignore 忽略"


@pytest.mark.slow
@pytest.mark.skipif(os.environ.get("CUAD_FETCH") != "1", reason="只在 CUAD_FETCH=1 时真下载 CUAD (约 100 MB)")
def test_real_download() -> None:
    target = ensure_cuad()
    assert cuad_fetch.recorded_md5(target.parent) == cuad_fetch.MD5
    assert (target / "master_clauses.csv").is_file()
    assert len(list((target / "full_contract_txt").glob("*.txt"))) == 510
    assert cuad_fetch.DISCLAIMER in (target / "CUAD_v1_README.txt").read_text(encoding="utf-8")  # 免责句是原句
