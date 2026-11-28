from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class TailQueueItem:
    destination: str
    order: int
    status: str = "pending"
    retry_count: int = 0
    last_phase: str | None = None
    last_method: str | None = None
    last_error: str | None = None
    last_price: float | None = None
    trace_path: str | None = None
    diagnostic_json_path: str | None = None

    def __post_init__(self) -> None:
        self.destination = str(self.destination).upper()
        self.order = int(self.order)
        self.status = str(self.status or "pending")
        self.retry_count = int(self.retry_count or 0)
        self.last_price = _optional_float(self.last_price)

    @classmethod
    def from_mapping(cls, data: dict[str, Any], *, destination: str | None = None, order: int | None = None) -> "TailQueueItem":
        return cls(
            destination=str(destination or data.get("destination") or "").upper(),
            order=int(order if order is not None else data.get("order") or 0),
            status=str(data.get("status") or "pending"),
            retry_count=int(data.get("retry_count") or 0),
            last_phase=data.get("last_phase"),
            last_method=data.get("last_method"),
            last_error=data.get("last_error"),
            last_price=data.get("last_price"),
            trace_path=data.get("trace_path"),
            diagnostic_json_path=data.get("diagnostic_json_path"),
        )

    def apply_attempt_update(self, update: dict[str, Any], *, retry_increment: int = 0) -> None:
        for key in [
            "status",
            "last_phase",
            "last_method",
            "last_error",
            "trace_path",
            "diagnostic_json_path",
        ]:
            if update.get(key) is not None:
                setattr(self, key, update[key])
        if update.get("last_price") is not None:
            self.last_price = _optional_float(update.get("last_price"))
        self.retry_count += retry_increment

    def to_record(self) -> dict[str, Any]:
        return {
            "destination": self.destination,
            "order": self.order,
            "status": self.status,
            "retry_count": self.retry_count,
            "last_phase": self.last_phase,
            "last_method": self.last_method,
            "last_error": self.last_error,
            "last_price": self.last_price,
            "trace_path": self.trace_path,
            "diagnostic_json_path": self.diagnostic_json_path,
        }


@dataclass(slots=True)
class TailDiscoveryAttempt:
    queue_key: str
    origin: str
    transfer: str
    destination: str
    departure_date: str
    cabin: str
    status: str = "unknown"
    provider: str = "ctrip"
    job_id: str | None = None
    phase: str | None = None
    method: str | None = None
    price: float | None = None
    detail_source: str | None = None
    detail_quality: str | None = None
    error: str | None = None
    observed_at: str | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)
    trace_path: str | None = None
    diagnostic_artifact: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        self.provider = str(self.provider)
        self.origin = str(self.origin).upper()
        self.transfer = str(self.transfer).upper()
        self.destination = str(self.destination).upper()
        self.departure_date = str(self.departure_date)
        self.cabin = str(self.cabin)
        self.status = str(self.status or "unknown")
        self.price = _optional_float(self.price)

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "TailDiscoveryAttempt":
        return cls(
            job_id=data.get("job_id"),
            queue_key=str(data["queue_key"]),
            provider=str(data.get("provider") or "ctrip"),
            origin=str(data["origin"]).upper(),
            transfer=str(data["transfer"]).upper(),
            destination=str(data["destination"]).upper(),
            departure_date=str(data["departure_date"]),
            cabin=str(data["cabin"]),
            status=str(data.get("status") or "unknown"),
            phase=data.get("phase"),
            method=data.get("method"),
            price=_optional_float(data.get("price")),
            detail_source=data.get("detail_source"),
            detail_quality=data.get("detail_quality"),
            error=data.get("error"),
            observed_at=data.get("observed_at"),
            raw_payload=dict(data.get("raw_payload") or {}),
            trace_path=data.get("trace_path"),
            diagnostic_artifact=data.get("diagnostic_artifact"),
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "queue_key": self.queue_key,
            "provider": self.provider,
            "origin": self.origin,
            "transfer": self.transfer,
            "destination": self.destination,
            "departure_date": self.departure_date,
            "cabin": self.cabin,
            "status": self.status,
            "phase": self.phase,
            "method": self.method,
            "price": self.price,
            "detail_source": self.detail_source,
            "detail_quality": self.detail_quality,
            "error": self.error,
            "observed_at": self.observed_at,
            "raw_payload": dict(self.raw_payload),
            "trace_path": self.trace_path,
            "diagnostic_artifact": self.diagnostic_artifact,
        }


