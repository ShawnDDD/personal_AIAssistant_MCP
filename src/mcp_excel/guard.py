"""写入安全护栏：路径校验、备份、格式丢失预警。"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp_common.config import get_settings
from mcp_common.errors import ToolError

SUPPORTED_SUFFIXES = {".xlsx", ".xlsm"}


def ensure_workbook_path(path: str | Path, must_exist: bool = True) -> Path:
    if not path:
        raise ToolError("path 不能为空", code="EMPTY_PATH")

    resolved = Path(path).expanduser().resolve()

    if resolved.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise ToolError(
            f"仅支持 .xlsx / .xlsm，收到「{resolved.suffix or '无扩展名'}」。"
            "旧版 .xls 请先用 Excel 另存为 .xlsx。",
            code="UNSUPPORTED_FORMAT",
        )
    if must_exist and not resolved.is_file():
        raise ToolError(f"文件不存在：{resolved}", code="FILE_NOT_FOUND")
    if not must_exist and not resolved.parent.is_dir():
        raise ToolError(f"目标目录不存在：{resolved.parent}", code="DIR_NOT_FOUND")

    return resolved


def backup_file(path: Path, keep: int | None = None) -> Path:
    """写入前备份原文件，并按份数上限清理旧备份。"""
    settings = get_settings()
    settings.backup_dir.mkdir(parents=True, exist_ok=True)

    # 精确到毫秒，避免同一秒内多次写入互相覆盖
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    target = settings.backup_dir / f"{path.stem}.{stamp}{path.suffix}"
    shutil.copy2(path, target)

    _prune(path.stem, path.suffix, keep if keep is not None else settings.backup_keep)
    return target


def _prune(stem: str, suffix: str, keep: int) -> None:
    settings = get_settings()
    prefix = f"{stem}."
    files = [
        item
        for item in settings.backup_dir.iterdir()
        if item.is_file() and item.suffix == suffix and item.name.startswith(prefix)
    ]
    files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    for old in files[max(keep, 1) :]:
        old.unlink(missing_ok=True)


def detect_lossy_features(workbook: Any) -> list[str]:
    """openpyxl 读入再保存会丢失的对象，提前告知调用方。"""
    warnings: list[str] = []

    for sheet in workbook.worksheets:
        if getattr(sheet, "_images", None):
            warnings.append(f"工作表「{sheet.title}」含图片，写回后可能丢失")
        if getattr(sheet, "_charts", None):
            warnings.append(f"工作表「{sheet.title}」含图表，写回后可能丢失")
        if getattr(sheet, "_pivots", None):
            warnings.append(f"工作表「{sheet.title}」含数据透视表，写回后可能丢失")

    if getattr(workbook, "_external_links", None):
        warnings.append("工作簿含外部链接，写回后可能丢失")

    return warnings


def save_workbook(workbook: Any, path: Path) -> None:
    try:
        workbook.save(path)
    except PermissionError as exc:
        raise ToolError(
            f"写入失败：{path.name} 正被占用，请先在 Excel 中关闭该文件后重试。",
            code="FILE_LOCKED",
        ) from exc
