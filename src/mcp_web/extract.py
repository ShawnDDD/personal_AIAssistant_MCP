"""页面快照与数据抽取。

快照有三种模式，目的是控制返回给模型的 token 量：
- outline：只列出可交互元素，带 ref，适合探索
- markdown：正文纯文本，适合看内容
- table：直接把表格转成对象数组
"""

from __future__ import annotations

from typing import Any

from mcp_common.errors import ToolError

OUTLINE_JS = """
(limit) => {
  const selector = 'a,button,input,select,textarea,[role="button"],[role="link"],[contenteditable="true"]';
  const result = [];
  for (const el of document.querySelectorAll(selector)) {
    if (result.length >= limit) break;
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) continue;
    const ref = result.length;
    el.setAttribute('data-mcp-ref', String(ref));
    result.push({
      ref,
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute('type') || null,
      id: el.id || null,
      name: el.getAttribute('name') || null,
      placeholder: el.getAttribute('placeholder') || null,
      text: (el.innerText || el.value || '').trim().replace(/\\s+/g, ' ').slice(0, 80)
    });
  }
  return result;
}
"""

TEXT_JS = """
(limit) => {
  const body = document.body ? document.body.innerText : '';
  return body.replace(/\\n{3,}/g, '\\n\\n').slice(0, limit);
}
"""

TABLE_JS = """
(args) => {
  const { selector } = args;
  let table = null;
  if (selector) {
    table = document.querySelector(selector);
  } else {
    let best = 0;
    for (const candidate of document.querySelectorAll('table')) {
      const count = candidate.querySelectorAll('tr').length;
      if (count > best) { best = count; table = candidate; }
    }
  }
  if (!table) return null;
  const rows = [];
  for (const tr of table.querySelectorAll('tr')) {
    const cells = [];
    for (const cell of tr.querySelectorAll('th,td')) {
      cells.push((cell.innerText || '').trim().replace(/\\s+/g, ' '));
    }
    if (cells.length) rows.push(cells);
  }
  return rows;
}
"""

FIELDS_JS = """
(args) => {
  const { root, fields } = args;
  const scope = root ? document.querySelector(root) : document;
  if (!scope) return null;
  const out = {};
  for (const [key, selector] of Object.entries(fields)) {
    const el = scope.querySelector(selector);
    out[key] = el ? (el.innerText || el.value || '').trim() : null;
  }
  return out;
}
"""


AG_GRID_JS = """
(args) => {
  const { selector } = args;
  const root = selector ? document.querySelector(selector) : document;
  if (!root) return null;
  const grid = root.querySelector('.ag-center-cols-container');
  if (!grid) return null;
  const headCells = Array.from(root.querySelectorAll('.ag-header-cell'))
    .filter(h => (h.innerText || '').trim() && h.getAttribute('col-id'));
  const headers = headCells.map(h => (h.innerText || '').trim());
  const keys = headCells.map(h => h.getAttribute('col-id'));
  const rows = Array.from(grid.querySelectorAll('.ag-row')).map(row => {
    const record = {__row_index: row.getAttribute('row-index') || ''};
    row.querySelectorAll('.ag-cell').forEach(cell => {
      const key = cell.getAttribute('col-id');
      if (key) record[key] = (cell.innerText || '').trim();
    });
    return record;
  });
  return {headers, keys, rows};
}
"""


