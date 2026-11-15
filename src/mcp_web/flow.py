"""YAML 流程引擎：加载、校验、执行。

设计约束：
- 变量只来自 params，用 {{ name }} 引用，不做表达式运算；
- 未知 action / 未知字段 / 未定义变量在加载或执行前报错，并指出位置；
- 默认只读，click 命中危险词会被 policy 拦截。
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mcp_common.config import get_settings
from mcp_common.errors import ToolError

from .extract import (
    element_text,
    extract_fields,
    extract_table,
    paginate_grid,
    resolve_locator,
    scroll_grid,
    set_datetime,
)
from .policy import load_policy

_VAR_PATTERN = re.compile(r"\{\{\s*([a-zA-Z_][\w]*)\s*\}\}")
_ENV_PATTERN = re.compile(r"\{\{\s*env\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class ParamSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: Literal["str", "int", "bool", "date"] = "str"
    required: bool = False
    default: Any = None


class StepSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    action: Literal[
        "goto",
        "fill",
        "click",
        "select",
        "press",
        "wait_for",
        "extract",
        "extract_table",
        "download",
        "sleep",
        "assert",
        "set_datetime",
        "scroll_grid",
        "paginate_grid",
        "click_prev_row",
        "student_status",
        "wait_ready",
        "reveal_on_match",
        "hide",
    ]
    url: str | None = None
    selector: str | None = None
    ref: int | None = None
    text: str | None = None
    value: Any = None
    label: str | None = None
    key: str | None = None
    fields: dict[str, str] | None = None
    as_: str | None = Field(default=None, alias="as")
    timeout_ms: int | None = None
    ms: int | None = None
    index: int | None = None
    times: int | None = None
    optional: bool = False
    # 点击后会在新标签页打开时置为 true，后续步骤自动切换到新页面
    expect_popup: bool = False
    # 点击后等待响应 URL 包含该子串，确保数据已刷新
    expect_response: str | None = None
    # click_prev_row 用：日期列、分组键列、要点击的列，以及锚点日期偏移天数
    date_column: str | None = None
    key_column: str | None = None
    click_column: str | None = None
    offset_days: int = 1
    next_selector: str | None = None
    scroll_selector: str | None = None
    max_pages: int | None = None
    scroll_times: int | None = None
    # click_prev_row 用：总课次列名（可选），用于把锚点课的节次带进结果
    lesson_column: str | None = None
    # student_status 用：把指定变量里的锚点课程信息合并进结果，便于人工复核
    anchor_from: str | None = None
    # click_prev_row 用：没有未来排课时，回看最近 N 天的作业完成情况（0 表示不回看）
    fallback_days: int = 0
    # reveal_on_match 用：数据来源变量、判断列、命中后弹出有头浏览器的触发值
    from_var: str | None = Field(default=None, alias="from")
    match_column: str | None = None
    match_value: str | None = None


class PaginateSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    next_selector: str | None = None
    table_selector: str | None = None
    max_pages: int = 20
    dedupe_by: list[str] | None = None
    stop_when: str | None = None


class FlowSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    site: str = "default"
    mode: Literal["readonly", "mutate"] = "readonly"
    # 单条流程可覆盖全局 MCP_WEB_HEADLESS；不填走全局配置
    headless: bool | None = None
    base_url: str = ""
    params: list[ParamSpec] = Field(default_factory=list)
    steps: list[StepSpec]
    paginate: PaginateSpec | None = None
    output: dict[str, Any] | None = None


def _format_validation(exc: ValidationError) -> str:
    lines = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"])
        lines.append(f"  - {location}: {error['msg']}")
    return "\n".join(lines)


def resolve_path(name_or_path: str) -> Path:
    candidate = Path(name_or_path).expanduser()
    if candidate.is_file():
        return candidate

    directory = get_settings().flows_dir
    for option in (directory / f"{name_or_path}.yaml", directory / name_or_path):
        if option.is_file():
            return option

    available = sorted(path.stem for path in directory.glob("*.yaml") if not path.name.startswith("_"))
    raise ToolError(
        f"找不到流程：{name_or_path}；可用流程：{', '.join(available) or '无'}",
        code="FLOW_INVALID",
    )


def load_flow(name_or_path: str) -> FlowSpec:
    import yaml

    path = resolve_path(name_or_path)

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ToolError(f"YAML 解析失败（{path.name}）：{exc}", code="FLOW_INVALID") from exc

    if not isinstance(data, dict):
        raise ToolError(f"流程文件必须是 YAML 映射（{path.name}）", code="FLOW_INVALID")

    try:
        return FlowSpec.model_validate(data)
    except ValidationError as exc:
        raise ToolError(
            f"流程校验失败（{path.name}）：\n{_format_validation(exc)}",
            code="FLOW_INVALID",
        ) from exc


def list_flows() -> list[dict]:
    directory = get_settings().flows_dir
    if not directory.is_dir():
        return []

    items: list[dict] = []
    for path in sorted(directory.glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        try:
            spec = load_flow(str(path))
        except ToolError as exc:
            items.append({"name": path.stem, "file": path.name, "error": exc.message})
            continue

        items.append(
            {
                "name": spec.name,
                "file": path.name,
                "site": spec.site,
                "mode": spec.mode,
                "description": spec.description,
                "params": [{"name": p.name, "required": p.required, "default": p.default} for p in spec.params],
            }
        )
    return items


def substitute(value: Any, params: dict[str, Any]) -> Any:
    if isinstance(value, str):
        def replace_env(match: re.Match[str]) -> str:
            name = match.group(1)
            resolved = os.environ.get(name, "")
            if not resolved:
                raise ToolError(
                    f"环境变量未设置：{name}；请在本地 .env 中配置（参考 .env.example）",
                    code="FLOW_INVALID",
                )
            return resolved

        value = _ENV_PATTERN.sub(replace_env, value)

        def replace(match: re.Match[str]) -> str:
            key = match.group(1)
            if key == "today":
                return date.today().isoformat()
            if key not in params:
                raise ToolError(
                    f"变量未定义：{{{{ {key} }}}}；可用变量：{', '.join(params) or '无'}",
                    code="FLOW_INVALID",
                )
            return str(params[key])

        return _VAR_PATTERN.sub(replace, value)

    if isinstance(value, list):
        return [substitute(item, params) for item in value]
    if isinstance(value, dict):
        return {key: substitute(item, params) for key, item in value.items()}
    return value


def resolve_params(spec: FlowSpec, provided: dict[str, Any] | None) -> dict[str, Any]:
    provided = provided or {}
    resolved: dict[str, Any] = {}
    missing: list[str] = []

    for param in spec.params:
        value = provided.get(param.name)
        if value is not None:
            resolved[param.name] = value
        elif param.default is not None:
            # 默认值里可能引用环境变量（如 {{ env.ERP_MANAGER }}），这里先解析
            resolved[param.name] = substitute(param.default, {})
        elif param.required:
            missing.append(param.name)
        else:
            resolved[param.name] = ""

    if missing:
        available = ", ".join(p.name for p in spec.params) or "无"
        raise ToolError(
            f"缺少必填参数：{', '.join(missing)}；该流程参数：{available}",
            code="MISSING_PARAMS",
        )

    return resolved


def _absolute_url(base_url: str, url: str) -> str:
    if url.startswith(("http://", "https://", "file://")):
        return url
    if not base_url:
        return url
    return base_url.rstrip("/") + "/" + url.lstrip("/")


async def _paginate(
    page: Any,
    spec: FlowSpec,
    params: dict[str, Any],
    dry_run: bool,
) -> dict | None:
    paginate = spec.paginate
    if paginate is None:
        return None

    selector = substitute(paginate.table_selector, params) if paginate.table_selector else None
    first = await extract_table(page, selector)
    headers = first["headers"]
    rows = list(first["rows"])
    page_count = 1

    step_limit = max(paginate.max_pages - 1, 0)
    clicked = 0

    while clicked < step_limit:
        if paginate.stop_when:
            stop_selector = substitute(paginate.stop_when, params)
            if await page.locator(stop_selector).count() > 0:
                break

        if not paginate.next_selector:
            break

        next_selector = substitute(paginate.next_selector, params)
        button = page.locator(next_selector)
        if await button.count() == 0:
            break

        first_button = button.first
        classes = (await first_button.get_attribute("class")) or ""
        if await first_button.get_attribute("disabled") is not None or "disabled" in classes:
            break

        if dry_run:
            break

        await first_button.click()
        try:
            await page.wait_for_load_state("networkidle", timeout=get_settings().web_timeout_ms)
        except Exception:  # noqa: BLE001 - 有些页面永远不会 idle
            pass
        await page.wait_for_timeout(300)

        current = await extract_table(page, selector)
        if current["headers"] != headers:
            headers = current["headers"]
        rows.extend(current["rows"])
        page_count += 1
        clicked += 1

    truncated = (not dry_run) and step_limit > 0 and clicked >= step_limit

    if paginate.dedupe_by:
        seen: set[tuple] = set()
        unique: list[dict] = []
        for row in rows:
            key = tuple(str(row.get(name, "")).strip() for name in paginate.dedupe_by)
            if key in seen:
                continue
            seen.add(key)
            unique.append(row)
        rows = unique

    return {"headers": headers, "rows": rows, "page_count": page_count, "truncated": truncated}


async def run_flow(
    manager: Any,
    spec: FlowSpec,
    params: dict[str, Any],
    dry_run: bool = False,
    run_dir: Path | None = None,
) -> dict:
    settings = get_settings()
    policy = load_policy()
    allow_dangerous = spec.mode == "mutate"

    if spec.headless is not None:
        await manager.start(spec.headless)
    page = await manager.page(spec.site)
    variables: dict[str, Any] = {}
    log: list[dict] = []
    warnings: list[str] = []
    index = 0
    keep_open = False

    try:
        for index, step in enumerate(spec.steps, start=1):
            entry: dict[str, Any] = {"step": index, "action": step.action}
            timeout = step.timeout_ms or settings.web_timeout_ms

            if step.action == "goto":
                url = substitute(step.url, params) if step.url else ""
                if not url:
                    raise ToolError(f"第 {index} 步 goto 缺少 url", code="FLOW_INVALID")
                entry["url"] = _absolute_url(substitute(spec.base_url, params), url)
                await page.goto(entry["url"], wait_until="domcontentloaded")

            elif step.action == "fill":
                if step.value is None:
                    raise ToolError(f"第 {index} 步 fill 缺少 value", code="FLOW_INVALID")
                locator = resolve_locator(page, step.ref, step.selector, None)
                value = str(substitute(step.value, params))
                entry["target"] = step.selector or f"ref={step.ref}"
                entry["value"] = value
                await locator.first.fill(value, timeout=timeout)
                if step.key:
                    await locator.first.press(step.key)

            elif step.action == "click":
                locator = resolve_locator(
                    page,
                    step.ref,
                    substitute(step.selector, params) if step.selector else None,
                    substitute(step.text, params) if step.text else None,
                )
                label = step.text or await element_text(locator)
                policy.check(label, allow=allow_dangerous)
                entry["target"] = step.text or step.selector or f"ref={step.ref}"
                if dry_run:
                    entry["skipped"] = "dry_run"
                elif step.expect_popup:
                    # 有的按钮是 window.open 打开新标签页，点击后要把后续步骤切到新页面
                    async with page.context.expect_page(timeout=max(timeout, 10_000)) as page_info:
                        await locator.first.click(timeout=timeout)
                    page = await page_info.value
                    await page.wait_for_load_state("domcontentloaded", timeout=timeout)
                    await page.wait_for_timeout(1500)
                    entry["popup_url"] = page.url
                    if step.as_:
                        variables[step.as_] = {"url": page.url, "title": await page.title()}
                elif step.expect_response:
                    # 点完等接口返回，确保表格已刷新到新结果（避免读到旧行）
                    async with page.expect_response(
                        lambda r: step.expect_response in r.url, timeout=timeout
                    ) as resp_info:
                        await locator.first.click(timeout=timeout)
                    await page.wait_for_timeout(600)
                    response = await resp_info.value
                    entry["response"] = response.url
                else:
                    await locator.first.click(timeout=timeout)

            elif step.action == "select":
                locator = resolve_locator(page, step.ref, step.selector, None)
                label = substitute(step.label, params) if step.label else None
                value = substitute(step.value, params) if step.value is not None else None
                entry["target"] = step.selector or f"ref={step.ref}"
                if dry_run:
                    entry["skipped"] = "dry_run"
                elif label is not None:
                    await locator.first.select_option(label=str(label), timeout=timeout)
                elif value is not None:
                    await locator.first.select_option(value=str(value), timeout=timeout)
                else:
                    raise ToolError(f"第 {index} 步 select 需要 value 或 label", code="FLOW_INVALID")

            elif step.action == "press":
                if not step.key:
                    raise ToolError(f"第 {index} 步 press 缺少 key", code="FLOW_INVALID")
                entry["key"] = step.key
                if not dry_run:
                    await page.keyboard.press(step.key)

            elif step.action == "wait_for":
                entry["target"] = step.selector or step.text or "networkidle"
                if step.selector:
                    await page.wait_for_selector(substitute(step.selector, params), timeout=timeout)
                elif step.text:
                    await page.get_by_text(substitute(step.text, params)).first.wait_for(timeout=timeout)
                else:
                    try:
                        await page.wait_for_load_state("networkidle", timeout=timeout)
                    except Exception:  # noqa: BLE001
                        warnings.append(f"第 {index} 步等待 networkidle 超时，已继续")

            elif step.action == "extract":
                if not step.fields:
                    raise ToolError(f"第 {index} 步 extract 缺少 fields", code="FLOW_INVALID")
                data = await extract_fields(
                    page,
                    substitute(step.selector, params) if step.selector else None,
                    {key: substitute(value, params) for key, value in step.fields.items()},
                )
                variables[step.as_ or f"extract_{index}"] = data
                entry["as"] = step.as_ or f"extract_{index}"

            elif step.action == "extract_table":
                try:
                    data = await extract_table(
                        page,
                        substitute(step.selector, params) if step.selector else None,
                    )
                except ToolError:
                    if not step.optional:
                        raise
                    data = {"rows": [], "headers": []}
                variables[step.as_ or f"table_{index}"] = data["rows"]
                entry["as"] = step.as_ or f"table_{index}"
                entry["rows"] = len(data["rows"])

            elif step.action == "set_datetime":
                if not step.selector or step.value is None:
                    raise ToolError(
                        f"第 {index} 步 set_datetime 缺少 selector 或 value",
                        code="FLOW_INVALID",
                    )
                value = str(substitute(step.value, params))
                entry["target"] = step.selector
                entry["value"] = value
                if dry_run:
                    entry["skipped"] = "dry_run"
                else:
                    await set_datetime(
                        page,
                        substitute(step.selector, params),
                        step.index or 0,
                        value,
                        timeout,
                    )

            elif step.action == "scroll_grid":
                if not step.selector:
                    raise ToolError(f"第 {index} 步 scroll_grid 缺少 selector", code="FLOW_INVALID")
                entry["target"] = step.selector
                entry["times"] = step.times or 1
                if dry_run:
                    entry["skipped"] = "dry_run"
                else:
                    entry["rendered_columns"] = await scroll_grid(
                        page,
                        substitute(step.selector, params),
                        step.times or 1,
                        step.ms or 600,
                    )

            elif step.action == "paginate_grid":
                if not step.selector or not step.next_selector:
                    raise ToolError(
                        f"第 {index} 步 paginate_grid 缺少 selector 或 next_selector",
                        code="FLOW_INVALID",
                    )
                entry["target"] = step.selector
                if dry_run:
                    entry["skipped"] = "dry_run"
                    variables[step.as_ or f"rows_{index}"] = []
                else:
                    rows = await paginate_grid(
                        page,
                        substitute(step.selector, params),
                        substitute(step.scroll_selector or step.selector, params),
                        substitute(step.next_selector, params),
                        step.max_pages or 20,
                        step.scroll_times or 5,
                    )
                    variables[step.as_ or f"rows_{index}"] = rows
                    entry["rows"] = len(rows)

            elif step.action == "click_prev_row":
                if not (step.selector and step.date_column and step.key_column and step.click_column):
                    raise ToolError(
                        f"第 {index} 步 click_prev_row 需要 selector / date_column / key_column / click_column",
                        code="FLOW_INVALID",
                    )
                entry["target"] = step.selector
                if dry_run:
                    entry["skipped"] = "dry_run"
                else:
                    target_el, picked = await _find_prev_row(page, step, params)
                    flat_rows = picked.pop("_flat", None)
                    if target_el is None and picked.get("no_upcoming"):
                        if step.fallback_days:
                            # 已结课：改为汇总最近 N 天的作业完成情况
                            lessons = await _fetch_lessons(page)
                            if lessons:
                                picked = await _scan_month_status(page, step, params, lessons, timeout)
                            else:
                                # VIP 1对1 等 packageCode 页面拿不到课次接口，走表格逐行点击
                                picked = await _scan_month_status_ui(
                                    page, step, params, flat_rows or [], timeout
                                )
                            entry.update(picked)
                            variables[step.as_ or f"detail_{index}"] = picked
                            log.append(entry)
                            break
                        picked["no_detail"] = True
                        picked["message"] = f"{picked['anchor_date']} 及之后没有排课，无法确定最近一节课"
                    entry.update(picked)
                    if target_el is None:
                        # 上一次课未布置作业，直接以该结论作为流程结果，后续步骤不再执行
                        variables[step.as_ or f"detail_{index}"] = picked
                        log.append(entry)
                        break
                    if step.expect_popup:
                        async with page.context.expect_page(
                            timeout=max(timeout, 10_000)
                        ) as page_info:
                            await target_el.click(timeout=timeout)
                        page = await page_info.value
                        await page.wait_for_load_state("domcontentloaded", timeout=timeout)
                        await page.wait_for_timeout(1500)
                        entry["popup_url"] = page.url
                        if step.as_:
                            # 保留锚点课程信息（anchor_*），供后续步骤合并进结果
                            variables[step.as_] = {**picked, "url": page.url, "title": await page.title()}
                    else:
                        await target_el.click(timeout=timeout)

            elif step.action == "student_status":
                if not step.value:
                    raise ToolError(f"第 {index} 步 student_status 缺少 value（学生姓名）", code="FLOW_INVALID")
                student = str(substitute(step.value, params))
                entry["student"] = student
                if dry_run:
                    entry["skipped"] = "dry_run"
                    continue

                header = ""
                if step.selector:
                    header_el = page.locator(substitute(step.selector, params)).first
                    header = re.sub(r"\s+", " ", await header_el.inner_text()).strip()

                found: dict[str, Any] = {"missing": True}
                for _ in range(max(1, timeout // 300)):
                    found = await page.evaluate(
                        """(name) => {
                            const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                            for (const tr of document.querySelectorAll('tbody tr')) {
                                const cell = tr.querySelector('.cell-name-left [title], .cell-name-left');
                                if (!cell) continue;
                                const rowName = norm(cell.getAttribute('title') || cell.textContent);
                                if (rowName !== name) continue;
                                const el = tr.querySelector('.status-red, .status-green, .status-gray, .status-blue');
                                return {status: el ? norm(el.textContent) : ''};
                            }
                            return {status: '', missing: true};
                        }""",
                        student,
                    )
                    if not found.get("missing"):
                        break
                    await page.wait_for_timeout(300)
                if found.get("missing"):
                    raise ToolError(f"页面上没有找到学生「{student}」的行", code="SELECTOR_NOT_FOUND")

                status = found["status"]
                head = _format_header(header)
                day_match = re.search(r"\d{4}-\d{2}-\d{2}", head)
                shot = await _snapshot(page, student, day_match.group(0) if day_match else "", status)
                result: dict[str, Any] = {
                    "student": student,
                    "status": status,
                    "header": head,
                    "screenshot": shot,
                }
                # 合并锚点课程信息，方便人工二次确认「为什么取这一节课」
                anchor = variables.get(step.anchor_from) if step.anchor_from else None
                if isinstance(anchor, dict):
                    for field in (
                        "anchor_date",
                        "anchor_teacher",
                        "anchor_lesson",
                        "anchor_time",
                        "row_date",
                        "cell_text",
                    ):
                        if anchor.get(field):
                            result[field] = anchor[field]
                if "未完成" in status:
                    result["message"] = f"{head}，{student} 作业未完成"
                variables[step.as_ or f"status_{index}"] = result
                entry["status"] = status
                entry["message"] = result.get("message", "")

            elif step.action == "wait_ready":
                # 等页面数据真正刷新：遮罩消失 + 网络静默，避免读到上一个页面的残留表格
                mask = substitute(step.selector, params) if step.selector else ".el-loading-mask"
                entry["target"] = mask
                if dry_run:
                    entry["skipped"] = "dry_run"
                    continue
                try:
                    await page.wait_for_selector(mask, state="hidden", timeout=timeout)
                except Exception:  # noqa: BLE001 - 页面没有遮罩时直接通过
                    pass
                try:
                    await page.wait_for_load_state("networkidle", timeout=min(timeout, 15000))
                except Exception:  # noqa: BLE001 - 轮询型页面可能长期不 idle
                    pass
                entry["settled"] = True

            elif step.action == "reveal_on_match":
                # 命中指定条件时，关掉无头浏览器，用有头浏览器重放筛选条件并保持打开
                rows = variables.get(step.from_var or "")
                column = substitute(step.match_column or "", params)
                wanted = substitute(step.match_value or "", params)
                entry["column"] = column
                if not isinstance(rows, list):
                    raise ToolError(
                        f"第 {index} 步 reveal_on_match 找不到数据变量：{step.from_var}",
                        code="FLOW_INVALID",
                    )
                hits = [r for r in rows if isinstance(r, dict) and str(r.get(column, "")).strip() == wanted]
                entry["matched"] = len(hits)
                if dry_run:
                    entry["skipped"] = "dry_run"
                    continue
                if not hits:
                    continue

                await manager.close()
                await manager.start(False)
                page = await manager.page(spec.site)
                window_id = await _hide_window(page)
                await _replay_filters(page, spec, params)
                await _show_window(page, window_id)
                keep_open = True
                entry["revealed"] = True
                entry["url"] = page.url
                entry["sample"] = [
                    {key: r.get(key, "") for key in ("学员姓名", "课次时间", column)}
                    for r in hits[:5]
                ]

            elif step.action == "hide":
                # 隐藏遮挡点击的浮层（如 Copilot 气泡、公告）
                selector = substitute(step.selector or "", params)
                entry["target"] = selector
                if dry_run:
                    entry["skipped"] = "dry_run"
                    continue
                hidden = await page.evaluate(
                    """(sel) => {
                        let n = 0;
                        document.querySelectorAll(sel).forEach((el) => {
                            el.style.display = 'none';
                            n += 1;
                        });
                        return n;
                    }""",
                    selector,
                )
                entry["hidden"] = hidden

            elif step.action == "sleep":
                milliseconds = step.ms or 1000
                entry["ms"] = milliseconds
                if not dry_run:
                    await asyncio.sleep(milliseconds / 1000)

            elif step.action == "assert":
                locator = resolve_locator(
                    page,
                    step.ref,
                    substitute(step.selector, params) if step.selector else None,
                    substitute(step.text, params) if step.text else None,
                )
                if await locator.count() == 0:
                    raise ToolError(
                        f"第 {index} 步断言失败，元素不存在：{step.selector or step.text or step.ref}",
                        code="SELECTOR_NOT_FOUND",
                    )

            log.append(entry)

        paginated = await _paginate(page, spec, params, dry_run)

    except ToolError as exc:
        if run_dir is not None:
            await _save_failure(page, run_dir, index)
        raise
    except Exception as exc:  # noqa: BLE001 - 统一翻译成可操作错误
        if run_dir is not None:
            await _save_failure(page, run_dir, index)
        raise ToolError(f"流程执行失败：{exc}", code="BROWSER_ERROR") from exc

    if paginated is not None:
        data: Any = paginated["rows"]
        page_count = paginated["page_count"]
        truncated = paginated["truncated"]
        if truncated:
            warnings.append(f"翻页达到上限 {spec.paginate.max_pages if spec.paginate else 0} 页，结果可能不完整")
    else:
        data = _build_output(spec, variables)
        page_count = 1
        truncated = False

    if dry_run:
        warnings.append("dry_run 模式：未执行 click / select / press / download")

    return {
        "data": data,
        "warnings": warnings,
        "log": log,
        "page_count": page_count,
        "truncated": truncated,
        "mode": spec.mode,
        "keep_open": keep_open,
    }


def _format_header(text: str) -> str:
    """把「老师 | 日期 课程-班次、课时 时间」整理成「老师｜日期 课程」。"""
    text = re.sub(r"\s+", " ", text or "").strip()
    if "|" not in text:
        return text
    teacher, rest = (part.strip() for part in text.split("|", 1))
    # 去掉「-3、4/14节课 13:20-15:20」这类班次与课时尾巴
    rest = re.sub(r"-\d+[、，,].*$", "", rest).strip()
    return f"{teacher}｜{rest}"


async def _scan_row_page(page: Any, step: StepSpec) -> list[dict[str, str]]:
    """读取当前页的表格行：日期列、键列、目标列文本。"""
    result = await page.evaluate(
        """(args) => {
            const rows = Array.from(document.querySelectorAll(args.selector));
            const headerTable = Array.from(document.querySelectorAll('table'))
                .find((t) => t.querySelectorAll('thead th').length > 0);
            const headers = headerTable
                ? Array.from(headerTable.querySelectorAll('thead th')).map(th => (th.innerText || '').trim())
                : [];
            const dIdx = headers.indexOf(args.date_column);
            const kIdx = headers.indexOf(args.key_column);
            const cIdx = headers.indexOf(args.click_column);
            const lIdx = args.lesson_column ? headers.indexOf(args.lesson_column) : -1;
            if (dIdx < 0 || kIdx < 0 || cIdx < 0) return {error: '表头缺少指定的列', headers};
            return {
                rows: rows.map((tr) => {
                    const c = Array.from(tr.querySelectorAll('td')).map(td => (td.innerText || '').trim());
                    return {
                        date: c[dIdx] || '',
                        key: c[kIdx] || '',
                        cell_text: c[cIdx] || '',
                        lesson: lIdx >= 0 ? (c[lIdx] || '') : '',
                    };
                }),
            };
        }""",
        {
            "selector": step.selector,
            "date_column": step.date_column,
            "key_column": step.key_column,
            "click_column": step.click_column,
            "lesson_column": step.lesson_column,
        },
    )
    if result.get("error"):
        hint = f"；表头：{', '.join(result['headers'])}" if result.get("headers") else ""
        raise ToolError(f"click_prev_row 失败：{result['error']}{hint}", code="SELECTOR_NOT_FOUND")
    return result["rows"]


async def _fetch_lessons(page: Any) -> list[dict[str, Any]]:
    """从 IL 课次接口取整班课次（含上课时间、是否已布置作业）。"""
    data = await page.evaluate(
        """async () => {
            const u = new URL(location.href);
            const classCode = u.searchParams.get('classCode') || '';
            const schoolId = u.searchParams.get('schoolId') || '';
            if (!classCode) return [];
            const url = `/plus/api/standard/admin/class/book/lessonList?schoolId=${schoolId}`
                + `&classCode=${classCode}&pageNo=1&pageSize=300&lessonOrder=0`;
            const body = await (await fetch(url, {credentials: 'include'})).json();
            const d = (body && body.data) || {};
            const list = d.list || d.records || [];
            return list.map((it) => ({
                date: (it.startDate || it.classBeginDate || '').slice(0, 10),
                lesson: it.lessonNos || String(it.lessonNo || ''),
                teacher: it.teacherName || '',
                teacher_code: it.teacherCode || '',
                begin: (it.sectBegin || '').slice(11, 16),
                end: (it.sectEnd || '').slice(11, 16),
                has_work: it.hasWork || 0,
            }));
        }"""
    )
    return [item for item in data if item.get("date")]


async def _read_student_status(page: Any, student: str, timeout: int) -> str | None:
    """在明细页定位学生行并读完成情况；找不到返回 None。"""
    found: dict[str, Any] = {"missing": True}
    for _ in range(max(1, timeout // 300)):
        found = await page.evaluate(
            """(name) => {
                const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                for (const tr of document.querySelectorAll('tbody tr')) {
                    const cell = tr.querySelector('.cell-name-left [title], .cell-name-left');
                    if (!cell) continue;
                    const rowName = norm(cell.getAttribute('title') || cell.textContent);
                    if (rowName !== name) continue;
                    const el = tr.querySelector('.status-red, .status-green, .status-gray, .status-blue');
                    return {status: el ? norm(el.textContent) : ''};
                }
                return {status: '', missing: true};
            }""",
            student,
        )
        if not found.get("missing"):
            return found["status"]
        await page.wait_for_timeout(300)
    return None


async def _scan_month_status(
    page: Any, step: StepSpec, params: dict[str, Any], lessons: list[dict[str, Any]], timeout: int
) -> dict[str, Any]:
    """无未来排课时：汇总该学生过去一段时间的作业完成情况。"""
    student = str(substitute(step.value, params)) if step.value else ""
    window = step.fallback_days or 30
    start = (date.today() - timedelta(days=window)).isoformat()
    candidates = sorted(
        [x for x in lessons if x["date"] >= start], key=lambda x: x["date"], reverse=True
    )
    if not candidates:
        # 近 N 天没有排课（课程更早结课）：退化为该班最近 4 节课
        candidates = sorted(lessons, key=lambda x: x["date"], reverse=True)[:4]
    if not candidates:
        return {
            "student": student,
            "status": "无排课",
            "no_detail": True,
            "message": f"{start} 以来没有排课记录",
        }

    base = page.url.split("/plus/")[0]
    query = dict(
        part.split("=", 1) for part in page.url.split("?", 1)[-1].split("&") if "=" in part
    )
    class_code = query.get("classCode", "")
    school_id = query.get("schoolId", "")

    rows: list[dict[str, Any]] = []
    shot = ""
    for item in candidates:
        entry = {
            "date": item["date"],
            "lesson": item["lesson"],
            "teacher": item["teacher"],
            "time": f"{item['begin']}-{item['end']}" if item["begin"] and item["end"] else "",
        }
        if not item.get("has_work"):
            entry["status"] = "未布置"
            rows.append(entry)
            continue
        detail = (
            f"{base}/plus/homework/homework-result/1?classCode={class_code}&schoolId={school_id}"
            f"&lessonType=2&lessonNo={item['lesson']}&teacherCode={item['teacher_code']}"
            f"&startDate={item['date']}&pageType=list"
        )
        await page.goto(detail, wait_until="domcontentloaded", timeout=timeout)
        await page.wait_for_timeout(1200)
        status = await _read_student_status(page, student, min(timeout, 15000))
        entry["status"] = status or "未知"
        rows.append(entry)
        shot = await _snapshot(page, student, item["date"], entry["status"])

    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    summary = "、".join(f"{k} {v} 次" for k, v in counts.items())
    return {
        "student": student,
        "status": "按月汇总",
        "window_start": start,
        "lessons": rows,
        "screenshot": shot,
        "message": f"{student} 近{window}天（{start} 起）共 {len(rows)} 节课：{summary}",
    }


async def _cell_target(page: Any, step: StepSpec, row_idx: int, cell_idx: int, text: str) -> Any:
    """取行内目标单元格里可点击的元素（优先文本匹配的内层元素）。"""
    cell = page.locator(step.selector).nth(row_idx).locator("td").nth(cell_idx)
    for el in await cell.locator("a, span, div, p").all():
        try:
            if (await el.inner_text()).strip() == text:
                return el
        except Exception:  # noqa: BLE001 - 元素可能已失效
            continue
    return cell


async def _scan_month_status_ui(
    page: Any, step: StepSpec, params: dict[str, Any], flat: list[Any], timeout: int
) -> dict[str, Any]:
    """无课次接口时（VIP 1对1）：用表格逐行点开明细页汇总完成情况。"""
    student = str(substitute(step.value, params)) if step.value else ""
    window = step.fallback_days or 30
    start = (date.today() - timedelta(days=window)).isoformat()
    cands = [x for x in flat if x[2]["date"] and x[2]["date"] >= start]
    if not cands:
        # 近 N 天无课（课程更早结课）：退化为最近 4 节课
        cands = sorted(
            [x for x in flat if x[2]["date"]], key=lambda x: x[2]["date"], reverse=True
        )[:4]
    if not cands:
        return {"student": student, "status": "无排课", "no_detail": True, "message": "没有可回看的课次"}

    cands = sorted(cands, key=lambda x: x[2]["date"], reverse=True)
    cell_idx = await _cell_index(page, step)
    rows: list[dict[str, Any]] = []
    shot = ""
    current_page: int | None = None
    for page_idx, row_idx, data in cands:
        entry: dict[str, Any] = {
            "date": data["date"],
            "lesson": data.get("lesson", ""),
            "teacher": data["key"],
            "time": "",
        }
        if data["cell_text"] in ("", "--", "-"):
            entry["status"] = "未布置"
            rows.append(entry)
            continue
        if current_page != page_idx:
            await _goto_page(page, page_idx)
            current_page = page_idx
        target = await _cell_target(page, step, row_idx, cell_idx, data["cell_text"])
        async with page.context.expect_page(timeout=max(timeout, 10_000)) as pinfo:
            await target.click(timeout=timeout)
        detail = await pinfo.value
        await detail.wait_for_load_state("domcontentloaded", timeout=timeout)
        await detail.wait_for_timeout(1200)
        status = await _read_student_status(detail, student, min(timeout, 15_000)) or "未知"
        entry["status"] = status
        # 明细页头部带上课时间（如 13:20-15:20;15:40-17:40），从文本里取
        try:
            head_text = re.sub(
                r"\s+", " ", await detail.locator(".part-left p").first.inner_text()
            ).strip()
        except Exception:  # noqa: BLE001 - 头部结构变化时不影响主流程
            head_text = ""
        time_match = re.search(
            r"\d{1,2}:\d{2}-\d{1,2}:\d{2}(?:;\d{1,2}:\d{2}-\d{1,2}:\d{2})*", head_text
        )
        entry["time"] = time_match.group(0) if time_match else ""
        rows.append(entry)
        shot = await _snapshot(detail, student, data["date"], status)
        await detail.close()

    counts: dict[str, int] = {}
    for item in rows:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    summary = "、".join(f"{k} {v} 次" for k, v in counts.items())
    return {
        "student": student,
        "status": "按月汇总",
        "window_start": start,
        "lessons": rows,
        "screenshot": shot,
        "message": f"{student} 近{window}天共 {len(rows)} 节课：{summary}",
    }


async def _goto_page(page: Any, index: int) -> None:
    """切到第 index 页（0 基），优先点页码，退化时用上一页按钮回退。"""
    pager = page.locator(".el-pager li.number")
    if await pager.count() > index:
        await pager.nth(index).click()
        await page.wait_for_timeout(1200)
        return
    prev = page.locator(".btn-prev")
    for _ in range(30):
        if await prev.count() == 0 or await prev.get_attribute("disabled") is not None:
            break
        await prev.first.click()
        await page.wait_for_timeout(900)


async def _snapshot(page: Any, student: str, day: str, status: str) -> str:
    """按「学生_日期_完成情况」保存整页截图，返回绝对路径。"""
    safe = re.sub(r"[\\/:*?\"<>|\s]+", "_", f"{student}_{day}_{status or '未知'}").strip("_")
    shot = Path(__file__).resolve().parents[2] / "data" / "screenshots" / f"{safe}.png"
    shot.parent.mkdir(parents=True, exist_ok=True)
    await page.screenshot(path=str(shot), full_page=True)
    return str(shot)


async def _hide_window(page: Any) -> int | None:
    """把有头浏览器窗口挪到屏幕外，让重放筛选的过程不可见。返回 windowId。"""
    try:
        cdp = await page.context.new_cdp_session(page)
        info = await cdp.send("Browser.getWindowForTarget")
        window_id = info["windowId"]
        # 先取消最大化，否则设置坐标无效
        await cdp.send("Browser.setWindowBounds", {"windowId": window_id, "bounds": {"windowState": "normal"}})
        await cdp.send(
            "Browser.setWindowBounds",
            {"windowId": window_id, "bounds": {"left": -3200, "top": 0, "width": 1680, "height": 1000}},
        )
        return window_id
    except Exception:  # noqa: BLE001 - 窗口控制失败不影响查询
        return None


async def _show_window(page: Any, window_id: int | None) -> None:
    """把窗口移回屏内并最大化，留给人工操作。"""
    if window_id is not None:
        try:
            cdp = await page.context.new_cdp_session(page)
            await cdp.send(
                "Browser.setWindowBounds",
                {"windowId": window_id, "bounds": {"left": 0, "top": 0, "width": 1680, "height": 1000}},
            )
            await cdp.send("Browser.setWindowBounds", {"windowId": window_id, "bounds": {"windowState": "maximized"}})
        except Exception:  # noqa: BLE001
            pass
    try:
        await page.bring_to_front()
    except Exception:  # noqa: BLE001
        pass


async def _replay_filters(page: Any, spec: FlowSpec, params: dict[str, Any]) -> None:
    """在有头浏览器里重放「填筛选条件 → 查询」这一段，用于把结果页展示给人看。

    只重放到第一次数据抓取（paginate_grid / click_prev_row）之前，避免重复抓一遍数据。
    """
    base_url = substitute(spec.base_url, params)
    timeout = get_settings().web_timeout_ms
    for step in spec.steps:
        if step.action in ("paginate_grid", "click_prev_row", "student_status", "reveal_on_match"):
            break
        if step.action == "goto":
            await page.goto(_absolute_url(base_url, substitute(step.url or "", params)), wait_until="domcontentloaded")
        elif step.action == "wait_for" and step.selector:
            await page.wait_for_selector(substitute(step.selector, params), state="visible", timeout=step.timeout_ms or timeout)
        elif step.action == "set_datetime":
            await set_datetime(
                page,
                substitute(step.selector or "", params),
                step.index or 0,
                substitute(step.value, params),
                step.timeout_ms or timeout,
            )
        elif step.action == "fill":
            await page.fill(
                substitute(step.selector or "", params),
                substitute(step.value, params),
                timeout=step.timeout_ms or timeout,
            )
        elif step.action == "click":
            await page.click(substitute(step.selector or "", params), timeout=step.timeout_ms or timeout)
        elif step.action == "wait_ready":
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:  # noqa: BLE001 - 轮询型页面可能长期不 idle
                pass
    # 说明：重放时故意跳过 sleep 步骤，用接口静默代替固定等待
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:  # noqa: BLE001 - 轮询型页面可能长期不 idle
        pass
    await page.wait_for_timeout(800)


async def _find_prev_row(page: Any, step: StepSpec, params: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """找「今天 + offset_days」之后最近的一节课，取该行 key 列的值，再定位同一 key 更早的最近一行。

    表格有分页时会在各页之间移动，最终回到目标行所在页并返回可点击元素。
    """
    anchor = (date.today() + timedelta(days=step.offset_days)).isoformat()

    collected: list[list[dict[str, str]]] = []
    max_pages = max(1, step.max_pages or 20)
    for _ in range(max_pages):
        collected.append(await _scan_row_page(page, step))
        if not step.next_selector:
            break
        nxt = page.locator(step.next_selector).first
        if await nxt.count() == 0:
            break
        if await nxt.get_attribute("disabled") is not None:
            break
        if "disabled" in (await nxt.get_attribute("class") or ""):
            break
        await nxt.click()
        await page.wait_for_timeout(1200)

    flat = [
        (page_index, row_index, row)
        for page_index, rows in enumerate(collected)
        for row_index, row in enumerate(rows)
    ]
    if not flat:
        raise ToolError("click_prev_row 失败：表格没有数据行", code="SELECTOR_NOT_FOUND")

    upcoming = [item for item in flat if item[2]["date"] and item[2]["date"] >= anchor]
    if not upcoming:
        # 没有未来排课（如已结课）：交给上层决定是否回看历史
        return None, {"anchor_date": anchor, "no_upcoming": True, "status": "无排课", "_flat": flat}
    _, _, anchor_data = min(upcoming, key=lambda x: (x[2]["date"], x[0], x[1]))
    key = anchor_data["key"]

    # 锚点课的上课时间：优先取课次接口里的 sectBegin/sectEnd
    lessons = await _fetch_lessons(page)
    anchor_lesson = anchor_data.get("lesson", "")
    anchor_time = ""
    for item in lessons:
        if item["date"] != anchor_data["date"]:
            continue
        if anchor_lesson and item["lesson"].replace(",", "、") != anchor_lesson:
            continue
        if item["begin"] and item["end"]:
            anchor_time = f"{item['begin']}-{item['end']}"
        break

    earlier = [
        item
        for item in flat
        if item[2]["key"] == key and item[2]["date"] and item[2]["date"] < anchor_data["date"]
    ]
    if not earlier:
        raise ToolError(f"click_prev_row 失败：{key} 在该日期之前没有更早的记录", code="SELECTOR_NOT_FOUND")
    target_page, target_row, target_data = max(earlier, key=lambda x: (x[2]["date"], x[0], x[1]))

    if target_page != len(collected) - 1:
        await _goto_page(page, target_page)

    row = page.locator(step.selector).nth(target_row)
    cell_index = await _cell_index(page, step)
    cell = row.locator("td").nth(cell_index)
    cell_text = target_data["cell_text"]
    if cell_text in ("", "--", "-"):
        student = str(substitute(step.value, params)) if step.value else ""
        shot = await _snapshot(page, student, target_data["date"], "未布置")
        picked = {
            "anchor_date": anchor_data["date"],
            "anchor_teacher": key,
            "anchor_lesson": anchor_data.get("lesson", ""),
            "anchor_time": anchor_time,
            "key": key,
            "row_date": target_data["date"],
            "cell_text": cell_text or "--",
            "status": "未布置",
            "no_detail": True,
            "message": f"{key}｜{target_data['date']} 该次课未布置作业，无作业完成情况",
            "screenshot": shot,
        }
        return None, picked

    target = cell
    inner = cell.locator("span, a, button")
    for i in range(await inner.count()):
        element = inner.nth(i)
        if (await element.inner_text()).strip() == cell_text:
            target = element
            break

    picked = {
        "anchor_date": anchor_data["date"],
        "anchor_teacher": key,
        "anchor_lesson": anchor_data.get("lesson", ""),
        "anchor_time": anchor_time,
        "key": key,
        "row_date": target_data["date"],
        "cell_text": cell_text,
        "pages": len(collected),
        "row_index": target_row,
    }
    return target, picked


async def _cell_index(page: Any, step: StepSpec) -> int:
    """取 click_column 在表头中的列下标。"""
    index = await page.evaluate(
        """(name) => {
            const headerTable = Array.from(document.querySelectorAll('table'))
                .find((t) => t.querySelectorAll('thead th').length > 0);
            if (!headerTable) return -1;
            return Array.from(headerTable.querySelectorAll('thead th'))
                .map(th => (th.innerText || '').trim()).indexOf(name);
        }""",
        step.click_column,
    )
    if index is None or index < 0:
        raise ToolError(f"click_prev_row 失败：表头没有「{step.click_column}」列", code="SELECTOR_NOT_FOUND")
    return index


def _build_output(spec: FlowSpec, variables: dict[str, Any]) -> Any:
    if spec.output:
        source = spec.output.get("from")
        if not (isinstance(source, str) and source in variables):
            for candidate in spec.output.get("from_any") or []:
                if isinstance(candidate, str) and candidate in variables:
                    source = candidate
                    break
        if isinstance(source, str) and source in variables:
            data = variables[source]
            where = spec.output.get("where")
            if isinstance(data, list) and isinstance(where, dict):
                # 只保留指定列等于给定值的行，例如只取某个学管的记录
                data = [
                    row
                    for row in data
                    if isinstance(row, dict)
                    and all(str(row.get(key, "")) == str(value) for key, value in where.items())
                ]
            return data
    if len(variables) == 1:
        return next(iter(variables.values()))
    return variables


async def _save_failure(page: Any, run_dir: Path, step: int) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        await page.screenshot(path=str(run_dir / f"error-step{step}.png"), full_page=True)
        html = await page.content()
        (run_dir / f"error-step{step}.html").write_text(html, encoding="utf-8")
    except Exception:  # noqa: BLE001 - 保存现场失败不应掩盖原始错误
        pass
