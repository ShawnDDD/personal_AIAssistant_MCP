"""Playwright 会话管理。

每个站点一个持久化 profile（data/profiles/<site>.json），存 cookie + localStorage。
首次接入时以有头模式人工登录一次，之后复用。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from mcp_common.config import get_settings
from mcp_common.errors import ToolError


class BrowserManager:
    def __init__(self) -> None:
        self._playwright: Any = None
        self._browser: Any = None
        self._contexts: dict[str, Any] = {}
        self._pages: dict[str, Any] = {}
        self.headless: bool | None = None

    async def start(self, headless: bool | None = None) -> None:
        from playwright.async_api import async_playwright

        settings = get_settings()
        wanted = settings.web_headless if headless is None else headless

        if self._browser is not None and self.headless == wanted:
            return
        if self._browser is not None:
            await self.close()

        try:
            self._playwright = await async_playwright().start()
            # channel="chromium" 用完整 Chromium 的新 headless 模式，
            # 避免额外依赖 chromium-headless-shell 组件。
            try:
                self._browser = await self._playwright.chromium.launch(
                    headless=wanted, channel="chromium", args=["--start-maximized"]
                )
            except Exception:  # noqa: BLE001 - 老版本 Playwright 不认 channel 参数
                self._browser = await self._playwright.chromium.launch(
                    headless=wanted, args=["--start-maximized"]
                )
        except Exception as exc:  # noqa: BLE001 - 需要把底层错误翻译成可操作提示
            raise ToolError(
                "浏览器启动失败。请先执行：playwright install chromium",
                code="BROWSER_ERROR",
            ) from exc

        self.headless = wanted

    async def context(self, site: str) -> Any:
        await self.start()
        if site in self._contexts:
            return self._contexts[site]

        settings = get_settings()
        settings.ensure_dirs()
        state = settings.profiles_dir / f"{site}.json"

        kwargs: dict[str, Any] = {}
        if self.headless:
            # 无头模式给一个足够宽的视口，避免宽表格按可视宽度裁掉右侧列
            kwargs["viewport"] = {"width": 1920, "height": 1080}
        else:
            # 可视模式下不锁死视口，让窗口最大化，避免页面右下角被屏幕裁掉
            kwargs["no_viewport"] = True
        if state.is_file():
            kwargs["storage_state"] = str(state)

        context = await self._browser.new_context(**kwargs)
        context.set_default_timeout(settings.web_timeout_ms)
        context.set_default_navigation_timeout(settings.web_timeout_ms)
        self._contexts[site] = context
        return context

    async def page(self, site: str = "default") -> Any:
        context = await self.context(site)
        page = self._pages.get(site)
        if page is None or page.is_closed():
            page = await context.new_page()
            self._pages[site] = page
        return page

    async def save_state(self, site: str) -> Path:
        context = await self.context(site)
        settings = get_settings()
        settings.ensure_dirs()
        target = settings.profiles_dir / f"{site}.json"
        await context.storage_state(path=str(target))
        return target

    def list_profiles(self) -> list[dict]:
        settings = get_settings()
        if not settings.profiles_dir.is_dir():
            return []

        items: list[dict] = []
        for path in sorted(settings.profiles_dir.glob("*.json")):
            stat = path.stat()
            items.append(
                {
                    "site": path.stem,
                    "saved_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                    "size": stat.st_size,
                }
            )
        return items

    async def close(self) -> None:
        for context in list(self._contexts.values()):
            try:
                await context.close()
            except Exception:  # noqa: BLE001 - 关闭失败不应阻断退出
                pass
        self._contexts.clear()
        self._pages.clear()

        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:  # noqa: BLE001
                pass
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:  # noqa: BLE001
                pass

        self._browser = None
        self._playwright = None
        self.headless = None


_manager: BrowserManager | None = None


def get_manager() -> BrowserManager:
    global _manager
    if _manager is None:
        _manager = BrowserManager()
    return _manager
