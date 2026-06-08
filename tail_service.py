from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any

from config_manager import normalize_airport_code
from tail_discovery import (
    TAIL_CODE_TEXT,
    candidate_by_code,
    default_tail_codes_for_airlines,
    prioritize_tail_codes,
)
from tail_models import TailQueueItem, coerce_tail_queue_item
from providers.ctrip_tail_parser import details_match_airlines, tail_text_tokens


@dataclass(slots=True)
class TailDiscoveryRequest:
    provider: str
    origin: str
    transfer: str
    departure_date: str
    cabin: str
    passengers: int
    preferred_airlines: list[str]
    candidate_profile: str
    candidates: list[str]
    max_price: float | None
    departure_time_start: str
    departure_time_end: str


def normalize_tail_discovery_request(payload: dict[str, Any]) -> TailDiscoveryRequest:
    provider = str(payload.get("provider") or "ctrip").strip().lower()
    if provider not in {"ctrip", "feizhu"}:
        provider = "ctrip"
    origin = normalize_airport_code(payload.get("origin"))
    transfer = normalize_airport_code(payload.get("transfer"))
    departure_date = str(payload.get("departure_date") or "").strip()
    cabin = str(payload.get("cabin") or "economy_plus").strip().lower()
    candidate_profile = str(payload.get("candidate_profile") or "").strip().upper()
    preferred_airlines = [
        item.strip().upper()
        for item in str(payload.get("preferred_airlines") or "").replace("\uff0c", ",").split(",")
        if item.strip()
    ]
    try:
        passengers = max(1, min(int(payload.get("passengers") or 1), 9))
    except Exception:
        passengers = 1
    try:
        max_price_value = float(payload.get("max_price") or 0)
        max_price = max_price_value if max_price_value > 0 else None
    except Exception:
        max_price = None
    departure_time_start = normalize_tail_time(payload.get("departure_time_start"))
    departure_time_end = normalize_tail_time(payload.get("departure_time_end"))

    requested_candidates = [
        code
        for item in payload.get("candidates", [])
        if (code := normalize_airport_code(item))
    ]
    candidates = requested_candidates or list(
        default_tail_codes_for_airlines(preferred_airlines, candidate_profile=candidate_profile)
    )
    candidates = prioritize_tail_codes(
        [
            code
            for code in dict.fromkeys(candidates)
            if tail_candidate_code_allowed(code) and code not in {origin, transfer}
        ],
        preferred_airlines,
    )
    return TailDiscoveryRequest(
        provider=provider,
        origin=origin,
        transfer=transfer,
        departure_date=departure_date,
        cabin=cabin,
        passengers=passengers,
        preferred_airlines=preferred_airlines,
        candidate_profile=candidate_profile,
        candidates=candidates,
        max_price=max_price,
        departure_time_start=departure_time_start,
        departure_time_end=departure_time_end,
    )


def tail_candidate_code_allowed(code: str) -> bool:
    normalized = str(code or "").strip().upper()
    return bool(normalized and (candidate_by_code(normalized) is not None or normalized in TAIL_CODE_TEXT))


