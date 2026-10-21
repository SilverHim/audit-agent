"""数据管理接口 (需求 R10-R16, R277): /engagements/{eid}/data 下的概览, 导入, 智能导入的批次, 补交请求与就绪确认,
Xero 的连接, 选组织, 同步与断开 (需求 R279; 授权回调在 integrations.py), Xero 组织信息与客户画像的核对 (需求 R291);
/data/schemas. 另有演示数据导入 (需求 R270)."""

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile

from backend.agents.base import AgentDeps
from backend.api.deps import current_user, get_data_service, get_decisions, get_deps, get_xero, owned_engagement
from backend.api.errors import api_error, catch_api
from backend.api.schemas import (
    AccountCategoryIn,
    DataReadyIn,
    DemoImportIn,
    ImportSheet,
    RequestUnavailableIn,
    XeroAuthorize,
    XeroDisconnected,
    XeroProfileCheckIn,
    XeroSyncStarted,
    XeroTenantIn,
)
from backend.contracts.data import (
    BatchItemEdit,
    CategoryVersion,
    DataReadiness,
    DataRequest,
    DataSummary,
    DemoImportResult,
    ImportBatch,
    ImportCommit,
    ImportPreview,
    ImportResult,
    TableKind,
    TablePage,
    TableSchema,
    XeroProfileCheck,
    XeroSource,
    XeroTenant,
)
from backend.contracts.models import Engagement, User
from backend.orchestration.decisions import GraphDecisionSubmit
from backend.services import data_import
from backend.services.data_service import DataService, schemas
from backend.services.xero_link import XeroLink

router = APIRouter(prefix="/engagements/{eid}/data", tags=["data"])
schemas_router = APIRouter(prefix="/data", tags=["data"])  # 不按项目的数据管理接口


@schemas_router.get("/schemas")
async def table_schemas(user: Annotated[User, Depends(current_user)]) -> list[TableSchema]:
    """各账表的导入字段, 按 TableKind 顺序; 字段名与 persistence/models.py 中 FINANCIAL 各表的列一致."""
    return schemas()


@router.post("/imports")
async def start_import(
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    user: Annotated[User, Depends(current_user)],
    service: Annotated[DataService, Depends(get_data_service)],
    files: Annotated[list[UploadFile], File(description="单个 CSV, XLSX 或 XLS 文件, 不超过 50 MB")],
    request_id: Annotated[str | None, Form()] = None,
) -> ImportPreview:
    """上传账表文件并解析第一个工作表, 返回列, 样例与建议. 不止一个文件或类型不支持 422 INVALID_VALUE;
    超过 50 MB 413 FILE_TOO_LARGE; 项目锁定 423."""
    if len(files) != 1:
        raise api_error("INVALID_VALUE", "每次导入一个文件")
    upload = files[0]
    data = await upload.read(data_import.MAX_BYTES + 1)
    if len(data) > data_import.MAX_BYTES:
        raise api_error("FILE_TOO_LARGE", f"{upload.filename} 超过 50 MB")
    async with catch_api():
        return await service.start_import(
            engagement.id, upload.filename or "upload", data, actor=user.id, request_id=request_id
        )


@router.put("/imports/{import_id}/sheet")
async def change_import_sheet(
    import_id: str,
    body: ImportSheet,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    service: Annotated[DataService, Depends(get_data_service)],
) -> ImportPreview:
    """换工作表, 返回该表的解析结果与建议. 导入不存在 404; 已提交或工作表不存在 422 INVALID_VALUE."""
    async with catch_api():
        return await service.change_sheet(engagement.id, import_id, body.sheet)


@router.post("/imports/{import_id}/commit", status_code=202)
async def commit_import(
    import_id: str,
    body: ImportCommit,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    user: Annotated[User, Depends(current_user)],
    service: Annotated[DataService, Depends(get_data_service)],
) -> ImportResult:
    """按列对应解析全表并写入新数据版本, 原文件登记为证据文件. 解析失败 422 IMPORT_PARSE_ERROR (detail.errors 为前
    20 处 {row, column, value}); 对应不可用 422 INVALID_VALUE 或 MISSING_FIELD; 忙 409; 锁定 423; 导入不存在 404."""
    async with catch_api():
        return await service.commit_import(engagement.id, import_id, body, user.id)


