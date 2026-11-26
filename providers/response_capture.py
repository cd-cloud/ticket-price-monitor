"""Provider-aware network response capture for browser automation."""

from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import unquote, urlsplit, urlunsplit

from playwright.async_api import Page


_PATH_SECRET_PATTERN = re.compile(
    r"(?:token|secret|session|auth|api[_-]?key|signature|jwt|password|passwd|sig)",
    re.IGNORECASE,
)


def _sanitize_url_path(path: str) -> str:
    segments = []
    for segment in str(path or "").split("/"):
        decoded = unquote(segment)
        if _PATH_SECRET_PATTERN.search(decoded) or (
            len(decoded) >= 16
            and re.fullmatch(r"[A-Za-z0-9._~+=-]+", decoded)
            and not decoded.isdigit()
        ):
            segments.append("[redacted]")
        else:
            segments.append(segment)
    return "/".join(segments)


def sanitize_url(value: object) -> str:
    try:
        parsed = urlsplit(str(value or ""))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return ""
        hostname = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        netloc = hostname + (f":{parsed.port}" if parsed.port else "")
        return urlunsplit((parsed.scheme, netloc, _sanitize_url_path(parsed.path), "", ""))
    except (TypeError, ValueError):
        return ""


_URL_FIELD_NAMES = {
    "url", "href", "link", "finalurl", "searchurl", "responseurl",
    "reviewurl", "jumpurl",
}
_SENSITIVE_FIELD_NAMES = {
    "apikey", "accesstoken", "auth", "authorization", "authtoken", "bearer",
    "clientsecret", "cookie", "credential", "credentials", "csrf", "jwt",
    "masterkey", "password", "passwd", "privatekey", "refreshtoken", "secret",
    "secretkey", "session", "sessionid", "sign", "signature", "token", "xsrf",
}
_SENSITIVE_FIELD_FRAGMENTS = (
    "apikey", "authorization", "authtoken", "clientsecret", "cookie", "credential",
    "masterkey", "password", "passwd", "privatekey", "secret", "signature", "token",
)
_CHINESE_SENSITIVE_FIELDS = {"密码", "口令", "令牌", "授权", "密钥", "会话", "凭据"}


def _normalized_field_name(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _is_sensitive_field_name(value: object) -> bool:
    text = str(value or "").strip()
    normalized = _normalized_field_name(text)
    return bool(
        text in _CHINESE_SENSITIVE_FIELDS
        or normalized in _SENSITIVE_FIELD_NAMES
        or any(fragment in normalized for fragment in _SENSITIVE_FIELD_FRAGMENTS)
    )


def sanitize_payload_urls(value, *, field_name: str | None = None):
    """Return a JSON-compatible copy with URLs and sensitive values redacted."""
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            safe_key = sanitize_sensitive_text(key) if isinstance(key, str) else key
            if safe_key in sanitized and safe_key != key:
                safe_key = f"[redacted_key_{len(sanitized)}]"
            if _is_sensitive_field_name(key):
                sanitized[safe_key] = "[redacted]"
            else:
                sanitized[safe_key] = sanitize_payload_urls(item, field_name=str(key))
        return sanitized
    if isinstance(value, list):
        return [sanitize_payload_urls(item, field_name=field_name) for item in value]
    if isinstance(value, tuple):
        return [sanitize_payload_urls(item, field_name=field_name) for item in value]
    if field_name and _normalized_field_name(field_name) in _URL_FIELD_NAMES:
        return sanitize_url(value)
    if isinstance(value, str):
        return sanitize_sensitive_text(value)
    return value


_URL_TEXT_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_SENSITIVE_TEXT_MARKER = (
    r"(?:api[_-]?key|access[_-]?token|auth[_-]?token|authorization|bearer|"
    r"client[_-]?secret|cookie|credential|csrf|jwt|master[_-]?key|password|"
    r"passwd|private[_-]?key|refresh[_-]?token|secret(?:[_-]?key)?|session|"
    r"sign(?:ature)?|token|ui[_-]?token|xsrf|密码|口令|令牌|授权|密钥|会话|凭据)"
)
_STRUCTURED_SECRET_PATTERN = re.compile(
    r'''(?i)(?P<prefix>["'][^"']{0,128}'''
    + _SENSITIVE_TEXT_MARKER
    + r'''[^"']{0,128}["']\s*:\s*["'])(?P<value>[^"']*)(?P<suffix>["'])'''
)
_AUTHORIZATION_PATTERN = re.compile(
    r"(?i)(\bauthorization\s*:\s*)(bearer\s+)?[^\s,;&]+"
)
_BEARER_PATTERN = re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]+")
_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)(?P<prefix>(?<![A-Za-z0-9])[^=\s:]{0,128}"
    + _SENSITIVE_TEXT_MARKER
    + r"[^=\s:]{0,128}\s*[:=]\s*)(?P<value>[^\s,;&\"']+)"
)


