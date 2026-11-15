"""web-mcp 核心行为测试：护栏、流程校验、变量替换、真实浏览器抽表。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from mcp_common.errors import ToolError
from mcp_web import extract
from mcp_web.browser import BrowserManager
from mcp_web.flow import (
    FlowSpec,
    ParamSpec,
    _plan_from_lessons,
    load_flow,
    resolve_params,
    substitute,
)
from mcp_web.policy import Policy

FIXTURE = Path(__file__).parent / "fixtures" / "query.html"


def test_policy_blocks_dangerous_click():
    with pytest.raises(ToolError) as excinfo:
        Policy().check("提交审批")
    assert excinfo.value.code == "BLOCKED_MUTATION"


def test_policy_allows_query_and_explicit_override():
    Policy().check("查询")
    Policy().check("提交审批", allow=True)


def test_substitute_replaces_vars():
    assert substitute("{{ a }}-{{b}}", {"a": "1", "b": "2"}) == "1-2"


def test_substitute_rejects_undefined_var():
    with pytest.raises(ToolError) as excinfo:
        substitute("{{ missing }}", {})
    assert "missing" in excinfo.value.message


def test_resolve_params_requires_required_param():
    spec = FlowSpec(name="x", steps=[], params=[ParamSpec(name="date", required=True)])
    with pytest.raises(ToolError) as excinfo:
        resolve_params(spec, {})
    assert excinfo.value.code == "MISSING_PARAMS"


def test_resolve_params_uses_defaults():
    spec = FlowSpec(name="x", steps=[], params=[ParamSpec(name="status", default="全部")])
    assert resolve_params(spec, {})["status"] == "全部"


def test_plan_from_lessons_picks_anchor_and_previous():
    lessons = [
        {"date": "2026-11-22", "lesson": "27,28", "teacher": "张力文", "begin": "13:20", "end": "17:40"},
        {"date": "2026-11-08", "lesson": "26", "teacher": "马倩丽", "begin": "09:00", "end": "11:00"},
        {"date": "2026-11-01", "lesson": "26", "teacher": "张力文", "begin": "09:00", "end": "11:00"},
        {"date": "2026-10-25", "lesson": "25", "teacher": "张力文", "begin": "09:00", "end": "11:00"},
    ]

    plan = _plan_from_lessons(lessons, "2026-11-16")

    assert plan["anchor_date"] == "2026-11-22"
    assert plan["anchor_teacher"] == "张力文"
    assert plan["anchor_lesson"] == "27、28"
    assert plan["anchor_time"] == "13:20-17:40"
    assert plan["row_date"] == "2026-11-01"


def test_plan_from_lessons_without_upcoming_returns_none():
    lessons = [{"date": "2026-10-18", "lesson": "37", "teacher": "杨子璇", "begin": "", "end": ""}]
    assert _plan_from_lessons(lessons, "2026-11-16") is None


def test_plan_from_lessons_without_earlier_lesson_returns_none():
    lessons = [{"date": "2026-11-22", "lesson": "27", "teacher": "王侠栋", "begin": "", "end": ""}]
    assert _plan_from_lessons(lessons, "2026-11-16") is None


def test_flow_rejects_unknown_action(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("name: bad\nsteps:\n  - action: launch\n", encoding="utf-8")

    with pytest.raises(ToolError) as excinfo:
        load_flow(str(path))
    assert "action" in excinfo.value.message


def test_flow_rejects_unknown_field(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("name: bad\nsteps:\n  - action: goto\n    urll: /x\n", encoding="utf-8")

    with pytest.raises(ToolError) as excinfo:
        load_flow(str(path))
    assert "urll" in excinfo.value.message


def test_flow_loads_minimal_spec(tmp_path):
    path = tmp_path / "ok.yaml"
    path.write_text(
        "name: ok\n"
        "site: demo\n"
        "steps:\n"
        "  - action: goto\n"
        "    url: /a\n"
        "  - action: extract_table\n"
        "    selector: '#t'\n"
        "    as: rows\n",
        encoding="utf-8",
    )

    spec = load_flow(str(path))
    assert spec.site == "demo"
    assert spec.mode == "readonly"
    assert spec.steps[1].as_ == "rows"


def test_browser_extract_table_and_outline():
    async def scenario():
        manager = BrowserManager()
        try:
            await manager.start(headless=True)
            page = await manager.page("test")
            await page.goto(FIXTURE.as_uri())
            table = await extract.extract_table(page, "#orders")
            items = await extract.outline(page, 50)
            return table, items
        finally:
            await manager.close()

    table, items = asyncio.run(scenario())

    assert table["headers"] == ["订单号", "客户", "金额"]
    assert [row["订单号"] for row in table["rows"]] == ["A001", "A002"]
    assert any(item["tag"] == "button" for item in items)
    assert any(item["id"] == "keyword" for item in items)