@router.post("/batches", status_code=202)
async def start_batch(
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    user: Annotated[User, Depends(current_user)],
    service: Annotated[DataService, Depends(get_data_service)],
    files: Annotated[
        list[UploadFile], File(description="一批文件: CSV, XLSX, XLS, PDF, DOCX, PNG, JPG 等, 单个不超过 50 MB")
    ],
    request_id: Annotated[str | None, Form()] = None,
) -> ImportBatch:
    """智能导入: 上传一批文件 (表格与其他文件混着), A3 在后台逐项识别, 生成确认单. 返回批次 (processing), 前端轮询
    GET /data/batches/{batch_id} 直到 ready. 补交卡片上的上传带 request_id. 确认前不写任何财务表, 文件暂存
    (status staged) 不被程序使用. 类型不支持 422 INVALID_VALUE; 单个超过 50 MB 413 FILE_TOO_LARGE; 锁定 423;
    忙 409."""
    contents = []
    for upload in files:
        data = await upload.read(data_import.MAX_BYTES + 1)
        if len(data) > data_import.MAX_BYTES:
            raise api_error("FILE_TOO_LARGE", f"{upload.filename} 超过 50 MB")
        contents.append((upload.filename or "upload", data))
    async with catch_api():
        return await service.start_batch(engagement.id, contents, actor=user.id, request_id=request_id)


@router.get("/batches")
async def list_batches(
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    service: Annotated[DataService, Depends(get_data_service)],
) -> list[ImportBatch]:
    """还没写入的智能导入 (识别中, 待确认与识别出错的), 按上传时间升序; 关掉确认单后从这里回到确认单."""
    async with catch_api():
        return await service.open_batches(engagement.id)


@router.get("/batches/{batch_id}")
async def get_batch(
    batch_id: str,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    service: Annotated[DataService, Depends(get_data_service)],
) -> ImportBatch:
    """批次与确认单: 每项的文件名与工作表, 识别为什么, 将写入的行数, 读到的公司与期间, 把握, 问题 (issues, 标红)
    与是否勾选. processing 时前端轮询; failed 时 error 为原因, 停止轮询, 可重新识别. 不存在 404."""
    async with catch_api():
        return await service.batch(engagement.id, batch_id)


@router.put("/batches/{batch_id}/items/{item_id}")
async def edit_batch_item(
    batch_id: str,
    item_id: str,
    body: BatchItemEdit,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    user: Annotated[User, Depends(current_user)],
    service: Annotated[DataService, Depends(get_data_service)],
) -> ImportBatch:
    """调整确认单的一项 (换规整方案, 改文件类型并可写理由, 勾选或取消), 返回整张确认单; 改了文件类型的批次回到
    processing, A3 在后台按新类型重新识别与抽取, 前端照常轮询. 理由选填 (Issue #61). 锁定 423; 忙 409 (识别中的
    批次由识别作业持锁); 批次或该项不存在 404; 批次不在待确认, 字段不适用 422 INVALID_VALUE."""
    async with catch_api():
        return await service.edit_batch_item(engagement.id, batch_id, item_id, body, user.id)


@router.post("/batches/{batch_id}/retry", status_code=202)
async def retry_batch(
    batch_id: str,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    service: Annotated[DataService, Depends(get_data_service)],
) -> ImportBatch:
    """重新识别: 识别作业出错 (failed) 或进程退出后停在识别中的批次, 由 A3 在后台从没做完的文件接着做; 返回置回
    processing 的批次, 前端照常轮询. 锁定 423; 忙 409 (识别中的批次由识别作业持锁); 批次不存在 404; 待确认或已写入
    422 INVALID_VALUE."""
    async with catch_api():
        return await service.retry_batch(engagement.id, batch_id)


@router.post("/batches/{batch_id}/confirm", status_code=202)
async def confirm_batch(
    batch_id: str,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    user: Annotated[User, Depends(current_user)],
    service: Annotated[DataService, Depends(get_data_service)],
) -> ImportBatch:
    """确认导入: 一个事务里写入所勾选的全部 (账表一个新数据版本, 文件按确认的类型登记, 合同与回函的抽取结果, 批次
    置 committed; 没勾选的文件转为未导入, files 里记 not_imported), 之后写留痕并体检一次; 返回写入后的批次
    (committed, 再确认返回原结果). 锁定 423; 忙 409 (识别中的批次由识别作业持锁); 批次不存在 404; 不在待确认, 勾选
    了不是账表的项或同一张表两项 422 INVALID_VALUE; 勾选的文件没有类型 422 MISSING_FIELD; 方案套不上 422
    INVALID_VALUE 或 MISSING_FIELD; 解析失败 422 IMPORT_PARSE_ERROR (detail.errors 为前 20 处)."""
    async with catch_api():
        return await service.confirm_batch(engagement.id, batch_id, user.id)


