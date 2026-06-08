import base64
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from dotenv import load_dotenv

from airport_aliases import CLEAN_AIRPORT_ALIAS_CODE_MAP
from providers.registry import provider_definition


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = ROOT / "config.json"
LOGGER = logging.getLogger(__name__)
DEFAULT_ENV_PATH = ROOT / ".env"
TRANSFER_POLICY_ALIASES = {
    "any": "any",
    "direct": "direct_only",
    "direct_only": "direct_only",
    "transfer": "transfer_only",
    "transfer_only": "transfer_only",
}
AIRPORT_ALIAS_CODE_MAP = {
    "北京": "BJS",
    "首都": "PEK",
    "首都机场": "PEK",
    "大兴": "PKX",
    "大兴机场": "PKX",
    "上海": "SHA",
    "虹桥": "SHA",
    "虹桥机场": "SHA",
    "浦东": "PVG",
    "浦东机场": "PVG",
    "广州": "CAN",
    "深圳": "SZX",
    "成都": "CTU",
    "成都双机场": "CTU",
    "成都两个机场": "CTU",
    "成都全部机场": "CTU",
    "双流": "CTU",
    "天府": "TFU",
    "青岛": "TAO",
    "流亭": "TAO",
    "胶东": "TAO",
    "西安": "XIY",
    "重庆": "CKG",
    "杭州": "HGH",
    "南京": "NKG",
    "武汉": "WUH",
    "天津": "TSN",
    "厦门": "XMN",
    "昆明": "KMG",
    "香港": "HKG",
    "澳门": "MFM",
    "台北": "TPE",
    "高雄": "KHH",
    "新加坡": "SIN",
    "樟宜": "SIN",
    "曼谷": "BKK",
    "吉隆坡": "KUL",
    "东京": "TYO",
    "成田": "NRT",
    "羽田": "HND",
    "大阪": "OSA",
    "关西": "KIX",
    "名古屋": "NGO",
    "福冈": "FUK",
    "札幌": "SPK",
    "冲绳": "OKA",
    "首尔": "SEL",
    "仁川": "ICN",
    "金浦": "GMP",
    "釜山": "PUS",
    "济州": "CJU",
    "河内": "HAN",
    "胡志明": "SGN",
    "胡志明市": "SGN",
    "岘港": "DAD",
    "普吉": "HKT",
    "清迈": "CNX",
    "雅加达": "JKT",
    "巴厘岛": "DPS",
    "登巴萨": "DPS",
    "马尼拉": "MNL",
    "宿务": "CEB",
    "金边": "PNH",
    "暹粒": "REP",
    "万象": "VTE",
    "仰光": "RGN",
    "加德满都": "KTM",
    "新德里": "DEL",
    "孟买": "BOM",
    "班加罗尔": "BLR",
    "科伦坡": "CMB",
    "马累": "MLE",
    "伦敦": "LHR",
    "巴黎": "PAR",
    "戴高乐": "CDG",
    "雅典": "ATH",
    "米兰": "MIL",
    "罗马": "ROM",
    "马德里": "MAD",
    "巴塞罗那": "BCN",
    "法兰克福": "FRA",
    "慕尼黑": "MUC",
    "柏林": "BER",
    "汉堡": "HAM",
    "苏黎世": "ZRH",
    "日内瓦": "GVA",
    "维也纳": "VIE",
    "布拉格": "PRG",
    "布达佩斯": "BUD",
    "华沙": "WAW",
    "哥本哈根": "CPH",
    "斯德哥尔摩": "STO",
    "奥斯陆": "OSL",
    "赫尔辛基": "HEL",
    "布鲁塞尔": "BRU",
    "都柏林": "DUB",
    "里斯本": "LIS",
    "波尔图": "OPO",
    "威尼斯": "VCE",
    "佛罗伦萨": "FLR",
    "莫斯科": "MOW",
    "伊尔库茨克": "IKT",
    "海参崴": "VVO",
    "符拉迪沃斯托克": "VVO",
    "阿姆斯特丹": "AMS",
    "纽约": "JFK",
    "芝加哥": "CHI",
    "波士顿": "BOS",
    "华盛顿": "WAS",
    "西雅图": "SEA",
    "洛杉矶": "LAX",
    "旧金山": "SFO",
    "拉斯维加斯": "LAS",
    "迈阿密": "MIA",
    "奥兰多": "ORL",
    "休斯顿": "HOU",
    "达拉斯": "DFW",
    "温哥华": "YVR",
    "多伦多": "YTO",
    "蒙特利尔": "YMQ",
    "卡尔加里": "YYC",
    "墨西哥城": "MEX",
    "坎昆": "CUN",
    "圣保罗": "SAO",
    "里约": "GIG",
    "里约热内卢": "GIG",
    "布宜诺斯艾利斯": "BUE",
    "利马": "LIM",
    "圣地亚哥": "SCL",
    "悉尼": "SYD",
    "墨尔本": "MEL",
    "布里斯班": "BNE",
    "珀斯": "PER",
    "奥克兰": "AKL",
    "迪拜": "DXB",
    "阿布扎比": "AUH",
    "多哈": "DOH",
    "伊斯坦布尔": "IST",
    "利雅得": "RUH",
    "吉达": "JED",
    "特拉维夫": "TLV",
    "开罗": "CAI",
    "约翰内斯堡": "JNB",
    "开普敦": "CPT",
    "内罗毕": "NBO",
    "卡萨布兰卡": "CMN",
    "beijing": "BJS",
    "shanghai": "SHA",
    "guangzhou": "CAN",
    "shenzhen": "SZX",
    "chengdu": "CTU",
    "qingdao": "TAO",
    "hong kong": "HKG",
    "hongkong": "HKG",
    "macau": "MFM",
    "macao": "MFM",
    "taipei": "TPE",
    "singapore": "SIN",
    "bangkok": "BKK",
    "kuala lumpur": "KUL",
    "tokyo": "TYO",
    "osaka": "OSA",
    "nagoya": "NGO",
    "fukuoka": "FUK",
    "sapporo": "SPK",
    "okinawa": "OKA",
    "seoul": "SEL",
    "incheon": "ICN",
    "gimpo": "GMP",
    "busan": "PUS",
    "jeju": "CJU",
    "hanoi": "HAN",
    "ho chi minh": "SGN",
    "danang": "DAD",
    "da nang": "DAD",
    "phuket": "HKT",
    "chiang mai": "CNX",
    "jakarta": "JKT",
    "bali": "DPS",
    "manila": "MNL",
    "cebu": "CEB",
    "phnom penh": "PNH",
    "siem reap": "REP",
    "vientiane": "VTE",
    "yangon": "RGN",
    "kathmandu": "KTM",
    "delhi": "DEL",
    "mumbai": "BOM",
    "bangalore": "BLR",
    "colombo": "CMB",
    "male": "MLE",
    "london": "LHR",
    "paris": "PAR",
    "athens": "ATH",
    "milan": "MIL",
    "rome": "ROM",
    "madrid": "MAD",
    "barcelona": "BCN",
    "frankfurt": "FRA",
    "munich": "MUC",
    "berlin": "BER",
    "hamburg": "HAM",
    "zurich": "ZRH",
    "geneva": "GVA",
    "vienna": "VIE",
    "prague": "PRG",
    "budapest": "BUD",
    "warsaw": "WAW",
    "copenhagen": "CPH",
    "stockholm": "STO",
    "oslo": "OSL",
    "helsinki": "HEL",
    "brussels": "BRU",
    "dublin": "DUB",
    "lisbon": "LIS",
    "porto": "OPO",
    "venice": "VCE",
    "florence": "FLR",
    "moscow": "MOW",
    "irkutsk": "IKT",
    "vladivostok": "VVO",
    "amsterdam": "AMS",
    "new york": "JFK",
    "chicago": "CHI",
    "boston": "BOS",
    "washington": "WAS",
    "seattle": "SEA",
    "los angeles": "LAX",
    "san francisco": "SFO",
    "las vegas": "LAS",
    "miami": "MIA",
    "orlando": "ORL",
    "houston": "HOU",
    "dallas": "DFW",
    "vancouver": "YVR",
    "toronto": "YTO",
    "montreal": "YMQ",
    "calgary": "YYC",
    "mexico city": "MEX",
    "cancun": "CUN",
    "sao paulo": "SAO",
    "rio": "GIG",
    "rio de janeiro": "GIG",
    "buenos aires": "BUE",
    "lima": "LIM",
    "santiago": "SCL",
    "sydney": "SYD",
    "melbourne": "MEL",
    "brisbane": "BNE",
    "perth": "PER",
    "auckland": "AKL",
    "dubai": "DXB",
    "abu dhabi": "AUH",
    "doha": "DOH",
    "istanbul": "IST",
    "riyadh": "RUH",
    "jeddah": "JED",
    "tel aviv": "TLV",
    "cairo": "CAI",
    "johannesburg": "JNB",
    "cape town": "CPT",
    "nairobi": "NBO",
    "casablanca": "CMN",
}


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

    compact = text.replace("国际机场", "").replace("机场", "").strip()
    alias_candidates = [
        text,
        compact,
        text.lower(),
        compact.lower(),
        text.replace(" ", "").lower(),
        compact.replace(" ", "").lower(),
    ]
    for candidate in alias_candidates:
        code = AIRPORT_ALIAS_CODE_MAP.get(candidate)
        if code:
            return code
    return text.upper()