def normalize_tail_time(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parts = text.split(":")
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except Exception:
        return ""
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return ""
    return f"{hour:02d}:{minute:02d}"


def validate_tail_result(
    result: dict[str, Any],
    *,
    origin: str,
    transfer: str,
    destination: str,
    departure_date: str,
    preferred_airlines: list[str] | None = None,
) -> dict[str, Any]:
    details = result.get("flight_details") or []
    raw_payload = result.get("raw_payload") if isinstance(result.get("raw_payload"), dict) else {}
    source = str(raw_payload.get("detail_source") or result.get("detail_source") or "")
    checks: dict[str, bool] = {}
    reasons: list[str] = []
    warnings: list[str] = []

    if not isinstance(details, list) or len(details) < 2:
        checks["has_two_or_more_legs"] = False
        reasons.append("航段数不足，无法确认 A-B-X。")
        return {"status": "invalid", "checks": checks, "reasons": reasons, "warnings": warnings}
    checks["has_two_or_more_legs"] = True

    first = details[0] if isinstance(details[0], dict) else {}
    last = details[-1] if isinstance(details[-1], dict) else {}
    evidence_text = tail_result_evidence_text(result, details, raw_payload)
    strict_evidence = source in {"text_fallback", "dom_transit_card"}

    route_checks = {
        "first_leg_starts_at_origin": detail_matches_place(first, origin, ["origin", "departure_airport"]),
        "first_leg_ends_at_transfer": detail_matches_place(first, transfer, ["destination", "arrival_airport", "tail_transfer"]),
        "last_leg_starts_at_transfer": detail_matches_place(last, transfer, ["origin", "departure_airport", "tail_transfer"]),
        "last_leg_ends_at_destination": detail_matches_place(last, destination, ["destination", "arrival_airport"]),
    }
    checks.update(route_checks)
    for check, ok in route_checks.items():
        if not ok:
            reasons.append(f"{check} 未通过。")

    if strict_evidence:
        evidence_checks = {
            "evidence_mentions_origin": text_mentions_place(evidence_text, origin),
            "evidence_mentions_transfer": text_mentions_place(evidence_text, transfer),
            "evidence_mentions_destination": text_mentions_place(evidence_text, destination),
        }
        checks.update(evidence_checks)
        for check, ok in evidence_checks.items():
            if not ok:
                reasons.append(f"{check} 未通过，原始卡片/文本缺少对应地点证据。")
    elif evidence_text:
        checks["evidence_mentions_transfer"] = text_mentions_place(evidence_text, transfer)
        if not checks["evidence_mentions_transfer"]:
            warnings.append("原始证据未明确出现中转地。")

    date_values = [
        str(detail.get("departure_date") or "").strip()
        for detail in details
        if isinstance(detail, dict) and str(detail.get("departure_date") or "").strip()
    ]
    date_values = tail_date_values_for_validation(date_values, departure_date)
    mismatched_dates = [value for value in date_values if value[:10] != departure_date]
    checks["date_matches"] = not mismatched_dates
    if mismatched_dates:
        reasons.append(f"航段日期与查询日期不一致：{', '.join(sorted(set(mismatched_dates)))}。")
    elif not date_values:
        warnings.append("航段里没有可校验的出发日期。")

    checks["airline_matches"] = details_match_airlines(details, preferred_airlines)
    if not checks["airline_matches"]:
        reasons.append("航司筛选不匹配。")

    if reasons:
        return {"status": "invalid", "checks": checks, "reasons": reasons, "warnings": warnings}
    if warnings or source in {"text_fallback", "dom_transit_card"}:
        return {"status": "needs_review", "checks": checks, "reasons": reasons, "warnings": warnings}
    return {"status": "valid", "checks": checks, "reasons": reasons, "warnings": warnings}


def tail_date_values_for_validation(date_values: list[str], departure_date: str) -> list[str]:
    if not date_values or date_values[0][:10] != departure_date:
        return date_values
    # Later legs in a valid connecting itinerary may depart after midnight.
    return [
        value if index == 0 or value[:10] < departure_date else departure_date
        for index, value in enumerate(date_values)
    ]


def detail_matches_place(detail: dict[str, Any], code: str, keys: list[str]) -> bool:
    tokens = place_tokens(code)
    for key in keys:
        value = str(detail.get(key) or "").upper()
        if value and any(token.upper() in value for token in tokens):
            return True
    return False


def text_mentions_place(text: str, code: str) -> bool:
    haystack = str(text or "").upper()
    return any(token.upper() in haystack for token in place_tokens(code))


def place_tokens(code: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(token or "").strip() for token in tail_text_tokens(code) if str(token or "").strip()))


def tail_result_evidence_text(
    result: dict[str, Any],
    details: list[Any],
    raw_payload: dict[str, Any],
) -> str:
    parts: list[str] = []
    dom_card = raw_payload.get("dom_card")
    if isinstance(dom_card, dict):
        parts.extend(str(dom_card.get(key) or "") for key in ["text", "transfer_text", "transfer_name"])
    for detail in details:
        if not isinstance(detail, dict):
            continue
        parts.extend(str(detail.get(key) or "") for key in ["row_text", "summary_text"])
    return " ".join(part for part in parts if part)


