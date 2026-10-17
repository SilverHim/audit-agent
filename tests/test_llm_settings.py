"""模型设置 (R6): LlmSettingsService 的存取, 加密, 回退与热切换, 测试连接, 以及 /settings/llm 接口.

不联网: 模型接口用假 HTTP 传输 (httpx.MockTransport); 存储用 FakeLlmSettingsStore (Postgres 实现见慢测试).
配置显式给全, 不读本机 .env.
"""

import asyncio
import json
import logging
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet

from backend.adapters.llm import DEFAULT_BASE_URL, LiveLLMClient, MockLLMClient, SwitchableLLMClient, make_llm_client
from backend.contracts.llm_outputs import ConfirmationExtraction
from backend.contracts.models import LlmProvider, LlmSlot
from backend.core.config import Settings
from backend.core.security import SecretKeyError, decrypt_secret
from backend.services.llm_settings import LlmSettingsService, SettingsRejected
from tests.fakes import FakeLlmSettingsStore, api_client, make_runner
from tests.test_ingest_doc06 import raw
from tests.test_llm_live import REQUEST, Server, completion

MASTER = Fernet.generate_key().decode()  # 测试用的主密钥
KEY = "sk-ant-test-0000-1234"
ANTHROPIC = "https://api.anthropic.com/v1"


def settings(**changes: Any) -> Settings:
    base: dict[str, Any] = {
        "LLM_MODE": "live",
        "LLM_BASE_URL": "",
        "LLM_MODEL": "",
        "LLM_API_KEY": "",
        "SETTINGS_SECRET_KEY": MASTER,
    }
    return Settings(_env_file=None, **{**base, **changes})  # type: ignore[call-arg]