def dedupe_headers(headers: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    result: list[str] = []

    for index, raw in enumerate(headers):
        name = str(raw).strip() or f"col_{index + 1}"
        if name in seen:
            seen[name] += 1
            result.append(f"{name}_{seen[name]}")
        else:
            seen[name] = 0
            result.append(name)

    return result


def resolve_locator(
    page: Any,
    ref: int | None = None,
    selector: str | None = None,
    text: str | None = None,
) -> Any:
    """按 ref / selector / text 三选一拿到 locator，ref 来自 outline 快照。"""
    if ref is not None:
        return page.locator(f'[data-mcp-ref="{ref}"]')
    if selector:
        return page.locator(selector)
    if text:
        return page.get_by_text(text, exact=False)

    raise ToolError("需要提供 ref、selector 或 text 之一", code="BAD_ARGS")


async def outline(page: Any, limit: int = 200) -> list[dict]:
    limit = max(1, min(limit, 500))
    return await page.evaluate(OUTLINE_JS, limit)


async def page_text(page: Any, max_chars: int = 4000) -> str:
    return await page.evaluate(TEXT_JS, max(1, max_chars))


GRID_HEADERS_JS = """
() => Array.from(document.querySelectorAll('.ag-header-cell'))
  .map(h => h.getAttribute('col-id')).filter(Boolean)
"""


async def set_datetime(page: Any, selector: str, index: int, value: str, timeout: int) -> None:
    """向 el-date-picker 输入日期时间，用键盘输入 + Enter 提交更接近人工操作。"""
    field = page.locator(selector).nth(index)
    await field.click(timeout=timeout)
    await page.wait_for_timeout(500)
    await page.keyboard.press("Control+A")
    await page.keyboard.type(value, delay=40)
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(800)
    await page.keyboard.press("Escape")
    await page.wait_for_timeout(500)


async def scroll_grid(page: Any, selector: str, times: int, delta: int = 600) -> list[str]:
    """ag-Grid 的列是虚拟渲染的，需要横向滚动才能让右侧列进入 DOM。"""
    box = await page.locator(selector).first.bounding_box()
    if box is None:
        raise ToolError(f"找不到可滚动的表格区域：{selector}", code="SELECTOR_NOT_FOUND")

    x = box["x"] + box["width"] / 2
    y = box["y"] + min(box["height"] / 2, 200)
    for _ in range(max(1, times)):
        await page.mouse.move(x, y)
        await page.keyboard.down("Shift")
        await page.mouse.wheel(delta, 0)
        await page.keyboard.up("Shift")
        await page.wait_for_timeout(400)
    return await page.evaluate(GRID_HEADERS_JS)


RESET_SCROLL_JS = """
(args) => {
  const el = document.querySelector(args.sel);
  if (!el) return false;
  el.scrollLeft = 0;
  el.dispatchEvent(new Event('scroll', {bubbles: true}));
  return true;
}
"""


VSCROLL_METRICS_JS = """
(args) => {
  const root = args.selector ? document.querySelector(args.selector) : document;
  if (!root) return null;
  const vp = root.querySelector('.ag-body-viewport');
  if (!vp) return null;
  return { top: vp.scrollTop, height: vp.scrollHeight, client: vp.clientHeight };
}
"""

SET_VSCROLL_JS = """
(args) => {
  const root = args.selector ? document.querySelector(args.selector) : document;
  const vp = root && root.querySelector('.ag-body-viewport');
  if (!vp) return false;
  vp.scrollTop = args.top;
  vp.dispatchEvent(new Event('scroll', {bubbles: true}));
  return true;
}
"""


async def _collect_grid_rows(
    page: Any,
    root_selector: str,
    merged: dict[str, dict],
    prefix: str = "",
) -> None:
    """ag-Grid 的行也是虚拟渲染的：纵向分屏滚动，把每屏渲染出的行按 row-index 合并。

    同一个 row-index 在不同横向位置会渲染出不同的列，因此这里用「合并」而不是「覆盖」。
    """
    metrics = await page.evaluate(VSCROLL_METRICS_JS, {"selector": root_selector})

    positions = [0]
    if metrics:
        client = max(int(metrics["client"]), 1)
        height = max(int(metrics["height"]), 1)
        step = max(client - 24, 60)
        positions = list(range(0, height, step)) or [0]

    for top in positions:
        if metrics:
            await page.evaluate(SET_VSCROLL_JS, {"selector": root_selector, "top": top})
            await page.wait_for_timeout(220)

        data = await page.evaluate(AG_GRID_JS, {"selector": root_selector})
        if not data:
            continue

        # 表头随横向滚动变化，每屏按当前可见列重建 col-id → 中文列名映射
        mapping = dict(zip(data["keys"], data["headers"]))
        for row in data["rows"]:
            index = str(row.get("__row_index", ""))
            record = merged.setdefault(f"{prefix}:{index}", {})
            for key, value in row.items():
                if key.startswith("__"):
                    continue
                if value:
                    record[mapping.get(key, key)] = value

    if metrics:
        await page.evaluate(SET_VSCROLL_JS, {"selector": root_selector, "top": 0})
        await page.wait_for_timeout(150)


HSCROLL_JS = """
(args) => {
  const el = document.querySelector(args.sel);
  if (!el) return null;
  const before = el.scrollLeft;
  const max = Math.max(el.scrollWidth - el.clientWidth, 0);
  el.scrollLeft = Math.min(before + args.delta, max);
  el.dispatchEvent(new Event('scroll', {bubbles: true}));
  return { before, after: el.scrollLeft, max };
}
"""

ZOOM_JS = """
(args) => {
  document.body.style.zoom = String(args.zoom);
  return document.body.style.zoom;
}
"""


async def paginate_grid(
    page: Any,
    root_selector: str,
    scroll_selector: str,
    next_selector: str,
    max_pages: int,
    scroll_times: int,
) -> list[dict]:
    """ag-Grid 分页：行与列都是虚拟渲染，需纵向分屏 + 横向滚动后合并，最后翻页。"""
    merged: dict[str, dict] = {}

    # 缩小页面，让一页 10 行尽量都落在视口内，减少需要纵向分屏的次数
    await page.evaluate(ZOOM_JS, {"zoom": 0.67})
    await page.wait_for_timeout(600)

    try:
        for page_no in range(max(1, max_pages)):
            # 翻页后横向位置会保留，必须先回滚到最左，否则右侧列会被滚出视口而不再渲染
            await page.evaluate(RESET_SCROLL_JS, {"sel": scroll_selector})
            await page.wait_for_timeout(300)

            # 横向逐屏滚动到最右，每个位置都做一次纵向全量采集，覆盖隐藏列和隐藏行
            for _ in range(max(1, scroll_times)):
                await _collect_grid_rows(page, root_selector, merged, prefix=str(page_no))
                info = await page.evaluate(
                    HSCROLL_JS, {"sel": scroll_selector, "delta": 800}
                )
                await page.wait_for_timeout(500)
                if not info or info["after"] >= info["max"] - 5 or info["after"] == info["before"]:
                    break
            await _collect_grid_rows(page, root_selector, merged, prefix=str(page_no))

            nxt = page.locator(next_selector).first
            if await nxt.count() == 0:
                break
            classes = await nxt.get_attribute("class") or ""
            disabled = await nxt.get_attribute("disabled")
            if disabled is not None or "disabled" in classes:
                break

            await nxt.click()
            await page.wait_for_timeout(3500)
    finally:
        await page.evaluate(ZOOM_JS, {"zoom": 1})

    return [row for row in merged.values() if row]


async def extract_table(page: Any, selector: str | None) -> dict:
    # 优先按 ag-Grid 解析：这类表格用 div 渲染，没有 <table> 元素
    grid = await page.evaluate(AG_GRID_JS, {"selector": selector})
    if grid is not None and not grid["rows"]:
        raise ToolError(f"表格没有数据行：{selector}", code="EMPTY_TABLE")
    if grid and grid["rows"]:
        # 把 col-id 换成中文列名，调用方拿到的结构与普通表格一致
        mapping = dict(zip(grid["keys"], grid["headers"]))
        rows = [
            {
                mapping.get(key, key): value
                for key, value in row.items()
                if not key.startswith("__")
            }
            for row in grid["rows"]
        ]
        return {
            "type": "ag-grid",
            "headers": grid["headers"],
            "keys": grid["headers"],
            "rows": rows,
            "raw_rows": [],
        }

    rows = await page.evaluate(TABLE_JS, {"selector": selector})
    if rows is None:
        raise ToolError(f"未找到表格：{selector or '页面中任意 table'}", code="SELECTOR_NOT_FOUND")
    if not rows:
        raise ToolError(f"表格没有数据行：{selector}", code="EMPTY_TABLE")

    headers = dedupe_headers(rows[0])
    records = []
    for row in rows[1:]:
        values = list(row) + [""] * (len(headers) - len(row))
        records.append(dict(zip(headers, values[: len(headers)])))

    return {"type": "table", "headers": headers, "keys": headers, "rows": records, "raw_rows": rows}


async def extract_fields(page: Any, selector: str | None, fields: dict[str, str]) -> dict:
    data = await page.evaluate(FIELDS_JS, {"root": selector, "fields": fields})
    if data is None:
        raise ToolError(f"未找到容器：{selector}", code="SELECTOR_NOT_FOUND")
    return data


async def element_text(locator: Any) -> str:
    try:
        return (await locator.first.inner_text()).strip()
    except Exception:  # noqa: BLE001 - 取不到文本时按空处理
        return ""
