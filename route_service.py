from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Callable

from config_manager import (
    AppConfig,
    ConfigManager,
    RouteQuery,
    RouteSegment,
    normalize_airport_code,
    normalize_transfer_policy,
)
from route_expander import expand_route_payload, payload_has_city_groups
from providers.registry import provider_definition


ALLOWED_CABINS = {
    "economy",
    "economy_plus",
    "premium_economy",
    "business",
    "business_first",
    "first",
}
ALLOWED_TRANSFER_POLICIES = {"any", "direct_only", "transfer_only"}
AIRPORT_CODE_PATTERN = re.compile(r"^[A-Z]{3}$")
AIRLINE_CODE_PATTERN = re.compile(r"^[A-Z0-9]{2}$")


class RouteService:
    """Owns route view models and config persistence for dashboard route CRUD."""

    def __init__(
        self,
        *,
        config_manager: ConfigManager,
        get_config: Callable[[], AppConfig],
        refresh_config: Callable[[], None],
        rebalance_auto_query: Callable[[list[RouteQuery]], None],
        auto_query_interval_hours: int,
    ) -> None:
        self.config_manager = config_manager
        self.get_config = get_config
        self.refresh_config = refresh_config
        self.rebalance_auto_query = rebalance_auto_query
        self.auto_query_interval_hours = auto_query_interval_hours

    def list_routes(self) -> list[dict[str, Any]]:
        config = self.get_config()
        return [
            {
                "route_key": route.route_key,
                "route_type": route.route_type,
                "origin": route.origin,
                "destination": route.destination,
                "departure_date": route.departure_date,
                "return_date": route.return_date,
                "display_label": route.display_label,
                "cabin": route.cabin,
                "transfer_policy": route.transfer_policy,
                "passengers": route.passengers,
                "providers": route.providers or list(config.providers.keys()),
                "preferred_airlines": route.preferred_airlines or [],
                "segments": [segment.to_dict() for segment in route.segments or []],
                "auto_query_enabled": route.auto_query_enabled,
                "auto_query_interval_hours": route.auto_query_interval_hours,
                "auto_query_next_run_at": route.auto_query_next_run_at,
                "group_id": route.group_id,
                "group_label": route.group_label,
            }
            for route in config.routes
        ]

    def upsert_route(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload_has_city_groups(payload):
            return self.upsert_route_batch(payload)

        self.refresh_config()
        config = self.get_config()
        previous_key = str(payload.get("previous_route_key") or "").strip()
        route_type = str(payload.get("route_type") or "one_way")
        existing_route = next(
            (
                item for item in config.routes
                if item.route_key == previous_key or item.route_key == str(payload.get("route_key") or "").strip()
            ),
            None,
        )
        route = self._route_from_payload(
            payload,
            route_type=route_type,
            existing_route=existing_route,
        )
        self._validate_route(route)

        routes = list(config.routes)
        replaced = False
        for index, existing in enumerate(routes):
            if existing.route_key == previous_key or existing.route_key == route.route_key:
                routes[index] = route
                replaced = True
                break
        if not replaced:
            routes.append(route)

        self._save_and_refresh(routes)
        saved_route = next((item for item in self.list_routes() if item["route_key"] == route.route_key), route.to_dict())
        return {"saved": True, "route": saved_route}

    def upsert_route_batch(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.refresh_config()
        config = self.get_config()
        routes_to_add = expand_route_payload(
            payload,
            auto_query_interval_hours=self.auto_query_interval_hours,
        )
        if not routes_to_add:
            raise ValueError("Route group did not produce any routes.")
        for route in routes_to_add:
            self._validate_route(route)

        previous_group_id = str(payload.get("previous_group_id") or "").strip()
        group_id = routes_to_add[0].group_id
        route_keys_to_add = {item.route_key for item in routes_to_add}
        routes = [
            route for route in config.routes
            if not (
                (previous_group_id and route.group_id == previous_group_id)
                or (group_id and route.group_id == group_id)
                or route.route_key in route_keys_to_add
            )
        ]
        routes.extend(routes_to_add)
        self._save_and_refresh(routes)
        saved_keys = {route.route_key for route in routes_to_add}
        saved_routes = [route for route in self.list_routes() if route["route_key"] in saved_keys]
        return {
            "saved": True,
            "group_id": group_id,
            "group_label": routes_to_add[0].group_label,
            "created": len(routes_to_add),
            "routes": saved_routes,
        }

    def delete_route(self, route_key: str) -> bool:
        self.refresh_config()
        config = self.get_config()
        routes = [route for route in config.routes if route.route_key != route_key]
        if len(routes) == len(config.routes):
            return False
        self._save_and_refresh(routes)
        return True

    def delete_route_group(self, group_id: str) -> bool:
        self.refresh_config()
        config = self.get_config()
        routes = [route for route in config.routes if route.group_id != group_id]
        if len(routes) == len(config.routes):
            return False
        self._save_and_refresh(routes)
        return True

    def update_auto_query(self, route_key: str, enabled: bool) -> dict[str, Any]:
        self.refresh_config()
        config = self.get_config()
        routes = list(config.routes)
        updated: RouteQuery | None = None
        for route in routes:
            if route.route_key != route_key:
                continue
            route.auto_query_enabled = enabled
            route.auto_query_interval_hours = self.auto_query_interval_hours
            if not enabled:
                route.auto_query_next_run_at = None
            updated = route
            break

        if updated is None:
            raise KeyError(route_key)

        self._save_and_refresh(routes)
        saved_route = next((item for item in self.list_routes() if item["route_key"] == route_key), None)
        return {"saved": True, "route": saved_route}

    def _route_from_payload(
        self,
        payload: dict[str, Any],
        *,
        route_type: str,
        existing_route: RouteQuery | None,
    ) -> RouteQuery:
        providers_payload = (
            payload.get("providers", [])
            if "providers" in payload
            else (existing_route.providers if existing_route else [])
        )
        providers = [str(name).strip() for name in providers_payload or [] if str(name).strip()]
        preferred_airlines = [
            str(code).strip().upper()
            for code in payload.get("preferred_airlines", [])
            if str(code).strip()
        ]
        auto_query_enabled = bool(
            payload["auto_query_enabled"]
            if "auto_query_enabled" in payload
            else (existing_route.auto_query_enabled if existing_route else False)
        )
        auto_query_next_run_at = str(
            payload["auto_query_next_run_at"]
            if "auto_query_next_run_at" in payload
            else (existing_route.auto_query_next_run_at if existing_route else "")
        ).strip() or None
        segments = self._segments_from_payload(payload.get("segments") or [])

        if route_type == "multi_city" and segments:
            return RouteQuery(
                origin=segments[0].origin,
                destination=segments[-1].destination,
                departure_date=segments[0].departure_date,
                return_date=segments[-1].departure_date if len(segments) > 1 else None,
                route_type=route_type,
                cabin=str(payload.get("cabin", "economy")).lower(),
                transfer_policy=normalize_transfer_policy(payload.get("transfer_policy", "any")),
                passengers=int(payload.get("passengers", 1)),
                providers=providers or None,
                preferred_airlines=preferred_airlines or None,
                segments=segments,
                auto_query_enabled=auto_query_enabled,
                auto_query_interval_hours=self.auto_query_interval_hours,
                auto_query_next_run_at=auto_query_next_run_at,
                group_id=str(payload.get("group_id") or "").strip() or None,
                group_label=str(payload.get("group_label") or "").strip() or None,
            )

        return RouteQuery(
            origin=normalize_airport_code(payload["origin"]),
            destination=normalize_airport_code(payload["destination"]),
            departure_date=str(payload["departure_date"]),
            return_date=str(payload.get("return_date") or "").strip() or None,
            route_type="one_way",
            cabin=str(payload.get("cabin", "economy")).lower(),
            transfer_policy=normalize_transfer_policy(payload.get("transfer_policy", "any")),
            passengers=int(payload.get("passengers", 1)),
            providers=providers or None,
            preferred_airlines=preferred_airlines or None,
            segments=None,
            auto_query_enabled=auto_query_enabled,
            auto_query_interval_hours=self.auto_query_interval_hours,
            auto_query_next_run_at=auto_query_next_run_at,
            group_id=str(payload.get("group_id") or "").strip() or None,
            group_label=str(payload.get("group_label") or "").strip() or None,
        )

    @staticmethod
    def _segments_from_payload(segments_payload: list[Any]) -> list[RouteSegment]:
        return [
            RouteSegment(
                origin=normalize_airport_code(item["origin"]),
                destination=normalize_airport_code(item["destination"]),
                departure_date=str(item["departure_date"]),
            )
            for item in segments_payload
            if isinstance(item, dict) and item.get("origin") and item.get("destination") and item.get("departure_date")
        ]

    def _validate_route(self, route: RouteQuery) -> None:
        config = self.get_config()
        if route.route_type not in {"one_way", "multi_city"}:
            raise ValueError("route_type must be one_way or multi_city.")
        if route.cabin not in ALLOWED_CABINS:
            raise ValueError(f"Unsupported cabin: {route.cabin}.")
        if route.transfer_policy not in ALLOWED_TRANSFER_POLICIES:
            raise ValueError(f"Unsupported transfer policy: {route.transfer_policy}.")
        if not 1 <= route.passengers <= 9:
            raise ValueError("passengers must be between 1 and 9.")

        providers = route.providers or []
        if not providers:
            raise ValueError("Select at least one provider.")
        for provider in providers:
            definition = provider_definition(provider)
            if definition is None or not definition.has_handler:
                raise ValueError(f"Provider is not available for browser queries: {provider}.")
            provider_config = config.providers.get(provider)
            if provider_config is None:
                raise ValueError(f"Unknown provider: {provider}.")
            if not provider_config.enabled:
                raise ValueError(f"Provider is disabled: {provider}.")

        for code in route.preferred_airlines or []:
            if not AIRLINE_CODE_PATTERN.fullmatch(code):
                raise ValueError(f"Invalid airline code: {code}.")

        if route.is_multi_city:
            segments = route.segments or []
            if len(segments) < 2:
                raise ValueError("multi_city routes require at least two segments.")
            previous_date: date | None = None
            for segment in segments:
                self._validate_airport_pair(segment.origin, segment.destination)
                segment_date = self._parse_future_date(segment.departure_date, "segment departure_date")
                if previous_date and segment_date < previous_date:
                    raise ValueError("multi_city segment dates must be non-decreasing.")
                previous_date = segment_date
            return

        self._validate_airport_pair(route.origin, route.destination)
        departure = self._parse_future_date(route.departure_date, "departure_date")
        if route.return_date:
            return_date = self._parse_future_date(route.return_date, "return_date")
            if return_date <= departure:
                raise ValueError("return_date must be later than departure_date.")

    @staticmethod
    def _validate_airport_pair(origin: str, destination: str) -> None:
        if not AIRPORT_CODE_PATTERN.fullmatch(origin):
            raise ValueError(f"Invalid origin airport code: {origin}.")
        if not AIRPORT_CODE_PATTERN.fullmatch(destination):
            raise ValueError(f"Invalid destination airport code: {destination}.")
        if origin == destination:
            raise ValueError("origin and destination cannot be the same.")

    @staticmethod
    def _parse_future_date(value: str, field_name: str) -> date:
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError(f"{field_name} must use YYYY-MM-DD.") from exc
        if parsed < date.today():
            raise ValueError(f"{field_name} cannot be in the past.")
        return parsed

    def _save_and_refresh(self, routes: list[RouteQuery]) -> None:
        self.rebalance_auto_query(routes)
        self.config_manager.save_routes(routes)
        self.refresh_config()