class EchoServer:
    """假模型接口: 回复里的模型名取请求体的 model, 记下收到的请求."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        model = json.loads(request.content)["model"]
        return httpx.Response(200, json=completion(raw("BA-01"), model=model))

    def models(self) -> list[str]:
        return [json.loads(r.content)["model"] for r in self.requests]


def service(
    transport: httpx.AsyncBaseTransport | None = None, store: FakeLlmSettingsStore | None = None, **changes: Any
) -> LlmSettingsService:
    s = settings(**changes)
    primary = SwitchableLLMClient(make_llm_client(s), s.LLM_MODEL)
    review = SwitchableLLMClient(make_llm_client(s), s.LLM_MODEL)
    return LlmSettingsService(store or FakeLlmSettingsStore(), primary, review, s, transport=transport)


async def save(
    svc: LlmSettingsService,
    slot: LlmSlot = "primary",
    provider: LlmProvider = "anthropic",
    base_url: str = ANTHROPIC,
    model: str = "claude-opus-5-5",
    api_key: str | None = KEY,
) -> Any:
    return await svc.update(slot, provider=provider, base_url=base_url, model=model, api_key=api_key, updated_by="U-1")


def store_of(svc: LlmSettingsService) -> FakeLlmSettingsStore:
    assert isinstance(svc.store, FakeLlmSettingsStore)
    return svc.store


# ── 保存与读取 ──


async def test_saved_key_is_encrypted_and_shown_as_last4() -> None:
    svc = service()
    view = await save(svc)
    assert (view.has_key, view.key_last4, view.updated_by) == (True, "1234", "U-1")
    row = store_of(svc).rows["primary"]
    assert row.api_key_encrypted is not None and KEY not in row.model_dump_json()
    assert decrypt_secret(row.api_key_encrypted, MASTER) == KEY
    assert KEY not in repr(view)


async def test_changing_only_model_keeps_key() -> None:
    svc = service()
    await save(svc)
    before = store_of(svc).rows["primary"].api_key_encrypted
    view = await save(svc, model="claude-sonnet-5-5", api_key=None)
    row = store_of(svc).rows["primary"]
    assert (row.model, row.api_key_encrypted, view.key_last4) == ("claude-sonnet-5-5", before, "1234")


async def test_stored_key_is_not_sent_to_a_new_address() -> None:
    """沿用已存的密钥时服务商与接口地址须不变: 否则测试连接或保存会把密钥发到别的地址."""
    svc = service(transport=httpx.MockTransport(EchoServer().handler))
    await save(svc)
    for changed in ({"base_url": "https://attacker.example/v1"}, {"provider": "openai"}):
        args = {"provider": "anthropic", "base_url": ANTHROPIC, "model": "m", "api_key": None, **changed}
        with pytest.raises(SettingsRejected) as e:
            await svc.update("primary", updated_by="U-1", **args)
        assert e.value.code == "INVALID_VALUE"
        with pytest.raises(SettingsRejected) as e:
            await svc.test("primary", **args)
        assert e.value.code == "INVALID_VALUE"
    assert store_of(svc).rows["primary"].base_url == ANTHROPIC


async def test_master_key_missing_or_invalid() -> None:
    for master, code in (("", "SETTINGS_KEY_MISSING"), ("not-a-fernet-key", "SETTINGS_KEY_INVALID")):
        svc = service(SETTINGS_SECRET_KEY=master)
        with pytest.raises(SecretKeyError) as e:
            await save(svc)
        assert e.value.code == code and store_of(svc).rows == {}
    saved = service()
    await save(saved)
    rotated = service(store=store_of(saved), SETTINGS_SECRET_KEY=Fernet.generate_key().decode())
    with pytest.raises(SecretKeyError) as e:  # 换过主密钥: 已存的密钥解不开
        await save(rotated, model="claude-sonnet-5-5", api_key=None)
    assert e.value.code == "SETTINGS_KEY_INVALID"


async def test_key_required_unless_ollama_and_ollama_clears_key() -> None:
    svc = service()
    with pytest.raises(SettingsRejected) as e:
        await save(svc, api_key=None)
    assert e.value.code == "MISSING_FIELD"
    await save(svc)
    view = await save(svc, provider="ollama", base_url="http://localhost:11434/v1", model="llama3", api_key=None)
    assert (view.has_key, view.key_last4, store_of(svc).rows["primary"].api_key_encrypted) == (False, None, None)


async def test_rejects_bad_url_and_empty_model() -> None:
    svc = service()
    for args in ({"base_url": "ftp://host/v1"}, {"base_url": "api.anthropic.com"}, {"model": "  "}):
        with pytest.raises(SettingsRejected) as e:
            await save(svc, **args)
        assert e.value.code == "INVALID_VALUE"
    view = await save(svc, base_url=" https://api.anthropic.com/v1/ ")
    assert view.base_url == ANTHROPIC  # 去掉空白与末尾斜杠


async def test_unsaved_slots_show_fallback() -> None:
    svc = service(LLM_MODEL="gpt-5", LLM_API_KEY="sk-env-9999")
    primary, review = await svc.view("primary"), await svc.view("review")
    assert (primary.provider, primary.base_url, primary.model, primary.key_last4) == (
        "openai",
        DEFAULT_BASE_URL,
        "gpt-5",
        "9999",
    )
    assert primary.updated_at is None and review == primary  # 复核模型跟随主模型
    custom = await service(LLM_BASE_URL="https://api.deepseek.com/v1").view("primary")
    assert custom.provider == "custom"

    await save(svc)
    review = await svc.view("review")
    assert (review.provider, review.model, review.updated_at) == ("anthropic", "claude-opus-5-5", None)


# ── 热切换 ──


async def test_hot_switch_next_call_uses_new_model_and_slots_are_independent() -> None:
    server = EchoServer()
    svc = service(transport=httpx.MockTransport(server.handler))
    primary, review = svc.clients["primary"], svc.clients["review"]

    await save(svc, model="claude-opus-5-5")
    await primary.complete(REQUEST, ConfirmationExtraction)
    await review.complete(REQUEST, ConfirmationExtraction)  # 复核模型没有自己的配置, 跟随主模型
    await save(svc, slot="review", model="claude-fable-5-1")
    await review.complete(REQUEST, ConfirmationExtraction)
    await save(svc, model="claude-sonnet-5-5", api_key=None)  # 复核模型已有配置, 不再跟随
    await primary.complete(REQUEST, ConfirmationExtraction)
    await review.complete(REQUEST, ConfirmationExtraction)

    assert server.models() == [
        "claude-opus-5-5",
        "claude-opus-5-5",
        "claude-fable-5-1",
        "claude-sonnet-5-5",
        "claude-fable-5-1",
    ]
    assert (primary.model_name, review.model_name) == ("claude-sonnet-5-5", "claude-fable-5-1")
    assert all(r.headers["authorization"] == f"Bearer {KEY}" for r in server.requests)
    assert svc.versions() == {
        "llm_mode": "live",
        "model": "claude-sonnet-5-5",
        "critic_model_version": "claude-fable-5-1",
    }


async def test_mock_mode_saves_but_keeps_fixtures() -> None:
    svc = service(LLM_MODE="mock")
    await save(svc)
    assert "primary" in store_of(svc).rows
    assert isinstance(svc.clients["primary"].inner, MockLLMClient)
    assert svc.versions() == {"llm_mode": "mock", "model": "mock", "critic_model_version": "mock"}


async def test_load_applies_saved_slots_and_survives_errors() -> None:
    saved = service()
    await save(saved, model="claude-opus-5-5")
    await save(saved, slot="review", model="claude-fable-5-1")

    svc = service(store=store_of(saved), LLM_MODEL="gpt-5", LLM_API_KEY="sk-env-9999")
    await svc.load()
    assert (svc.clients["primary"].model_name, svc.clients["review"].model_name) == (
        "claude-opus-5-5",
        "claude-fable-5-1",
    )
    assert (
        isinstance(svc.clients["primary"].inner, LiveLLMClient) and svc.clients["primary"].inner.provider == "anthropic"
    )

    store_of(svc).fail = True
    broken = service(store=store_of(svc), LLM_MODEL="gpt-5", LLM_API_KEY="sk-env-9999")
    await broken.load()  # 库不可用: 记日志, 沿用 .env
    assert broken.clients["primary"].model_name == "gpt-5"


# ── 测试连接 ──


async def test_connection_success_unauthorized_and_timeout() -> None:
    ok = Server((200, completion('{"ok": true}', model="claude-opus-5-5")))
    svc = service(transport=httpx.MockTransport(ok.handler))
    result = await svc.test("primary", provider="anthropic", base_url=ANTHROPIC, model="claude-opus-5-5", api_key=KEY)
    assert (result.ok, result.model, result.message) == (True, "claude-opus-5-5", "连接成功")
    body = json.loads(ok.requests[0].content)
    assert "JSON" in body["messages"][0]["content"] and "temperature" not in body
    assert store_of(svc).rows == {}  # 测试不保存

    denied = Server((401, f"invalid x-api-key {KEY}"))
    result = await service(transport=httpx.MockTransport(denied.handler)).test(
        "primary", provider="anthropic", base_url=ANTHROPIC, model="m", api_key=KEY
    )
    assert not result.ok and result.message.startswith("HTTP 401") and KEY not in result.message

    slow = Server(httpx.ReadTimeout("slow"), (200, completion('{"ok": true}')))
    result = await service(transport=httpx.MockTransport(slow.handler)).test(
        "primary", provider="openai", base_url=DEFAULT_BASE_URL, model="gpt-5", api_key=KEY
    )
    assert not result.ok and "超时" in result.message and len(slow.requests) == 1  # 不重试


async def test_connection_uses_stored_key_when_blank() -> None:
    server = Server((200, completion('{"ok": true}')))
    svc = service(transport=httpx.MockTransport(server.handler))
    await save(svc)
    result = await svc.test("primary", provider="anthropic", base_url=ANTHROPIC, model="claude-opus-5-5", api_key=None)
    assert result.ok and server.requests[0].headers["authorization"] == f"Bearer {KEY}"


# ── 版本快照 ──


async def test_runner_freezes_versions_from_callable() -> None:
    runner, _ = make_runner()
    svc = service(transport=httpx.MockTransport(EchoServer().handler))
    runner.llm = svc.versions
    await save(svc, model="claude-opus-5-5")
    assert runner.freeze_versions()["model"] == "claude-opus-5-5"


# ── 接口 ──


async def test_settings_api_round_trip() -> None:
    runner, _ = make_runner()
    server = Server((200, completion('{"ok": true}', model="claude-opus-5-5")))
    svc = service(transport=httpx.MockTransport(server.handler), LLM_MODEL="gpt-5", LLM_API_KEY="sk-env-9999")
    body = {"provider": "anthropic", "base_url": ANTHROPIC, "model": "claude-opus-5-5", "api_key": KEY}
    async with api_client(runner, llm_settings=svc) as client:
        before = (await client.get("/api/v1/settings/llm")).json()
        saved = await client.put("/api/v1/settings/llm/primary", json=body)
        tested = await client.post("/api/v1/settings/llm/primary/test", json={**body, "api_key": None})
        after = (await client.get("/api/v1/settings/llm")).json()
        bad_slot = await client.put("/api/v1/settings/llm/other", json=body)

    assert (before["primary"]["model"], before["primary"]["updated_at"]) == ("gpt-5", None)
    assert saved.status_code == 200 and KEY not in saved.text
    assert (saved.json()["has_key"], saved.json()["key_last4"]) == (True, "1234")
    assert tested.json()["ok"] is True and KEY not in tested.text
    assert after["primary"]["model"] == "claude-opus-5-5" and after["review"]["model"] == "claude-opus-5-5"
    assert after["review"]["updated_at"] is None and KEY not in json.dumps(after)
    assert bad_slot.status_code == 422


async def test_settings_api_errors() -> None:
    runner, _ = make_runner()
    body = {"provider": "anthropic", "base_url": ANTHROPIC, "model": "claude-opus-5-5"}
    async with api_client(runner, llm_settings=service(SETTINGS_SECRET_KEY="")) as client:
        no_master = await client.put("/api/v1/settings/llm/primary", json={**body, "api_key": KEY})
        no_key = await client.put("/api/v1/settings/llm/review", json=body)
        anonymous = await client.get("/api/v1/settings/llm", headers={"Authorization": ""})
    assert (no_master.status_code, no_master.json()["detail"]["code"]) == (503, "SETTINGS_KEY_MISSING")
    assert (no_key.status_code, no_key.json()["detail"]["code"]) == (422, "MISSING_FIELD")
    assert anonymous.status_code == 401


# ── Codex 审核 (FE-R6 第 1 轮) 的探针: 校验错误不回显密钥, 并发保存, 畸形地址, 进行中的调用, 加载失败的日志 ──


@pytest.mark.parametrize(("method", "suffix"), [("PUT", ""), ("POST", "/test")])
async def test_validation_errors_do_not_echo_api_key(method: str, suffix: str) -> None:
    """缺字段时 FastAPI 默认的 422 会在 detail[].input 回显整个请求体; 设置接口去掉 input 与 ctx."""
    runner, _ = make_runner()
    body = {"provider": "anthropic", "base_url": ANTHROPIC, "api_key": KEY}  # 缺 model
    async with api_client(runner, llm_settings=service()) as client:
        response = await client.request(method, f"/api/v1/settings/llm/primary{suffix}", json=body)
        other = await client.post("/api/v1/engagements/ENG-2026-001/deferrals", json={"target_type": "gate"})
    assert response.status_code == 422 and KEY not in response.text
    assert [e["loc"] for e in response.json()["detail"]] == [["body", "model"]]
    assert "input" in other.json()["detail"][0]  # 其他接口仍是 FastAPI 默认格式


class PausedReviewRead(FakeLlmSettingsStore):
    """第一次读复核模型的配置时停住, 模拟两个保存请求交错."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.resume = asyncio.Event()
        self.paused = False

    async def get(self, slot: LlmSlot) -> Any:
        answer = await super().get(slot)
        if slot == "review" and not self.paused:
            self.paused = True
            self.entered.set()
            await self.resume.wait()
        return answer


