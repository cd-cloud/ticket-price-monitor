import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from config_manager import RouteQuery


class AutoQueryWorker:
    def __init__(self, service, interval_hours: int, min_gap_minutes: int) -> None:
        self.service = service
        self.interval_hours = interval_hours
        self.min_gap_minutes = min_gap_minutes
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cycle_lock = threading.Lock()
        self._last_query_started_at: datetime | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="flight-auto-query-worker",
            daemon=True,
        )
        self._thread.start()
        logging.info("auto query worker started")

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        self._thread = None

    def process(self) -> dict[str, Any]:
        if not self._cycle_lock.acquire(blocking=False):
            return {"running": True, "message": "auto worker is already processing"}

        try:
            self.service.refresh_config()
            now = utc_now()
            next_allowed = self.next_allowed_at()
            due_routes = [
                route
                for route in self.service.config.routes
                if route.auto_query_enabled and route.auto_query_next_run_at
                and parse_iso_datetime(route.auto_query_next_run_at) <= now
            ]
            due_routes.sort(key=lambda item: parse_iso_datetime(item.auto_query_next_run_at))

            if not due_routes:
                return {"running": False, "saved": 0, "errors": []}
            if now < next_allowed:
                return {
                    "running": False,
                    "saved": 0,
                    "errors": [],
                    "next_allowed_run_at": format_iso_datetime(next_allowed),
                }

            route = due_routes[0]
            self._last_query_started_at = now
            logging.info("starting auto query for route=%s", route.route_key)
            result = self.service.run_route_once_blocking(route.route_key)
            self.update_route_next_run(route.route_key, started_at=now)
            try:
                self.service.report()
            except Exception:
                logging.exception("report refresh failed after auto query")
            return result
        finally:
            self._cycle_lock.release()

    def update_route_next_run(self, route_key: str, started_at: datetime) -> None:
        self.service.refresh_config()
        routes = list(self.service.config.routes)
        route = next((item for item in routes if item.route_key == route_key), None)
        if route is None or not route.auto_query_enabled:
            return

        previous_due = parse_iso_datetime(route.auto_query_next_run_at) if route.auto_query_next_run_at else started_at
        interval = timedelta(hours=max(route.auto_query_interval_hours, self.interval_hours))
        route.auto_query_next_run_at = format_iso_datetime(max(started_at + interval, previous_due + interval))
        self.rebalance(routes)
        self.service.config_manager.save_routes(routes)
        self.service.refresh_config()

    def rebalance(self, routes: list[RouteQuery]) -> None:
        rebalance_auto_query_schedule(
            routes,
            interval_hours=self.interval_hours,
            min_gap_minutes=self.min_gap_minutes,
        )

    def next_allowed_at(self) -> datetime:
        if self._last_query_started_at is None:
            return utc_now()
        return self._last_query_started_at + timedelta(minutes=self.min_gap_minutes)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.process()
            except Exception:
                logging.exception("auto query worker cycle failed")
            self._stop.wait(60)


def rebalance_auto_query_schedule(
    routes: list[RouteQuery],
    *,
    interval_hours: int,
    min_gap_minutes: int,
) -> None:
    now = utc_now()
    min_gap = timedelta(minutes=min_gap_minutes)
    enabled_routes = [route for route in routes if route.auto_query_enabled]
    disabled_routes = [route for route in routes if not route.auto_query_enabled]

    for route in disabled_routes:
        route.auto_query_next_run_at = None
        route.auto_query_interval_hours = interval_hours

    enabled_routes.sort(
        key=lambda route: (
            parse_iso_datetime(route.auto_query_next_run_at)
            if route.auto_query_next_run_at else now,
            route.route_key,
        )
    )
    cursor = now + timedelta(minutes=1)
    for route in enabled_routes:
        route.auto_query_interval_hours = interval_hours
        scheduled = parse_iso_datetime(route.auto_query_next_run_at) if route.auto_query_next_run_at else cursor
        if scheduled < cursor:
            scheduled = cursor
        route.auto_query_next_run_at = format_iso_datetime(scheduled)
        cursor = scheduled + min_gap


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def format_iso_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso_datetime(value: str | None) -> datetime:
    if not value:
        return utc_now()
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0)