@router.get("/summary")
async def summary(
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    service: Annotated[DataService, Depends(get_data_service)],
) -> DataSummary:
    """数据管理页的概览. processing 不为 0 时前端每 3 秒轮询."""
    async with catch_api():
        return await service.summary(engagement.id)


@router.get("/tables/{kind}")
async def table_page(
    kind: TableKind,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    service: Annotated[DataService, Depends(get_data_service)],
    q: str | None = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> TablePage:
    """查看一张账表: {total, columns, rows}. q 为不分大小写的包含搜索 (科目表查代码与名称); limit 不超过 200.
    科目表的行另带 category, category_basis, balance_current_cents, balance_prior_cents, edited. 账表数值不能改,
    只能经导入写新版本."""
    async with catch_api():
        return await service.table_page(engagement.id, kind, q, offset, limit)


@router.put("/accounts/{code}")
async def set_account_category(
    code: str,
    body: AccountCategoryIn,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    user: Annotated[User, Depends(current_user)],
    service: Annotated[DataService, Depends(get_data_service)],
) -> CategoryVersion:
    """修改科目的标准分类, 生成新的一版并返回; 数据版本 +1, 写 data 留痕 (action edit). 理由选填 (Issue #61).
    分类不属于该科目的类别或与当前相同 422 INVALID_VALUE; 科目不存在 404; 锁定 423; 忙 409."""
    async with catch_api():
        return await service.set_account_category(engagement.id, code, body.category, body.reason, user.id)


@router.get("/accounts/{code}/history")
async def account_history(
    code: str,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    service: Annotated[DataService, Depends(get_data_service)],
) -> list[CategoryVersion]:
    """科目标准分类的各版, 按版本升序; 科目不存在 404."""
    async with catch_api():
        return await service.account_history(engagement.id, code)


@router.get("/requests")
async def data_requests(
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    deps: Annotated[AgentDeps, Depends(get_deps)],
) -> list[DataRequest]:
    """A3 提出的补交资料请求 (含已收到的), 按提出时间升序; status 为 open 的是待补交. 补交时账表走导入
    (POST /data/imports), 文件走上传 (POST /documents), 都带 request_id."""
    async with catch_api():
        return await deps.data.requests(engagement.id)


@router.post("/requests/{request_id}/unavailable")
async def mark_request_unavailable(
    request_id: str,
    body: RequestUnavailableIn,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    user: Annotated[User, Depends(current_user)],
    service: Annotated[DataService, Depends(get_data_service)],
) -> DataRequest:
    """标 "客户无此类资料" (需求 R280): 请求转 unavailable, 等它的程序记未执行 (无数据). 返回改后的请求.
    理由选填 (Issue #61). 请求不存在 404; 不是待补交 409 REQUEST_NOT_OPEN; 就绪必需的表 422 INVALID_VALUE; 锁定
    423; 忙 409 ENGAGEMENT_BUSY."""
    async with catch_api():
        return await service.mark_unavailable(engagement.id, request_id, body.reason, user.id)


@router.post("/ready", status_code=202)
async def ready(
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    user: Annotated[User, Depends(current_user)],
    service: Annotated[DataService, Depends(get_data_service)],
    body: DataReadyIn | None = None,
) -> DataReadiness:
    """确认数据已齐, 越过 P1 开始前的等待. 理由选填, 最近一次体检有不通过的项时也可不写 (Issue #61). 不在等数据
    409 NOT_AWAITING_DATA; 缺必需表 409 DATA_NOT_READY (detail.missing_tables); 项目锁定 423, 忙 409
    ENGAGEMENT_BUSY. 返回确认后的就绪状态 (含理由与未通过项), 后台已在推进."""
    async with catch_api():
        return await service.confirm_ready(engagement.id, user.id, body.reason if body else None)


@router.post("/demo-import", status_code=202)
async def demo_import(
    body: DemoImportIn,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    user: Annotated[User, Depends(current_user)],
    service: Annotated[DataService, Depends(get_data_service)],
) -> DemoImportResult:
    """导入演示资料的一个阶段 (需求 R269, R270): 后台按补造清单写入账表, 上传文件交 A3 处理并补登记, 做完体检;
    返回 status started 的结果 (要写的表与要上传的文件), 作业期间概览的 processing 计入. 结果看之后概览的
    demo.last 与 stages_done. 阶段不认识或为 manual, 数据集没有演示资料 (Numbat, 没有补造目录的快照, 没有数据集的
    新客户), 补造目录与快照不符 422 INVALID_VALUE; 锁定 423; 忙 409 ENGAGEMENT_BUSY."""
    async with catch_api():
        return await service.demo_import(engagement.id, body.stage, user.id)


# ── Xero 网页连接 (需求 R279) ──


@router.get("/xero/connect")
async def xero_connect(
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    user: Annotated[User, Depends(current_user)],
    xero: Annotated[XeroLink, Depends(get_xero)],
) -> XeroAuthorize:
    """Xero 授权地址, 前端跳过去; state 绑定本项目与当前用户, 10 分钟内有效, 只能用一次. 授权后 Xero 回调
    /integrations/xero/callback, 再 302 回数据管理页. 项目锁定 423; 未配 XERO_CLIENT_ID 503 XERO_NOT_CONFIGURED;
    加密主密钥不可用 503 SETTINGS_KEY_MISSING / SETTINGS_KEY_INVALID."""
    async with catch_api():
        return XeroAuthorize(authorize_url=await xero.authorize(engagement.id, user.id))


@router.get("/xero/tenants")
async def xero_tenants(
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    xero: Annotated[XeroLink, Depends(get_xero)],
) -> list[XeroTenant]:
    """授权给到的组织 [{id, name}] (连接时存下的, 不调 Xero). 没有连接 409 XERO_NOT_CONNECTED."""
    async with catch_api():
        return await xero.tenants(engagement.id)


@router.put("/xero/tenant")
async def xero_tenant(
    body: XeroTenantIn,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    user: Annotated[User, Depends(current_user)],
    xero: Annotated[XeroLink, Depends(get_xero)],
) -> XeroSource:
    """选定要同步的组织, 返回概览里的 Xero 连接. 不是授权给到的组织 422 INVALID_VALUE; 没有连接 409
    XERO_NOT_CONNECTED; 锁定 423; 忙 409."""
    async with catch_api():
        return await xero.choose_tenant(engagement.id, body.tenant_id, user.id)


@router.post("/xero/sync", status_code=202)
async def xero_sync(
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    user: Annotated[User, Depends(current_user)],
    xero: Annotated[XeroLink, Depends(get_xero)],
) -> XeroSyncStarted:
    """开始同步: 后台按项目审计期间与上期期末取数, 写成一个新数据版本 (来源 xero), 写完体检; 返回计划写的表, 期间
    概览里这些表为 converting, processing 计数; 实际写的表看完成后概览的 scope (没取到银行对账单时银行这一对不写).
    没有连接 409 XERO_NOT_CONNECTED; 多个组织未选 409 XERO_TENANT_REQUIRED; 授权失效 409 XERO_RECONNECT_REQUIRED;
    Xero 连不上 502 XERO_UNAVAILABLE; 锁定 423; 忙 (含另一同步或导入) 409 ENGAGEMENT_BUSY."""
    async with catch_api():
        return XeroSyncStarted(tables=await xero.sync(engagement.id, user.id))


@router.post("/xero/profile-check")
async def xero_profile_check(
    body: XeroProfileCheckIn,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    user: Annotated[User, Depends(current_user)],
    xero: Annotated[XeroLink, Depends(get_xero)],
    decisions: Annotated[GraphDecisionSubmit, Depends(get_decisions)],
) -> XeroProfileCheck | None:
    """对 Xero 组织信息与客户画像不一致的选择, 返回处理后的核对 (概览 sources.xero.profile_check 的新值). keep 可写理由
    (选填, Issue #61); update 按 Xero 改对不上的项出画像新版本 (来源 xero): 停在 ACC-01 时本项目采用并重跑
    ACC-04, 其余时候只给客户写新版本. key 不是当前待处理的核对 409 PROFILE_CHECK_STALE; 锁定 423; 没有连接
    409 XERO_NOT_CONNECTED; Xero 带出的 ABN 校验位不对 422 INVALID_VALUE, 已是别的客户的 409 ABN_TAKEN; 停在 ACC-01 时
    同更新客户资料 (忙 409 ENGAGEMENT_BUSY, 版本过期 409 VERSION_MISMATCH)."""
    async with catch_api():
        return await xero.resolve_profile(engagement.id, body.key, body.decision, body.reason, user.id, decisions)


@router.delete("/xero")
async def xero_disconnect(
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    user: Annotated[User, Depends(current_user)],
    xero: Annotated[XeroLink, Depends(get_xero)],
) -> XeroDisconnected:
    """断开: 调 Xero 撤销接口, 删除本项目的连接与令牌; 撤销失败也删 (revoked 为 false, 须到 Xero 后台手动移除).
    已同步的数据与版本保留, 概览回到 not_connected. 锁定的项目也可以断开. 没有连接 409 XERO_NOT_CONNECTED; 忙 409."""
    async with catch_api():
        return XeroDisconnected(revoked=await xero.disconnect(engagement.id, user.id))
