import asyncio
import json
import logging
import random
import re
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from playwright.async_api import BrowserContext, Page

from browser_page_operations import BrowserPageOperations
from browser_query_result import OfferDetails, QueryResult, build_query_result
from browser_retry_policy import (
    looks_like_ctrip_profile_pollution,
    looks_like_no_fare,
    looks_like_rate_limit,
    should_stop_after_no_fare,
)
from browser_session_policy import (
    BrowserSessionArtifact,
    BrowserSessionMetadata,
    credential_login_allowed_for_backend,
    saved_session_allowed_for_backend,
    storage_state_is_usable,
)
from browser_session_manager import BrowserSessionManager
from browser_use_adapter import BrowserUseAdapter
from config_manager import AppConfig, ProviderConfig, RouteQuery
from provider_runtime import ProviderRuntime
from providers.base import ProviderQueryOutcome
from providers.ctrip_browser_operations import CtripBrowserOperations
from providers.ctrip_runtime import CtripRuntime
from providers.ctrip_support import provider_cabin_value
from providers.ctrip_tail_operations import CtripTailOperations
from providers.response_capture import attach_response_listeners, ResponseStore
from providers.tail_registry import create_tail_runner
from providers import ctrip
from providers import ctrip_network_parser
from providers import feizhu
from providers import priceline


PROVIDER_HANDLERS = {
    ctrip.name: ctrip,
    feizhu.name: feizhu,
    priceline.name: priceline,
}

LOGGER = logging.getLogger(__name__)
PRICE_PATTERN = re.compile(r"(?<!\d)(?:CNY|RMB|[¥￥])?\s*([1-9]\d{1,5}(?:\.\d{1,2})?)")