@dataclass(slots=True)
class TailDiscoveryResult:
    provider: str
    origin: str
    transfer: str
    destination: str
    departure_date: str
    cabin: str
    price: float
    observed_at: str
    scraped_at: str
    passengers: int = 1
    currency: str = "CNY"
    flight_details: list[dict[str, Any]] = field(default_factory=list)
    raw_payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.provider = str(self.provider)
        self.origin = str(self.origin).upper()
        self.transfer = str(self.transfer).upper()
        self.destination = str(self.destination).upper()
        self.departure_date = str(self.departure_date)
        self.cabin = str(self.cabin)
        self.passengers = int(self.passengers)
        self.currency = str(self.currency or "CNY")
        self.price = float(self.price)
        self.observed_at = str(self.observed_at)
        self.scraped_at = str(self.scraped_at)
        self.flight_details = [leg.to_record() for leg in coerce_tail_flight_legs(self.flight_details)]
        self.raw_payload = dict(self.raw_payload or {})
        self.raw_payload.setdefault("structured_itinerary", self.structured_itinerary().to_record())

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "TailDiscoveryResult":
        return cls(
            provider=str(data["provider"]),
            origin=str(data["origin"]).upper(),
            transfer=str(data["transfer"]).upper(),
            destination=str(data["destination"]).upper(),
            departure_date=str(data["departure_date"]),
            cabin=str(data["cabin"]),
            passengers=int(data.get("passengers", 1)),
            currency=str(data.get("currency") or "CNY"),
            price=float(data["price"]),
            observed_at=str(data["observed_at"]),
            scraped_at=str(data["scraped_at"]),
            flight_details=list(data.get("flight_details") or []),
            raw_payload=dict(data.get("raw_payload") or {}),
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "origin": self.origin,
            "transfer": self.transfer,
            "destination": self.destination,
            "departure_date": self.departure_date,
            "cabin": self.cabin,
            "passengers": self.passengers,
            "currency": self.currency,
            "price": self.price,
            "observed_at": self.observed_at,
            "scraped_at": self.scraped_at,
            "flight_details": list(self.flight_details),
            "raw_payload": dict(self.raw_payload),
        }

    def structured_itinerary(self) -> "TailItineraryRecord":
        validation = self.raw_payload.get("validation") if isinstance(self.raw_payload.get("validation"), dict) else {}
        return TailItineraryRecord(
            origin=self.origin,
            transfer=self.transfer,
            destination=self.destination,
            departure_date=self.departure_date,
            cabin=self.cabin,
            price=self.price,
            currency=self.currency,
            source=_optional_text(self.raw_payload.get("detail_source")),
            quality=_optional_text(self.raw_payload.get("detail_quality")),
            validation_status=_optional_text(validation.get("status")) if isinstance(validation, dict) else None,
            legs=coerce_tail_flight_legs(self.flight_details),
        )


