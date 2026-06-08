"""Human-like browser action helpers to reduce automation fingerprints."""

from __future__ import annotations

import random
import logging

from playwright.async_api import Page

LOGGER = logging.getLogger(__name__)


async def human_like_type(page: Page, selector: str, text: str, *, timeout_ms: int = 5000) -> None:
    """Fill text with natural-feeling pauses before and after.

    Per-character typing is usually invisible to server-side风控,
    so we keep the random waits but use a single fill for reliability.
    """
    locator = page.locator(selector).first
    await locator.click(timeout=timeout_ms)
    await page.wait_for_timeout(random.randint(150, 450))
    await locator.fill(text, timeout=timeout_ms)
    await page.wait_for_timeout(random.randint(400, 900))


async def hover_then_click(
    page: Page,
    selector: str,
    *,
    hover_ms_min: int = 300,
    hover_ms_max: int = 1200,
    timeout_ms: int = 5000,
    force: bool = False,
) -> None:
    """Hover over an element, wait a bit, then click."""
    locator = page.locator(selector).first
    await locator.hover(timeout=timeout_ms)
    await page.wait_for_timeout(random.randint(hover_ms_min, hover_ms_max))
    await locator.click(timeout=timeout_ms, force=force)


async def random_scroll_and_pause(
    page: Page,
    *,
    min_ms: int = 800,
    max_ms: int = 3000,
    scroll_probability: float = 0.35,
) -> None:
    """Randomly scroll a bit and pause."""
    if random.random() > scroll_probability:
        await page.wait_for_timeout(random.randint(min_ms, max_ms))
        return
    direction = random.choice(["down", "up"])
    distance = random.randint(200, 800)
    script = f"window.scrollBy(0, {'+' if direction == 'down' else '-'}{distance})"
    try:
        await page.evaluate(script)
    except Exception:
        pass
    await page.wait_for_timeout(random.randint(min_ms, max_ms))


async def random_body_click(page: Page, *, timeout_ms: int = 2000) -> None:
    """Click a blank area of the body to dismiss overlays, like a human would."""
    try:
        x = random.randint(12, 120)
        y = random.randint(12, 120)
        await page.locator("body").click(position={"x": x, "y": y}, timeout=timeout_ms, force=True)
    except Exception:
        pass
