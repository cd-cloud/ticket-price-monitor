"""Conservative itinerary chronology checks; missing evidence is not validity."""
from datetime import date, datetime, timedelta, timezone
import re

from tail_discovery import ALL_DOMESTIC_TAIL_CODES

_CN_CODES = set(ALL_DOMESTIC_TAIL_CODES) | {"PEK", "PKX", "PVG", "TFU", "HKG", "MFM"}
_CN_ZONE = timezone(timedelta(hours=8))


def _timestamp(leg, side, warnings, reasons, index):
    value = str(leg.get(f"{side}_time") or "").strip()
    day = str(leg.get(f"{side}_date") or "").strip()
    label = f"第{index}段{'出发' if side == 'departure' else '到达'}"
    if not value:
        warnings.append(f"{label}时间未确认")
        return None
    try:
        if re.match(r"^\d{4}-\d{2}-\d{2}[T ]", value):
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if day and date.fromisoformat(day[:10]) != stamp.date():
                reasons.append(f"{label}日期与时间字段冲突")
        else:
            if not re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?", value):
                warnings.append(f"{label}时间格式无法确认")
                return None
            if not day:
                warnings.append(f"{label}日期未确认，不能推定同日或跨夜")
                return None
            clock = value if len(value.split(":")[0]) == 2 else "0" + value
            stamp = datetime.fromisoformat(f"{day[:10]}T{clock}".replace("Z", "+00:00"))
    except ValueError:
        reasons.append(f"{label}日期或时间无效")
        return None
    place = str(leg.get("origin" if side == "departure" else "destination") or "").upper()
    airport = str(leg.get("departure_airport" if side == "departure" else "arrival_airport") or "").upper()
    if stamp.tzinfo is None and (place in _CN_CODES or airport in _CN_CODES):
        stamp = stamp.replace(tzinfo=_CN_ZONE)
    return stamp


def chronology_checks(details, departure_date):
    reasons, warnings, stamps = [], [], []
    for index, leg in enumerate(details, 1):
        if not str(leg.get("flight_no") or "").strip():
            warnings.append(f"第{index}段航班号未确认")
        dep = _timestamp(leg, "departure", warnings, reasons, index)
        arr = _timestamp(leg, "arrival", warnings, reasons, index)
        stamps.append((dep, arr))
        if dep and index == 1 and dep.date().isoformat() != departure_date:
            reasons.append("首段实际出发日期与查询日期不一致")
        if dep and arr:
            if dep.tzinfo is not None and arr.tzinfo is not None:
                duration = (arr - dep).total_seconds() / 3600
                if duration <= 0:
                    reasons.append(f"第{index}段到达不晚于出发")
                elif duration > 48:
                    reasons.append(f"第{index}段飞行时间异常（超过48小时）")
            else:
                warnings.append(f"第{index}段时区未确认，无法校验飞行时间")
    for index, ((_, arr), (dep, _)) in enumerate(zip(stamps, stamps[1:])):
        if not arr or not dep:
            continue
        left, right = details[index], details[index + 1]
        same_airport = bool(left.get("arrival_airport") and
                            left.get("arrival_airport") == right.get("departure_airport"))
        if arr.tzinfo is not None and dep.tzinfo is not None:
            connection = dep - arr
        elif same_airport and arr.tzinfo is None and dep.tzinfo is None:
            connection = dep - arr  # Same airport's local clock; no cross-zone inference.
        else:
            warnings.append("中转机场时区未确认，无法校验衔接")
            continue
        if connection <= timedelta():
            reasons.append("次段出发不晚于首段到达")
        elif connection > timedelta(days=7):
            reasons.append("中转间隔异常（超过7天）")
        elif connection > timedelta(hours=24):
            warnings.append("中转超过24小时，需核实是否为有效联程")
        elif connection < timedelta(minutes=45):
            warnings.append("中转不足45分钟，需核实机场最短衔接要求")
    if len(stamps) >= 2 and stamps[0][0] and stamps[-1][0]:
        # A loose date sanity bound, not a timezone-dependent flight duration check.
        gap = (stamps[-1][0].date() - stamps[0][0].date()).days
        if gap > 7 or gap < -1:
            reasons.append("后续航段日期跨度异常")
    return reasons, warnings
