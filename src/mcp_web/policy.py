"""只读护栏。

需求以查询为主，所以默认拦截会改变数据的点击。命中词表即拒绝，
需要写操作时必须在流程里声明 mode: mutate，或调用时显式 allow_dangerous=true。
"""

from __future__ import annotations

from typing import Any

from mcp_common.config import get_settings
from mcp_common.errors import ToolError

DEFAULT_BLOCKED = [
    "提交",
    "保存",
    "删除",
    "审批通过",
    "驳回",
    "发布",
    "支付",
    "下单",
    "退订",
    "作废",
    "停用",
]


class Policy:
    def __init__(self, blocked: list[str] | None = None) -> None:
        self.blocked = [word for word in (blocked or DEFAULT_BLOCKED) if word]

    def check(self, text: str | None, allow: bool = False) -> None:
        if allow or not text:
            return

        normalized = text.strip()
        for word in self.blocked:
            if word in normalized:
                raise ToolError(
                    f"只读模式下拦截了包含「{word}」的点击：{normalized[:60]}。"
                    "确认要执行时，请在流程里设置 mode: mutate，或调用时传 allow_dangerous=true。",
                    code="BLOCKED_MUTATION",
                )


def load_policy() -> Policy:
    """读取 flows/_policy.yaml 的 blocked 词表；文件缺失时用默认词表。"""
    path = get_settings().flows_dir / "_policy.yaml"
    if not path.is_file():
        return Policy()

    import yaml

    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return Policy()

    blocked = data.get("blocked") if isinstance(data, dict) else None
    return Policy(blocked if isinstance(blocked, list) else None)
