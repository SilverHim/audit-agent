"""跨模块异常，定义见 开发接口文档.md 第 4 节。"""


class VersionConflict(Exception):
    """同 id 的版本不连续，或 reducer 收到更低版本。"""


class EngagementLocked(Exception):
    """项目已 locked，拒绝写入。"""


class RuleError(Exception):
    """规则 SQL 缺必需列、报错或超时。"""


class DecisionRejected(Exception):
    """DecisionSubmit 校验不通过；code 见 开发接口文档.md 第 7、10 节的错误码表。"""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message
