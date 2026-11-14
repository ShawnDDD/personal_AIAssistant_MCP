"""MCP Server 启动公共逻辑。"""

from __future__ import annotations

from typing import Any

from .config import get_settings
from .logging import setup_logging


def build_server(name: str, instructions: str | None = None) -> Any:
    """创建 MCP Server 实例，并初始化日志。

    mcp 2.x 把 FastMCP 更名为 MCPServer（mcp.server.mcpserver）。
    这里延迟导入，保证 common 包在没装 mcp 时也能被单独引用（例如测试）。
    """
    from mcp.server.mcpserver import MCPServer

    settings = get_settings()
    setup_logging(settings.log_level, settings.log_file)
    return MCPServer(name, instructions=instructions)


def run(mcp: Any) -> None:
    """以 stdio 启动。P5 需要远程部署时再扩展 HTTP 传输。"""
    mcp.run()
