import asyncio
import json
import logging
import random
import re
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from playwright.async_api import BrowserContext, Page, TimeoutError
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
from config_manager import AppConfig, ProviderConfig, RouteQuery, RouteSegment
from providers.base import ProviderQueryOutcome
from provider_runtime import ProviderRuntime
from providers.ctrip_support import provider_cabin_value
from providers.response_capture import attach_response_listeners
from providers.tail_registry import create_tail_runner
from providers import ctrip_page
from providers import ctrip_parser
from providers import ctrip_itinerary
from providers import ctrip_network_parser
from providers import ctrip_tail_parser
from providers import ctrip_tail_discovery
from providers import ctrip
from providers import feizhu
from providers import priceline


PROVIDER_HANDLERS = {
    ctrip.name: ctrip,
    feizhu.name: feizhu,
    priceline.name: priceline,
}

CTRIP_VERIFICATION_KEYWORDS = (
    "\u9a8c\u8bc1\u7801",
    "\u5b89\u5168\u9a8c\u8bc1",
    "\u6ed1\u5757",
    "\u4eba\u673a\u9a8c\u8bc1",
    "\u8bf7\u5b8c\u6210\u9a8c\u8bc1",
    "\u8bbf\u95ee\u5f02\u5e38",
    "captcha",
    "verify",
    "verification",
    "robot",
)
LOGGER = logging.getLogger(__name__)
PRICE_PATTERN = re.compile(r"(?<!\d)(?:CNY|RMB|[\u00a5\uffe5])?\s*([1-9]\d{1,5}(?:\.\d{1,2})?)")


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
        self.provider_runtime = ProviderRuntime(self)
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
                response_store: list[dict] = []
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
                        route.route_key,
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
        provider = self.app_config.providers[provider_name]
        page, context = await self._new_page(provider)
        try:
            session_metadata = self._session_metadata_for_context(context)
            response_store: list[dict] = []
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
            return await handler.query(self.provider_runtime, provider, route, page, response_store)

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

    async def _tail_cancellable_wait(
        self,
        page: Page,
        wait_ms: int,
        *,
        cancel_checker: Callable[[], bool] | None = None,
    ) -> None:
        remaining = max(0, int(wait_ms))
        while remaining > 0:
            if cancel_checker and cancel_checker():
                return
            step = min(1000, remaining)
            await page.wait_for_timeout(step)
            remaining -= step

    async def _start_ctrip_tail_trace(
        self,
        context: BrowserContext,
        *,
        origin: str,
        transfer: str,
        destination: str,
        departure_date: str,
    ) -> bool:
        return await ctrip_tail_discovery.start_trace(
            context,
            origin=origin,
            transfer=transfer,
            destination=destination,
            departure_date=departure_date,
        )

    async def _stop_ctrip_tail_trace(
        self,
        context: BrowserContext,
        *,
        trace_started: bool,
    ) -> Path | None:
        return await ctrip_tail_discovery.stop_trace(self, context, trace_started=trace_started)

    async def _ctrip_results_date_evidence(self, page: Page, route: RouteQuery) -> dict[str, object]:
        try:
            return await ctrip_page.collect_results_date_evidence(page, route.departure_date)
        except Exception as exc:
            return {"expected": route.departure_date, "url": getattr(page, "url", ""), "error": str(exc)}

    async def _goto_ctrip_tail_results(self, provider: ProviderConfig, route: RouteQuery, page: Page) -> None:
        await ctrip_tail_discovery.goto_tail_results(self, provider, route, page)

    async def _warm_ctrip_tail_result_data(self, page: Page, response_store: list[dict]) -> None:
        await ctrip_tail_discovery.warm_tail_result_data(self, page, response_store)

    async def _wait_for_ctrip_verification_continue(
        self,
        page: Page,
        *,
        cancel_checker: Callable[[], bool] | None = None,
        verification_continue_checker: Callable[[], bool] | None = None,
    ) -> bool:
        await self._browser_use_assist_page(
            page,
            "ctrip_verification_wait",
            "Ctrip is showing a security verification screen. Wait for the user to finish it manually, then continue.",
            {"provider": "ctrip", "safety": "manual_verification_only"},
        )
        deadline = datetime.utcnow().timestamp() + 15 * 60
        while datetime.utcnow().timestamp() < deadline:
            if cancel_checker and cancel_checker():
                return False
            if verification_continue_checker and verification_continue_checker():
                return True
            try:
                if not await self._ctrip_verification_detected(page):
                    return True
            except Exception:
                pass
            await page.wait_for_timeout(2000)
        return False

    def _response_store_has_flight_itineraries(self, response_store: list[dict]) -> bool:
        return ctrip_tail_discovery.response_store_has_flight_itineraries(
            response_store,
            data_walker=self._walk_ctrip_data_nodes,
        )

    async def _try_enrich_ctrip_tail_match_details(self, page: Page, match: dict[str, object]) -> None:
        card = match.get("card")
        if not isinstance(card, dict):
            match.setdefault("detail_source", "network_exact")
            match.setdefault("detail_quality", "complete")
            return
        index = card.get("card_index")
        if not isinstance(index, int) or index < 0:
            return
        try:
            row = page.locator(".list-content-item-transit").nth(index)
            await row.scroll_into_view_if_needed(timeout=3000)
            for selector in [".detail", ".airline", ".box-row"]:
                try:
                    await row.locator(selector).first.click(timeout=1500, force=True)
                    await page.wait_for_timeout(900)
                    break
                except Exception:
                    continue
            details_text = await self._visible_ctrip_expanded_detail_text(page, row)
        except Exception:
            return
        if not details_text:
            return
        existing_details = match.get("details")
        if not isinstance(existing_details, list) or not existing_details:
            return
        first_detail = existing_details[0] if isinstance(existing_details[0], dict) else {}
        last_detail = existing_details[-1] if isinstance(existing_details[-1], dict) else {}
        enriched = ctrip_tail_parser.build_tail_details_from_text_block(
            details_text,
            origin=str(first_detail.get("origin") or ""),
            transfer=str(first_detail.get("destination") or first_detail.get("tail_transfer") or ""),
            destination=str(last_detail.get("destination") or ""),
            departure_date=str(first_detail.get("departure_date") or ""),
            cabin=str(first_detail.get("cabin") or "economy_plus"),
        )
        has_more_signal = any(item.get("flight_no") for item in enriched) or len(details_text) > len(str(card.get("text") or ""))
        if has_more_signal:
            match["details"] = enriched
            match["detail_source"] = "expanded_dom"
            match["detail_quality"] = "expanded"

    async def _visible_ctrip_expanded_detail_text(self, page: Page, row) -> str:
        candidates: list[str] = []
        for selector in [
            ".flight-detail",
            ".flight-detail-list",
            ".detail-content",
            ".drawer",
            ".modal",
            ".popup",
            ".ant-modal",
            ".el-dialog",
        ]:
            try:
                loc = page.locator(selector)
                count = min(await loc.count(), 5)
            except Exception:
                continue
            for index in range(count):
                try:
                    text = (await loc.nth(index).inner_text(timeout=800)).strip()
                except Exception:
                    continue
                if text and len(text) > 40:
                    candidates.append(text)
        try:
            row_text = (await row.inner_text(timeout=1000)).strip()
            if row_text:
                candidates.append(row_text)
        except Exception:
            pass
        candidates.sort(key=len, reverse=True)
        return candidates[0] if candidates else ""

    async def _ctrip_verification_detected(self, page: Page) -> bool:
        try:
            url = (page.url or "").lower()
            if any(token in url for token in ["captcha", "verify", "verification", "risk"]):
                return True
            body = (await page.locator("body").inner_text(timeout=2500)).lower()
        except Exception:
            return False
        return any(keyword.lower() in body for keyword in CTRIP_VERIFICATION_KEYWORDS)

    async def _query_single_route_offer(
        self,
        provider: ProviderConfig,
        route: RouteQuery,
        page: Page,
        response_store: list[dict],
    ) -> tuple[OfferDetails, str, str]:
        if provider.name == "ctrip":
            result = await ctrip.query_single_route_offer(self, provider, route, page, response_store)
            await self._ensure_ctrip_results_date(page, route, provider.timeout_ms)
            return result
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

    async def _query_ctrip_multi_city_offer(
        self,
        provider: ProviderConfig,
        route: RouteQuery,
        page: Page,
        response_store: list[dict],
    ) -> tuple[OfferDetails, str, str]:
        return await ctrip.query_multi_city_offer(self, provider, route, page, response_store)

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

            # 1) Try a saved or persistent browser session first.
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
            # Just check the current page state without navigating away.
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

    async def _prepare_results_page(self, provider: ProviderConfig, page: Page, route: RouteQuery) -> None:
        if provider.name == "ctrip":
            await self._prepare_ctrip_results_page(page, route, provider.timeout_ms)

    async def _prepare_ctrip_results_page(self, page: Page, route: RouteQuery, timeout_ms: int) -> None:
        await ctrip_page.prepare_results_page(self, page, route, timeout_ms)

    async def _ensure_ctrip_results_date(self, page: Page, route: RouteQuery, timeout_ms: int) -> None:
        await ctrip_page.ensure_results_departure_date(page, route, timeout_ms)

    async def _activate_ctrip_multi_city(self, page: Page, timeout_ms: int) -> None:
        await ctrip_page.activate_multi_city(page, timeout_ms)

    async def _activate_ctrip_one_way(self, page: Page, timeout_ms: int) -> None:
        await ctrip_page.activate_one_way(page, timeout_ms)

    async def _activate_ctrip_round_trip(self, page: Page, timeout_ms: int) -> None:
        await ctrip_page.activate_round_trip(page, timeout_ms)

    async def _dismiss_ctrip_login_overlays(self, page: Page, timeout_ms: int) -> None:
        await ctrip_page.dismiss_login_overlays(page, timeout_ms)

    async def _prepare_ctrip_one_way_form(
        self,
        page: Page,
        route: RouteQuery,
        timeout_ms: int,
        *,
        skip_origin: bool = False,
    ) -> None:
        try:
            await ctrip_page.prepare_one_way_form(self, page, route, timeout_ms, skip_origin=skip_origin)
        except Exception as exc:
            await self._browser_use_assist_page(
                page,
                "ctrip_one_way_form",
                "Ctrip one-way form preparation failed. Help diagnose city/date field state using normal UI only.",
                {"route_key": route.route_key, "origin": route.origin, "destination": route.destination, "error": str(exc)},
            )
            raise

    async def _prepare_ctrip_round_trip_form(
        self,
        page: Page,
        route: RouteQuery,
        timeout_ms: int,
    ) -> None:
        try:
            await ctrip_page.prepare_round_trip_form(self, page, route, timeout_ms)
        except Exception as exc:
            await self._browser_use_assist_page(
                page,
                "ctrip_round_trip_form",
                "Ctrip round-trip form preparation failed. Help diagnose city/date field state using normal UI only.",
                {"route_key": route.route_key, "origin": route.origin, "destination": route.destination, "error": str(exc)},
            )
            raise

    async def _prepare_ctrip_multi_city_form(
        self,
        page: Page,
        segments: list[RouteSegment],
        timeout_ms: int,
    ) -> None:
        try:
            await ctrip_page.prepare_multi_city_form(self, page, segments, timeout_ms)
        except Exception as exc:
            await self._browser_use_assist_page(
                page,
                "ctrip_multi_city_form",
                "Ctrip multi-city form preparation failed. Help diagnose city/date field state using normal UI only.",
                {"segments": [segment.to_dict() for segment in segments], "error": str(exc)},
            )
            raise

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
        if provider.name == "ctrip":
            if route.is_multi_city:
                ctrip_offer = await self._extract_ctrip_offer(page, route)
                if ctrip_offer is not None and ctrip_offer.row_text:
                    return ctrip_offer
                raise RuntimeError(
                    "Could not extract a reliable Ctrip multi-city fare matched to this route. "
                    "Network responses did not contain a matching multi-city itinerary and the visible result row was incomplete."
                )

            network_price = ctrip_network_parser.extract_single_route_price_from_network(response_store or [], route)
            if network_price is not None:
                return OfferDetails(price=network_price)

            ctrip_offer = await self._extract_ctrip_offer(page, route)
            if ctrip_offer is not None and ctrip_offer.row_text:
                return ctrip_offer

            raise RuntimeError(
                "Could not extract a reliable Ctrip fare matched to this route/date. "
                "Network responses did not contain a matching itinerary or low-price calendar item."
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

    async def _extract_ctrip_offer(self, page: Page, route: RouteQuery) -> OfferDetails | None:
        return await ctrip_parser.extract_offer(self, page, route)

    async def _texts_for_selectors(self, page: Page, selectors: list[str], limit: int = 6) -> list[str]:
        texts: list[str] = []
        for selector in selectors:
            try:
                locator = page.locator(selector)
                count = min(await locator.count(), limit)
                for index in range(count):
                    text = (await locator.nth(index).inner_text()).strip()
                    if text:
                        texts.append(text)
            except Exception:
                continue
        return texts

    async def _collect_candidate_texts(self, provider: ProviderConfig, page: Page) -> dict[str, list[str]]:
        selector_sets = {
            "price_candidates": self._selector_list(provider, "lowest_price")
            or [
                "[class*='price']",
                "[class*='Price']",
                "[class*='fare']",
                "[class*='Fare']",
                "[data-testid*='price']",
            ],
            "button_candidates": ["button", "a", "[role='button']"],
        }
        result: dict[str, list[str]] = {}
        for key, selectors in selector_sets.items():
            result[key] = await self._texts_for_selectors(page, selectors, limit=10)
        return result

    def _extract_price_from_network(self, response_store: list[dict]) -> float | None:
        return ctrip_network_parser.extract_price_from_network(response_store)

    def _extract_ctrip_multi_city_match_from_network(
        self,
        response_store: list[dict],
        route: RouteQuery,
        *,
        preferred_total: float | None = None,
    ) -> dict[str, object] | None:
        return ctrip_network_parser.extract_multi_city_match_from_network(
            response_store,
            route,
            preferred_total=preferred_total,
        )

    def _extract_ctrip_tail_match_from_network(
        self,
        response_store: list[dict],
        *,
        origin: str,
        transfer: str,
        destination: str,
        preferred_airlines: list[str] | None = None,
    ) -> dict[str, object] | None:
        return ctrip_tail_discovery.extract_tail_match_from_network(
            self,
            response_store,
            origin=origin,
            transfer=transfer,
            destination=destination,
            preferred_airlines=preferred_airlines,
        )

    async def _extract_ctrip_tail_match_from_page(
        self,
        page: Page,
        *,
        origin: str,
        transfer: str,
        destination: str,
        departure_date: str,
        cabin: str,
        preferred_airlines: list[str] | None = None,
    ) -> dict[str, object] | None:
        return await ctrip_tail_discovery.extract_tail_match_from_page(
            page,
            origin=origin,
            transfer=transfer,
            destination=destination,
            departure_date=departure_date,
            cabin=cabin,
            preferred_airlines=preferred_airlines,
        )

    async def _extract_ctrip_tail_page_diagnostics(
        self,
        page: Page,
        *,
        response_store: list[dict] | None = None,
        origin: str = "",
        transfer: str,
        destination: str = "",
        preferred_airlines: list[str] | None = None,
    ) -> str:
        return await ctrip_tail_discovery.extract_tail_page_diagnostics(
            self,
            page,
            response_store=response_store,
            origin=origin,
            transfer=transfer,
            destination=destination,
            preferred_airlines=preferred_airlines,
        )

    async def _dump_ctrip_tail_diagnostics(
        self,
        page: Page,
        *,
        response_store: list[dict],
        origin: str,
        transfer: str,
        destination: str,
        departure_date: str,
    ) -> dict[str, str] | None:
        return await ctrip_tail_discovery.dump_tail_diagnostics(
            self,
            page,
            response_store=response_store,
            origin=origin,
            transfer=transfer,
            destination=destination,
            departure_date=departure_date,
        )

    def _summarize_response_for_diagnostics(self, item: dict) -> dict[str, object]:
        return ctrip_network_parser.summarize_response_for_diagnostics(item)

    def _payload_shape(self, payload: dict) -> str:
        return ctrip_network_parser.payload_shape(payload)

    def _ctrip_tail_network_diagnostics(
        self,
        response_store: list[dict],
        *,
        origin: str,
        transfer: str,
        destination: str,
        preferred_airlines: list[str] | None = None,
    ) -> str:
        return ctrip_tail_discovery.network_diagnostics(
            self,
            response_store,
            origin=origin,
            transfer=transfer,
            destination=destination,
            preferred_airlines=preferred_airlines,
        )

    @staticmethod
    def _summarize_tail_details(details: list[dict[str, object]]) -> str:
        return ctrip_tail_discovery.summarize_tail_details(details)

    def _details_match_tail_route(
        self,
        details: list[dict[str, object]],
        origin: str,
        transfer: str,
        destination: str,
    ) -> bool:
        return ctrip_tail_discovery.details_match_tail_route(details, origin, transfer, destination)

    @staticmethod
    def _code_matches_target(value: object, target: str) -> bool:
        return ctrip_tail_discovery.code_matches_target(value, target)

    @staticmethod
    def _ctrip_total_price(price: dict) -> float | None:
        return ctrip_itinerary.total_price(price)

    @staticmethod
    def _multi_city_details_are_direct(details: list[dict[str, object]], route: RouteQuery) -> bool:
        return ctrip_itinerary.multi_city_details_are_direct(details, route)

    def _walk_ctrip_data_nodes(self, node):
        yield from ctrip_itinerary.walk_data_nodes(node)

    def _build_ctrip_itinerary_details(self, itinerary: dict, price: dict) -> list[dict[str, object]]:
        return ctrip_itinerary.build_itinerary_details(itinerary, price)

    def _build_multi_city_details(self, itinerary: dict, price: dict) -> list[dict[str, object]]:
        return ctrip_itinerary.build_multi_city_details(itinerary, price)

    def _build_domestic_online_details(self, itinerary: dict, price: dict) -> list[dict[str, object]]:
        return ctrip_itinerary.build_domestic_online_details(itinerary, price)

    def _extract_flight_info_list(self, price: dict) -> list[dict]:
        return ctrip_itinerary.extract_flight_info_list(price)

    @staticmethod
    def _normalize_cabin_from_seat(seat: dict[str, object]) -> str | None:
        return ctrip_itinerary.normalize_cabin_from_seat(seat)

    @staticmethod
    def _normalize_cabin_code(value: object) -> str | None:
        return ctrip_itinerary.normalize_cabin_code(value)

    @staticmethod
    def _format_ctrip_dt(value: object) -> str | None:
        return ctrip_itinerary.format_ctrip_dt(value)

    @staticmethod
    def _coerce_price(value) -> float | None:
        return ctrip_itinerary.coerce_price(value)

    async def _fill_first(self, page: Page, selectors: list[str], value: str) -> None:
        last_error: Exception | None = None
        for selector in selectors:
            try:
                await page.locator(selector).first.fill(value)
                return
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"Could not fill any selector from {selectors}") from last_error

    async def _click_first(self, page: Page, selectors: list[str]) -> None:
        last_error: Exception | None = None
        for selector in selectors:
            try:
                await page.locator(selector).first.click()
                return
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"Could not click any selector from {selectors}") from last_error

    async def _click_first_available(self, scope, selectors: list[str], timeout_ms: int) -> bool:
        click_timeout = max(500, min(timeout_ms, 1500))
        for selector in selectors:
            try:
                locator = scope.locator(selector)
                if await locator.count() == 0:
                    continue
                await locator.first.click(timeout=click_timeout, force=True)
                return True
            except Exception:
                continue
        return False

    async def _throttle(self, provider: ProviderConfig) -> None:
        wait_seconds = random.uniform(provider.min_delay_seconds, provider.max_delay_seconds)
        await asyncio.sleep(wait_seconds)

    @staticmethod
    def _selector_list(provider: ProviderConfig, key: str) -> list[str]:
        raw = provider.selectors.get(key)
        if raw is None:
            return []
        if isinstance(raw, str):
            return [raw] if raw.strip() else []
        return [item for item in raw if isinstance(item, str) and item.strip()]

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

    @staticmethod
    def _build_itinerary_summary(route: RouteQuery) -> str:
        if not route.segments:
            return f"{route.origin}->{route.destination} {route.departure_date}"
        return " / ".join(
            f"{segment.origin}->{segment.destination} {segment.departure_date}"
            for segment in route.segments
        )
