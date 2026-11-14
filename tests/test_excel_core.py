"""Excel 核心行为测试：结构探查、去重追加、dry_run、upsert、查询、模板填充。"""

from __future__ import annotations

import pytest
from openpyxl import Workbook

from mcp_common.config import get_settings
from mcp_excel import reader, writer


@pytest.fixture(autouse=True)
def isolate_dirs(tmp_path, monkeypatch):
    """把 data/backups 指到临时目录，避免测试污染项目目录。"""
    monkeypatch.setenv("MCP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MCP_BACKUP_DIR", str(tmp_path / "backups"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def make_book(path):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    sheet.append(["项目号", "内容", "日期"])
    sheet.append(["P1", "a", "2026-11-10"])
    sheet.append(["P2", "b", "2026-11-10"])
    workbook.save(path)
    return path


def test_describe_reads_structure(tmp_path):
    path = make_book(tmp_path / "t.xlsx")
    info = reader.describe(str(path))

    assert info["ok"] is True
    assert info["sheet_count"] == 1

    sheet = info["sheets"][0]
    assert sheet["headers"] == ["项目号", "内容", "日期"]
    assert sheet["header_row"] == 1
    assert sheet["data_rows"] == 2


def test_read_range_returns_dict_rows(tmp_path):
    path = make_book(tmp_path / "t.xlsx")
    result = reader.read_range(str(path), sheet="Data")

    assert result["returned"] == 2
    assert result["rows"][0]["项目号"] == "P1"


def test_append_skips_duplicates(tmp_path):
    path = make_book(tmp_path / "t.xlsx")

    result = writer.append_rows(
        str(path),
        sheet="Data",
        rows=[{"项目号": "P2", "内容": "重复"}, {"项目号": "P3", "内容": "c"}],
        dedup_key=["项目号"],
        dry_run=False,
    )

    assert result["appended"] == 1
    assert result["skipped_duplicates"] == 1
    assert result["applied"] is True

    rows = reader.read_range(str(path), sheet="Data")["rows"]
    assert [row["项目号"] for row in rows] == ["P1", "P2", "P3"]


def test_append_dry_run_keeps_file_unchanged(tmp_path):
    path = make_book(tmp_path / "t.xlsx")

    result = writer.append_rows(
        str(path),
        sheet="Data",
        rows=[{"项目号": "P9", "内容": "不会写入"}],
        dry_run=True,
    )

    assert result["dry_run"] is True
    assert result["applied"] is False
    assert result["appended"] == 1

    assert len(reader.read_range(str(path), sheet="Data")["rows"]) == 2


def test_upsert_updates_and_inserts(tmp_path):
    path = make_book(tmp_path / "t.xlsx")

    result = writer.upsert_rows(
        str(path),
        sheet="Data",
        key_column=["项目号"],
        rows=[
            {"项目号": "P1", "内容": "已更新"},
            {"项目号": "P4", "内容": "新增"},
        ],
        dry_run=False,
    )

    assert result["updated"] == 1
    assert result["inserted"] == 1

    rows = {row["项目号"]: row for row in reader.read_range(str(path), sheet="Data")["rows"]}
    assert rows["P1"]["内容"] == "已更新"
    assert rows["P4"]["内容"] == "新增"


def test_query_filters_rows(tmp_path):
    path = make_book(tmp_path / "t.xlsx")

    result = reader.query(str(path), sheet="Data", where={"项目号": "P2"})

    assert result["matched"] == 1
    assert result["rows"][0]["内容"] == "b"


def test_write_cells_reports_before_after(tmp_path):
    path = make_book(tmp_path / "t.xlsx")

    result = writer.write_cells(
        str(path),
        sheet="Data",
        cells=[{"cell": "B2", "value": "改过"}],
        dry_run=False,
    )

    assert result["changes"][0]["before"] == "a"
    assert result["changes"][0]["after"] == "改过"
    assert reader.read_range(str(path), sheet="Data")["rows"][0]["内容"] == "改过"


def test_create_from_template_keeps_structure(tmp_path):
    template = make_book(tmp_path / "template.xlsx")
    dest = tmp_path / "out.xlsx"

    result = writer.create_from_template(
        str(template),
        str(dest),
        {"Data!B2": "模板填充"},
    )

    assert result["written"] == 1
    assert dest.exists()

    rows = reader.read_range(str(dest), sheet="Data")["rows"]
    assert rows[0]["内容"] == "模板填充"
    assert len(rows) == 2
