import base64
import hashlib
import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from dotenv import load_dotenv

from airport_aliases import AIRPORT_NAME_SUFFIXES, CLEAN_AIRPORT_ALIAS_CODE_MAP
from providers.registry import provider_definition
from browser_environment import BrowserEnvironment, parse_browser_environment, expand_environment_preset
from browser_fingerprint import default_fingerprint
from private_files import ensure_private_directory, interprocess_lock, make_file_private


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = ROOT / "config.json"
LOGGER = logging.getLogger(__name__)
DEFAULT_ENV_PATH = ROOT / ".env"


class ConfigConflictError(RuntimeError):
    """Raised when a writer tries to replace a config changed by another process."""


TRANSFER_POLICY_ALIASES = {
    "any": "any",
    "direct": "direct_only",
    "direct_only": "direct_only",
    "transfer": "transfer_only",
    "transfer_only": "transfer_only",
}
AIRPORT_ALIAS_CODE_MAP = dict(CLEAN_AIRPORT_ALIAS_CODE_MAP)


@dataclass(slots=True)
class RouteSegment:
    origin: str
    destination: str
    departure_date: str

    @property
    def segment_key(self) -> str:
        return f"{self.origin.upper()}-{self.destination.upper()}-{self.departure_date}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "origin": self.origin,
            "destination": self.destination,
            "departure_date": self.departure_date,
        }


@dataclass(slots=True)
class RouteQuery:
    origin: str
    destination: str
    departure_date: str
    return_date: str | None = None
    route_type: str = "one_way"
    cabin: str = "economy"
    transfer_policy: str = "any"
    passengers: int = 1
    providers: list[str] | None = None
    preferred_airlines: list[str] | None = None
    segments: list[RouteSegment] | None = None
    auto_query_enabled: bool = False
    auto_query_interval_hours: int = 12
    auto_query_next_run_at: str | None = None
    group_id: str | None = None
    group_label: str | None = None

    @property
    def route_key(self) -> str:
        airlines = sorted({str(code).strip().upper() for code in (self.preferred_airlines or []) if str(code).strip()})
        airline_suffix = [",".join(airlines)] if airlines else []
        if self.is_multi_city:
            segment_part = "^".join(segment.segment_key for segment in self.segments or [])
            return "|".join(
                [
                    "multi_city",
                    segment_part,
                    self.cabin.lower(),
                    self.transfer_policy.lower(),
                    str(self.passengers),
                    *airline_suffix,
                ]
            )
        return "|".join(
            [
                self.origin.upper(),
                self.destination.upper(),
                self.departure_date,
                self.return_date or "",
                self.cabin.lower(),
                self.transfer_policy.lower(),
                str(self.passengers),
                *airline_suffix,
            ]
        )

    @property
    def legacy_route_key(self) -> str:
        if self.is_multi_city:
            segment_part = "^".join(segment.segment_key for segment in self.segments or [])
            return "|".join(
                [
                    "multi_city",
                    segment_part,
                    self.cabin.lower(),
                    self.transfer_policy.lower(),
                    str(self.passengers),
                ]
            )
        return "|".join(
            [
                self.origin.upper(),
                self.destination.upper(),
                self.departure_date,
                self.return_date or "",
                self.cabin.lower(),
                self.transfer_policy.lower(),
                str(self.passengers),
            ]
        )

    @property
    def is_multi_city(self) -> bool:
        return self.route_type == "multi_city" and bool(self.segments)

    def to_dict(self) -> dict[str, Any]:
        return {
            "origin": self.origin,
            "destination": self.destination,
            "departure_date": self.departure_date,
            "return_date": self.return_date,
            "route_type": self.route_type,
            "cabin": self.cabin,
            "transfer_policy": self.transfer_policy,
            "passengers": self.passengers,
            "providers": self.providers,
            "preferred_airlines": self.preferred_airlines,
            "segments": [segment.to_dict() for segment in self.segments or []] or None,
            "auto_query_enabled": self.auto_query_enabled,
            "auto_query_interval_hours": self.auto_query_interval_hours,
            "auto_query_next_run_at": self.auto_query_next_run_at,
            "group_id": self.group_id,
            "group_label": self.group_label,
        }

    @property
    def display_label(self) -> str:
        if self.is_multi_city:
            return " / ".join(
                f"{segment.origin}->{segment.destination}"
                for segment in self.segments or []
            )
        return f"{self.origin}->{self.destination}"


