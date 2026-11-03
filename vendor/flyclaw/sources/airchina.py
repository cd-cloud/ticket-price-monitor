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

    def query_by_flight(self, flight_number: str) -> list[dict]:
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
        if return_date:
            raise ValueError("Air China source currently supports one-way queries only.")
        if adults != 1:
            raise ValueError("Air China source currently requires exactly one adult.")
        if stops != 0:
            raise ValueError("Air China source currently supports direct flights only.")
        if cabin not in {"economy", "business"}:
            raise ValueError("Air China source currently supports economy or business cabin.")
        if self.query_service is None:
            from providers.airchina_direct import build_query_service
            self.query_service = build_query_service(self.config_path)
        if timeout is not None:
            self.timeout = timeout
        from providers.airchina_direct import query_airchina_source
        return await query_airchina_source(
            self,
            origin,
            destination,
            date,
            cabin=cabin,
            limit=limit,
        )
