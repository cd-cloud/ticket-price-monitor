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


PROVIDERS: dict[str, ProviderDefinition] = {
    "ctrip": ProviderDefinition(
        name="ctrip",
        display_name="Ctrip",
        status="stable",
        has_handler=True,
        supports_credentials=True,
        supports_saved_session=True,
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
        }
        for item in PROVIDERS.values()
    ]
