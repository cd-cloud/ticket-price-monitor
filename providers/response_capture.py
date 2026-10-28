"""Provider-aware network response capture for browser automation."""

from __future__ import annotations

import asyncio
import json

from playwright.async_api import Page


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