def normalize_airport_code(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) == 3 and text.isascii() and text.isalpha():
        return text.upper()

    compact = text
    for suffix in ["国际机场", "机场", "國際機場", "機場", "鍥介檯鏈哄満", "鏈哄満"]:
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
        code = AIRPORT_ALIAS_CODE_MAP.get(candidate)
        if code:
            return code
    return text.upper()


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
        self.config_path = config_path or DEFAULT_CONFIG_PATH
        self.env_path = env_path or (self.config_path.parent / ".env" if config_path else DEFAULT_ENV_PATH)
        load_dotenv(self.env_path, override=False)
        self._config_cache: AppConfig | None = None

    def load(self) -> AppConfig:
        if self._config_cache is not None:
            return self._config_cache

        if not self.config_path.exists():
            raise FileNotFoundError(
                f"Missing config file: {self.config_path}. "
                "Run `python main.py init-config` to create a local config.json from config.example.json."
            )

        raw = _read_json(self.config_path)
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

        runtime_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        credentials_path.parent.mkdir(parents=True, exist_ok=True)
        salt_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.parent.mkdir(parents=True, exist_ok=True)

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
            )

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
        if app_config.salt_file.exists():
            return app_config.salt_file.read_bytes()
        salt = os.urandom(16)
        app_config.salt_file.write_bytes(salt)
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
        app_config = self.load()
        fernet = self._build_fernet(app_config)
        current = self.load_credentials(allow_missing=True)
        current[provider] = {"username": username, "password": password}
        encrypted = fernet.encrypt(json.dumps(current, ensure_ascii=False).encode("utf-8"))
        app_config.encrypted_credentials_path.write_bytes(encrypted)
        return app_config.encrypted_credentials_path

    def load_credentials(self, allow_missing: bool = False) -> dict[str, dict[str, str]]:
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
        decrypted = fernet.decrypt(path.read_bytes())
        payload = json.loads(decrypted.decode("utf-8"))
        merged = self._load_env_credentials()
        merged.update(payload)
        return merged

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
        destination.write_text(example_path.read_text(encoding="utf-8"), encoding="utf-8")
        return destination

    def init_config(self, *, overwrite: bool = False) -> Path:
        if self.config_path.exists() and not overwrite:
            raise FileExistsError(
                f"Config file already exists: {self.config_path}. "
                "Pass --overwrite only if you intentionally want to replace it."
            )
        self.export_example_config(self.config_path)
        self._config_cache = None
        return self.config_path

    def save_routes(self, routes: list[RouteQuery]) -> Path:
        raw = _read_json(self.config_path)
        raw["routes"] = [route.to_dict() for route in routes]
        self.config_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
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
