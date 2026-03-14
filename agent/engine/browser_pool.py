"""
Browser Pool — Shared Playwright browser contexts for Grippy.

Instead of spawning a new Chromium instance per request (which causes OOM),
this module maintains a pool of reusable browser contexts.

Usage:
    pool = BrowserPool.get_instance()
    ctx = await pool.acquire()
    try:
        page = await ctx.new_page()
        # ... do work ...
    finally:
        await pool.release(ctx)
"""

import asyncio
import logging
import os
from typing import Optional

from playwright.async_api import async_playwright, Browser, BrowserContext, Playwright

logger = logging.getLogger(__name__)

POOL_SIZE = int(os.environ.get("BROWSER_POOL_SIZE", "3"))
MAX_USES_PER_CONTEXT = int(os.environ.get("BROWSER_CONTEXT_MAX_USES", "10"))


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def get_browser_launch_options() -> dict[str, object]:
    """Build Chromium launch options from environment."""
    return {
        "headless": _env_bool("GRIPPY_BROWSER_HEADLESS", True),
        "slow_mo": int(os.environ.get("GRIPPY_BROWSER_SLOWMO_MS", "0")),
        "args": [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ],
    }


class BrowserPool:
    """Singleton browser pool managing Playwright contexts."""

    _instance: Optional["BrowserPool"] = None
    _lock = asyncio.Lock()

    def __init__(self) -> None:
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._available: asyncio.Queue[BrowserContext] = asyncio.Queue()
        self._all_contexts: list[BrowserContext] = []
        self._use_counts: dict[int, int] = {}
        self._pool_size = POOL_SIZE
        self._initialized = False
        self._init_lock = asyncio.Lock()

    @classmethod
    async def get_instance(cls) -> "BrowserPool":
        """Get or create the singleton pool instance."""
        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = BrowserPool()
        pool = cls._instance
        if not pool._initialized:
            await pool._initialize()
        return pool

    async def _initialize(self) -> None:
        """Launch the browser and create initial contexts."""
        async with self._init_lock:
            if self._initialized:
                return
            try:
                await self._restart_browser()
                self._initialized = True
                logger.info("Browser pool initialized with %d contexts", self._pool_size)
            except Exception as exc:
                logger.error("Failed to initialize browser pool: %s", exc)
                raise

    async def _restart_browser(self) -> None:
        """Relaunch Playwright and repopulate the context pool."""
        for ctx in self._all_contexts:
            try:
                await ctx.close()
            except Exception:
                pass
        self._all_contexts.clear()
        self._use_counts.clear()
        self._available = asyncio.Queue()

        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None

        if self._playwright is None:
            self._playwright = await async_playwright().start()

        self._browser = await self._playwright.chromium.launch(
            **get_browser_launch_options(),
        )

        for _ in range(self._pool_size):
            ctx = await self._create_context()
            self._available.put_nowait(ctx)

    def _browser_usable(self) -> bool:
        """Return whether the shared browser process is still connected."""
        return bool(self._browser and self._browser.is_connected())

    async def _discard_context(self, ctx: BrowserContext) -> None:
        """Drop a broken context from tracking and close it best-effort."""
        ctx_id = id(ctx)
        if ctx in self._all_contexts:
            self._all_contexts.remove(ctx)
        self._use_counts.pop(ctx_id, None)
        try:
            await ctx.close()
        except Exception:
            pass

    async def _create_context_with_recovery(self) -> BrowserContext:
        """Create a context, relaunching the browser first if it has died."""
        if not self._browser_usable():
            logger.warning("Browser process unavailable; restarting browser pool")
            await self._restart_browser()
        try:
            return await self._create_context()
        except Exception:
            logger.warning("Context creation failed; restarting browser pool")
            await self._restart_browser()
            return await self._create_context()

    async def _context_is_usable(self, ctx: BrowserContext) -> bool:
        """Open and close a throwaway page to validate a pooled context."""
        try:
            page = await ctx.new_page()
            await page.close()
            return True
        except Exception:
            return False

    async def _create_context(self) -> BrowserContext:
        """Create a new browser context with stealth settings."""
        if not self._browser:
            raise RuntimeError("Browser not initialized")
        ctx = await self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            java_script_enabled=True,
            ignore_https_errors=True,
        )
        ctx_id = id(ctx)
        self._all_contexts.append(ctx)
        self._use_counts[ctx_id] = 0
        return ctx

    async def acquire(self, timeout: float = 120.0) -> BrowserContext:
        """
        Acquire a browser context from the pool.
        Blocks until one is available or timeout is reached.
        """
        if not self._initialized:
            await self._initialize()

        try:
            ctx = await asyncio.wait_for(self._available.get(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("Browser pool exhausted, creating overflow context")
            ctx = await self._create_context_with_recovery()

        if not await self._context_is_usable(ctx):
            logger.warning("Discarding unusable browser context from pool")
            await self._discard_context(ctx)
            ctx = await self._create_context_with_recovery()

        self._use_counts[id(ctx)] = self._use_counts.get(id(ctx), 0) + 1
        logger.debug("Context acquired (uses: %d)", self._use_counts[id(ctx)])
        return ctx

    async def release(self, ctx: BrowserContext) -> None:
        """
        Return a browser context to the pool.
        If it has exceeded max uses, recycle it.
        """
        ctx_id = id(ctx)
        uses = self._use_counts.get(ctx_id, 0)

        # Close all pages to clean state
        try:
            for page in ctx.pages:
                await page.close()
        except Exception:
            pass

        if uses >= MAX_USES_PER_CONTEXT:
            # Recycle: close old context and create a new one
            logger.info("Recycling context after %d uses", uses)
            try:
                if ctx in self._all_contexts:
                    self._all_contexts.remove(ctx)
                del self._use_counts[ctx_id]
                await ctx.close()
            except Exception:
                pass
            try:
                new_ctx = await self._create_context_with_recovery()
                self._available.put_nowait(new_ctx)
            except Exception as exc:
                logger.error("Failed to create replacement context: %s", exc)
        else:
            self._available.put_nowait(ctx)

    def status(self) -> dict:
        """Return pool status for health checks."""
        return {
            "initialized": self._initialized,
            "pool_size": self._pool_size,
            "available": self._available.qsize(),
            "total_contexts": len(self._all_contexts),
            "status": "ok" if self._initialized else "not_initialized",
        }

    async def shutdown(self) -> None:
        """Gracefully shut down the pool."""
        logger.info("Shutting down browser pool...")
        for ctx in self._all_contexts:
            try:
                await ctx.close()
            except Exception:
                pass
        self._all_contexts.clear()
        self._use_counts.clear()
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        self._initialized = False
        logger.info("Browser pool shut down")