@dataclass(slots=True)
class ProviderConfig:
    name: str
    enabled: bool
    login_url: str
    search_url_template: str
    storage_state_file: Path
    selectors: dict[str, str | list[str]]
    timeout_ms: int
    extra_headers: dict[str, str]
    min_delay_seconds: float
    max_delay_seconds: float
    locale: str
    timezone: str
    browser_backend: str | None = None
    browser_backend_fallbacks: list[str] | None = None
    browser_environment: BrowserEnvironment | None = None


@dataclass(slots=True)
class AppConfig:
    root_dir: Path
    runtime_dir: Path
    output_dir: Path
    database_path: Path
    encrypted_credentials_path: Path
    salt_file: Path
    log_file: Path
    headless: bool
    automation_backend: str
    browser_backend: str
    browser_backend_fallbacks: list[str]
    browser_channel: str | None
    scheduler_interval_minutes: int
    timezone: str
    default_currency: str
    providers: dict[str, ProviderConfig]
    routes: list[RouteQuery]


def _expand_path(value: str, base_dir: Path) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(value))
    path = Path(expanded)
    if not path.is_absolute():
        path = (base_dir / path).resolve()
    return path


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_json_with_fingerprint(path: Path) -> tuple[dict[str, Any], str]:
    data = path.read_bytes()
    return json.loads(data.decode("utf-8")), hashlib.sha256(data).hexdigest()


