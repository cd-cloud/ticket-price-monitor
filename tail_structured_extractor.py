"""Strict structured models for tail-discovery results.

This module is the local, deterministic half of the future Pydantic AI
fallback. It gives us a stable schema and hard validation boundary before any
LLM-assisted extraction is allowed to affect stored results.
"""

from __future__ import annotations

import importlib.util
from typing import Any

from pydantic import BaseModel, Field

from tail_models import TailDiscoveryResult


class FlightLeg(BaseModel):
    segment_index: int | None = None
    origin: str | None = None
    destination: str | None = None
    departure_airport: str | None = None
    arrival_airport: str | None = None
    departure_date: str | None = None
    departure_time: str | None = None
    arrival_time: str | None = None
    airline: str | None = None
    flight_no: str | None = None
    aircraft: str | None = None
    cabin: str | None = None
    tail_transfer: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)


class TailItinerary(BaseModel):
    origin: str
    transfer: str
    destination: str
    departure_date: str
    cabin: str | None = None
    price: float | None = None
    currency: str | None = None
    source: str | None = None
    quality: str | None = None
    validation_status: str | None = None
    legs: list[FlightLeg] = Field(default_factory=list)


def pydantic_ai_status() -> dict[str, Any]:
    available = importlib.util.find_spec("pydantic_ai") is not None
    return {
        "available": available,
        "mode": "schema_ready" if available else "schema_only",
        "reason": (
            "pydantic-ai is installed; strict schema extraction can be enabled later."
            if available
            else "pydantic-ai is not installed; deterministic schema validation remains active."
        ),
    }


def tail_itinerary_schema() -> dict[str, Any]:
    if hasattr(TailItinerary, "model_json_schema"):
        return TailItinerary.model_json_schema()
    return TailItinerary.schema()


def model_to_dict(model: BaseModel) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


def tail_itinerary_from_result(result: dict[str, Any]) -> TailItinerary:
    normalized = TailDiscoveryResult.from_mapping(
        {
            **result,
            "provider": result.get("provider") or "ctrip",
            "passengers": result.get("passengers") or 1,
            "currency": result.get("currency") or "CNY",
            "observed_at": result.get("observed_at") or "",
            "scraped_at": result.get("scraped_at") or result.get("observed_at") or "",
        }
    )
    structured = normalized.structured_itinerary().to_record()
    raw_payload = result.get("raw_payload") if isinstance(result.get("raw_payload"), dict) else {}
    validation = result.get("validation") if isinstance(result.get("validation"), dict) else raw_payload.get("validation")
    if not isinstance(validation, dict):
        validation = {}
    legs = [
        FlightLeg(
            segment_index=_optional_int(item.get("segment_index")),
            origin=_optional_text(item.get("origin")),
            destination=_optional_text(item.get("destination")),
            departure_airport=_optional_text(item.get("departure_airport")),
            arrival_airport=_optional_text(item.get("arrival_airport")),
            departure_date=_optional_text(item.get("departure_date")),
            departure_time=_optional_text(item.get("departure_time")),
            arrival_time=_optional_text(item.get("arrival_time")),
            airline=_optional_text(item.get("airline")),
            flight_no=_optional_text(item.get("flight_no")),
            aircraft=_optional_text(item.get("aircraft")),
            cabin=_optional_text(item.get("cabin")),
            tail_transfer=_optional_text(item.get("tail_transfer")),
            confidence=_leg_confidence(item, raw_payload),
        )
        for item in structured.get("legs") or []
        if isinstance(item, dict)
    ]
    return TailItinerary(
        origin=_required_text(structured.get("origin"), "origin"),
        transfer=_required_text(structured.get("transfer"), "transfer"),
        destination=_required_text(structured.get("destination"), "destination"),
        departure_date=_required_text(structured.get("departure_date"), "departure_date"),
        cabin=_optional_text(structured.get("cabin")),
        price=_optional_float(structured.get("price")),
        currency=_optional_text(structured.get("currency")),
        source=_optional_text(structured.get("source") or raw_payload.get("detail_source") or result.get("detail_source")),
        quality=_optional_text(structured.get("quality") or raw_payload.get("detail_quality") or result.get("detail_quality")),
        validation_status=_optional_text(validation.get("status")),
        legs=legs,
    )


def _required_text(value: Any, field_name: str) -> str:
    text = _optional_text(value)
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def _leg_confidence(detail: dict[str, Any], raw_payload: dict[str, Any]) -> float:
    source = str(raw_payload.get("detail_source") or "")
    if source == "network_exact":
        return 0.95
    if source == "dom_transit_card":
        return 0.75
    if source in {"text_fallback", "cleaned_text_fallback"}:
        return 0.55
    if detail.get("flight_no") and detail.get("departure_time"):
        return 0.65
    return 0.4
