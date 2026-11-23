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
            len(decoded) >= 32
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
    "apikey", "accesstoken", "auth", "authorization", "authtoken", "clientsecret",
    "cookie", "credential", "credentials", "masterkey", "password", "passwd",
    "privatekey", "refreshtoken", "secret", "sessionid", "sign", "signature", "token",
}


def _normalized_field_name(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def sanitize_payload_urls(value, *, field_name: str | None = None):
    """Return a JSON-compatible copy with URLs and sensitive values redacted."""
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            normalized = _normalized_field_name(key)
            if normalized in _SENSITIVE_FIELD_NAMES:
                sanitized[key] = "[redacted]"
            else:
                sanitized[key] = sanitize_payload_urls(item, field_name=str(key))
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
_STRUCTURED_SECRET_PATTERN = re.compile(
    r'''(?i)(?P<prefix>["'](?:api[_-]?key|access[_-]?token|auth[_-]?token|'''
    r'''client[_-]?secret|cookie|password|passwd|refresh[_-]?token|session[_-]?id|'''
    r'''sign(?:ature)?|token|secret|authorization)["']\s*:\s*["'])'''
    r'''(?P<value>[^"']*)(?P<suffix>["'])'''
)
_AUTHORIZATION_PATTERN = re.compile(
    r"(?i)(\bauthorization\s*:\s*)(bearer\s+)?[^\s,;&]+"
)
_BEARER_PATTERN = re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]+")
_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)(?P<prefix>\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|"
    r"client[_-]?secret|cookie|password|passwd|refresh[_-]?token|session[_-]?id|"
    r"sign(?:ature)?|token|secret)\s*[:=]\s*)(?P<value>[^\s,;&]+)"
)


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
    return _SECRET_ASSIGNMENT_PATTERN.sub(
        lambda match: f"{match.group('prefix')}[redacted]", text
    )


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
