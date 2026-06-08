"""FlyAI CLI adapter for Feizhu/Fliggy flight queries.

Replaces browser automation with direct CLI calls to @fly-ai/flyai-cli,
which uses Fliggy's MCP API under the hood. No login or browser required.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
from typing import Any

logger = logging.getLogger(__name__)


def _find_flyai_executable() -> list[str]:
    """Return the command list to invoke flyai-cli.

    We prefer the bare binary on Unix and npx on Windows, because
    Windows .cmd wrappers don't play well with asyncio subprocess.
    """
    # Prefer direct binary on Unix-like systems
    if sys.platform != "win32":
        bin_path = shutil.which("flyai")
        if bin_path:
            return [bin_path]

    # On Windows (or if bare binary missing), use npx — it works everywhere.
    # We intentionally use the bare command name (not the full path) because
    # Windows asyncio subprocess in shell mode needs PATH resolution.
    if shutil.which("npx"):
        return ["npx", "--no-install", "flyai"]

    return []

# City/airport code → Chinese city name mapping for CLI origin/destination args.
# flyai-cli accepts Chinese city names (e.g. "成都", "新加坡") rather than IATA codes.
_CITY_CODE_TO_NAME: dict[str, str] = {
    # Domestic
    "BJS": "北京",
    "PEK": "北京",
    "PKX": "北京",
    "SHA": "上海",
    "PVG": "上海",
    "CAN": "广州",
    "SZX": "深圳",
    "CTU": "成都",
    "TFU": "成都",
    "HGH": "杭州",
    "XIY": "西安",
    "CKG": "重庆",
    "WUH": "武汉",
    "NKG": "南京",
    "TAO": "青岛",
    "XMN": "厦门",
    "CSX": "长沙",
    "KMG": "昆明",
    "DLC": "大连",
    "TSN": "天津",
    "CGO": "郑州",
    "SHE": "沈阳",
    "HRB": "哈尔滨",
    "HAK": "海口",
    "SYX": "三亚",
    "KWE": "贵阳",
    "LHW": "兰州",
    "URC": "乌鲁木齐",
    "LXA": "拉萨",
    "NNG": "南宁",
    "TNA": "济南",
    "FOC": "福州",
    "HFE": "合肥",
    "NGB": "宁波",
    "WNZ": "温州",
    "KHN": "南昌",
    "TYN": "太原",
    "CGQ": "长春",
    "NTG": "南通",
    "YNT": "烟台",
    "WUX": "无锡",
    "SWA": "汕头",
    "LYG": "连云港",
    "ZUH": "珠海",
    "JJN": "泉州",
    "WXN": "万州",
    # International
    "HKG": "香港",
    "TPE": "台北",
    "MFM": "澳门",
    "NRT": "东京",
    "HND": "东京",
    "KIX": "大阪",
    "FUK": "福冈",
    "CTS": "札幌",
    "ICN": "首尔",
    "SEL": "首尔",
    "PUS": "釜山",
    "BKK": "曼谷",
    "CNX": "清迈",
    "SIN": "新加坡",
    "KUL": "吉隆坡",
    "DPS": "巴厘岛",
    "CGK": "雅加达",
    "MNL": "马尼拉",
    "SGN": "胡志明市",
    "HAN": "河内",
    "RGN": "仰光",
    "PNH": "金边",
    "REP": "暹粒",
    "LON": "伦敦",
    "LHR": "伦敦",
    "LGW": "伦敦",
    "PAR": "巴黎",
    "CDG": "巴黎",
    "ORY": "巴黎",
    "FRA": "法兰克福",
    "MUC": "慕尼黑",
    "AMS": "阿姆斯特丹",
    "FCO": "罗马",
    "MAD": "马德里",
    "BCN": "巴塞罗那",
    "ZUR": "苏黎世",
    "VIE": "维也纳",
    "PRG": "布拉格",
    "BUD": "布达佩斯",
    "WAW": "华沙",
    "IST": "伊斯坦布尔",
    "DUB": "都柏林",
    "EDI": "爱丁堡",
    "MAN": "曼彻斯特",
    "JFK": "纽约",
    "NYC": "纽约",
    "LAX": "洛杉矶",
    "SFO": "旧金山",
    "SEA": "西雅图",
    "ORD": "芝加哥",
    "BOS": "波士顿",
    "MIA": "迈阿密",
    "LAS": "拉斯维加斯",
    "HNL": "夏威夷",
    "YVR": "温哥华",
    "YYZ": "多伦多",
    "YUL": "蒙特利尔",
    "SYD": "悉尼",
    "MEL": "墨尔本",
    "BNE": "布里斯班",
    "PER": "珀斯",
    "AKL": "奥克兰",
    "DXB": "迪拜",
    "DOH": "多哈",
    "AUH": "阿布扎比",
    "CAI": "开罗",
    "CPT": "开普敦",
    "JNB": "约翰内斯堡",
    "MEX": "墨西哥城",
    "GRU": "圣保罗",
    "EZE": "布宜诺斯艾利斯",
    "SCL": "圣地亚哥",
    "BOG": "波哥大",
    "MLE": "马尔代夫",
    "CMB": "科伦坡",
    "DEL": "德里",
    "BOM": "孟买",
    "MAA": "金奈",
    "BLR": "班加罗尔",
    "KTM": "加德满都",
    "DAC": "达卡",
    "ISB": "伊斯兰堡",
}


# Mapping from our cabin class names to flyai-cli --seat-class-name values.
_CABIN_MAP: dict[str, str] = {
    "economy": "economy",
    "economy_plus": "economy",
    "premium_economy": "economy",
    "business": "business",
    "first": "first",
    "any": "economy",
}


# Sort type preference: 3 = price ascending (cheapest first).
_DEFAULT_SORT_TYPE = "3"


def _resolve_city_name(code: str) -> str:
    """Convert IATA city/airport code to Chinese city name for flyai-cli."""
    return _CITY_CODE_TO_NAME.get(code.upper(), code)


def _build_cli_args(
    *,
    origin: str,
    destination: str,
    departure_date: str,
    cabin: str = "economy",
    journey_type: str | None = None,
) -> list[str]:
    """Build flyai search-flight CLI arguments (without the executable name)."""
    args = [
        "search-flight",
        "--origin", _resolve_city_name(origin),
        "--destination", _resolve_city_name(destination),
        "--dep-date", departure_date,
        "--sort-type", _DEFAULT_SORT_TYPE,
    ]
    seat_class = _CABIN_MAP.get(cabin.lower(), "economy")
    if seat_class != "economy":
        args.extend(["--seat-class-name", seat_class])
    if journey_type is not None:
        args.extend(["--journey-type", journey_type])
    return args


def _parse_flyai_response(raw: str) -> dict[str, Any]:
    """Parse raw stdout from flyai-cli into a Python dict."""
    # flyai outputs single-line JSON to stdout; anything else (errors, hints) goes to stderr.
    # However, sometimes stderr may be mixed in; try to find the first JSON object.
    text = raw.strip()
    if not text:
        raise RuntimeError("flyai-cli returned empty output.")

    # Try direct parse first.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Fallback: look for the first '{' ... '}' block.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"flyai-cli output is not valid JSON: {exc}") from exc

    raise RuntimeError(f"flyai-cli output is not valid JSON: {text[:500]}")


def _segment_to_flight_detail(seg: dict[str, Any], *, segment_index: int = 1) -> dict[str, Any]:
    """Convert a flyai-cli segment dict into our canonical flight_detail format."""
    dep_dt = str(seg.get("depDateTime") or "")
    arr_dt = str(seg.get("arrDateTime") or "")
    dep_time = dep_dt.split(" ")[1] if " " in dep_dt else dep_dt
    arr_time = arr_dt.split(" ")[1] if " " in arr_dt else arr_dt
    dep_date = dep_dt.split(" ")[0] if " " in dep_dt else None

    duration_raw = seg.get("duration")
    try:
        duration_minutes = int(duration_raw) if duration_raw is not None else None
    except (ValueError, TypeError):
        duration_minutes = None

    return {
        "segment_index": segment_index,
        "airline": seg.get("marketingTransportName"),
        "flight_no": seg.get("marketingTransportNo"),
        "cabin": seg.get("seatClassName"),
        "seat_class": seg.get("seatClassName"),
        "departure_time": dep_time or None,
        "departure_airport": seg.get("depStationCode"),
        "departure_terminal": seg.get("depTerm") or None,
        "arrival_time": arr_time or None,
        "arrival_airport": seg.get("arrStationCode"),
        "arrival_terminal": seg.get("arrTerm") or None,
        "origin": seg.get("depCityCode"),
        "destination": seg.get("arrCityCode"),
        "departure_date": dep_date,
        "duration_minutes": duration_minutes,
        "aircraft": None,
        "stops": None,
    }


def _item_to_offer(item: dict[str, Any]) -> dict[str, Any]:
    """Convert a single flyai-cli item into our internal offer representation."""
    journeys = item.get("journeys") or []
    if not journeys:
        raise ValueError("flyai item has no journeys")

    journey = journeys[0]
    segments = journey.get("segments") or []
    if not segments:
        raise ValueError("flyai journey has no segments")

    # Price
    price_raw = item.get("ticketPrice")
    try:
        price = float(price_raw) if price_raw is not None else 0.0
    except (ValueError, TypeError):
        price = 0.0

    # First segment drives the title and departure_time
    first_seg = segments[0]
    flight_no = str(first_seg.get("marketingTransportNo") or "")
    airline = str(first_seg.get("marketingTransportName") or "")
    title = f"{airline} {flight_no}".strip() if (airline or flight_no) else "flyai flight"

    dep_dt = str(first_seg.get("depDateTime") or "")
    departure_time = dep_dt.split(" ")[1] if " " in dep_dt else dep_dt or None

    # Build flight_details for all segments
    flight_details = [
        _segment_to_flight_detail(seg, segment_index=i + 1)
        for i, seg in enumerate(segments)
    ]

    # Route summary text
    route_codes = [s.get("depCityCode") for s in segments] + [segments[-1].get("arrCityCode")]
    route_text = "→".join(str(c) for c in route_codes if c)
    row_text = f"{route_text} | {title} | ¥{price}"

    return {
        "price": price,
        "title": title,
        "departure_time": departure_time,
        "row_text": row_text,
        "flight_details": flight_details,
        "final_url": item.get("jumpUrl") or "https://www.fliggy.com/",
        "journey_type": journey.get("journeyType"),
        "total_duration": item.get("totalDuration"),
    }


async def search_flights(
    *,
    origin: str,
    destination: str,
    departure_date: str,
    cabin: str = "economy",
    journey_type: str | None = None,
    timeout_seconds: int = 30,
) -> list[dict[str, Any]]:
    """Call flyai-cli search-flight and return a list of offer dicts.

    Each offer dict contains: price, title, departure_time, row_text,
    flight_details, final_url.
    """
    flyai_cmd = _find_flyai_executable()
    if not flyai_cmd:
        raise RuntimeError(
            "flyai-cli is not installed or not on PATH. "
            "Install it with: npm install -g @fly-ai/flyai-cli"
        )

    args = flyai_cmd + _build_cli_args(
        origin=origin,
        destination=destination,
        departure_date=departure_date,
        cabin=cabin,
        journey_type=journey_type,
    )

    logger.debug("Running flyai-cli: %s", " ".join(args))

    try:
        completed = await asyncio.to_thread(_run_flyai_command, args, timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"flyai-cli timed out after {timeout_seconds}s") from exc

    stderr_text = completed.stderr.strip()
    if stderr_text:
        logger.warning("flyai-cli stderr: %s", stderr_text[:500])

    if completed.returncode != 0:
        raise RuntimeError(
            f"flyai-cli exited with code {completed.returncode}. stderr: {stderr_text[:500]}"
        )

    stdout_text = completed.stdout.strip()
    if not stdout_text:
        raise RuntimeError("flyai-cli returned empty stdout.")

    parsed = _parse_flyai_response(stdout_text)
    item_list = parsed.get("data", {}).get("itemList", [])
    if not item_list:
        logger.warning("flyai-cli returned no flight items for %s→%s on %s", origin, destination, departure_date)
        return []

    offers: list[dict[str, Any]] = []
    for item in item_list:
        try:
            offer = _item_to_offer(item)
            offers.append(offer)
        except Exception as exc:
            logger.warning("Skipping malformed flyai item: %s", exc)
            continue

    return sorted(offers, key=lambda o: o["price"])


def _run_flyai_command(args: list[str], timeout_seconds: int) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if sys.platform == "win32":
        env["PATHEXT"] = env.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD"
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
        env=env,
        check=False,
    )


def _find_flyai_executable() -> list[str]:
    """Return the command list to invoke flyai-cli."""
    if sys.platform == "win32":
        flyai_cmd = shutil.which("flyai.cmd")
        if flyai_cmd:
            bin_dir = os.path.dirname(flyai_cmd)
            node_path = os.path.join(bin_dir, "node.exe")
            bundle_path = os.path.join(
                bin_dir,
                "node_modules",
                "@fly-ai",
                "flyai-cli",
                "dist",
                "flyai-bundle.cjs",
            )
            if os.path.exists(node_path) and os.path.exists(bundle_path):
                return [node_path, bundle_path]
            return [flyai_cmd]

    if sys.platform != "win32":
        bin_path = shutil.which("flyai")
        if bin_path:
            return [bin_path]

    npx_path = shutil.which("npx.cmd" if sys.platform == "win32" else "npx")
    if npx_path:
        return [npx_path, "--no-install", "flyai"]

    return []