def _redact_assignment(match: re.Match[str]) -> str:
    if (
        match.group("value") == "[redacted]"
        or "authorization" in match.group("prefix").lower()
    ):
        return match.group(0)
    return f"{match.group('prefix')}[redacted]"


def sanitize_sensitive_text(value: object) -> str:
    """Redact URLs and common credential assignments before logs or persistence."""
    text = str(value or "")
    text = _URL_TEXT_PATTERN.sub("[url]", text)
    text = _STRUCTURED_SECRET_PATTERN.sub(
        lambda match: f"{match.group('prefix')}[redacted]{match.group('suffix')}", text
    )
    text = _AUTHORIZATION_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2) or ''}[redacted]", text
    )
    text = _BEARER_PATTERN.sub(lambda match: f"{match.group(1)}[redacted]", text)
    return _SECRET_ASSIGNMENT_PATTERN.sub(_redact_assignment, text)


class ResponseStore(list):
    """A search generation fences requests AND async response-body decoding."""

    def __init__(self):
        super().__init__()
        self.generation = 0
        self.requests = {}
        self.decode_tasks = {}
        self.loading_evidence = None

    def clear(self):
        self.generation += 1
        self.requests.clear()
        self.loading_evidence = None
        super().clear()

    @property
    def inflight(self):
        requests = sum(
            generation == self.generation
            and getattr(request, "resource_type", "") in {"xhr", "fetch"}
            and any(word in getattr(request, "url", "").lower() for word in ("flight", "search", "query", "itinerary"))
            for request, generation in self.requests.items()
        )
        decodes = sum(generation == self.generation and not task.done()
                      for task, generation in self.decode_tasks.items())
        return requests + decodes


def begin_response_batch(store):
    store.clear()


async def attach_response_listeners(page: Page, response_store: list[dict]) -> None:
    scoped = isinstance(response_store, ResponseStore)

    def on_request(request):
        if scoped:
            response_store.requests[request] = response_store.generation

    async def handle_response(response, generation) -> None:
        if scoped and generation != response_store.generation:
            return
        url = response.url
        content_type = response.headers.get("content-type", "")
        lowered = url.lower()
        is_json_like = "json" in content_type.lower() or "/json/" in lowered
        is_flight_like = any(keyword in lowered for keyword in ["flight", "search", "query", "list", "price", "fare"])
        is_ctrip_api = "ctrip.com" in lowered and ("restapi" in lowered or "soa2" in lowered)
        is_feizhu_api = any(host in lowered for host in ["fliggy.com", "taobao.com", "alicdn.com"]) and (
            "mtop" in lowered or is_flight_like or "jipiao" in lowered
        )
        if not is_json_like and not is_ctrip_api and not is_feizhu_api:
            return
        if not is_flight_like and not is_ctrip_api and not is_feizhu_api:
            return
        try:
            payload = await response.json()
        except Exception:
            try:
                from providers import feizhu_parser
                text = await response.text()
                if (
                    "flightItineraryList" not in text
                    and "flightSegments" not in text
                    and not feizhu_response_text_looks_useful(text)
                ):
                    return
                payload = feizhu_parser.parse_json_or_jsonp(text) or json.loads(text)
            except Exception:
                return
        if scoped and generation != response_store.generation:
            return
        response_store.append(
            {
                "url": url,
                "status": response.status,
                "payload": payload,
                "query_generation": generation,
            }
        )

    def on_response(response):
        generation = response_store.requests.pop(response.request, None) if scoped else None
        if scoped and generation != response_store.generation:
            return
        task = asyncio.create_task(handle_response(response, generation))
        if scoped:
            response_store.decode_tasks[task] = generation
            task.add_done_callback(lambda done: response_store.decode_tasks.pop(done, None))
        return task

    page.on("request", on_request)
    page.on("requestfailed", lambda request: response_store.requests.pop(request, None) if scoped else None)
    page.on("response", on_response)


def feizhu_response_text_looks_useful(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(
        marker in lowered
        for marker in [
            "price",
            "fare",
            "flight",
            "adultprice",
            "ticket",
            "航班",
            "票价",
            "机票",
        ]
    )
