"""Excel MCP Server：工具注册与启动入口。"""

from __future__ import annotations

from typing import Any

from mcp_common.server import build_server, run
from mcp_common.tools import safe_tool

from . import reader, writer

mcp = build_server(
    "excel",
    instructions=(
        "读写本地 Excel（.xlsx / .xlsm）。"
        "读取顺序：先用 xlsx_describe 了解 sheet、表头、列类型，再用 xlsx_read_range 或 xlsx_query 取数。"
        "写入顺序：先传 dry_run=true 预览变更，确认后再传 dry_run=false 真正落盘。"
        "所有写操作都会自动备份原文件到 backups/，并在检测到图片、图表、透视表等易丢失对象时给出 warnings。"
        "需要保留复杂格式时，优先用 xlsx_create_from_template 在模板上填值。"
    ),
)


@mcp.tool()
@safe_tool
def xlsx_describe(path: str) -> dict:
    """查看工作簿结构。

    返回每个 sheet 的行列数、表头行、列名、推断类型、合并单元格、公式数量。
    在不确定表结构时先调用本工具，不要一上来就读全表。
    """
    return reader.describe(path)


@mcp.tool()
@safe_tool
def xlsx_read_range(
    path: str,
    sheet: str | None = None,
    cell_range: str | None = None,
    header_row: int | None = None,
    max_rows: int = 200,
    as_dict: bool = True,
) -> dict:
    """读取指定 sheet 或单元格区域。

    - 不传 cell_range：按表头返回对象数组（as_dict=true）或二维数组（as_dict=false）。
    - 传 cell_range（如 "A1:D50"）：按原始二维数组返回，不做表头处理。
    - header_row 不传时自动识别前 10 行中最像表头的一行。
    """
    return reader.read_range(
        path=path,
        sheet=sheet,
        cell_range=cell_range,
        header_row=header_row,
        max_rows=max_rows,
        as_dict=as_dict,
    )


@mcp.tool()
@safe_tool
def xlsx_query(
    path: str,
    sheet: str | None = None,
    where: dict[str, Any] | None = None,
    contains: dict[str, str] | None = None,
    columns: list[str] | None = None,
    limit: int = 100,
    offset: int = 0,
    header_row: int | None = None,
) -> dict:
    """按条件查询数据行。

    - where：等值过滤，如 {"状态": "进行中"}。
    - contains：包含过滤（不区分大小写），如 {"内容": "风险"}。
    - columns：只返回这些列；limit/offset 控制分页。
    比 read_range 更适合从大表里挑数据。
    """
    return reader.query(
        path=path,
        sheet=sheet,
        where=where,
        contains=contains,
        columns=columns,
        limit=limit,
        offset=offset,
        header_row=header_row,
    )


@mcp.tool()
@safe_tool
def xlsx_append_rows(
    path: str,
    sheet: str | None = None,
    rows: list[dict] | None = None,
    dedup_key: list[str] | None = None,
    dry_run: bool | None = None,
    header_row: int | None = None,
) -> dict:
    """在表尾追加数据行。

    - rows 是对象数组，key 必须能在表头里找到，例如 [{"项目号": "P1", "状态": "进行中"}]。
    - dedup_key 指定去重列（可多列），已存在的键会被跳过，保证重复执行不产生重复行。
    - dry_run 为 true 时只返回将写入的行号和内容，不修改文件；为 false 时先备份再写入。
    """
    return writer.append_rows(
        path=path,
        sheet=sheet,
        rows=rows,
        dedup_key=dedup_key,
        dry_run=dry_run,
        header_row=header_row,
    )


@mcp.tool()
@safe_tool
def xlsx_upsert_rows(
    path: str,
    sheet: str | None = None,
    rows: list[dict] | None = None,
    key_column: list[str] | None = None,
    dry_run: bool | None = None,
    header_row: int | None = None,
) -> dict:
    """按主键更新或插入。

    key_column 指定的列组合作为主键：已存在则更新该行其余列，不存在则追加到表尾。
    返回 inserted / updated 数量，以及每个被修改单元格的前后值。
    适合「每天同步一批数据到同一张表」这类场景。
    """
    return writer.upsert_rows(
        path=path,
        sheet=sheet,
        rows=rows,
        key_column=key_column,
        dry_run=dry_run,
        header_row=header_row,
    )


@mcp.tool()
@safe_tool
def xlsx_write_cells(
    path: str,
    sheet: str | None = None,
    cells: list[dict] | None = None,
    dry_run: bool | None = None,
) -> dict:
    """精确写入若干单元格。

    cells 形如 [{"cell": "B2", "value": 100}, {"cell": "C3", "value": "已完成"}]。
    返回每个单元格修改前后的值，适合填报表固定字段。
    """
    return writer.write_cells(path=path, sheet=sheet, cells=cells, dry_run=dry_run)


@mcp.tool()
@safe_tool
def xlsx_write_table(
    path: str,
    sheet: str | None = None,
    start_cell: str = "A1",
    columns: list[str] | None = None,
    rows: list[dict] | None = None,
    include_header: bool = True,
    dry_run: bool | None = None,
) -> dict:
    """从指定起始单元格开始写入一张表。

    columns 决定列顺序（不传则取 rows 第一行的 key 顺序），
    include_header 控制是否写表头。适合在空白 sheet 或指定区域生成报表。
    """
    return writer.write_table(
        path=path,
        sheet=sheet,
        start_cell=start_cell,
        columns=columns,
        rows=rows,
        include_header=include_header,
        dry_run=dry_run,
    )


@mcp.tool()
@safe_tool
def xlsx_create_from_template(
    template: str,
    dest: str,
    mapping: dict[str, Any] | None = None,
    overwrite: bool = False,
) -> dict:
    """复制模板并填充指定单元格，是保留复杂格式最稳妥的方式。

    mapping 形如 {"Sheet1!B2": "2026-11-11", "Sheet1!C5": 128}；
    不带 sheet 前缀时写入活动 sheet。目标文件已存在时需显式 overwrite=true。
    """
    return writer.create_from_template(
        template=template,
        dest=dest,
        mapping=mapping,
        overwrite=overwrite,
    )


@mcp.tool()
@safe_tool
def xlsx_check(path: str) -> dict:
    """检查工作簿健康状况。

    返回 #REF! / #VALUE! 等错误值位置、sheet 清单，
    以及写回后会丢失的对象（图片、图表、透视表、外部链接）。
    """
    return writer.check(path)


@mcp.tool()
@safe_tool
def xlsx_export(
    path: str,
    sheet: str | None = None,
    fmt: str = "csv",
    dest: str | None = None,
) -> dict:
    """把某个 sheet 导出为 CSV 或 JSONL，便于下游分析。

    fmt 支持 csv / jsonl；CSV 使用 utf-8-sig，Excel 直接打开中文不乱码。
    dest 不传时输出到 data/exports/ 下。
    """
    return writer.export(path=path, sheet=sheet, fmt=fmt, dest=dest)


def main() -> None:
    run(mcp)


if __name__ == "__main__":
    main()
