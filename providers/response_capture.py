"""Provider-aware network response capture for browser automation."""

from __future__ import annotations

import asyncio
import json

from playwright.async_api import Page

from providers import feizhu_parser


async def attach_response_listeners(page: Page, response_store: list[dict]) -> None:
    async def handle_response(response) -> None:
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
        response_store.append(
            {
                "url": url,
                "status": response.status,
                "payload": payload,
            }
        )

    page.on("response", lambda response: asyncio.create_task(handle_response(response)))


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
