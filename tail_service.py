from dataclasses import dataclass
import hashlib
import json
import math
import re
from datetime import date
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

OPEN_DESTINATION = "__OPEN__"  # One atomic query checkpoint, never a result destination.

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
    ignore_cache: bool
    mode: str = "explore"
    source_job_id: str = ""
    max_candidates: int = 0
    max_minutes: float = 0
    target_results: int = 0
    alternatives_per_flight: int = 3
    quote_ttl_minutes: int = 20
    expected_itinerary_key: str = ""
    source_result_id: int = 0
    search_scope: str = "candidates"
    filter_scope: str = "first_leg"
    max_baseline_queries: int = 3
    max_followup_candidates: int = 5
    max_provider_calls: int = 0


def normalize_tail_discovery_request(payload: dict[str, Any]) -> TailDiscoveryRequest:
    provider = str(payload.get("provider") or "ctrip").strip().lower()
    if provider not in {"ctrip", "feizhu"}:
        raise ValueError("Unsupported tail-discovery provider.")
    origin = normalize_airport_code(payload.get("origin"))
    transfer = normalize_airport_code(payload.get("transfer"))
    departure_date = str(payload.get("departure_date") or "").strip()
    cabin = str(payload.get("cabin") or "economy_plus").strip().lower()
    if cabin not in {"economy_plus", "business_first", "economy", "premium_economy", "business", "first"}:
        raise ValueError("不支持的舱位。")
    candidate_profile = str(payload.get("candidate_profile") or "").strip().upper()
    airline_input = payload.get("preferred_airlines") or ""
    if isinstance(airline_input, list):
        airline_input = ",".join(str(item) for item in airline_input)
    preferred_airlines = sorted(set(
        item.strip().upper()
        for item in str(airline_input).replace("\uff0c", ",").split(",")
        if item.strip()
    ))
    try:
        passengers = int(payload.get("passengers", 1))
        if not 1 <= passengers <= 9:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError("人数必须为 1 到 9 的整数。")
    try:
        max_price_value = float(payload.get("max_price") or 0)
        if not math.isfinite(max_price_value) or max_price_value < 0:
            raise ValueError()
        max_price = max_price_value if max_price_value > 0 else None
    except (ValueError, TypeError):
        raise ValueError("价格上限必须为非负有效数字。")
    departure_time_start = normalize_tail_time(payload.get("departure_time_start"))
    departure_time_end = normalize_tail_time(payload.get("departure_time_end"))
    if not origin or not transfer or origin == transfer:
        raise ValueError("请填写不同的出发地和目标中转地。")
    date.fromisoformat(departure_date)
    for field in ("departure_time_start", "departure_time_end"):
        if payload.get(field) and not normalize_tail_time(payload[field]):
            raise ValueError("出发时段必须为有效的 HH:MM。")
    if departure_time_start and departure_time_end and departure_time_start > departure_time_end:
        raise ValueError("出发时段结束时间不能早于开始时间。")
    mode = str(payload.get("mode") or "explore")
    if mode not in {"explore", "resume", "refresh", "recheck"}:
        raise ValueError("Unknown discovery mode.")
    if mode == "resume" and not payload.get("source_job_id"):
        raise ValueError("继续任务需要 source_job_id。")
    search_scope = str(payload.get("search_scope") or "candidates")
    filter_scope = str(payload.get("filter_scope") or "first_leg")
    if filter_scope not in {"first_leg", "all_legs"}:
        raise ValueError("筛选范围必须为首段或全行程。")
    if search_scope not in {"candidates", "open", "hybrid"}:
        raise ValueError("不支持的目的地查询范围。")
    if search_scope in {"open", "hybrid"}:
        if provider != "feizhu":
            raise ValueError("开放目的地仅支持飞猪官方 CLI。")
        if mode not in {"explore", "resume"}:
            raise ValueError("开放目的地用于发现；刷新或复核请指定已发现的目的地。")
        if float(payload.get("max_candidates") or 0) or float(payload.get("target_results") or 0):
            raise ValueError("开放/混合模式请使用时间、调用和定向补查预算；城市数和首段目标需设为 0。")
    def budget(name: str, maximum: float, default: float = 0) -> float:
        value = float(payload.get(name, default) or 0)
        if not math.isfinite(value) or value < 0 or value > maximum:
            raise ValueError(f"{name} must be between 0 and {maximum:g}.")
        return value

    baseline_budget = budget("max_baseline_queries", 20, 3)
    if not baseline_budget.is_integer():
        raise ValueError("补查基准次数必须为 0 到 20 的整数。")
    followup_budget = budget("max_followup_candidates", 50, 5)
    call_budget = budget("max_provider_calls", 500, 20 if search_scope == "hybrid" else 0)
    if not followup_budget.is_integer() or not call_budget.is_integer():
        raise ValueError("调用次数及定向补查城市数必须为整数。")
    if provider != "feizhu" and call_budget:
        raise ValueError("平台调用预算仅支持飞猪；携程请使用城市数和时间预算。")
    if search_scope == "candidates" and "candidates" in payload and (not isinstance(payload["candidates"], list) or not payload["candidates"]):
        raise ValueError("请选择至少一个尾段目的地。")
    if search_scope == "hybrid" and "candidates" in payload and not isinstance(payload["candidates"], list):
        raise ValueError("混合模式候选种子必须为列表，可为空。")
    requested_candidates = [
        code
        for item in (payload.get("candidates", []) if search_scope != "open" else [])
        if (code := normalize_airport_code(item))
    ]
    candidates = requested_candidates if "candidates" in payload else list(
        default_tail_codes_for_airlines(preferred_airlines, candidate_profile=candidate_profile)
    )
    candidates = prioritize_tail_codes(
        [
            code
            for code in dict.fromkeys(candidates)
            if (tail_candidate_code_allowed(code) or provider == "feizhu" and re.fullmatch(r"[A-Z]{3}", code)) and code not in {origin, transfer}
        ],
        preferred_airlines,
    )
    if len(candidates) > 500:
        raise ValueError("候选种子最多 500 个城市。")
    return TailDiscoveryRequest(
        provider=provider,
        origin=origin,
        transfer=transfer,
        departure_date=departure_date,
        cabin=cabin,
        passengers=passengers,
        preferred_airlines=preferred_airlines,
        candidate_profile=candidate_profile,
        candidates=[OPEN_DESTINATION] if search_scope == "open" else candidates,
        search_scope=search_scope,
        filter_scope=filter_scope,
        max_baseline_queries=int(baseline_budget),
        max_followup_candidates=int(followup_budget),
        max_provider_calls=int(call_budget),
        max_price=max_price,
        departure_time_start=departure_time_start,
        departure_time_end=departure_time_end,
        ignore_cache=str(payload.get("ignore_cache") or "").lower() in {"1", "true", "yes", "on"},
        mode=mode,
        source_job_id=str(payload.get("source_job_id") or ""),
        max_candidates=int(budget("max_candidates", 500)),
        max_minutes=budget("max_minutes", 1440),
        target_results=int(budget("target_results", 100)),
        alternatives_per_flight=max(1, int(budget("alternatives_per_flight", 10, 3))),
        quote_ttl_minutes=max(1, int(budget("quote_ttl_minutes", 120, 20))),
        expected_itinerary_key=str(payload.get("expected_itinerary_key") or ""),
        source_result_id=int(budget("source_result_id", 2**53)),
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
    filter_scope: str = "first_leg",
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
    if any(not isinstance(leg, dict) for leg in details):
        return {"status": "invalid", "checks": checks, "reasons": ["航段结构无效"], "warnings": []}

    first = details[0] if isinstance(details[0], dict) else {}
    last = details[-1] if isinstance(details[-1], dict) else {}
    evidence_text = tail_result_evidence_text(result, details, raw_payload)
    strict_evidence = source in {"text_fallback", "dom_transit_card", "cleaned_text_fallback"}

    route_checks = {
        "first_leg_starts_at_origin": detail_matches_place(first, origin, ["origin", "departure_airport"]),
        "first_leg_ends_at_transfer": detail_matches_place(first, transfer, ["destination", "arrival_airport", "tail_transfer"]),
        "last_leg_starts_at_transfer": detail_matches_place(last, transfer, ["origin", "departure_airport", "tail_transfer"]),
        "last_leg_ends_at_destination": detail_matches_place(last, destination, ["destination", "arrival_airport"]),
    }
    checks.update(route_checks)
    if len(details) != 2:
        reasons.append("当前甩尾查询只接受已确认的 A-B-C 两航段组合。")
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

    date_values = [str(first["departure_date"]).strip()] if first.get("departure_date") else []
    mismatched_dates = [value for value in date_values if value[:10] != departure_date]
    checks["date_matches"] = not mismatched_dates
    if mismatched_dates:
        reasons.append(f"航段日期与查询日期不一致：{', '.join(sorted(set(mismatched_dates)))}。")
    elif not date_values:
        warnings.append("航段里没有可校验的出发日期。")
    if not first.get("departure_date"):
        warnings.append("首段的实际出发日期未确认。")

    from tail_validation import chronology_checks
    time_reasons, time_warnings = chronology_checks(details, departure_date)
    reasons.extend(time_reasons)
    warnings.extend(time_warnings)
    checks["chronology_confirmed"] = not time_reasons and not time_warnings
    selected_legs = details[:1] if filter_scope == "first_leg" else details
    checks["airline_matches"] = all(details_match_airlines([leg], preferred_airlines) for leg in selected_legs)
    if not checks["airline_matches"]:
        reasons.append("航司筛选不匹配。")

    if reasons:
        return {"status": "invalid", "checks": checks, "reasons": reasons, "warnings": warnings}
    if warnings or source in {"text_fallback", "dom_transit_card", "cleaned_text_fallback"}:
        return {"status": "needs_review", "checks": checks, "reasons": reasons, "warnings": warnings}
    return {"status": "valid", "checks": checks, "reasons": reasons, "warnings": warnings}


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
    payload = dict(payload)
    for field in ("candidates", "preferred_airlines"):
        if isinstance(payload.get(field), list):
            payload[field] = sorted(set(payload[field]))
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
    temporary = queue_file.with_suffix(queue_file.suffix + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(queue_file)


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
    elif phase == "negative_cache":
        queue_status = "cache_skipped"
    elif phase in {"filtered", "price_filtered", "departure_time_filtered"}:
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
    if "date_unconfirmed" in haystack or "result date unconfirmed" in haystack:
        return {"code": "date_unconfirmed", "label": "结果日期证据不足，可重试", "severity": "medium"}
    if "validation_failed" in haystack or "硬校验" in error:
        return {"code": "validation_failed", "label": "航段硬校验失败", "severity": "high"}
    if phase == "negative_cache":
        return {"code": "negative_cache", "label": "近期无结果缓存", "severity": "low"}
    if phase == "route_absent":
        return {"code": "route_absent", "label": "确认无航班", "severity": "medium"}
    if phase == "no_via_transfer":
        return {"code": "no_via_transfer", "label": "有航班但不经过中转点", "severity": "medium"}
    if phase == "parse_incomplete":
        return {"code": "parse_incomplete", "label": "结果加载或解析不完整", "severity": "high"}
    if phase == "search_inconclusive":
        return {"code": "search_inconclusive", "label": "本次未发现，覆盖未确认，可重试", "severity": "medium"}
    if phase == "price_filtered":
        return {"code": "price_filtered", "label": "被价格筛选排除", "severity": "low"}
    if phase == "departure_time_filtered":
        return {"code": "departure_time_filtered", "label": "被起飞时间筛选排除", "severity": "low"}
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
