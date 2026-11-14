"""可预期的工具错误。

这类错误会被 safe_tool 捕获并转成 {"ok": false, ...} 返回给模型，
避免一次参数写错就让整个 MCP 调用链失败。
"""

from __future__ import annotations


class ToolError(Exception):
    def __init__(self, message: str, code: str = "TOOL_ERROR") -> None:
        super().__init__(message)
        self.message = message
        self.code = code

    def to_dict(self) -> dict:
        return {"ok": False, "error": self.message, "error_code": self.code}
