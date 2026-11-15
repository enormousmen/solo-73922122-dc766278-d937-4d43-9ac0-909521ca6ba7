"""自定义异常，统一映射为 HTTP 400 响应。"""


class ValidationError(Exception):
    """输入不满足约束时抛出。"""


class ScriptRejected(Exception):
    """SQL 脚本违反安全/确定性要求时抛出。"""


class JobError(Exception):
    """任务状态相关错误。"""


class DependencyRejected(Exception):
    """脚本依赖核查无法完成（语句不支持或无法判定）。

    携带出错语句的序号与行列位置，便于页面精确定位；
    出现该错误时不输出任何部分排程。
    """

    def __init__(self, message: str, statement_index: int | None = None,
                 line: int | None = None, column: int | None = None) -> None:
        super().__init__(message)
        self.statement_index = statement_index
        self.line = line
        self.column = column

    def to_dict(self) -> dict[str, object]:
        return {
            "detail": str(self),
            "statement_index": self.statement_index,
            "line": self.line,
            "column": self.column,
        }