class BrowserAutomation:
    offer_details_type = OfferDetails
    TAIL_DISCOVERY_CANDIDATE_TIMEOUT_SECONDS = 120

    def __init__(
        self,
        app_config: AppConfig,
        credentials: dict[str, dict[str, str]],
        backend_override: str | None = None,
    ) -> None:
        self.app_config = app_config
        self.credentials = credentials
        self.backend_override = str(backend_override or "").strip().lower() or None
        self.session_manager = BrowserSessionManager(app_config)
        self.page_operations = BrowserPageOperations()
        self.provider_runtime = ProviderRuntime(self)
        self.ctrip_runtime = CtripRuntime(self)
        self.ctrip_browser = CtripBrowserOperations(self, self.ctrip_runtime)
        self.ctrip_tail = CtripTailOperations(self, self.ctrip_runtime, self.ctrip_browser)
        self.automation_backend = (getattr(app_config, "automation_backend", "playwright") or "playwright").lower()
        self.browser_use_adapter = BrowserUseAdapter(app_config.runtime_dir)

    async def __aenter__(self) -> "BrowserAutomation":
        if self.automation_backend not in {"playwright", "hybrid", "browser_use"}:
            raise RuntimeError(
                "Unsupported automation_backend "
                f"{self.automation_backend!r}; use 'playwright', 'hybrid', or 'browser_use'."
            )
        if self.automation_backend == "browser_use":
            self.browser_use_adapter.ensure_available()
        elif self.automation_backend == "hybrid" and not self.browser_use_adapter.status.available:
            LOGGER.info("Hybrid browser-use backend falling back to Playwright: %s", self.browser_use_adapter.backend_note)

        started = await self.session_manager.start([self.backend_override] if self.backend_override else None)
        LOGGER.info("BrowserAutomation using backend: %s", started)
        health = await self.session_manager.health_check()
        for item in health:
            if item.get("ok"):
                LOGGER.info("Browser backend health ok: %s", item.get("backend"))
            else:
                LOGGER.warning("Browser backend health failed: %s error=%s", item.get("backend"), item.get("error"))

        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.session_manager.stop()

    async def bootstrap_session(self, provider_name: str, manual: bool = False) -> BrowserSessionArtifact:
        provider = self.app_config.providers[provider_name]
        page, context = await self._new_page(provider)
        try:
            await page.goto(provider.login_url, wait_until="domcontentloaded", timeout=provider.timeout_ms)
            if manual:
                print(f"[{provider_name}] Browser opened for manual login. Waiting 180 seconds before saving session.")
                await page.wait_for_timeout(180000)
            else:
                await self.login(provider_name, page=page, context=context, save_state=False)
            await self._save_storage_state_if_allowed(context, provider)
            return self.session_manager.session_artifact_for_context(context, provider)
        finally:
            await self._close_context_quietly(context)

    async def inspect_query_page(self, provider_name: str, route: RouteQuery, pause: bool = True) -> Path:
        if provider_name == "airchina":
            from airchina_probe import inspect_airchina_page
            return await inspect_airchina_page(self, route, manual_wait_seconds=20 if pause else 0)
        provider = self.app_config.providers[provider_name]
        page, context = await self._new_page(provider)
        try:
            network_log: list[dict] = []
            page.on("response", lambda response: network_log.append({"url": response.url, "status": response.status}))
            await self.login(provider_name, page=page, context=context)
            await self._throttle(provider)
            await page.goto(
                self._build_search_url(provider, route),
                wait_until="domcontentloaded",
                timeout=provider.timeout_ms,
            )
            await self._prepare_results_page(provider, page, route)
            await self._wait_for_results(provider, page)

            dump_dir = self.app_config.runtime_dir / "inspections"
            dump_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
            prefix = f"{provider_name}_{route.origin}_{route.destination}_{stamp}"
            screenshot_path = dump_dir / f"{prefix}.png"
            html_path = dump_dir / f"{prefix}.html"
            json_path = dump_dir / f"{prefix}.json"

            await page.screenshot(path=str(screenshot_path), full_page=True)
            html_path.write_text(await page.content(), encoding="utf-8")
            payload = {
                "provider": provider_name,
                "route_key": route.route_key,
                "url": page.url,
                "title": await page.title(),
                "candidate_texts": await self._collect_candidate_texts(provider, page),
                "configured_selectors": provider.selectors,
                "network_log": network_log[-80:],
                "screenshot_path": str(screenshot_path),
                "html_path": str(html_path),
                "captured_at": datetime.utcnow().isoformat() + "Z",
            }
            json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            if pause:
                await page.wait_for_timeout(20000)
            return json_path
        finally:
            await self._close_context_quietly(context)

    async def query_provider(self, provider_name: str, route: RouteQuery) -> QueryResult:
        if provider_name == "airchina":
            from providers.airchina_page import QUERY_NOT_READY
            raise RuntimeError(QUERY_NOT_READY)
        provider = self.app_config.providers[provider_name]
        errors: list[str] = []
        backend_names = self._backend_names_for_provider(provider)
        attempt_index = 0
        profile_retry_used = False
        while attempt_index < len(backend_names):
            backend_name = backend_names[attempt_index]
            page = None
            context = None
            retry_current_backend = False
            polluted_profile_name = None
            try:
                page, context = await self._new_page(provider, backend_name=backend_name)
                LOGGER.info("query provider=%s route=%s backend=%s", provider_name, route.route_key, backend_name)
                session_metadata = self._session_metadata_for_context(context)
                response_store: list[dict] = ResponseStore()
                await attach_response_listeners(page, response_store)
                await self.login(provider_name, page=page, context=context)
                outcome = await self._query_provider_with_handler(provider, route, page, response_store)
                result = build_query_result(
                    provider_name=provider_name,
                    route=route,
                    outcome=outcome,
                    session_metadata=session_metadata,
                    response_store=response_store,
                    automation_backend=self.automation_backend,
                    browser_use_status=self.browser_use_adapter.status,
                    browser_use_note=self.browser_use_adapter.backend_note,
                    default_currency=self.app_config.default_currency,
                    include_browser_backend_note=True,
                )
                result.raw_payload["backend_attempts"] = [*errors, f"{backend_name}: ok"]
                return result
            except Exception as exc:
                message = f"{backend_name}: {exc}"
                errors.append(message)
                LOGGER.warning("query provider=%s route=%s backend=%s failed: %s", provider_name, route.route_key, backend_name, exc)
                if looks_like_ctrip_profile_pollution(provider_name, backend_name, exc) and not profile_retry_used:
                    polluted_profile_name = self.session_manager.profile_name_for_context(context)
                    retry_current_backend = True
                    profile_retry_used = True
                    LOGGER.warning(
                        "provider=%s route=%s backend=%s detected polluted Chrome profile=%s; rotating profile and retrying once",
                        provider_name,
                        backend_name,
                        polluted_profile_name or "selected",
                    )
                elif looks_like_rate_limit(exc):
                    LOGGER.warning("rate-limit signal detected for provider=%s; aborting backend retries", provider_name)
                    break
                elif looks_like_no_fare(exc) and should_stop_after_no_fare(
                    provider_name,
                    route,
                    attempt_index,
                    len(backend_names),
                ):
                    LOGGER.info("no fare signal detected for provider=%s; aborting backend retries", provider_name)
                    break
                if attempt_index < len(backend_names) - 1:
                    cooldown = random.randint(15 + attempt_index * 15, 45 + attempt_index * 30)
                    LOGGER.info("cooling down %ss before next backend attempt", cooldown)
                    await asyncio.sleep(cooldown)
            finally:
                if context is not None:
                    await self._close_context_quietly(context)
            if retry_current_backend:
                self.session_manager.mark_chrome_profile_polluted(
                    provider,
                    backend_name,
                    profile_name=polluted_profile_name,
                    reason=f"login overlay blocked route={route.route_key}",
                )
                backend_names.insert(attempt_index + 1, backend_name)
                await asyncio.sleep(2)
            attempt_index += 1
        raise RuntimeError(f"All browser backends failed for provider={provider_name} route={route.route_key}: {' | '.join(errors)}")

    async def _query_provider_without_backend_retry(self, provider_name: str, route: RouteQuery) -> QueryResult:
        if provider_name == "airchina":
            from providers.airchina_page import QUERY_NOT_READY
            raise RuntimeError(QUERY_NOT_READY)
        provider = self.app_config.providers[provider_name]
        page, context = await self._new_page(provider)
        try:
            session_metadata = self._session_metadata_for_context(context)
            response_store: list[dict] = ResponseStore()
            await attach_response_listeners(page, response_store)
            await self.login(provider_name, page=page, context=context)
            outcome = await self._query_provider_with_handler(provider, route, page, response_store)
            return build_query_result(
                provider_name=provider_name,
                route=route,
                outcome=outcome,
                session_metadata=session_metadata,
                response_store=response_store,
                automation_backend=self.automation_backend,
                browser_use_status=self.browser_use_adapter.status,
                browser_use_note=self.browser_use_adapter.backend_note,
                default_currency=self.app_config.default_currency,
                include_browser_backend_note=False,
            )
        finally:
            await self._close_context_quietly(context)

    async def _query_provider_with_handler(
        self,
        provider: ProviderConfig,
        route: RouteQuery,
        page: Page,
        response_store: list[dict],
    ) -> ProviderQueryOutcome:
        handler = PROVIDER_HANDLERS.get(provider.name)
        if handler is not None:
            runtime = self.ctrip_runtime if provider.name == "ctrip" else self.provider_runtime
            return await handler.query(runtime, provider, route, page, response_store)

        if route.is_multi_city:
            raise RuntimeError(f"Provider {provider.name} does not support native multi-city automation in this tool yet.")
        offer, final_url, title = await self._query_single_route_offer(provider, route, page, response_store)
        return ProviderQueryOutcome(
            price=offer.price,
            final_url=final_url,
            title=title,
            departure_time=offer.departure_time,
            row_text=offer.row_text,
        )

    async def _query_single_route_offer(
        self,
        provider: ProviderConfig,
        route: RouteQuery,
        page: Page,
        response_store: list[dict],
    ) -> tuple[OfferDetails, str, str]:
        if provider.name == "ctrip":
            return await self.ctrip_browser._query_single_route_offer(provider, route, page, response_store)
        if provider.name == "priceline":
            return await priceline.query_single_route_offer(self, provider, route, page, response_store)

        await self._throttle(provider)
        await page.goto(
            self._build_search_url(provider, route),
            wait_until="domcontentloaded",
            timeout=provider.timeout_ms,
        )
        await self._prepare_results_page(provider, page, route)
        await self._wait_for_results(provider, page)
        offer = await self._extract_offer(provider, route, page, response_store)
        return offer, page.url, await page.title()

    async def query_tail_baseline(
        self,
        provider_name: str,
        *,
        origin: str,
        transfer: str,
        departure_date: str,
        cabin: str,
        passengers: int,
        departure_time_start: str = "",
        departure_time_end: str = "",
        preferred_airlines: list[str] | None = None,
    ) -> dict[str, object] | None:
        return await create_tail_runner(provider_name, self).query_baseline(
            origin=origin,
            transfer=transfer,
            departure_date=departure_date,
            cabin=cabin,
            passengers=passengers,
            departure_time_start=departure_time_start,
            departure_time_end=departure_time_end,
            preferred_airlines=preferred_airlines,
        )

    async def discover_tail_routes(
        self,
        provider_name: str,
        *,
        origin: str,
        transfer: str,
        departure_date: str,
        cabin: str,
        passengers: int,
        candidates: list[str],
        preferred_airlines: list[str] | None = None,
        progress_callback: Callable[[dict[str, object]], None] | None = None,
        cancel_checker: Callable[[], bool] | None = None,
        verification_continue_checker: Callable[[], bool] | None = None,
        filter_scope: str = "first_leg",
    ) -> dict[str, object]:
        return await create_tail_runner(provider_name, self).run(
            origin=origin,
            transfer=transfer,
            departure_date=departure_date,
            cabin=cabin,
            passengers=passengers,
            candidates=candidates,
            preferred_airlines=preferred_airlines,
            progress_callback=progress_callback,
            cancel_checker=cancel_checker,
            verification_continue_checker=verification_continue_checker,
            filter_scope=filter_scope,
        )

    async def discover_ctrip_tail_routes(
        self,
        **kwargs,
    ) -> dict[str, object]:
        return await self.discover_tail_routes("ctrip", **kwargs)

    async def discover_feizhu_tail_routes(
        self,
        **kwargs,
    ) -> dict[str, object]:
        return await self.discover_tail_routes("feizhu", **kwargs)

    async def login(
        self,
        provider_name: str,
        page: Page | None = None,
        context: BrowserContext | None = None,
        save_state: bool = True,
    ) -> None:
        provider = self.app_config.providers[provider_name]
        owned_page = False
        if page is None or context is None:
            page, context = await self._new_page(provider)
            owned_page = True

        try:
            if provider.name == "priceline":
                return

            backend_name = self._context_backend_name(context)
            session_metadata = self._session_metadata_for_context(context)

            if provider.name == "ctrip":
                LOGGER.info(
                    "provider=%s backend=%s skipping login preflight; Ctrip query flow starts from flight pages",
                    provider.name,
                    backend_name or "unknown",
                )
                return

            if session_metadata.persistent_profile_loaded and not session_metadata.storage_state_loaded:
                await page.goto(provider.login_url, wait_until="domcontentloaded", timeout=provider.timeout_ms)
                if await self._verify_saved_session(page, provider):
                    LOGGER.info("provider=%s backend=%s persistent profile verified", provider.name, backend_name)
                    return
                LOGGER.info(
                    "provider=%s backend=%s persistent profile not logged in, will re-login",
                    provider.name,
                    backend_name,
                )
            elif session_metadata.storage_state_loaded:
                if await self._verify_saved_session(page, provider):
                    LOGGER.info("provider=%s backend=%s saved session verified", provider.name, backend_name)
                    return
                LOGGER.info(
                    "provider=%s backend=%s saved session expired, will re-login",
                    provider.name,
                    backend_name,
                )

            if not credential_login_allowed_for_backend(backend_name):
                LOGGER.info(
                    "skipping credential login for provider=%s backend=%s",
                    provider.name,
                    backend_name or "unknown",
                )
                return

            await page.goto(provider.login_url, wait_until="domcontentloaded", timeout=provider.timeout_ms)
            creds = self.credentials.get(provider_name)
            username_selectors = self._selector_list(provider, "username")
            password_selectors = self._selector_list(provider, "password")
            submit_selectors = self._selector_list(provider, "submit")

            if creds and username_selectors and password_selectors:
                await self._fill_first(page, username_selectors, creds["username"])
                await self._throttle(provider)
                await self._fill_first(page, password_selectors, creds["password"])
                if submit_selectors:
                    await self._click_first(page, submit_selectors)
                await page.wait_for_load_state("networkidle", timeout=provider.timeout_ms)
            else:
                LOGGER.info(
                    "provider=%s no saved session and no credentials; continuing without login",
                    provider_name,
                )

            if save_state:
                await self._save_storage_state_if_allowed(context, provider)
        finally:
            if owned_page:
                await self._close_context_quietly(context)

    async def _close_context_quietly(self, context: BrowserContext | None) -> None:
        await self.session_manager.close_context_quietly(context)

    async def _new_page(
        self,
        provider: ProviderConfig,
        *,
        allow_saved_session: bool = True,
        backend_name: str | None = None,
    ) -> tuple[Page, BrowserContext]:
        return await self.session_manager.new_page(
            provider,
            allow_saved_session=allow_saved_session,
            backend_name=backend_name,
        )

    def _configured_backend_names(self) -> list[str]:
        return self.session_manager.configured_backend_names()

    def _backend_names_for_provider(self, provider: ProviderConfig) -> list[str]:
        if self.backend_override:
            return [self.backend_override]
        return self.session_manager.backend_names_for_provider(provider)

    def _context_backend_name(self, context: BrowserContext | None) -> str:
        return self.session_manager.context_backend_name(context)

    def _storage_state_path_for_backend(self, provider: ProviderConfig, backend_name: str | None) -> str | None:
        return self.session_manager.storage_state_path_for_backend(provider, backend_name)

    @staticmethod
    def _saved_session_allowed_for_backend(backend_name: str | None) -> bool:
        return saved_session_allowed_for_backend(backend_name)

    def _session_metadata_for_context(self, context: BrowserContext | None) -> BrowserSessionMetadata:
        return self.session_manager.session_metadata_for_context(context)

    async def _verify_saved_session(self, page: Page, provider: ProviderConfig) -> bool:
        """Lightweight probe to check whether a loaded storage_state is still valid.

        IMPORTANT: This must NOT navigate the provided page, because the caller
        relies on the page remaining on its current URL for the subsequent query flow.
        """
        try:
            title = (await page.title()).lower()
            url = (page.url or "").lower()
            if any(token in url for token in ("passport", "login", "signin", "auth")):
                return False
            if provider.name == "ctrip":
                try:
                    body = (await page.locator("body").inner_text(timeout=3000)).lower()
                    if any(m in body for m in ("欢迎登录", "手机号登录", "密码登录", "短信登录", "账号登录")):
                        return False
                except Exception:
                    pass
            return True
        except Exception:
            return True

    async def _save_storage_state_if_allowed(self, context: BrowserContext, provider: ProviderConfig) -> None:
        await self.session_manager.save_storage_state_if_allowed(context, provider)

    @staticmethod
    def _looks_like_rate_limit(exc: Exception) -> bool:
        return looks_like_rate_limit(exc)

    @staticmethod
    def _looks_like_no_fare(exc: Exception) -> bool:
        return looks_like_no_fare(exc)

    @staticmethod
    def _looks_like_ctrip_profile_pollution(provider_name: str, backend_name: str | None, exc: Exception) -> bool:
        return looks_like_ctrip_profile_pollution(provider_name, backend_name, exc)

    @staticmethod
    def _should_stop_after_no_fare(
        provider_name: str,
        route: RouteQuery,
        attempt_index: int,
        backend_count: int,
    ) -> bool:
        return should_stop_after_no_fare(provider_name, route, attempt_index, backend_count)

    async def _texts_for_selectors(self, page: Page, selectors: list[str], limit: int = 6) -> list[str]:
        return await self.page_operations._texts_for_selectors(page, selectors, limit)

    async def _collect_candidate_texts(self, provider: ProviderConfig, page: Page) -> dict[str, list[str]]:
        return await self.page_operations._collect_candidate_texts(provider, page)

    async def _fill_first(self, page: Page, selectors: list[str], value: str) -> None:
        await self.page_operations._fill_first(page, selectors, value)

    async def _click_first(self, page: Page, selectors: list[str]) -> None:
        await self.page_operations._click_first(page, selectors)

    async def _click_first_available(self, scope, selectors: list[str], timeout_ms: int) -> bool:
        return await self.page_operations._click_first_available(scope, selectors, timeout_ms)

    async def _throttle(self, provider: ProviderConfig) -> None:
        await self.page_operations._throttle(provider)

    @staticmethod
    def _selector_list(provider: ProviderConfig, key: str) -> list[str]:
        return BrowserPageOperations._selector_list(provider, key)

    @staticmethod
    def _build_itinerary_summary(route: RouteQuery) -> str:
        return BrowserPageOperations._build_itinerary_summary(route)

    async def _prepare_results_page(self, provider: ProviderConfig, page: Page, route: RouteQuery) -> None:
        if provider.name == "ctrip":
            await self.ctrip_browser._prepare_ctrip_results_page(page, route, provider.timeout_ms)

    async def _browser_use_assist_page(
        self,
        page: Page,
        kind: str,
        instruction: str,
        context: dict[str, object] | None = None,
    ) -> None:
        if self.automation_backend not in {"hybrid", "browser_use"}:
            return
        result = await self.browser_use_adapter.assist_page(
            page=page,
            kind=kind,
            instruction=instruction,
            context=context or {},
        )
        LOGGER.info(
            "browser-use assist kind=%s attempted=%s success=%s reason=%s artifact=%s",
            kind,
            result.attempted,
            result.success,
            result.reason,
            result.artifact_path,
        )

    async def _wait_for_results(self, provider: ProviderConfig, page: Page) -> None:
        if provider.name == "ctrip":
            result_selectors = [
                ".list-content-item-transit",
                ".search-result-item",
                ".box-row",
                "[class*='flight'][class*='item']",
            ]
            try:
                await page.locator(", ".join(result_selectors)).first.wait_for(timeout=min(provider.timeout_ms, 30000))
                await page.wait_for_timeout(1500)
                return
            except Exception:
                await page.wait_for_timeout(3000)
                return

        selectors = self._selector_list(provider, "lowest_price")
        if selectors:
            for selector in selectors:
                try:
                    await page.locator(selector).first.wait_for(timeout=provider.timeout_ms)
                    return
                except Exception:
                    continue

        await page.locator("body").wait_for(timeout=provider.timeout_ms)
        await page.wait_for_timeout(3000)

    async def _extract_offer(
        self,
        provider: ProviderConfig,
        route: RouteQuery,
        page: Page,
        response_store: list[dict] | None = None,
    ) -> OfferDetails:
        if provider.name == "airchina":
            from providers.airchina_page import QUERY_NOT_READY
            raise RuntimeError(QUERY_NOT_READY)
        if provider.name == "ctrip":
            match = (ctrip_network_parser.extract_multi_city_match_from_network(response_store or [], route)
                     if route.is_multi_city else
                     ctrip_network_parser.extract_single_route_match_from_network(response_store or [], route))
            if match is not None:
                return OfferDetails(price=match["total_price"], matched_quote=match)
            raise RuntimeError(
                "Ctrip quote unverified: no exact itinerary matches route/date/cabin/airlines. "
                "Calendar and visible-only fares are diagnostic evidence, not reliable saved quotes."
            )

        network_price = self._extract_price_from_network(response_store or [])
        if network_price is not None:
            return OfferDetails(price=network_price)

        texts = await self._texts_for_selectors(page, self._selector_list(provider, "lowest_price"))
        if not texts:
            texts = [await page.locator("body").inner_text()]

        for text in texts:
            match = PRICE_PATTERN.search(text.replace(",", ""))
            if match:
                return OfferDetails(price=float(match.group(1)))

        raise RuntimeError(
            f"Could not extract price for provider {provider.name}. "
            "Run inspect-page to capture the live DOM and update selectors.lowest_price."
        )

    def _extract_price_from_network(self, response_store: list[dict]) -> float | None:
        return ctrip_network_parser.extract_price_from_network(response_store)

    @staticmethod
    def _storage_state_is_usable(path: Path) -> bool:
        return storage_state_is_usable(path)

    def _build_search_url(self, provider: ProviderConfig, route: RouteQuery) -> str:
        if provider.name == "ctrip":
            return ctrip.build_online_results_url(route)

        return provider.search_url_template.format(
            origin=quote(route.origin),
            destination=quote(route.destination),
            departure_date=quote(route.departure_date),
            return_date=quote(route.return_date or ""),
            cabin=quote(provider_cabin_value(provider.name, route.cabin)),
            passengers=quote(str(route.passengers)),
        )
