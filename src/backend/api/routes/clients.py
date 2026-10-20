"""GET /clients: 新建项目时可选的客户 (每个客户一行, 不按数据集); POST /clients: 新建客户 (需求 R288);
GET /clients/abn-lookup: 表单里的 ABN 在线核对 (需求 R289)."""

from collections.abc import Callable
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends
from pydantic import ValidationError

from backend.agents.base import AgentDeps
from backend.agents.client_intel import normalize_name
from backend.api.deps import current_user, get_data_service, get_deps
from backend.api.errors import api_error
from backend.api.schemas import AbnLookupView, ClientCreate, ClientDataset, ClientSummary
from backend.contracts.errors import AbnTaken, RegistryUnavailable, VersionConflict
from backend.contracts.models import ClientProfile, ProfileSource, User
from backend.core.config import get_settings
from backend.services.abn import abn_problem, dataset_profiles, normalize_abn
from backend.services.data_service import DataService

router = APIRouter(prefix="/clients", tags=["clients"])


def dataset_clients(
    deps: AgentDeps, has_demo: Callable[[str], bool] = lambda _: False
) -> dict[str, tuple[ClientProfile, list[ClientDataset]]]:
    """数据集客户 (DatasetLoader 现算), 按画像 id 归并: id → (数据集画像, 该客户的数据集). 画像或期间读不了的数据集
    跳过 (同 GET /datasets); 同一客户有多个数据集时画像取数据集名最小的那个. has_demo 判断数据集带不带演示资料
    (ClientDataset.demo), 缺省都不带 (只要归并结果的调用方不必给)."""
    default = get_settings().DEMO_DATASET
    found: dict[str, tuple[ClientProfile, list[ClientDataset]]] = {}
    for name in sorted(deps.datasets.datasets()):
        try:
            profile = deps.datasets.client_profile(name)
            period = deps.datasets.period(name)
        except (OSError, ValueError):  # 文件缺失或不合格 (ValidationError 是 ValueError 的子类)
            continue
        start, end = period if period is not None else (None, None)
        entry = found.setdefault(profile.id, (profile, []))
        entry[1].append(
            ClientDataset(
                dataset=name, period_start=start, period_end=end, default=name == default, demo=has_demo(name)
            )
        )
    return found


@router.get("")
async def list_clients(
    _user: Annotated[User, Depends(current_user)],
    deps: Annotated[AgentDeps, Depends(get_deps)],
    service: Annotated[DataService, Depends(get_data_service)],
) -> list[ClientSummary]:
    """每个客户一行: 库里各客户的最新一版, 并上还没进库的数据集客户. origin 取第 1 版的来源, 老版本没记时数据集
    客户为 dataset. 数据集带不带演示资料 (demo) 同数据管理页概览的 demo.available (DataService.demo.has_supplement,
    只查目录在不在, 需求 R287). 排序: 有带演示资料的数据集的 (演示公司) 在前, 再是有默认数据集的, 再按名称 (不分
    大小写) 与编号."""
    demo = service.demo
    datasets = dataset_clients(deps, demo.has_supplement if demo is not None else lambda _: False)
    stored = {entry.profile.id: entry for entry in await deps.engagement_store.clients()}
    rows: list[ClientSummary] = []
    for client_id in set(datasets) | set(stored):
        entry, from_dataset = stored.get(client_id), datasets.get(client_id)
        profile = entry.profile if entry is not None else from_dataset[0] if from_dataset is not None else None
        assert profile is not None
        origin = entry.first_source if entry is not None else None
        if origin is None and from_dataset is not None:
            origin = ProfileSource.dataset
        rows.append(
            ClientSummary(
                client_id=client_id,
                legal_name=profile.legal_name,
                abn=profile.abn,
                origin=origin,
                latest_version=entry.profile.version if entry is not None else None,
                datasets=from_dataset[1] if from_dataset is not None else [],
            )
        )
    rows.sort(
        key=lambda row: (
            not any(d.demo for d in row.datasets),
            not any(d.default for d in row.datasets),
            row.legal_name.casefold(),
            row.client_id,
        )
    )
    return rows


