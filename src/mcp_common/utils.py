"""通用工具函数。"""

from __future__ import annotations

import datetime as dt
import math
from decimal import Decimal
from typing import Any


def to_jsonable(value: Any) -> Any:
    """把 openpyxl / pandas 的值转成可 JSON 序列化的类型。

    MCP 工具返回值最终要序列化为 JSON，datetime、Decimal、NaN 直接返回会报错。
    """
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return None
        if math.isinf(value):
            return str(value)
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    return str(value)


def truncate(items: list[Any], limit: int) -> tuple[list[Any], bool]:
    """截断返回给模型的长列表，避免单次响应过大。"""
    if len(items) <= limit:
        return items, False
    return items[:limit], True
