"""Explicit browser emulation and proxy configuration; no automatic rotation."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import os
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class BrowserEnvironmentError(ValueError):
    def __init__(self):
        # Never echo a supplied proxy URL, password, user agent or raw config.
        super().__init__("浏览器环境配置无效或代理环境变量缺失；请检查本地配置。")


@dataclass(frozen=True)
class BrowserEnvironment:
    options: dict = field(repr=False)
    key: str

    def context_options(self):
        return deepcopy(self.options)


def environment_key(provider):
    environment = getattr(provider, "browser_environment", None)
    return environment.key if environment is not None else ""


def environment_summary(provider):
    environment = getattr(provider, "browser_environment", None)
    options = environment.context_options() if environment else {}
    return {
        "configured": environment is not None,
        "network": "proxy" if options.get("proxy") else "system",
        "locale": options.get("locale", getattr(provider, "locale", None)),
        "timezone_id": options.get("timezone_id", getattr(provider, "timezone", None)),
        "viewport": options.get("viewport"),
        "screen": options.get("screen"),
        "device_scale_factor": options.get("device_scale_factor"),
        "color_scheme": options.get("color_scheme"),
        "custom_user_agent": bool(options.get("user_agent")),
        "automatic_rotation": False,
    }


def parse_browser_environment(raw):
    """Resolve only explicit options. Absence preserves the legacy environment."""
    if raw is None:
        return None
    try:
        return _parse(raw)
    except (ValueError, TypeError, KeyError, ZoneInfoNotFoundError, OverflowError):
        raise BrowserEnvironmentError() from None


def _parse(raw):
    allowed = {"viewport", "screen", "device_scale_factor", "user_agent",
               "locale", "timezone_id", "color_scheme", "network"}
    if not isinstance(raw, dict) or set(raw) - allowed:
        raise ValueError()
    options = {}
    for name in ("viewport", "screen"):
        if name not in raw:
            continue
        size = raw[name]
        if not isinstance(size, dict) or set(size) != {"width", "height"}:
            raise ValueError()
        if any(type(v) is not int or not 320 <= v <= 7680 for v in size.values()):
            raise ValueError()
        options[name] = dict(size)
    if "screen" in options:
        if "viewport" not in options or any(
            options["screen"][k] < options["viewport"][k] for k in ("width", "height")
        ):
            raise ValueError()
    if "device_scale_factor" in raw:
        scale = raw["device_scale_factor"]
        if type(scale) not in (float, int) or not 0.5 <= scale <= 4:
            raise ValueError()
        options["device_scale_factor"] = scale
    for name in ("locale", "timezone_id", "user_agent"):
        if name not in raw:
            continue
        value = raw[name]
        if not isinstance(value, str) or not 1 <= len(value) <= 512 or any(
            ord(c) < 32 or ord(c) == 127 for c in value
        ):
            raise ValueError()
        if name == "locale" and not re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{2,8})*", value):
            raise ValueError()
        if name == "timezone_id":
            ZoneInfo(value)
        options[name] = value
    if "color_scheme" in raw:
        if raw["color_scheme"] not in ("light", "dark", "no-preference"):
            raise ValueError()
        options["color_scheme"] = raw["color_scheme"]
    network = raw.get("network", {"mode": "system"})
    if not isinstance(network, dict):
        raise ValueError()
    if network.get("mode") == "system":
        if set(network) != {"mode"}:
            raise ValueError()
    elif network.get("mode") == "proxy":
        if set(network) - {"mode", "server_env", "username_env", "password_env"}:
            raise ValueError()
        def secret(name):
            ref = network[name]
            if not isinstance(ref, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", ref):
                raise ValueError()
            value = os.environ.get(ref)
            if not value or len(value) > 2048 or any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise ValueError()
            return value
        server = secret("server_env")
        parsed = urlsplit(server)
        if (parsed.scheme not in {"http", "https", "socks5"} or not parsed.hostname
                or parsed.port is None or not 1 <= parsed.port <= 65535
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or any(c.isspace() for c in server)):
            raise ValueError()
        proxy = {"server": server.rstrip("/")}
        if ("username_env" in network) != ("password_env" in network):
            raise ValueError()
        if "username_env" in network:
            # Chromium's SOCKS5 authentication is not supported by this path.
            if parsed.scheme == "socks5":
                raise ValueError()
            proxy.update(username=secret("username_env"), password=secret("password_env"))
        options["proxy"] = proxy
    else:
        raise ValueError()
    # Includes resolved credentials to invalidate retained sessions when changed.
    # Only this opaque digest is used in local cache/profile namespace keys.
    key = hashlib.sha256(json.dumps(options, sort_keys=True).encode()).hexdigest()[:24]
    return BrowserEnvironment(options, key)


def launch_options(kwargs):
    """Explicit allowlist; never forward arbitrary launch flags or init scripts."""
    options = kwargs.get("browser_environment_options") or {}
    allowed = {"viewport", "screen", "device_scale_factor", "user_agent",
               "color_scheme", "proxy"}
    if not isinstance(options, dict) or set(options) - allowed:
        raise BrowserEnvironmentError()
    return deepcopy(options)