async def test_concurrent_primary_saves_keep_following_review_current() -> None:
    store, server = PausedReviewRead(), EchoServer()
    svc = service(store=store, transport=httpx.MockTransport(server.handler))
    first = asyncio.create_task(save(svc, model="primary-v1"))
    await asyncio.wait_for(store.entered.wait(), 2)
    second = asyncio.create_task(save(svc, model="primary-v2"))
    await asyncio.sleep(0)
    store.resume.set()
    await first
    await second
    assert store.rows["primary"].model == "primary-v2"
    await svc.clients["primary"].complete(REQUEST, ConfirmationExtraction)
    await svc.clients["review"].complete(REQUEST, ConfirmationExtraction)
    assert server.models() == ["primary-v2", "primary-v2"]


async def test_concurrent_first_review_save_stays_independent() -> None:
    store, server = PausedReviewRead(), EchoServer()
    svc = service(store=store, transport=httpx.MockTransport(server.handler))
    first = asyncio.create_task(save(svc, model="primary-model"))
    await asyncio.wait_for(store.entered.wait(), 2)
    second = asyncio.create_task(save(svc, slot="review", model="independent-review-model"))
    await asyncio.sleep(0)
    store.resume.set()
    await first
    await second
    assert store.rows["review"].model == "independent-review-model"
    await svc.clients["review"].complete(REQUEST, ConfirmationExtraction)
    assert server.models() == ["independent-review-model"]


