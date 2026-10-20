"""浏览器端到端走查 (需求 R345) 的启动与报告: 在测试进程里起后端 (组装根, 连测试库), 在前端目录起 vite 开发服务
(配置与缓存都在输出目录, 代理到后端), 跑 Node 驱动 walk.mjs, 把它的结果与 R311 口径的汇总写成走查报告.

后端用应用本身的 create_app 与 lifespan (配置取环境变量), 只把启动时的 recover 换成空操作: 测试库是共用的, 里面有
别的用例留下的项目, 走查只管自己新建的这一个. 模型用量的预算装在 lifespan 建好的依赖上 (同 R311 的 install_budget).
"""

import asyncio
import json
import os
import shutil
import socket
from collections import Counter
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from backend import main
from backend.core.config import get_settings

REPO = Path(__file__).resolve().parents[2]
WALK = Path(__file__).with_name("walk.mjs")
FRONTEND = REPO / "src" / "frontend"
NODE_MODULES = Path(os.getenv("E2E_NODE_MODULES", "/opt/node-tools/node_modules"))  # node 版 playwright 所在
BROWSERS = Path(os.getenv("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers"))  # Chromium, 不在测试里 install
# 走查用的 vite 配置, 写在输出目录 (不写进前端目录): 原配置之上把 /api 代理到走查的后端, 依赖预构建的缓存放输出目录.
# 前端的 node_modules 可能链到别的工作副本 (那里不能写), 缓存缺省却在 node_modules/.vite; 配置文件不在前端目录,
# 打包配置的临时文件也就不进 node_modules/.vite-temp. 依赖目录的实际路径与缓存目录不在前端目录下, 加进 fs.allow,
# 不然开发服务拒绝读 (403)
VITE_CONFIG = """// 走查用的 vite 配置 (tests/e2e/harness.py 写入输出目录)
import base from {base}

export default {{
  ...base,
  cacheDir: {cache},
  server: {{ ...base.server, proxy: {{ '/api': {api} }}, fs: {{ allow: {allow} }} }},
}}
"""


def require_tools() -> None:
    """没有 node, node 版 playwright, Chromium 或前端依赖时跳过 (不在测试里安装)."""
    missing = [
        name
        for name, ok in (
            ("node", shutil.which("node") is not None),
            (f"{NODE_MODULES}/playwright", (NODE_MODULES / "playwright").is_dir()),
            (str(BROWSERS), BROWSERS.is_dir()),
            ("src/frontend/node_modules", (FRONTEND / "node_modules" / ".bin" / "vite").exists()),
        )
        if not ok
    ]
    if missing:
        pytest.skip(f"浏览器走查缺: {', '.join(missing)}")


def out_dir(now: datetime | None = None) -> Path:
    """本次走查的输出目录 var/e2e/<UTC> (E2E_OUT 可另指上级目录): 报告, 截图, 驱动结果与日志."""
    root = Path(os.getenv("E2E_OUT") or REPO / "var" / "e2e")
    path = root / f"{(now or datetime.now(UTC)):%Y%m%dT%H%M%SZ}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def skip_recover(*args: Any, **kwargs: Any) -> None:
    """启动时不恢复测试库里别的项目 (见模块说明)."""


@asynccontextmanager
async def backend(monkeypatch: pytest.MonkeyPatch, env: Mapping[str, str]) -> AsyncIterator[tuple[str, FastAPI]]:
    """按 env 配置起后端 (uvicorn, 与测试同一个事件循环), 返回 (地址, 应用); lifespan 起好之后才返回, 退出时停服务
    (lifespan 收尾停后台作业并释放连接)."""
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    monkeypatch.setattr(main, "recover", skip_recover)
    app = main.create_app()
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="on", log_level="warning"))
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            if task.done():
                task.result()  # 启动失败: 抛出原异常
                raise RuntimeError("后端没有起来")
            await asyncio.sleep(0.1)
        yield f"http://127.0.0.1:{port}", app
    finally:
        server.should_exit = True
        await task
        get_settings.cache_clear()


@asynccontextmanager
async def frontend(out: Path, api: str) -> AsyncIterator[str]:
    """在 src/frontend 起 vite 开发服务, 把 /api 代理到 api, 配置与依赖缓存在输出目录 (见 VITE_CONFIG); 返回页面
    地址. 退出时停服务并删掉缓存 (日志留在 out/vite.log)."""
    cache = out / "vite-cache"
    allow = [str(FRONTEND), str((FRONTEND / "node_modules").resolve()), str(cache)]
    config = out / "vite.e2e.config.mts"  # .mts: 输出目录没有 package.json, 按 ESM 读
    q = json.dumps
    config.write_text(
        VITE_CONFIG.format(base=q(str(FRONTEND / "vite.config.ts")), cache=q(str(cache)), api=q(api), allow=q(allow)),
        encoding="utf-8",
    )
    port = free_port()
    log = (out / "vite.log").open("wb")
    vite = FRONTEND / "node_modules" / ".bin" / "vite"
    args = ["serve", str(FRONTEND), "--config", str(config), "--host", "127.0.0.1", "--port", str(port), "--strictPort"]
    proc = await asyncio.create_subprocess_exec(
        str(vite), *args, cwd=FRONTEND, stdout=log, stderr=asyncio.subprocess.STDOUT
    )
    url = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient() as client:
            for _ in range(240):
                if proc.returncode is not None:
                    raise RuntimeError(f"vite 退出 ({proc.returncode}), 见 {out / 'vite.log'}")
                try:
                    if (await client.get(url)).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.5)
            else:
                raise RuntimeError("vite 两分钟内没有起来")
        yield url
    finally:
        if proc.returncode is None:
            proc.terminate()
            await proc.wait()
        log.close()
        shutil.rmtree(cache, ignore_errors=True)


