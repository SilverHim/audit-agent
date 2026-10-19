"""跨模块异常, 定义见 开发接口文档.md 第 4 节."""


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
    """新建客户时 ABN 已被另一个客户使用; client_id 与 legal_name 为已有的那个客户 (供提示改选)."""

    def __init__(self, abn: str, client_id: str, legal_name: str) -> None:
        super().__init__(f"ABN {abn} 已是客户 {client_id} ({legal_name}) 的 ABN")
        self.abn = abn
        self.client_id = client_id
        self.legal_name = legal_name


class RegistryUnavailable(Exception):
    """项目没有可查的登记来源 (如新建的客户没有数据集的登记 mock, ABR 与摘录未接入): ACC-04 转人工确认."""