@pytest.mark.parametrize("base_url", ["http://[", "https://[bad]/v1"])
async def test_malformed_urls_rejected(base_url: str) -> None:
    for call in ("update", "test"):
        svc = service()
        with pytest.raises(SettingsRejected) as e:
            if call == "update":
                await save(svc, base_url=base_url)
            else:
                await svc.test("primary", provider="anthropic", base_url=base_url, model="m", api_key=KEY)
        assert e.value.code == "INVALID_VALUE"


async def test_reload_does_not_change_an_inflight_call() -> None:
    entered, resume, server = asyncio.Event(), asyncio.Event(), EchoServer()

    async def handler(request: httpx.Request) -> httpx.Response:
        entered.set()
        await resume.wait()
        return server.handler(request)

    svc = service(transport=httpx.MockTransport(handler))
    await save(svc, model="old-model")
    pending = asyncio.create_task(svc.clients["primary"].complete(REQUEST, ConfirmationExtraction))
    await asyncio.wait_for(entered.wait(), 2)
    await save(svc, model="new-model")
    resume.set()
    assert (await pending).model == "old-model"
    assert (await svc.clients["primary"].complete(REQUEST, ConfirmationExtraction)).model == "new-model"


async def test_load_errors_do_not_log_connection_details(caplog: pytest.LogCaptureFixture) -> None:
    detail = "postgresql://fake-user:fake-password@fake-host/fake-db"

    class BrokenStore(FakeLlmSettingsStore):
        async def get(self, slot: LlmSlot) -> Any:
            raise OSError(detail)

    caplog.set_level(logging.WARNING)
    svc = service(store=BrokenStore(), LLM_MODEL="env-model", LLM_API_KEY="fake-env-key")
    await svc.load()
    assert (svc.clients["primary"].model_name, svc.clients["review"].model_name) == ("env-model", "env-model")
    assert detail not in caplog.text and "OSError" in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
