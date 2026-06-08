"""Browser backend registry and capability metadata."""

from __future__ import annotations

from dataclasses import dataclass

from .base import BrowserBackend
from .playwright import PlaywrightBackend


@dataclass(frozen=True, slots=True)
class BackendCapabilities:
    supports_storage_state: bool
    supports_credential_login: bool
    supports_persistent_profile: bool = False


@dataclass(frozen=True, slots=True)
class BackendDefinition:
    name: str
    description: str
    safety_tier: str
    capabilities: BackendCapabilities
    cls: type[BrowserBackend] | None = None
    unavailable_reason: str = ""

    @property
    def available(self) -> bool:
        return self.cls is not None and not self.unavailable_reason


_BACKENDS: dict[str, type[BrowserBackend]] = {
    "playwright": PlaywrightBackend,
}
_DEFINITIONS: dict[str, BackendDefinition] = {
    "playwright": BackendDefinition(
        name="playwright",
        description="Standard Playwright Chromium backend.",
        safety_tier="stable",
        capabilities=BackendCapabilities(
            supports_storage_state=True,
            supports_credential_login=True,
        ),
        cls=PlaywrightBackend,
    )
}
_OPTIONAL_LOADED = False


_OPTIONAL_BACKENDS: dict[str, tuple[str, str, str, BackendCapabilities]] = {
    "rebrowser": (
        "backends.rebrowser",
        "Rebrowser Playwright replacement with stealth patches.",
        "experimental",
        BackendCapabilities(
            supports_storage_state=False,
            supports_credential_login=False,
        ),
    ),
    "patchright": (
        "backends.patchright",
        "Patchright Chromium backend with anti-detection fixes.",
        "experimental",
        BackendCapabilities(
            supports_storage_state=False,
            supports_credential_login=False,
        ),
    ),
    "camoufox": (
        "backends.camoufox",
        "Camoufox anti-detect Firefox backend.",
        "experimental",
        BackendCapabilities(
            supports_storage_state=False,
            supports_credential_login=False,
        ),
    ),
    "chrome": (
        "backends.chrome",
        "System Google Chrome with a persistent provider profile.",
        "stable",
        BackendCapabilities(
            supports_storage_state=False,
            supports_credential_login=True,
            supports_persistent_profile=True,
        ),
    ),
    "chrome_cdp": (
        "backends.chrome_cdp",
        "System Google Chrome launched normally and attached over CDP.",
        "experimental",
        BackendCapabilities(
            supports_storage_state=False,
            supports_credential_login=False,
            supports_persistent_profile=True,
        ),
    ),
}


def _load_optional_backends() -> None:
    """Lazy-load optional backends and keep failure reasons for diagnostics."""
    global _OPTIONAL_LOADED
    if _OPTIONAL_LOADED:
        return

    for name, (module_name, description, safety_tier, capabilities) in _OPTIONAL_BACKENDS.items():
        try:
            module = __import__(module_name, fromlist=[""])
            class_name = {
                "rebrowser": "RebrowserBackend",
                "patchright": "PatchrightBackend",
                "camoufox": "CamoufoxBackend",
                "chrome": "SystemChromeBackend",
                "chrome_cdp": "CdpChromeBackend",
            }[name]
            cls = getattr(module, class_name)
            _BACKENDS[name] = cls
            _DEFINITIONS[name] = BackendDefinition(
                name=name,
                description=description,
                safety_tier=safety_tier,
                capabilities=capabilities,
                cls=cls,
            )
        except Exception as exc:
            _DEFINITIONS[name] = BackendDefinition(
                name=name,
                description=description,
                safety_tier=safety_tier,
                capabilities=capabilities,
                cls=None,
                unavailable_reason=f"{type(exc).__name__}: {exc}",
            )
    _OPTIONAL_LOADED = True


def backend_definition(name: str | None) -> BackendDefinition | None:
    normalized = str(name or "").strip().lower()
    if not normalized:
        return None
    _load_optional_backends()
    return _DEFINITIONS.get(normalized)


def backend_capabilities(name: str | None) -> BackendCapabilities:
    definition = backend_definition(name)
    if definition is None:
        return BackendCapabilities(
            supports_storage_state=False,
            supports_credential_login=False,
        )
    return definition.capabilities


def known_backend_names() -> list[str]:
    _load_optional_backends()
    return list(_DEFINITIONS.keys())


def available_backend_names() -> list[str]:
    _load_optional_backends()
    return [name for name, definition in _DEFINITIONS.items() if definition.available]


def backend_registry_payload() -> list[dict[str, object]]:
    _load_optional_backends()
    return [
        {
            "name": definition.name,
            "description": definition.description,
            "safety_tier": definition.safety_tier,
            "available": definition.available,
            "unavailable_reason": definition.unavailable_reason,
            "capabilities": {
                "supports_storage_state": definition.capabilities.supports_storage_state,
                "supports_credential_login": definition.capabilities.supports_credential_login,
                "supports_persistent_profile": definition.capabilities.supports_persistent_profile,
            },
        }
        for definition in _DEFINITIONS.values()
    ]


def create_backend(name: str) -> BrowserBackend:
    normalized = str(name or "").strip().lower()
    definition = backend_definition(normalized)
    if definition is None:
        raise ValueError(
            f"Unknown browser backend: {name!r}. "
            f"Known: {known_backend_names()}"
        )
    if definition.cls is None:
        raise ValueError(
            f"Browser backend {normalized!r} is unavailable: "
            f"{definition.unavailable_reason}"
        )
    return definition.cls()


def register_backend(name: str, cls: type[BrowserBackend]) -> None:
    normalized = str(name or "").strip().lower()
    if not normalized:
        raise ValueError("backend name cannot be empty")
    _BACKENDS[normalized] = cls
    _DEFINITIONS[normalized] = BackendDefinition(
        name=normalized,
        description="Registered custom backend.",
        safety_tier="custom",
        capabilities=BackendCapabilities(
            supports_storage_state=False,
            supports_credential_login=False,
        ),
        cls=cls,
    )
