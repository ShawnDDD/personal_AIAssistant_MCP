"""写入：追加、更新插入、单元格/表格写入、模板填充。

所有写操作统一遵守：
1. 默认 dry_run（来自配置），只返回计划变更，不落盘；
2. 真正写入前自动备份原文件；
3. 写回前检测会丢失的对象（图片/图表/透视表/外部链接）并给出告警。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from mcp_common.config import get_settings
from mcp_common.errors import ToolError
from mcp_common.utils import to_jsonable, truncate

from .guard import backup_file, detect_lossy_features, ensure_workbook_path, save_workbook
from .reader import ERROR_VALUES, find_header_row, get_sheet, row_headers

PREVIEW_LIMIT = 50


def _open(path: Path):
    return load_workbook(path, data_only=False, keep_vba=path.suffix.lower() == ".xlsm")


def _norm(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _as_list(value: str | list[str] | None) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [str(item).strip() for item in value if str(item).strip()]


def _resolve_dry_run(dry_run: bool | None) -> bool:
    return get_settings().dry_run_default if dry_run is None else dry_run


def _header_map(sheet: Any, header_row: int) -> dict[str, int]:
    return {
        name: index
        for index, name in enumerate(row_headers(sheet, header_row), start=1)
    }


def _last_data_row(sheet: Any, header_row: int) -> int:
    """从下往上找最后一行真正有值的行，避免 max_row 的尾部空行影响追加位置。"""
    for row in range(sheet.max_row, header_row, -1):
        if any(
            sheet.cell(row, column).value not in (None, "")
            for column in range(1, sheet.max_column + 1)
        ):
            return row
    return header_row


def _check_columns(rows: list[dict], header_map: dict[str, int]) -> None:
    unknown = sorted({str(key) for row in rows for key in row} - set(header_map))
    if unknown:
        raise ToolError(
            f"以下列不在表头中：{', '.join(unknown)}；可用列：{', '.join(header_map)}",
            code="UNKNOWN_COLUMN",
        )


def _check_keys(keys: list[str], header_map: dict[str, int]) -> None:
    missing = [key for key in keys if key not in header_map]
    if missing:
        raise ToolError(f"主键列不存在：{', '.join(missing)}", code="UNKNOWN_COLUMN")


def append_rows(
    path: str,
    sheet: str | None = None,
    rows: list[dict] | None = None,
    dedup_key: list[str] | None = None,
    dry_run: bool | None = None,
    header_row: int | None = None,
) -> dict:
    if not rows:
        raise ToolError("rows 不能为空", code="EMPTY_INPUT")

    resolved = ensure_workbook_path(path)
    dry = _resolve_dry_run(dry_run)

    workbook = _open(resolved)
    worksheet = get_sheet(workbook, sheet)
    header_row = header_row or find_header_row(worksheet)
    header_map = _header_map(worksheet, header_row)

    _check_columns(rows, header_map)

    keys = _as_list(dedup_key)
    _check_keys(keys, header_map)

    existing: set[tuple] = set()
    if keys:
        for row in range(header_row + 1, worksheet.max_row + 1):
            existing.add(
                tuple(_norm(worksheet.cell(row, header_map[key]).value) for key in keys)
            )

    to_write: list[dict] = []
    batch_keys: set[tuple] = set()
    skipped = 0

    for row in rows:
        if keys:
            key = tuple(_norm(row.get(name)) for name in keys)
            if key in existing or key in batch_keys:
                skipped += 1
                continue
            batch_keys.add(key)
        to_write.append(row)

    start_row = _last_data_row(worksheet, header_row) + 1
    preview = [
        {"row": start_row + index, "values": to_jsonable(row)}
        for index, row in enumerate(to_write)
    ]

    result = {
        "ok": True,
        "dry_run": dry,
        "applied": False,
        "path": str(resolved),
        "sheet": worksheet.title,
        "header_row": header_row,
        "appended": len(to_write),
        "skipped_duplicates": skipped,
        "rows": truncate(preview, PREVIEW_LIMIT)[0],
    }

    if dry:
        return result

    warnings = detect_lossy_features(workbook)
    backup = backup_file(resolved)

    for index, row in enumerate(to_write):
        target_row = start_row + index
        for name, value in row.items():
            worksheet.cell(target_row, header_map[name]).value = to_jsonable(value)

    save_workbook(workbook, resolved)

    result.update(applied=True, backup=str(backup), warnings=warnings)
    return result


def upsert_rows(
    path: str,
    sheet: str | None = None,
    rows: list[dict] | None = None,
    key_column: list[str] | None = None,
    dry_run: bool | None = None,
    header_row: int | None = None,
) -> dict:
    if not rows:
        raise ToolError("rows 不能为空", code="EMPTY_INPUT")

    keys = _as_list(key_column)
    if not keys:
        raise ToolError("key_column 不能为空", code="EMPTY_INPUT")

    resolved = ensure_workbook_path(path)
    dry = _resolve_dry_run(dry_run)

    workbook = _open(resolved)
    worksheet = get_sheet(workbook, sheet)
    header_row = header_row or find_header_row(worksheet)
    header_map = _header_map(worksheet, header_row)

    _check_columns(rows, header_map)
    _check_keys(keys, header_map)

    index: dict[tuple, int] = {}
    for row in range(header_row + 1, worksheet.max_row + 1):
        key = tuple(_norm(worksheet.cell(row, header_map[name]).value) for name in keys)
        if all(value == "" for value in key):
            continue
        index.setdefault(key, row)

    next_row = _last_data_row(worksheet, header_row) + 1
    inserts: list[dict] = []
    updates: list[dict] = []

    for row in rows:
        key = tuple(_norm(row.get(name)) for name in keys)
        if all(value == "" for value in key):
            raise ToolError(f"主键列不能全为空：{', '.join(keys)}", code="EMPTY_KEY")

        target_row = index.get(key)
        if target_row is None:
            inserts.append({"row": next_row, "key": list(key), "values": row})
            index[key] = next_row
            next_row += 1
            continue

        changes: list[dict] = []
        for name, value in row.items():
            if name in keys:
                continue
            column = header_map[name]
            before = to_jsonable(worksheet.cell(target_row, column).value)
            after = to_jsonable(value)
            if before != after:
                changes.append(
                    {
                        "cell": f"{get_column_letter(column)}{target_row}",
                        "column": name,
                        "before": before,
                        "after": after,
                    }
                )
        updates.append({"row": target_row, "key": list(key), "changes": changes})

    result = {
        "ok": True,
        "dry_run": dry,
        "applied": False,
        "path": str(resolved),
        "sheet": worksheet.title,
        "inserted": len(inserts),
        "updated": len(updates),
        "changed_cells": sum(len(item["changes"]) for item in updates),
        "inserted_rows": truncate(
            [dict(item, values=to_jsonable(item["values"])) for item in inserts],
            PREVIEW_LIMIT,
        )[0],
        "updated_rows": truncate(updates, PREVIEW_LIMIT)[0],
    }

    if dry:
        return result

    warnings = detect_lossy_features(workbook)
    backup = backup_file(resolved)

    for update in updates:
        for change in update["changes"]:
            worksheet.cell(update["row"], header_map[change["column"]]).value = change["after"]

    for insert in inserts:
        for name, value in insert["values"].items():
            worksheet.cell(insert["row"], header_map[name]).value = to_jsonable(value)

    save_workbook(workbook, resolved)

    result.update(applied=True, backup=str(backup), warnings=warnings)
    return result


def write_cells(
    path: str,
    sheet: str | None = None,
    cells: list[dict] | None = None,
    dry_run: bool | None = None,
) -> dict:
    if not cells:
        raise ToolError("cells 不能为空", code="EMPTY_INPUT")

    resolved = ensure_workbook_path(path)
    dry = _resolve_dry_run(dry_run)

    workbook = _open(resolved)
    worksheet = get_sheet(workbook, sheet)

    changes: list[dict] = []
    for item in cells:
        address = str(item.get("cell", "")).strip()
        if not address:
            raise ToolError(
                'cells 每项需要 cell 字段，例如 {"cell": "B2", "value": 1}',
                code="BAD_CELL_REF",
            )
        try:
            cell = worksheet[address]
        except (KeyError, ValueError, AttributeError) as exc:
            raise ToolError(f"无效单元格引用：{address}", code="BAD_CELL_REF") from exc

        changes.append(
            {
                "cell": address,
                "before": to_jsonable(cell.value),
                "after": to_jsonable(item.get("value")),
            }
        )

    result = {
        "ok": True,
        "dry_run": dry,
        "applied": False,
        "path": str(resolved),
        "sheet": worksheet.title,
        "change_count": len(changes),
        "changes": truncate(changes, 200)[0],
    }

    if dry:
        return result

    warnings = detect_lossy_features(workbook)
    backup = backup_file(resolved)

    for change in changes:
        worksheet[change["cell"]].value = change["after"]

    save_workbook(workbook, resolved)

    result.update(applied=True, backup=str(backup), warnings=warnings)
    return result


def write_table(
    path: str,
    sheet: str | None = None,
    start_cell: str = "A1",
    columns: list[str] | None = None,
    rows: list[dict] | None = None,
    include_header: bool = True,
    dry_run: bool | None = None,
) -> dict:
    if not rows:
        raise ToolError("rows 不能为空", code="EMPTY_INPUT")

    columns = columns or [str(key) for key in rows[0]]
    if not columns:
        raise ToolError("columns 不能为空", code="EMPTY_INPUT")

    resolved = ensure_workbook_path(path)
    dry = _resolve_dry_run(dry_run)

    workbook = _open(resolved)
    worksheet = get_sheet(workbook, sheet)

    try:
        anchor = worksheet[start_cell]
    except (KeyError, ValueError, AttributeError) as exc:
        raise ToolError(f"无效起始单元格：{start_cell}", code="BAD_CELL_REF") from exc

    start_row, start_col = anchor.row, anchor.column
    cursor = start_row
    changes: list[dict] = []

    if include_header:
        for offset, name in enumerate(columns):
            address = f"{get_column_letter(start_col + offset)}{cursor}"
            changes.append(
                {
                    "cell": address,
                    "before": to_jsonable(worksheet[address].value),
                    "after": name,
                }
            )
        cursor += 1

    for row in rows:
        for offset, name in enumerate(columns):
            address = f"{get_column_letter(start_col + offset)}{cursor}"
            before = to_jsonable(worksheet[address].value)
            after = to_jsonable(row.get(name))
            if before != after:
                changes.append({"cell": address, "before": before, "after": after})
        cursor += 1

    result = {
        "ok": True,
        "dry_run": dry,
        "applied": False,
        "path": str(resolved),
        "sheet": worksheet.title,
        "start_cell": start_cell,
        "row_count": len(rows),
        "change_count": len(changes),
        "changes": truncate(changes, 200)[0],
    }

    if dry:
        return result

    warnings = detect_lossy_features(workbook)
    backup = backup_file(resolved)

    for change in changes:
        worksheet[change["cell"]].value = change["after"]

    save_workbook(workbook, resolved)

    result.update(applied=True, backup=str(backup), warnings=warnings)
    return result


def create_from_template(
    template: str,
    dest: str,
    mapping: dict[str, Any] | None = None,
    overwrite: bool = False,
) -> dict:
    """复制模板后按单元格填值，是最稳妥的写报表方式（格式全部保留）。"""
    if not mapping:
        raise ToolError(
            'mapping 不能为空，格式如 {"Sheet1!B2": "值"}',
            code="EMPTY_INPUT",
        )

    source = ensure_workbook_path(template)

    # 先建目录再校验，支持目标路径包含尚不存在的子目录
    raw_target = Path(dest).expanduser().resolve()
    raw_target.parent.mkdir(parents=True, exist_ok=True)
    target = ensure_workbook_path(raw_target, must_exist=False)

    if target.exists() and not overwrite:
        raise ToolError(
            f"目标文件已存在：{target}；如需覆盖请传 overwrite=true",
            code="DEST_EXISTS",
        )

    shutil.copy2(source, target)

    workbook = _open(target)
    changes: list[dict] = []

    for reference, value in mapping.items():
        sheet_name = ""
        address = reference

        if "!" in reference:
            sheet_name, address = reference.rsplit("!", 1)
            sheet_name = sheet_name.strip("'")

        worksheet = get_sheet(workbook, sheet_name or None)
        address = address.replace("$", "").strip()

        try:
            cell = worksheet[address]
        except (KeyError, ValueError, AttributeError) as exc:
            raise ToolError(f"无效单元格引用：{reference}", code="BAD_CELL_REF") from exc

        changes.append(
            {
                "cell": f"{worksheet.title}!{address}",
                "before": to_jsonable(cell.value),
                "after": to_jsonable(value),
            }
        )
        cell.value = to_jsonable(value)

    save_workbook(workbook, target)

    return {
        "ok": True,
        "path": str(target),
        "template": str(source),
        "written": len(changes),
        "changes": changes,
        "warnings": detect_lossy_features(workbook),
    }


def check(path: str) -> dict:
    """体检：错误值、格式丢失风险、sheet 清单。"""
    resolved = ensure_workbook_path(path)
    workbook = _open(resolved)

    errors: list[dict] = []
    for worksheet in workbook.worksheets:
        for row in worksheet.iter_rows():
            for cell in row:
                value = cell.value
                if isinstance(value, str) and value.strip().upper() in ERROR_VALUES:
                    errors.append(
                        {"sheet": worksheet.title, "cell": cell.coordinate, "value": value}
                    )

    preview, truncated = truncate(errors, 200)

    return {
        "ok": True,
        "path": str(resolved),
        "sheets": workbook.sheetnames,
        "error_count": len(errors),
        "errors": preview,
        "truncated": truncated,
        "risks": detect_lossy_features(workbook),
    }


def export(
    path: str,
    sheet: str | None = None,
    fmt: str = "csv",
    dest: str | None = None,
) -> dict:
    import pandas as pd

    resolved = ensure_workbook_path(path)
    fmt = fmt.lower()

    if fmt not in {"csv", "jsonl"}:
        raise ToolError("fmt 仅支持 csv 或 jsonl", code="BAD_FORMAT")

    frame = pd.read_excel(resolved, sheet_name=sheet or 0, dtype=object)

    if dest:
        target = Path(dest).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
    else:
        out_dir = get_settings().data_dir / "exports"
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / f"{resolved.stem}{'.csv' if fmt == 'csv' else '.jsonl'}"

    if fmt == "csv":
        # utf-8-sig 保证 Excel 直接打开中文不乱码
        frame.to_csv(target, index=False, encoding="utf-8-sig")
    else:
        frame.to_json(target, orient="records", lines=True, force_ascii=False)

    return {"ok": True, "path": str(target), "format": fmt, "rows": len(frame)}
