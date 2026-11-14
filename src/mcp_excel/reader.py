"""读取：结构探查、区域读取、条件查询。"""

from __future__ import annotations

import datetime as dt
from typing import Any

from openpyxl import load_workbook
from openpyxl.worksheet.worksheet import Worksheet

from mcp_common.errors import ToolError
from mcp_common.utils import to_jsonable, truncate

from .guard import ensure_workbook_path

HEADER_SCAN_ROWS = 10
TYPE_SAMPLE_ROWS = 50
FORMULA_SCAN_LIMIT = 200_000

ERROR_VALUES = {"#REF!", "#VALUE!", "#DIV/0!", "#N/A", "#NAME?", "#NULL!", "#NUM!"}


def get_sheet(workbook: Any, sheet: str | None) -> Worksheet:
    if not sheet:
        return workbook.active
    if sheet not in workbook.sheetnames:
        raise ToolError(
            f"工作表不存在：{sheet}；可选：{', '.join(workbook.sheetnames)}",
            code="SHEET_NOT_FOUND",
        )
    return workbook[sheet]


def find_header_row(sheet: Worksheet) -> int:
    """在前 10 行里挑非空单元格最多的一行作为表头。

    比「默认第一行」更抗标题行、空行干扰。
    """
    best_row, best_count = 1, -1
    last_row = min(max(sheet.max_row, 1), HEADER_SCAN_ROWS)

    for row in sheet.iter_rows(min_row=1, max_row=last_row):
        count = sum(1 for cell in row if cell.value not in (None, ""))
        if count > best_count:
            best_row, best_count = row[0].row, count

    return best_row


