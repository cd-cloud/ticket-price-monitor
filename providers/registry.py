from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ProviderStatus = Literal["stable", "experimental", "generic"]


@dataclass(frozen=True, slots=True)
class ProviderDefinition:
    name: str
    display_name: str
    status: ProviderStatus
    has_handler: bool
    supports_credentials: bool
    supports_saved_session: bool
    supports_round_trip: bool = False
    supports_multi_city: bool = False
    max_passengers: int = 1
    supports_transfer_filter: bool = False
    supports_airline_filter: bool = False
    requires_credentials: bool = False


PROVIDERS: dict[str, ProviderDefinition] = {
    "ctrip": ProviderDefinition(
        name="ctrip",
        display_name="Ctrip",
        status="stable",
        has_handler=True,
        supports_credentials=True,
        supports_saved_session=True,
        supports_round_trip=True,
        supports_multi_city=True,
        max_passengers=9,
        supports_transfer_filter=True,
        supports_airline_filter=True,
    ),
    "priceline": ProviderDefinition(
        name="priceline",
        display_name="Priceline",
        status="experimental",
        has_handler=True,
        supports_credentials=False,
        supports_saved_session=False,
    ),
    "feizhu": ProviderDefinition(
        name="feizhu",
        display_name="Fliggy",
        status="experimental",
        has_handler=True,
        supports_credentials=False,
        supports_saved_session=False,
        supports_transfer_filter=True,
        supports_airline_filter=True,
    ),
    "airchina": ProviderDefinition(
        name="airchina",
        display_name="Air China",
        status="generic",
        has_handler=False,
        supports_credentials=True,
        supports_saved_session=True,
    ),
}


CLI_PROVIDER_CHOICES = tuple(PROVIDERS)


def provider_definition(name: str) -> ProviderDefinition | None:
    return PROVIDERS.get(str(name).strip().lower())


def provider_registry_payload() -> list[dict[str, object]]:
    return [
        {
            "name": item.name,
            "display_name": item.display_name,
            "status": item.status,
            "has_handler": item.has_handler,
            "supports_credentials": item.supports_credentials,
            "supports_saved_session": item.supports_saved_session,
            "supports_round_trip": item.supports_round_trip,
            "supports_multi_city": item.supports_multi_city,
            "max_passengers": item.max_passengers,
            "supports_transfer_filter": item.supports_transfer_filter,
            "supports_airline_filter": item.supports_airline_filter,
            "requires_credentials": item.requires_credentials,
        }
        for item in PROVIDERS.values()
    ]
