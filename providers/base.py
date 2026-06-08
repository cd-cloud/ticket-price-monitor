"""Shared provider contracts for browser-driven flight queries."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from playwright.async_api import Page

from config_manager import ProviderConfig, RouteQuery


@dataclass(slots=True)
class ProviderQueryOutcome:
    price: float
    final_url: str
    title: str
    departure_time: str | None = None
    row_text: str | None = None
    flight_details: list[dict[str, Any]] = field(default_factory=list)
    extra_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ParsedProviderResult:
    """Provider-neutral parsed result before persistence payload assembly."""

    provider: str
    price: float
    final_url: str
    title: str
    departure_time: str | None = None
    row_text: str | None = None
    flight_details: list[dict[str, Any]] = field(default_factory=list)
    parser: str | None = None
    confidence: str | None = None
    extra_payload: dict[str, Any] = field(default_factory=dict)

    def to_outcome(self) -> ProviderQueryOutcome:
        payload = dict(self.extra_payload)
        if self.parser:
            payload.setdefault("parser", self.parser)
        if self.confidence:
            payload.setdefault("parser_confidence", self.confidence)
        return ProviderQueryOutcome(
            price=float(self.price),
            final_url=self.final_url,
            title=self.title,
            departure_time=self.departure_time,
            row_text=self.row_text,
            flight_details=list(self.flight_details),
            extra_payload=payload,
        )


class FlightProvider(Protocol):
    name: str
    supports_multi_city: bool
    supports_tail_discovery: bool

    async def query(
        self,
        runtime: Any,
        provider: ProviderConfig,
        route: RouteQuery,
        page: Page,
        response_store: list[dict],
    ) -> ProviderQueryOutcome:
        """Run a provider query and return normalized result details."""
