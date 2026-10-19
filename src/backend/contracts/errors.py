"""跨模块异常, 定义见 开发接口文档.md 第 4 节."""

from typing import Literal

# 查不了登记的原因 (类别, 不带原始响应): 没有登记来源 (没有数据集的登记 mock), 没配 ABN Lookup 的 GUID, ABN Lookup
# 不可达 (连接失败, 超时), GUID 不被认可 (未注册, 注册已取消), ABN Lookup 服务出错 (HTTP 错误, 其余异常, 响应读不了);
# 新建的客户的 ASIC (需求 R290): ABN 登记没有 ACN (不是公司, 不要求 ASIC 摘录), 公司而没有可用的 ASIC 摘录 (标了
# 客户无此类资料, 或上传的摘录抽取没成)
AbnLookupReason = Literal["no_source", "not_configured", "unreachable", "rejected", "service_error"]  # ABN 一侧的类别
RegistryReason = Literal[AbnLookupReason, "not_company", "no_extract"]


class VersionConflict(Exception):
    """同 id 的版本不连续, 或 reducer 收到更低版本."""


class EngagementLocked(Exception):
    """项目已 locked, 拒绝写入."""


class RuleError(Exception):
    """规则 SQL 缺必需列, 报错或超时."""


class DecisionRejected(Exception):
    """DecisionSubmit 校验不通过; code 见 开发接口文档.md 第 7, 10 节的错误码表."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


class DataRejected(DecisionRejected):
    """数据管理的写操作被拒; extra 随错误体一并返回, 如 DATA_NOT_READY 的 missing_tables.

    沿用 DecisionRejected 的错误码表 (开发接口文档第 10 节), 接口层按同一规则映射 HTTP 状态.
    """

    def __init__(self, code: str, message: str = "", **extra: object) -> None:
        super().__init__(code, message)
        self.extra = extra


class AbnTaken(Exception):
    """新建客户或 ACC-01 改画像时 ABN 已被另一个客户使用; client_id 与 legal_name 为已有的那个客户 (供提示改选)."""

    def __init__(self, abn: str, client_id: str, legal_name: str) -> None:
        super().__init__(f"ABN {abn} 已是客户 {client_id} ({legal_name}) 的 ABN")
        self.abn = abn
        self.client_id = client_id
        self.legal_name = legal_name


class RegistryUnavailable(Exception):
    """查不了登记 (没有登记来源, ABN Lookup 未配置, 不可达, 不认可 GUID 或出错): ACC-04 转人工确认. reason 为类别;
    消息只写类别的说明, 不带原始响应 (ABN Lookup 的响应会回显 GUID)."""

    def __init__(self, message: str = "", reason: RegistryReason = "no_source") -> None:
        super().__init__(message or reason)
        self.reason: RegistryReason = reason
