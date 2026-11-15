"""web-mcp Server：原子工具 + YAML 流程工具。"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp_common.config import get_settings
from mcp_common.errors import ToolError
from mcp_common.server import build_server, run
from mcp_common.tools import async_safe_tool

from . import extract, flow, policy
from .browser import get_manager

mcp = build_server(
    "web",
    instructions=(
        "在固定网页系统上执行查询类操作。"
        "业务口径（术语、参数、判定规则、结论格式、只读边界）见项目 skill："
        ".trae/skills/work-assistant-query/SKILL.md，调用流程前先读它，不要自行解释"
        "「一对一」「班课」等术语。"
        "探索页面用 web_open + web_snapshot(mode=outline)，看清元素后再用 web_fill / web_click / web_extract_table。"
        "固定流程写成 flows/*.yaml，用 web_flow_run 一键执行；先 web_flow_validate 校验，再 web_flow_dry_run 验证选择器。"
        "默认只读：点击提交/删除/审批等按钮会被拦截并返回 BLOCKED_MUTATION。"
        "遇到验证码或登录失效会返回 NEED_HUMAN，请人工接管后调用 web_save_state 保存登录态。"
    ),
)

_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def _ok(
    data: Any,
    *,
    site: str | None = None,
    warnings: list[str] | None = None,
    artifacts: dict[str, Any] | None = None,
    **meta: Any,
) -> dict:
    payload: dict[str, Any] = {
        "ok": True,
        "data": data,
        "meta": meta,
        "warnings": warnings or [],
        "artifacts": artifacts or {},
    }
    if site:
        payload["meta"]["site"] = site
    return payload


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(name).name).strip()
    if not cleaned or cleaned in {".", ".."}:
        cleaned = "download"
    if Path(cleaned).stem.upper() in _RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned


def _limit_rows(rows: Any) -> tuple[Any, bool]:
    limit = get_settings().web_max_rows
    if isinstance(rows, list) and len(rows) > limit:
        return rows[:limit], True
    return rows, False


# ---------- 原子工具 ----------


@mcp.tool()
@async_safe_tool
async def web_open(url: str, site: str = "default", headless: bool | None = None) -> dict:
    """打开页面。

    site 对应登录态文件 data/profiles/<site>.json；首次接入某站点时传 headless=false
    人工登录，然后调用 web_save_state 保存。之后同样的 site 会自动复用登录态。
    """
    manager = get_manager()
    await manager.start(headless)
    page = await manager.page(site)
    try:
        await page.goto(url, wait_until="domcontentloaded")
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"打开页面失败：{exc}", code="BROWSER_ERROR") from exc

    return _ok({"url": page.url, "title": await page.title()}, site=site)


@mcp.tool()
@async_safe_tool
async def web_snapshot(
    mode: str = "outline",
    selector: str | None = None,
    max_chars: int = 4000,
    site: str = "default",
) -> dict:
    """查看页面结构，三种模式。

    - outline：列出可交互元素（按钮/输入框/链接），每个带 ref，用来决定点哪里；
    - markdown：返回正文纯文本，用来读内容；
    - table：把表格转成对象数组，selector 不传时取页面里行数最多的表。
    """
    manager = get_manager()
    page = await manager.page(site)

    if mode == "outline":
        items = await extract.outline(page, limit=200)
        return _ok(items, mode="outline", count=len(items), site=site)

    if mode == "markdown":
        text = await extract.page_text(page, max_chars)
        return _ok(text, mode="markdown", site=site)

    if mode == "table":
        table = await extract.extract_table(page, selector)
        rows, truncated = _limit_rows(table["rows"])
        warnings = ["结果超过上限已截断"] if truncated else []
        return _ok(rows, mode="table", headers=table["headers"], site=site, warnings=warnings)

    raise ToolError("mode 仅支持 outline / markdown / table", code="BAD_ARGS")


@mcp.tool()
@async_safe_tool
async def web_click(
    ref: int | None = None,
    selector: str | None = None,
    text: str | None = None,
    allow_dangerous: bool = False,
    site: str = "default",
) -> dict:
    """点击元素，ref / selector / text 三选一。

    ref 来自 web_snapshot(mode=outline)。默认只读，命中「提交/保存/删除」等
    危险词会被拦截并返回 BLOCKED_MUTATION；确认要执行时传 allow_dangerous=true。
    """
    manager = get_manager()
    page = await manager.page(site)
    locator = extract.resolve_locator(page, ref, selector, text)

    label = text or await extract.element_text(locator)
    policy.load_policy().check(label, allow=allow_dangerous)

    try:
        await locator.first.click()
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"点击失败：{exc}", code="SELECTOR_NOT_FOUND") from exc

    return _ok({"clicked": label[:80], "url": page.url}, site=site)


@mcp.tool()
@async_safe_tool
async def web_fill(
    value: str,
    selector: str | None = None,
    ref: int | None = None,
    press_enter: bool = False,
    site: str = "default",
) -> dict:
    """填充输入框，press_enter=true 时填完回车（很多查询页回车才触发搜索）。"""
    manager = get_manager()
    page = await manager.page(site)
    locator = extract.resolve_locator(page, ref, selector, None)

    try:
        await locator.first.fill(str(value))
        if press_enter:
            await locator.first.press("Enter")
            await page.wait_for_load_state("domcontentloaded")
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"填充失败：{exc}", code="SELECTOR_NOT_FOUND") from exc

    return _ok({"value": value, "pressed_enter": press_enter}, site=site)


@mcp.tool()
@async_safe_tool
async def web_select(
    selector: str | None = None,
    ref: int | None = None,
    value: str | None = None,
    label: str | None = None,
    site: str = "default",
) -> dict:
    """操作下拉框，value 按 option 的 value 选，label 按可见文字选。"""
    manager = get_manager()
    page = await manager.page(site)
    locator = extract.resolve_locator(page, ref, selector, None)

    try:
        if label is not None:
            await locator.first.select_option(label=label)
        elif value is not None:
            await locator.first.select_option(value=value)
        else:
            raise ToolError("需要提供 value 或 label", code="BAD_ARGS")
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"下拉选择失败：{exc}", code="SELECTOR_NOT_FOUND") from exc

    return _ok({"value": value, "label": label}, site=site)


@mcp.tool()
@async_safe_tool
async def web_set_date(
    value: str,
    selector: str | None = None,
    ref: int | None = None,
    site: str = "default",
) -> dict:
    """设置日期控件，value 用 YYYY-MM-DD。

    很多日期控件是 readonly 或由 JS 接管，直接 fill 无效，这里用脚本赋值并触发
    input/change 事件。若站点用了完全自定义的日历组件，请在流程里改用 click 逐步选择。
    """
    manager = get_manager()
    page = await manager.page(site)
    locator = extract.resolve_locator(page, ref, selector, None)

    script = """
    (el, v) => {
      el.value = v;
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
    }
    """
    try:
        await locator.first.evaluate(script, value)
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"日期设置失败：{exc}", code="SELECTOR_NOT_FOUND") from exc

    return _ok({"value": value}, site=site)


@mcp.tool()
@async_safe_tool
async def web_press(key: str, site: str = "default") -> dict:
    """在页面上按键，如 Enter、Escape、Tab、Control+A。"""
    manager = get_manager()
    page = await manager.page(site)
    await page.keyboard.press(key)
    return _ok({"key": key}, site=site)


@mcp.tool()
@async_safe_tool
async def web_wait_for(
    selector: str | None = None,
    text: str | None = None,
    timeout_ms: int | None = None,
    site: str = "default",
) -> dict:
    """等待条件满足：元素出现（selector）、文字出现（text），都不传则等网络空闲。"""
    manager = get_manager()
    page = await manager.page(site)
    timeout = timeout_ms or get_settings().web_timeout_ms

    try:
        if selector:
            await page.wait_for_selector(selector, timeout=timeout)
            target = selector
        elif text:
            await page.get_by_text(text).first.wait_for(timeout=timeout)
            target = text
        else:
            await page.wait_for_load_state("networkidle", timeout=timeout)
            target = "networkidle"
    except Exception as exc:  # noqa: BLE001
        raise ToolError(
            f"等待超时（{timeout}ms）：{selector or text or 'networkidle'}",
            code="TIMEOUT",
        ) from exc

    return _ok({"waited_for": target, "timeout_ms": timeout}, site=site)


@mcp.tool()
@async_safe_tool
async def web_extract(
    fields: dict[str, str],
    selector: str | None = None,
    site: str = "default",
) -> dict:
    """按选择器抽字段，返回对象。

    fields 形如 {"订单号": "#orderNo", "金额": ".amount"}；selector 作为容器限定范围，
    不传则在整页内查找。取不到的元素返回 null。
    """
    manager = get_manager()
    page = await manager.page(site)
    data = await extract.extract_fields(page, selector, fields)
    return _ok(data, site=site)


@mcp.tool()
@async_safe_tool
async def web_extract_table(selector: str | None = None, site: str = "default") -> dict:
    """把表格抽成对象数组，第一行作为表头。

    selector 不传时取页面中行数最多的 table。合并单元格会导致列错位，
    遇到复杂表头请先在 web_snapshot(mode=table) 里确认结构。
    """
    manager = get_manager()
    page = await manager.page(site)
    table = await extract.extract_table(page, selector)
    rows, truncated = _limit_rows(table["rows"])
    warnings = ["结果超过上限已截断"] if truncated else []
    return _ok(rows, headers=table["headers"], total=len(table["rows"]), site=site, warnings=warnings)


@mcp.tool()
@async_safe_tool
async def web_paginate(
    next_selector: str,
    table_selector: str | None = None,
    max_pages: int = 20,
    dedupe_by: list[str] | None = None,
    site: str = "default",
) -> dict:
    """自动翻页并累计表格数据。

    从当前页开始，反复点 next_selector 直到按钮消失/禁用或达到 max_pages。
    dedupe_by 按列去重，避免数据变动导致重复行。
    """
    manager = get_manager()
    page = await manager.page(site)

    spec = flow.FlowSpec(
        name="paginate",
        steps=[],
        paginate=flow.PaginateSpec(
            next_selector=next_selector,
            table_selector=table_selector,
            max_pages=max_pages,
            dedupe_by=dedupe_by,
        ),
    )
    result = await flow._paginate(page, spec, {}, dry_run=False)
    if result is None:
        raise ToolError("未配置分页参数", code="BAD_ARGS")

    rows, truncated = _limit_rows(result["rows"])
    warnings = []
    if result["truncated"] or truncated:
        warnings.append(f"翻页达到上限 {max_pages} 页，结果可能不完整")

    return _ok(
        rows,
        headers=result["headers"],
        page_count=result["page_count"],
        total=len(result["rows"]),
        site=site,
        warnings=warnings,
    )


@mcp.tool()
@async_safe_tool
async def web_screenshot(
    path: str | None = None,
    full_page: bool = True,
    site: str = "default",
) -> dict:
    """截图，用于排查。不传 path 时存到 data/runs/ 下。"""
    manager = get_manager()
    page = await manager.page(site)
    settings = get_settings()
    settings.ensure_dirs()

    if path:
        target = Path(path).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
    else:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = settings.runs_dir / f"shot-{stamp}.png"

    await page.screenshot(path=str(target), full_page=full_page)
    return _ok({"path": str(target)}, site=site)


@mcp.tool()
@async_safe_tool
async def web_download(
    trigger_selector: str | None = None,
    trigger_text: str | None = None,
    dest: str | None = None,
    site: str = "default",
) -> dict:
    """触发导出按钮并保存文件。

    点击后等待下载完成，文件存到 dest 目录（不传则 data/downloads/）。
    文件名会做清洗，防止路径穿越。
    """
    manager = get_manager()
    page = await manager.page(site)
    locator = extract.resolve_locator(page, None, trigger_selector, trigger_text)

    settings = get_settings()
    settings.ensure_dirs()
    target_dir = Path(dest).expanduser().resolve() if dest else settings.data_dir / "downloads"
    target_dir.mkdir(parents=True, exist_ok=True)

    try:
        async with page.expect_download() as info:
            await locator.first.click()
        download = await info.value
        target = target_dir / _safe_filename(download.suggested_filename)
        await download.save_as(str(target))
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"下载失败：{exc}", code="BROWSER_ERROR") from exc

    return _ok({"path": str(target), "filename": target.name}, site=site)


@mcp.tool()
@async_safe_tool
async def web_save_state(site: str) -> dict:
    """人工登录后保存该站点的登录态，之后同 site 自动复用，无需再登录。"""
    manager = get_manager()
    path = await manager.save_state(site)
    return _ok({"site": site, "path": str(path)}, site=site)


@mcp.tool()
async def web_list_profiles() -> dict:
    """列出已保存登录态的站点及其保存时间。"""
    items = get_manager().list_profiles()
    return _ok(items, count=len(items))


# ---------- 流程工具 ----------


@mcp.tool()
async def web_flow_list() -> dict:
    """列出 flows/ 下已注册的流程，含站点、模式、参数。"""
    items = flow.list_flows()
    return _ok(items, count=len(items))


@mcp.tool()
async def web_flow_validate(name: str) -> dict:
    """校验流程文件，返回参数清单和步骤数；有错时指出具体字段。"""
    spec = flow.load_flow(name)
    return _ok(
        {
            "name": spec.name,
            "site": spec.site,
            "mode": spec.mode,
            "params": [{"name": p.name, "type": p.type, "required": p.required, "default": p.default} for p in spec.params],
            "steps": [step.action for step in spec.steps],
            "paginate": spec.paginate is not None,
        }
    )


@mcp.tool()
@async_safe_tool
async def web_flow_dry_run(name: str, params: dict[str, Any] | None = None) -> dict:
    """演练流程：执行 goto/fill/wait_for/extract，但不真正点击。

    用来验证选择器是否正确，不会触发查询按钮，也不会改数据。
    """
    spec = flow.load_flow(name)
    resolved = flow.resolve_params(spec, params)
    manager = get_manager()
    result = await flow.run_flow(manager, spec, resolved, dry_run=True)

    data, truncated = _limit_rows(result["data"])
    warnings = list(result["warnings"])
    if truncated:
        warnings.append("结果超过上限已截断")

    return _ok(
        data,
        flow=spec.name,
        mode=spec.mode,
        page_count=result["page_count"],
        log=result["log"],
        warnings=warnings,
    )


@mcp.tool()
@async_safe_tool
async def web_flow_run(name: str, params: dict[str, Any] | None = None) -> dict:
    """执行流程并返回结构化结果。

    结果会落到 data/runs/<时间>-<流程>/result.json，失败时同目录下有截图和 HTML，
    便于排查页面改版问题。
    """
    spec = flow.load_flow(name)
    resolved = flow.resolve_params(spec, params)

    settings = get_settings()
    settings.ensure_dirs()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = settings.runs_dir / f"{stamp}-{spec.name}"

    manager = get_manager()
    result = await flow.run_flow(manager, spec, resolved, dry_run=False, run_dir=run_dir)

    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "result.json").write_text(
        json.dumps(result["data"], ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    data, truncated = _limit_rows(result["data"])
    warnings = list(result["warnings"])
    if truncated:
        warnings.append("结果超过上限已截断")

    return _ok(
        data,
        flow=spec.name,
        mode=spec.mode,
        page_count=result["page_count"],
        truncated=result["truncated"] or truncated,
        params=resolved,
        warnings=warnings,
        artifacts={"run_dir": str(run_dir), "result": str(run_dir / "result.json")},
    )


def main() -> None:
    run(mcp)


if __name__ == "__main__":
    main()
