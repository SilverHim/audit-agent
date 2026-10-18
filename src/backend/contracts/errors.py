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
