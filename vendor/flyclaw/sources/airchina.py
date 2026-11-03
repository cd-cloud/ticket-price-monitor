"""Air China source using the host project's validated reference-fare adapter."""

import asyncio
import threading


class AirChinaSource:
    def __init__(
        self,
        *,
        timeout: int = 240,
        query_service=None,
        session_pool=None,
        session_mode: str = "fresh",
        progress=None,
        manual_handler=None,
        cancel_event=None,
        session_callback=None,
        entry_callback=None,
        network_callback=None,
        notice_callback=None,
        diagnostic_callback=None,
        auto_confirm_notices: bool = False,
        flight_no: str | None = None,
        entry_mode: str = "form",
        config_path: str | None = None,
        max_airport_pairs: int = 4,
        reference_cache: bool = True,
    ):
        self.timeout = timeout
        self.query_service = query_service
        self.session_pool = session_pool
        self.session_mode = session_mode
        self.progress = progress or (lambda _stage: None)
        self.manual_handler = manual_handler
        self.cancel_event = cancel_event or threading.Event()
        self.session_callback = session_callback
        self.entry_callback = entry_callback
        self.network_callback = network_callback
        self.notice_callback = notice_callback
        self.diagnostic_callback = diagnostic_callback
        self.auto_confirm_notices = auto_confirm_notices
        self.flight_no = flight_no
        self.entry_mode = entry_mode
        self.config_path = config_path
        self.last_report = None
        self.last_query = None
        self.max_airport_pairs = max_airport_pairs
        self.reference_cache = reference_cache
        self._query_lock = threading.Lock()

    def query_by_flight(self, flight_number: str) -> list[dict]:
        self.last_query = {"state": "unsupported", "reason": "route_and_date_required",
                           "coverage_complete": False}
        self.last_report = None
        return []

    def query_by_route(
        self,
        origin: str,
        destination: str,
        date: str,
        *,
        return_date: str | None = None,
        adults: int = 1,
        cabin: str = "economy",
        stops: int | str = 0,
        limit: int | None = None,
        timeout: int | None = None,
    ) -> list[dict]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("Use await query_by_route_async() inside an event loop.")
        return asyncio.run(self.query_by_route_async(
            origin,
            destination,
            date,
            return_date=return_date,
            adults=adults,
            cabin=cabin,
            stops=stops,
            limit=limit,
            timeout=timeout,
        ))

    async def query_by_route_async(
        self, origin: str, destination: str, date: str, *,
        return_date: str | None = None, adults: int = 1, cabin: str = "economy",
        stops: int | str = 0, limit: int | None = None, timeout: int | None = None,
    ) -> list[dict]:
        if not self._query_lock.acquire(blocking=False):
            raise RuntimeError("An Air China query is already active on this source.")
        try:
            return await self._query_by_route_async(
                origin, destination, date, return_date=return_date, adults=adults,
                cabin=cabin, stops=stops, limit=limit, timeout=timeout,
            )
        finally:
            self._query_lock.release()

    async def _query_by_route_async(
        self,
        origin: str,
        destination: str,
        date: str,
        *,
        return_date: str | None = None,
        adults: int = 1,
        cabin: str = "economy",
        stops: int | str = 0,
        limit: int | None = None,
        timeout: int | None = None,
    ) -> list[dict]:
        # No stale successful report may survive a rejected/failed new query.
        self.last_report = None
        self.last_query = {"state": "validating", "coverage_complete": False}
        from providers.airchina_source_contract import plan_query
        try:
            plan = plan_query(
                origin, destination, date, return_date=return_date, adults=adults,
                cabin=cabin, stops=stops, limit=limit,
                timeout=self.timeout if timeout is None else timeout,
                max_airport_pairs=self.max_airport_pairs, entry_mode=self.entry_mode,
                flight_no=self.flight_no,
            )
        except ValueError:
            self.last_query = {"state": "invalid_request", "coverage_complete": False}
            raise
        if self.query_service is None:
            from providers.airchina_direct import build_query_service
            try:
                self.query_service = build_query_service(self.config_path)
            except Exception:
                self.last_query = {"state": "error", "reason": "configuration_unavailable",
                                   "coverage_complete": False}
                raise ValueError("Air China configuration could not be loaded.") from None
        from providers.airchina_direct import query_airchina_plan
        return await query_airchina_plan(self, plan)
