"""日志与脱敏。

注意：stdio 模式下 stdout 是 MCP 协议通道，日志必须只写 stderr，
否则会污染协议流导致客户端解析失败。
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

# 手机号 / 邮箱 / 身份证
_PATTERNS = [
    (re.compile(r"(1[3-9]\d)\d{4}(\d{4})"), r"\1****\2"),
    (re.compile(r"([\w.+-]{2})[\w.+-]*(@[\w-]+\.[\w.]+)"), r"\1***\2"),
    (re.compile(r"(\d{6})\d{8}(\d{3}[\dXx])"), r"\1********\2"),
]


def redact(text: str) -> str:
    if not text:
        return text
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        return True


def setup_logging(level: str = "INFO", log_file: Path | None = None) -> None:
    root = logging.getLogger()
    if root.handlers:
        return

    root.setLevel(level.upper())
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    stream.addFilter(RedactFilter())
    root.addHandler(stream)

    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler.addFilter(RedactFilter())
        root.addHandler(file_handler)