@router.get("/abn-lookup")
async def abn_lookup(
    abn: str,
    _user: Annotated[User, Depends(current_user)],
    deps: Annotated[AgentDeps, Depends(get_deps)],
    legal_name: str = "",
) -> AbnLookupView:
    """新建客户与改画像的表单里的 ABN 在线核对: ABN 去空格后不是 11 位数字或校验位不对 422 INVALID_VALUE (同
    POST /clients, 不发请求); 否则经 ABR ABN Lookup 查 (同一 ABN 短时内取缓存), 返回 found 与登记信息, 或
    not_found; 没配 GUID, 不可达, GUID 不被认可, 服务出错时为 unavailable 与原因类别 (不报错, 建项目后 ACC-04 转人工
    确认). 给了 legal_name 且登记名称公开时, 按 ACC-04 的规则比较是否一致. 不写留痕 (还没有项目; 建项目后 ACC-04
    照常查并留痕)."""
    problem = abn_problem(abn)
    if problem is not None:
        raise api_error("INVALID_VALUE", problem)
    try:
        record = await deps.abn_lookup.abn(normalize_abn(abn))
    except RegistryUnavailable as exc:
        return AbnLookupView(status="unavailable", reason=exc.reason)
    if record is None:
        return AbnLookupView(status="not_found")
    matches = None
    if legal_name.strip() and record.entity_name:
        matches = normalize_name(record.entity_name) == normalize_name(legal_name)
    return AbnLookupView(status="found", record=record, name_matches=matches)


@router.post("", status_code=201)
async def create_client(
    body: ClientCreate,
    user: Annotated[User, Depends(current_user)],
    deps: Annotated[AgentDeps, Depends(get_deps)],
) -> ClientSummary:
    """新建客户, 写画像第 1 版 (来源 manual, 写入人为当前用户). 按顺序拒绝: 法定名称去空白后为空 422 MISSING_FIELD;
    ABN 去空格后不是 11 位数字或校验位不对 (ABR 公布的算法) 422 INVALID_VALUE; 其余字段不合 ClientProfile 422
    INVALID_VALUE; 同一 ABN 已是别的客户 (库里各客户的最新一版与数据集画像) 409 ABN_TAKEN, detail 带已有的
    client_id 与 legal_name. 编号为 CLIENT-M-{12 位十六进制}, 撞号时 409 VERSION_MISMATCH, 不覆盖."""
    legal_name = body.legal_name.strip()
    if not legal_name:
        raise api_error("MISSING_FIELD", "法定名称不能为空")
    problem = abn_problem(body.abn)
    if problem is not None:
        raise api_error("INVALID_VALUE", problem)
    client_id = f"CLIENT-M-{uuid4().hex[:12]}"
    try:
        profile = ClientProfile.model_validate(
            {
                **body.model_dump(),
                "legal_name": legal_name,
                "abn": normalize_abn(body.abn),
                "id": client_id,
                "version": 1,
            }
        )
    except ValidationError as exc:
        raise api_error("INVALID_VALUE", str(exc)) from exc
    known = dataset_profiles(deps.datasets)
    try:
        saved = await deps.engagement_store.create_client(profile, created_by=user.id, dataset_profiles=known)
    except AbnTaken as exc:
        raise api_error("ABN_TAKEN", str(exc), client_id=exc.client_id, legal_name=exc.legal_name) from exc
    except VersionConflict as exc:
        raise api_error("VERSION_MISMATCH", f"客户编号 {client_id} 已存在, 请重试") from exc
    return ClientSummary(
        client_id=saved.id,
        legal_name=saved.legal_name,
        abn=saved.abn,
        origin=ProfileSource.manual,
        latest_version=saved.version,
        datasets=[],
    )