def _parse_fallbacks(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip().lower() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    return [part.strip().lower() for part in text.split(",") if part.strip()]


def _validate_browser_backends(names: list[str]) -> None:
    """Validate backend names at config load time."""
    from backends import backend_definition, known_backend_names

    for name in names:
        if backend_definition(name) is None:
            raise ValueError(
                f"Unknown browser backend: {name!r}. "
                f"Known: {known_backend_names()}"
            )


def normalize_airport_code(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) == 3 and text.isascii() and text.isalpha():
        return text.upper()

    compact = text
    for suffix in AIRPORT_NAME_SUFFIXES:
        compact = compact.replace(suffix, "")
    compact = compact.strip()
    alias_candidates = [
        text,
        compact,
        text.lower(),
        compact.lower(),
        text.replace(" ", "").lower(),
        compact.replace(" ", "").lower(),
    ]
    for candidate in alias_candidates:
        code = CLEAN_AIRPORT_ALIAS_CODE_MAP.get(candidate)
        if code:
            return code
    return text.upper()


def _atomic_write_bytes(path: Path, data: bytes, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        if mode is not None:
            try:
                os.fchmod(descriptor, mode)
            except (AttributeError, OSError):
                pass
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if mode is not None:
            try:
                os.chmod(path, mode)
            except OSError:
                pass
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _shift_expired_example_routes(raw: dict[str, Any], *, today: date | None = None) -> dict[str, Any]:
    tz_name = str((raw.get("scheduler") or {}).get("timezone") or "Asia/Shanghai")
    try:
        current_day = today or datetime.now(ZoneInfo(tz_name)).date()
    except Exception:
        current_day = today or date.today()

    routes = raw.get("routes")
    if not isinstance(routes, list) or not routes:
        return raw

    parsed_dates: list[date] = []
    for route in routes:
        if not isinstance(route, dict):
            continue
        for key in ("departure_date", "return_date"):
            value = route.get(key)
            if isinstance(value, str) and value:
                try:
                    parsed_dates.append(date.fromisoformat(value))
                except ValueError:
                    pass
        for segment in route.get("segments") or []:
            if isinstance(segment, dict) and isinstance(segment.get("departure_date"), str):
                try:
                    parsed_dates.append(date.fromisoformat(segment["departure_date"]))
                except ValueError:
                    pass

    if not parsed_dates or min(parsed_dates) >= current_day:
        return raw

    offset = (current_day + timedelta(days=30)) - min(parsed_dates)

    def _shift(value: Any) -> Any:
        if not isinstance(value, str) or not value:
            return value
        try:
            return (date.fromisoformat(value) + offset).isoformat()
        except ValueError:
            return value

    for route in routes:
        if not isinstance(route, dict):
            continue
        route["departure_date"] = _shift(route.get("departure_date"))
        if route.get("return_date"):
            route["return_date"] = _shift(route.get("return_date"))
        for segment in route.get("segments") or []:
            if isinstance(segment, dict) and segment.get("departure_date"):
                segment["departure_date"] = _shift(segment.get("departure_date"))
    return raw


def normalize_transfer_policy(value: Any) -> str:
    text = str(value or "any").strip().lower()
    return TRANSFER_POLICY_ALIASES.get(text, text)


def _locale_from_accept_language(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    first = text.split(",", 1)[0].split(";", 1)[0].strip()
    return first or ""


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=390000,
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


class ConfigManager:
    def __init__(self, config_path: Path | None = None, env_path: Path | None = None) -> None:
        self.config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        self.env_path = Path(env_path) if env_path else (
            self.config_path.parent / ".env" if config_path else DEFAULT_ENV_PATH
        )
        if self.env_path.exists():
            make_file_private(self.env_path)
        load_dotenv(self.env_path, override=False)
        self.lock = threading.RLock()
        self._config_cache: AppConfig | None = None
        self._loaded_fingerprint: str | None = None
        self._config_lock_path = self.config_path.with_name(f".{self.config_path.name}.lock")

    def load(self) -> AppConfig:
        with self.lock:
            with interprocess_lock(self._config_lock_path):
                if not self.config_path.exists():
                    raise FileNotFoundError(
                        f"Missing config file: {self.config_path}. "
                        "Run `python main.py init-config` to create a local config.json from config.example.json."
                    )

                make_file_private(self.config_path)
                raw, fingerprint = _read_json_with_fingerprint(self.config_path)
                if self._config_cache is not None and fingerprint == self._loaded_fingerprint:
                    return self._config_cache
                config = self._load_from_raw(raw)
                self._loaded_fingerprint = fingerprint
                return config

    def _load_from_raw(self, raw: dict[str, Any]) -> AppConfig:
        storage = raw.get("storage", {})
        runtime_dir = _expand_path(storage.get("runtime_dir", "./runtime"), self.config_path.parent)
        output_dir = _expand_path(storage.get("output_dir", "./output"), self.config_path.parent)
        database_path = _expand_path(storage.get("database_path", "./runtime/flight_prices.db"), self.config_path.parent)
        credentials_path = _expand_path(
            storage.get("encrypted_credentials_path", "./runtime/credentials.enc.json"),
            self.config_path.parent,
        )
        salt_file = _expand_path(storage.get("salt_file", "./runtime/master.salt"), self.config_path.parent)
        log_file = _expand_path(storage.get("log_file", "./runtime/flight_tracker.log"), self.config_path.parent)

        for directory in {
            runtime_dir,
            output_dir,
            database_path.parent,
            credentials_path.parent,
            salt_file.parent,
            log_file.parent,
        }:
            ensure_private_directory(directory)

        defaults = raw.get("defaults", {})
        scheduler = raw.get("scheduler", {})
        default_locale = str(defaults.get("locale", "zh-CN"))
        default_timezone = str(scheduler.get("timezone", "Asia/Shanghai"))

        providers: dict[str, ProviderConfig] = {}
        for name, provider_raw in raw.get("providers", {}).items():
            if provider_definition(name) is None:
                LOGGER.info("unknown provider configured: %s", name)
            extra_headers = dict(provider_raw.get("extra_headers", {}))
            provider_locale = str(
                provider_raw.get("locale")
                or _locale_from_accept_language(extra_headers.get("Accept-Language"))
                or default_locale
            )
            # Missing settings enable fixed CPU/memory for Air China only.
            # An explicit null environment opts out entirely; a null fingerprint
            # keeps display/network settings without injecting a fingerprint.
            environment_raw = provider_raw.get("browser_environment", {} if name == "airchina" else None)
            environment_raw = expand_environment_preset(environment_raw)
            if isinstance(environment_raw, dict):
                if name == "airchina" and "fingerprint" not in environment_raw:
                    environment_raw = {**environment_raw, "fingerprint": default_fingerprint()}
                environment_raw = {"locale": provider_locale,
                                   "timezone_id": str(provider_raw.get("timezone", default_timezone)),
                                   **environment_raw}
            environment = parse_browser_environment(environment_raw)
            if environment is not None and name != "airchina":
                raise ValueError("当前浏览器环境配置仅支持国航实验。")
            # The browser generates locale/UA headers consistently with context
            # emulation; hand-written versions can contradict those settings.
            if environment is not None:
                reserved = {"user-agent", "accept-language", "sec-ch-ua",
                            "sec-ch-ua-mobile", "sec-ch-ua-platform"}
                extra_headers = {k: v for k, v in extra_headers.items() if k.lower() not in reserved}
            providers[name] = ProviderConfig(
                name=name,
                enabled=bool(provider_raw.get("enabled", True)),
                login_url=str(provider_raw.get("login_url") or ""),
                search_url_template=str(provider_raw.get("search_url_template") or ""),
                storage_state_file=_expand_path(
                    provider_raw.get("storage_state_file", f"./runtime/{name}_state.json"),
                    self.config_path.parent,
                ),
                selectors=dict(provider_raw.get("selectors", {})),
                timeout_ms=int(provider_raw.get("timeout_ms", 45000)),
                extra_headers=extra_headers,
                min_delay_seconds=float(provider_raw.get("min_delay_seconds", defaults.get("min_delay_seconds", 2.0))),
                max_delay_seconds=float(provider_raw.get("max_delay_seconds", defaults.get("max_delay_seconds", 5.0))),
                locale=provider_locale,
                timezone=str(provider_raw.get("timezone", default_timezone)),
                browser_backend=str(provider_raw.get("browser_backend", "")).strip().lower() or None,
                browser_backend_fallbacks=_parse_fallbacks(provider_raw.get("browser_backend_fallbacks")),
                browser_environment=environment,
            )

        for provider in providers.values():
            ensure_private_directory(provider.storage_state_file.parent)
            if provider.storage_state_file.exists():
                make_file_private(provider.storage_state_file)

        routes = [
            self._build_route(item)
            for item in raw.get("routes", [])
        ]
        self._validate_route_providers(routes, providers)

        # Early validation of all backend names
        default_browser_backend = str(defaults.get("browser_backend", "chrome")).strip().lower() or "chrome"
        default_browser_fallbacks = _parse_fallbacks(defaults.get("browser_backend_fallbacks"))
        if default_browser_backend == "chrome" and not default_browser_fallbacks:
            default_browser_fallbacks = ["playwright"]
        all_backend_names: list[str] = [default_browser_backend]
        all_backend_names.extend(default_browser_fallbacks)
        for p in providers.values():
            if p.browser_backend:
                all_backend_names.append(p.browser_backend)
            all_backend_names.extend(p.browser_backend_fallbacks or [])
        _validate_browser_backends(all_backend_names)

        self._config_cache = AppConfig(
            root_dir=self.config_path.parent,
            runtime_dir=runtime_dir,
            output_dir=output_dir,
            database_path=database_path,
            encrypted_credentials_path=credentials_path,
            salt_file=salt_file,
            log_file=log_file,
            headless=bool(defaults.get("headless", True)),
            automation_backend=str(defaults.get("automation_backend", "playwright")).strip().lower() or "playwright",
            browser_backend=default_browser_backend,
            browser_backend_fallbacks=default_browser_fallbacks,
            browser_channel=defaults.get("browser_channel"),
            scheduler_interval_minutes=int(scheduler.get("interval_minutes", 360)),
            timezone=default_timezone,
            default_currency=str(defaults.get("currency", "CNY")),
            providers=providers,
            routes=routes,
        )
        return self._config_cache

    def _read_salt(self, app_config: AppConfig) -> bytes:
        lock_path = app_config.salt_file.with_name(f".{app_config.salt_file.name}.lock")
        with interprocess_lock(lock_path):
            if app_config.salt_file.exists():
                make_file_private(app_config.salt_file)
                return app_config.salt_file.read_bytes()
            salt = os.urandom(16)
            _atomic_write_bytes(app_config.salt_file, salt, mode=0o600)
            return salt

    def _require_master_passphrase(self) -> str:
        passphrase = os.getenv("FLIGHT_TRACKER_MASTER_KEY")
        if not passphrase:
            raise RuntimeError(
                "Missing FLIGHT_TRACKER_MASTER_KEY in environment or .env. "
                "Set a strong local passphrase before reading or writing encrypted credentials."
            )
        return passphrase

    def _build_fernet(self, app_config: AppConfig) -> Fernet:
        passphrase = self._require_master_passphrase()
        salt = self._read_salt(app_config)
        return Fernet(_derive_key(passphrase, salt))

    def save_credentials(self, provider: str, username: str, password: str) -> Path:
        with self.lock:
            app_config = self.load()
            fernet = self._build_fernet(app_config)
            lock_path = app_config.encrypted_credentials_path.with_name(
                f".{app_config.encrypted_credentials_path.name}.lock"
            )
            with interprocess_lock(lock_path):
                current = self.load_credentials(allow_missing=True)
                current[provider] = {"username": username, "password": password}
                encrypted = fernet.encrypt(json.dumps(current, ensure_ascii=False).encode("utf-8"))
                _atomic_write_bytes(app_config.encrypted_credentials_path, encrypted, mode=0o600)
            return app_config.encrypted_credentials_path

    def load_credentials(self, allow_missing: bool = False) -> dict[str, dict[str, str]]:
        with self.lock:
            app_config = self.load()
            path = app_config.encrypted_credentials_path
            if not path.exists():
                if allow_missing:
                    return self._load_env_credentials()
                raise FileNotFoundError(
                    f"Encrypted credentials file not found: {path}. "
                    "Use the CLI command to save local credentials first."
                )

            fernet = self._build_fernet(app_config)
            make_file_private(path)
            decrypted = fernet.decrypt(path.read_bytes())
            payload = json.loads(decrypted.decode("utf-8"))
            merged = self._load_env_credentials()
            merged.update(payload)
            return merged

    def load_environment_credentials(self) -> dict[str, dict[str, str]]:
        return self._load_env_credentials()

    def _load_env_credentials(self) -> dict[str, dict[str, str]]:
        credentials: dict[str, dict[str, str]] = {}
        pairs = {
            "ctrip": ("CTRIP_USERNAME", "CTRIP_PASSWORD"),
            "airchina": ("AIRCHINA_USERNAME", "AIRCHINA_PASSWORD"),
            "priceline": ("PRICELINE_USERNAME", "PRICELINE_PASSWORD"),
        }
        for provider, (user_key, password_key) in pairs.items():
            username = os.getenv(user_key)
            password = os.getenv(password_key)
            if username and password:
                credentials[provider] = {"username": username, "password": password}
        return credentials

    def export_example_config(self, destination: Path) -> Path:
        example_path = ROOT / "config.example.json"
        raw = _shift_expired_example_routes(_read_json(example_path))
        _atomic_write_bytes(
            destination,
            json.dumps(raw, ensure_ascii=False, indent=2).encode("utf-8"),
            mode=0o600,
        )
        return destination

    def init_config(self, *, overwrite: bool = False) -> Path:
        with self.lock:
            with interprocess_lock(self._config_lock_path):
                if self.config_path.exists() and not overwrite:
                    raise FileExistsError(
                        f"Config file already exists: {self.config_path}. "
                        "Pass --overwrite only if you intentionally want to replace it."
                    )
                self.export_example_config(self.config_path)
                _raw, self._loaded_fingerprint = _read_json_with_fingerprint(self.config_path)
                self._config_cache = None
                return self.config_path

    def save_routes(self, routes: list[RouteQuery]) -> Path:
        with self.lock:
            with interprocess_lock(self._config_lock_path):
                if not self.config_path.exists():
                    raise FileNotFoundError(f"Missing config file: {self.config_path}.")
                raw, current_fingerprint = _read_json_with_fingerprint(self.config_path)
                if (
                    self._loaded_fingerprint is not None
                    and current_fingerprint != self._loaded_fingerprint
                ):
                    self._config_cache = None
                    raise ConfigConflictError(
                        "Config changed in another process; reload and retry the route update."
                    )
                raw["routes"] = [route.to_dict() for route in routes]
                encoded = json.dumps(raw, ensure_ascii=False, indent=2).encode("utf-8")
                _atomic_write_bytes(self.config_path, encoded, mode=0o600)
                self._loaded_fingerprint = hashlib.sha256(encoded).hexdigest()
                self._config_cache = None
                return self.config_path

    @staticmethod
    def _validate_route_providers(routes: list[RouteQuery], providers: dict[str, ProviderConfig]) -> None:
        configured = set(providers)
        for route in routes:
            for provider in route.providers or []:
                if provider not in configured:
                    raise ValueError(
                        f"Route {route.route_key} references unknown provider {provider!r}. "
                        f"Configured providers: {sorted(configured)}"
                    )

    def _build_route(self, item: dict[str, Any]) -> RouteQuery:
        route_type = str(item.get("route_type", "one_way"))
        segments_raw = item.get("segments") or []
        segments = [
            RouteSegment(
                origin=normalize_airport_code(segment["origin"]),
                destination=normalize_airport_code(segment["destination"]),
                departure_date=str(segment["departure_date"]),
            )
            for segment in segments_raw
        ] or None

        if route_type == "multi_city" and segments:
            origin = segments[0].origin
            destination = segments[-1].destination
            departure_date = segments[0].departure_date
            return_date = segments[-1].departure_date if len(segments) > 1 else None
        else:
            origin = normalize_airport_code(item["origin"])
            destination = normalize_airport_code(item["destination"])
            departure_date = str(item["departure_date"])
            return_date = item.get("return_date")

        return RouteQuery(
            origin=origin,
            destination=destination,
            departure_date=departure_date,
            return_date=return_date,
            route_type=route_type,
            cabin=str(item.get("cabin", "economy")),
            transfer_policy=normalize_transfer_policy(item.get("transfer_policy", "any")),
            passengers=int(item.get("passengers", 1)),
            providers=[str(name) for name in (item.get("providers") or [])] or None,
            preferred_airlines=[str(name).upper() for name in (item.get("preferred_airlines") or [])] or None,
            segments=segments,
            auto_query_enabled=bool(item.get("auto_query_enabled", False)),
            auto_query_interval_hours=max(int(item.get("auto_query_interval_hours", 12) or 12), 12),
            auto_query_next_run_at=str(item.get("auto_query_next_run_at") or "").strip() or None,
            group_id=str(item.get("group_id") or "").strip() or None,
            group_label=str(item.get("group_label") or "").strip() or None,
        )