@dataclass(slots=True)
class TailFlightLeg:
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
    seat_class: str | None = None
    tail_transfer: str | None = None
    confidence: float | None = None

    def __post_init__(self) -> None:
        self.segment_index = _optional_int(self.segment_index)
        self.origin = _optional_code(self.origin)
        self.destination = _optional_code(self.destination)
        self.departure_airport = _optional_text(self.departure_airport)
        self.arrival_airport = _optional_text(self.arrival_airport)
        self.departure_date = _optional_text(self.departure_date)
        self.departure_time = _optional_text(self.departure_time)
        self.arrival_time = _optional_text(self.arrival_time)
        self.airline = _optional_text(self.airline)
        self.flight_no = _optional_text(self.flight_no)
        self.aircraft = _optional_text(self.aircraft)
        self.cabin = _optional_text(self.cabin)
        self.seat_class = _optional_text(self.seat_class)
        self.tail_transfer = _optional_text(self.tail_transfer)
        self.confidence = _bounded_optional_float(self.confidence, minimum=0, maximum=1)

    @classmethod
    def from_mapping(cls, data: dict[str, Any], *, index: int | None = None) -> "TailFlightLeg":
        return cls(
            segment_index=data.get("segment_index") or index,
            origin=data.get("origin"),
            destination=data.get("destination"),
            departure_airport=data.get("departure_airport"),
            arrival_airport=data.get("arrival_airport"),
            departure_date=data.get("departure_date"),
            departure_time=data.get("departure_time"),
            arrival_time=data.get("arrival_time"),
            airline=data.get("airline"),
            flight_no=data.get("flight_no"),
            aircraft=data.get("aircraft"),
            cabin=data.get("cabin"),
            seat_class=data.get("seat_class"),
            tail_transfer=data.get("tail_transfer"),
            confidence=data.get("confidence"),
        )

    def to_record(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "segment_index": self.segment_index,
                "origin": self.origin,
                "destination": self.destination,
                "departure_airport": self.departure_airport,
                "arrival_airport": self.arrival_airport,
                "departure_date": self.departure_date,
                "departure_time": self.departure_time,
                "arrival_time": self.arrival_time,
                "airline": self.airline,
                "flight_no": self.flight_no,
                "aircraft": self.aircraft,
                "cabin": self.cabin,
                "seat_class": self.seat_class,
                "tail_transfer": self.tail_transfer,
                "confidence": self.confidence,
            }.items()
            if value is not None
        }


@dataclass(slots=True)
class TailItineraryRecord:
    origin: str
    transfer: str
    destination: str
    departure_date: str
    cabin: str | None = None
    price: float | None = None
    currency: str | None = "CNY"
    source: str | None = None
    quality: str | None = None
    validation_status: str | None = None
    legs: list[TailFlightLeg] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.origin = str(self.origin).upper()
        self.transfer = str(self.transfer).upper()
        self.destination = str(self.destination).upper()
        self.departure_date = str(self.departure_date)
        self.cabin = _optional_text(self.cabin)
        self.price = _optional_float(self.price)
        self.currency = _optional_text(self.currency) or "CNY"
        self.source = _optional_text(self.source)
        self.quality = _optional_text(self.quality)
        self.validation_status = _optional_text(self.validation_status)
        self.legs = coerce_tail_flight_legs(self.legs)

    def to_record(self) -> dict[str, Any]:
        return {
            "origin": self.origin,
            "transfer": self.transfer,
            "destination": self.destination,
            "departure_date": self.departure_date,
            "cabin": self.cabin,
            "price": self.price,
            "currency": self.currency,
            "source": self.source,
            "quality": self.quality,
            "validation_status": self.validation_status,
            "legs": [leg.to_record() for leg in self.legs],
        }


def coerce_tail_attempt(value: TailDiscoveryAttempt | dict[str, Any]) -> TailDiscoveryAttempt:
    if isinstance(value, TailDiscoveryAttempt):
        return value
    return TailDiscoveryAttempt.from_mapping(value)


def coerce_tail_result(value: TailDiscoveryResult | dict[str, Any]) -> TailDiscoveryResult:
    if isinstance(value, TailDiscoveryResult):
        return value
    return TailDiscoveryResult.from_mapping(value)


def coerce_tail_flight_legs(values: list[Any]) -> list[TailFlightLeg]:
    legs: list[TailFlightLeg] = []
    for index, value in enumerate(values or [], start=1):
        if isinstance(value, TailFlightLeg):
            leg = value
        elif isinstance(value, dict):
            leg = TailFlightLeg.from_mapping(value, index=index)
        else:
            continue
        legs.append(leg)
    return legs


def coerce_tail_queue_item(value: TailQueueItem | dict[str, Any]) -> TailQueueItem:
    if isinstance(value, TailQueueItem):
        return value
    return TailQueueItem.from_mapping(value)


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except Exception:
        return None


def _bounded_optional_float(value: Any, *, minimum: float, maximum: float) -> float | None:
    number = _optional_float(value)
    if number is None:
        return None
    return max(minimum, min(maximum, number))


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except Exception:
        return None


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _optional_code(value: Any) -> str | None:
    text = _optional_text(value)
    return text.upper() if text and len(text) <= 4 else text