def dedupe_headers(headers: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    result: list[str] = []

    for raw in headers:
        name = str(raw).strip() or "col"
        if name in seen:
            seen[name] += 1
            result.append(f"{name}_{seen[name]}")
        else:
            seen[name] = 0
            result.append(name)

    return result


def row_headers(sheet: Worksheet, header_row: int) -> list[str]:
    raw: list[str] = []
    for column in range(1, sheet.max_column + 1):
        value = sheet.cell(header_row, column).value
        raw.append(str(value) if value not in (None, "") else f"col_{column}")
    return dedupe_headers(raw)


def row_values(sheet: Worksheet, row: int, max_column: int | None = None) -> list[Any]:
    max_column = max_column or sheet.max_column
    return [to_jsonable(sheet.cell(row, column).value) for column in range(1, max_column + 1)]


def _value_type(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, dt.datetime | dt.date | dt.time):
        return "date"
    return "text"


def _merge_types(types: set[str]) -> str:
    if not types:
        return "empty"
    if len(types) == 1:
        return next(iter(types))
    return "mixed"


def describe_columns(sheet: Worksheet, header_row: int, headers: list[str]) -> list[dict]:
    columns: list[dict] = []
    last_row = min(sheet.max_row, header_row + TYPE_SAMPLE_ROWS)

    for index, name in enumerate(headers, start=1):
        types: set[str] = set()
        samples: list[Any] = []

        for row in range(header_row + 1, last_row + 1):
            value = sheet.cell(row, index).value
            if value in (None, ""):
                continue
            types.add(_value_type(value))
            if len(samples) < 3:
                samples.append(to_jsonable(value))

        columns.append(
            {
                "name": name,
                "column": index,
                "type": _merge_types(types),
                "samples": samples,
            }
        )

    return columns


def describe(path: str) -> dict:
    resolved = ensure_workbook_path(path)
    values_book = load_workbook(resolved, data_only=True)
    formula_book = load_workbook(resolved, data_only=False)

    sheets: list[dict] = []

    for sheet in values_book.worksheets:
        header_row = find_header_row(sheet)
        headers = row_headers(sheet, header_row)

        formula_sheet = formula_book[sheet.title]
        formula_cells: list[str] = []
        scanned = 0
        truncated = False

        for row in formula_sheet.iter_rows():
            for cell in row:
                scanned += 1
                if scanned > FORMULA_SCAN_LIMIT:
                    truncated = True
                    break
                if isinstance(cell.value, str) and cell.value.startswith("="):
                    formula_cells.append(cell.coordinate)
            if truncated:
                break

        sheets.append(
            {
                "sheet": sheet.title,
                "max_row": sheet.max_row,
                "max_column": sheet.max_column,
                "header_row": header_row,
                "data_rows": max(sheet.max_row - header_row, 0),
                "headers": headers,
                "columns": describe_columns(sheet, header_row, headers),
                "merged_ranges": [str(item) for item in sheet.merged_cells.ranges][:20],
                "formula_count": len(formula_cells),
                "formula_samples": formula_cells[:10],
                "formula_scan_truncated": truncated,
            }
        )

    return {
        "ok": True,
        "path": str(resolved),
        "active_sheet": getattr(values_book.active, "title", None),
        "sheet_count": len(sheets),
        "sheets": sheets,
    }


def read_range(
    path: str,
    sheet: str | None = None,
    cell_range: str | None = None,
    header_row: int | None = None,
    max_rows: int = 200,
    as_dict: bool = True,
) -> dict:
    resolved = ensure_workbook_path(path)
    workbook = load_workbook(resolved, data_only=True)
    worksheet = get_sheet(workbook, sheet)
    max_rows = max(1, min(max_rows, 2000))

    # 指定区域时按原始二维表返回，不做表头处理
    if cell_range:
        try:
            cells = worksheet[cell_range]
        except (KeyError, ValueError) as exc:
            raise ToolError(f"无效区域：{cell_range}", code="BAD_RANGE") from exc

        if hasattr(cells, "value"):
            matrix = [[to_jsonable(cells.value)]]
        else:
            matrix = [[to_jsonable(cell.value) for cell in row] for row in cells]

        preview, truncated = truncate(matrix, max_rows)
        return {
            "ok": True,
            "sheet": worksheet.title,
            "range": cell_range,
            "rows": preview,
            "total_rows": len(matrix),
            "truncated": truncated,
        }

    header_row = header_row or find_header_row(worksheet)
    headers = row_headers(worksheet, header_row)

    start = header_row + 1
    end = min(worksheet.max_row, start + max_rows - 1)

    records: list[Any] = []
    for row in range(start, end + 1):
        values = row_values(worksheet, row, len(headers))
        if all(value in (None, "") for value in values):
            continue
        records.append(dict(zip(headers, values)) if as_dict else values)

    return {
        "ok": True,
        "sheet": worksheet.title,
        "header_row": header_row,
        "headers": headers,
        "total_rows": max(worksheet.max_row - header_row, 0),
        "returned": len(records),
        "truncated": worksheet.max_row > end,
        "rows": records,
    }


def query(
    path: str,
    sheet: str | None = None,
    where: dict[str, Any] | None = None,
    contains: dict[str, str] | None = None,
    columns: list[str] | None = None,
    limit: int = 100,
    offset: int = 0,
    header_row: int | None = None,
) -> dict:
    """按等值条件（where）和包含条件（contains）过滤，底层用 pandas。"""
    import pandas as pd

    resolved = ensure_workbook_path(path)
    header_index = (header_row - 1) if header_row else 0

    frame = pd.read_excel(resolved, sheet_name=sheet or 0, header=header_index, dtype=object)
    frame.columns = dedupe_headers([str(column) for column in frame.columns])

    if where:
        for column, expected in where.items():
            if column not in frame.columns:
                raise ToolError(_missing_column(column, frame.columns), code="UNKNOWN_COLUMN")
            matched = frame[column].astype(str).str.strip() == str(expected).strip()
            frame = frame[matched]

    if contains:
        for column, keyword in contains.items():
            if column not in frame.columns:
                raise ToolError(_missing_column(column, frame.columns), code="UNKNOWN_COLUMN")
            matched = (
                frame[column]
                .astype(str)
                .str.contains(str(keyword), case=False, na=False, regex=False)
            )
            frame = frame[matched]

    matched_count = len(frame)

    if columns:
        missing = [column for column in columns if column not in frame.columns]
        if missing:
            raise ToolError(_missing_column(missing[0], frame.columns), code="UNKNOWN_COLUMN")
        frame = frame[columns]

    if offset:
        frame = frame.iloc[offset:]

    frame = frame.head(max(1, min(limit, 2000)))

    records = [
        {str(key): to_jsonable(value) for key, value in record.items()}
        for record in frame.to_dict(orient="records")
    ]

    return {
        "ok": True,
        "sheet": sheet or "active",
        "matched": matched_count,
        "returned": len(records),
        "rows": records,
    }


def _missing_column(column: str, available: Any) -> str:
    names = ", ".join(str(name) for name in available)
    return f"列不存在：{column}；可用列：{names}"
