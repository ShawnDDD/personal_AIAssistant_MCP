"""MCP 工具包装。"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any

from .errors import ToolError


def safe_tool(fn: Callable[..., Any]) -> Callable[..., Any]:
    """把 ToolError 转成结构化错误结果，其他异常照常抛出。

    用法：

        @mcp.tool()
        @safe_tool
        def my_tool(...): ...
    """

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except ToolError as exc:
            return exc.to_dict()

    return wrapper


def async_safe_tool(fn: Callable[..., Any]) -> Callable[..., Any]:
    """async 版本的 safe_tool，供 Playwright 这类异步工具使用。

    用法：

        @mcp.tool()
        @async_safe_tool
        async def my_tool(...): ...
    """

    @wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except ToolError as exc:
            return exc.to_dict()

    return wrapper
