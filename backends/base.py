from abc import ABC, abstractmethod

from playwright.async_api import BrowserContext, Page


class BrowserBackend(ABC):
    """Abstract base for browser automation backends.

    Implementations must return Playwright-compatible BrowserContext and Page
    objects so that provider layers can use standard Playwright APIs.
    """

    @abstractmethod
    async def start(self, headless: bool, browser_channel: str | None, **kwargs) -> None:
        """Launch the browser."""

    @abstractmethod
    async def stop(self) -> None:
        """Shut down the browser."""

    @abstractmethod
    async def new_context(
        self,
        *,
        storage_state: str | None = None,
        extra_http_headers: dict[str, str] | None = None,
        locale: str | None = None,
        timezone_id: str | None = None,
        **kwargs,
    ) -> BrowserContext:
        """Create a new browser context."""

    @abstractmethod
    async def new_page(self, context: BrowserContext) -> Page:
        """Create a new page in the given context."""

    @abstractmethod
    async def close_context(self, context: BrowserContext) -> None:
        """Close the context quietly."""