async def run_walk(config: dict[str, Any], out: Path, timeout_s: float) -> dict[str, Any]:
    """跑驱动 (node walk.mjs), 返回它写的 results.json; 驱动的输出在 out/walk.log. 配置里有本次注册的随机口令, 跑完
    删掉配置文件."""
    path = out / "config.json"
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    env = os.environ | {"PLAYWRIGHT_BROWSERS_PATH": str(BROWSERS), "E2E_NODE_MODULES": str(NODE_MODULES)}
    with (out / "walk.log").open("wb") as log:
        proc = await asyncio.create_subprocess_exec(
            "node", str(WALK), str(path), stdout=log, stderr=asyncio.subprocess.STDOUT, env=env
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout_s)
        except TimeoutError:
            proc.kill()
            await proc.wait()
    path.unlink()
    results = out / "results.json"
    if not results.is_file():
        raise RuntimeError(f"驱动没有写出结果 (退出码 {proc.returncode}), 见 {out / 'walk.log'}")
    return json.loads(results.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def write_report(out: Path, mode: str, results: Mapping[str, Any], data: Mapping[str, Any]) -> Path:
    """走查报告 out/report.md: 概况, 问题 (步骤, 页面, 现象, 截图, 期望), 各步, 用量 (调用, token, 花费) 与各判断的
    建议动作和固定输出的对照 (同 R311 的汇总口径)."""
    issues = results["issues"]
    usage = data.get("usage", {})
    gates = ", ".join(results.get("gates", [])) or "—"
    lines = [
        f"# 浏览器走查报告 ({mode})",
        "",
        f"- 项目: {results.get('eid')}; 走到归档: {'是' if results.get('reached_archive') else '否'}",
        f"- 闸门: {gates}; FIN-01: {results.get('fin')} (候选 {results.get('candidate')})",
        f"- 复核决定: {dict(Counter(d for _, d in results.get('decisions', [])))}; 重跑: {data.get('reruns', {})}",
        f"- 步数: {len(results['steps'])}; 问题: {len(issues)}; 不算问题的已知情况: {results.get('known') or '—'}",
        "",
        "## 问题",
        "",
    ]
    if issues:
        lines += ["| 步骤 | 页面 | 现象 | 截图 | 期望 |", "|---|---|---|---|---|"]
        lines += [
            f"| {i['step']} | {cell(i['page'])} | {cell(i['symptom'])} | shots/{i['shot']} | {cell(i['expected'])} |"
            for i in issues
        ]
    else:
        lines.append("没有.")
    lines += ["", "## 各步", "", "| 步骤 | 名称 | 页面 | 用时 (秒) | 截图 |", "|---|---|---|---|---|"]
    lines += [
        f"| {s['no']} | {cell(s['name'])} | {cell(s['url'])} | {s['ms'] / 1000:.1f} | shots/{s['shot']} |"
        for s in results["steps"]
    ]
    lines += [
        "",
        "## 用量",
        "",
        f"- 调用 {usage.get('calls')}, 输入 token {usage.get('input_tokens')}, 输出 token {usage.get('output_tokens')}"
        + (f", 花费 {usage['cost']}" if "cost" in usage else ""),
        "",
        "| 程序 | 调用 | 尝试次数 | 校验 |",
        "|---|---|---|---|",
    ]
    lines += [
        f"| {pid} | {row['calls']} | {row['attempts']} | {row['level_failed']} |"
        for pid, row in usage.get("by_procedure", {}).items()
    ]
    lines += [
        "",
        "## 判断的建议动作与固定输出",
        "",
        "| 程序 | 对象 | 建议 | 固定输出 | 相同 |",
        "|---|---|---|---|---|",
    ]
    lines += [
        f"| {r['procedure_id']} | {r['subject_id']} | {r['proposed_action']} | {r['fixture']} | {r['same']} |"
        for r in data.get("judgments", [])
    ]
    lines += [
        "",
        f"缺模型结论的对象: {data.get('missing_subjects', [])}; 解析不了的指针: {data.get('unresolved_pointers', [])}",
    ]
    path = out / "report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