def tail_queue_key(**payload: Any) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def load_tail_queue(queue_file: Path, queue_key: str) -> dict[str, Any]:
    try:
        data = json.loads(queue_file.read_text(encoding="utf-8"))
        if data.get("key") == queue_key:
            return data
    except Exception:
        pass
    return {"key": queue_key, "completed": [], "failed": [], "attempts": []}


def save_tail_queue(queue_file: Path, queue_key: str, state: dict[str, Any]) -> None:
    queue_file.parent.mkdir(parents=True, exist_ok=True)
    state["key"] = queue_key
    queue_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def build_tail_request_queue(
    candidates: list[str],
    previous_attempts: list[dict[str, Any]],
    previous_items: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    previous_by_code = {
        str(item.get("destination") or "").upper(): item
        for item in previous_items or []
        if isinstance(item, dict) and item.get("destination")
    }
    attempts_by_code: dict[str, list[dict[str, Any]]] = {}
    for attempt in previous_attempts:
        if not isinstance(attempt, dict):
            continue
        code = str(attempt.get("destination") or "").upper()
        if not code:
            continue
        attempts_by_code.setdefault(code, []).append(attempt)

    queue: list[dict[str, Any]] = []
    for index, code in enumerate(dict.fromkeys(str(item or "").upper() for item in candidates if item)):
        item = TailQueueItem.from_mapping(
            previous_by_code.get(code) or {},
            destination=code,
            order=index,
        )
        code_attempts = attempts_by_code.get(code) or []
        if code_attempts:
            latest = code_attempts[-1]
            item.apply_attempt_update(tail_queue_item_update_from_attempt(latest))
            item.retry_count = sum(
                1
                for attempt in code_attempts
                if attempt.get("status") == "failed" or attempt.get("phase") in {"validation_failed", "filtered"}
            )
        if item.status in {"running", "opening", "parsing"}:
            item.status = "pending"
        queue.append(item.to_record())
    return queue


def tail_queue_item_update_from_attempt(attempt: dict[str, Any]) -> dict[str, Any]:
    status = str(attempt.get("status") or "").lower()
    phase = str(attempt.get("phase") or "").lower()
    if status == "matched":
        queue_status = "saved"
    elif phase == "validation_failed":
        queue_status = "validation_failed"
    elif phase == "filtered":
        queue_status = "filtered"
    elif status == "needs_verification":
        queue_status = "needs_verification"
    elif status == "failed":
        queue_status = "retryable_failed"
    elif status == "no_match":
        queue_status = "no_match"
    elif status == "started":
        queue_status = "running"
    else:
        queue_status = status or "pending"
    raw_payload = attempt.get("raw_payload") if isinstance(attempt.get("raw_payload"), dict) else {}
    artifact = attempt.get("diagnostic_artifact") or raw_payload.get("diagnostic_artifact") or {}
    return {
        "status": queue_status,
        "last_phase": attempt.get("phase"),
        "last_method": attempt.get("method"),
        "last_error": attempt.get("error"),
        "last_price": attempt.get("price"),
        "trace_path": attempt.get("trace_path") or raw_payload.get("trace_path"),
        "diagnostic_json_path": artifact.get("json_path") if isinstance(artifact, dict) else None,
    }


def tail_queue_report(queue_items: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    normalized_items = [coerce_tail_queue_item(item).to_record() for item in queue_items]
    for item in normalized_items:
        status = str(item.get("status") or "pending")
        counts[status] = counts.get(status, 0) + 1
    return {
        "total": len(normalized_items),
        "counts": counts,
        "pending": counts.get("pending", 0),
        "running": counts.get("running", 0),
        "saved": counts.get("saved", 0),
        "retryable_failed": counts.get("retryable_failed", 0),
        "no_match": counts.get("no_match", 0),
        "items": normalized_items[-120:],
    }


def tail_batch_report(candidates: list[str], attempts: list[dict[str, Any]], saved: int) -> dict[str, Any]:
    statuses = {str(item.get("destination") or "").upper(): item for item in attempts if item.get("destination")}
    matched = [code for code, item in statuses.items() if item.get("status") == "matched"]
    failed = [code for code, item in statuses.items() if item.get("status") == "failed"]
    no_match = [code for code, item in statuses.items() if item.get("status") == "no_match"]
    pending = [code for code in candidates if code not in statuses]
    diagnostics = tail_diagnostics_report(list(statuses.values()))
    return {
        "total": len(candidates),
        "attempted": len(statuses),
        "matched": len(matched),
        "saved": saved,
        "no_match": len(no_match),
        "failed": len(failed),
        "pending": len(pending),
        "matched_codes": matched,
        "failed_codes": failed,
        "no_match_codes": no_match,
        "pending_codes": pending,
        "attempts": attempts[-100:],
        "diagnostics": diagnostics,
    }


def tail_progress_payload(
    stage: str,
    candidates: list[str],
    attempts: list[dict[str, Any]],
    saved: int,
    message: str,
    **extra: Any,
) -> dict[str, Any]:
    report = tail_batch_report(candidates, attempts, saved)
    progress = {
        "stage": stage,
        "message": message,
        "total": report["total"],
        "attempted": report["attempted"],
        "matched": report["matched"],
        "saved": report["saved"],
        "no_match": report["no_match"],
        "failed": report["failed"],
        "pending": report["pending"],
        "attempts": report["attempts"],
        "diagnostics": report["diagnostics"],
    }
    progress.update(extra)
    return progress


def tail_attempt_diagnostic(attempt: dict[str, Any]) -> dict[str, str]:
    status = str(attempt.get("status") or "").lower()
    phase = str(attempt.get("phase") or "").lower()
    method = str(attempt.get("method") or "").lower()
    error = str(attempt.get("error") or "")
    haystack = " ".join([status, phase, method, error]).lower()
    if status == "matched":
        return {"code": "matched", "label": "已命中", "severity": "ok"}
    if "verification" in haystack or "captcha" in haystack or "拒绝访问" in error:
        return {"code": "verification", "label": "验证码/拒绝访问", "severity": "high"}
    if "date_mismatch" in haystack or "different date" in haystack or "did not switch" in haystack:
        return {"code": "date_mismatch", "label": "日期未切换成功", "severity": "high"}
    if "validation_failed" in haystack or "硬校验" in error:
        return {"code": "validation_failed", "label": "航段硬校验失败", "severity": "high"}
    if phase == "filtered":
        return {"code": "filtered", "label": "被价格/时间筛选排除", "severity": "low"}
    if "not reach" in haystack or "direct result url" in haystack or "timed out" in haystack:
        return {"code": "search_failed", "label": "结果页打开/加载失败", "severity": "high"}
    if "airline" in haystack or "航司" in error:
        return {"code": "airline_mismatch", "label": "航司筛选不匹配", "severity": "medium"}
    if "no itinerary via" in haystack or "transfer tokens" in haystack or "中转" in error:
        return {"code": "no_transfer_match", "label": "未找到指定中转", "severity": "medium"}
    if status == "no_match":
        return {"code": "no_match", "label": "无匹配航班/未解析到", "severity": "medium"}
    if status == "failed":
        return {"code": "failed", "label": "查询失败", "severity": "high"}
    return {"code": "other", "label": "其他状态", "severity": "low"}


def tail_diagnostics_report(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    buckets: dict[str, dict[str, Any]] = {}
    for attempt in attempts:
        diagnostic = tail_attempt_diagnostic(attempt)
        code = diagnostic["code"]
        bucket = buckets.setdefault(
            code,
            {
                "code": code,
                "label": diagnostic["label"],
                "severity": diagnostic["severity"],
                "count": 0,
                "destinations": [],
                "examples": [],
            },
        )
        bucket["count"] += 1
        destination = str(attempt.get("destination") or "").upper()
        if destination and destination not in bucket["destinations"]:
            bucket["destinations"].append(destination)
        note = str(attempt.get("error") or attempt.get("phase") or attempt.get("method") or "").strip()
        if note and len(bucket["examples"]) < 2:
            bucket["examples"].append(note[:180])
    order = {"high": 0, "medium": 1, "low": 2, "ok": 3}
    items = sorted(
        buckets.values(),
        key=lambda item: (order.get(str(item.get("severity")), 9), -int(item.get("count") or 0), str(item.get("label"))),
    )
    return {
        "items": items,
        "summary": "；".join(f"{item['label']} {item['count']}" for item in items if item["code"] != "matched"),
    }
