"""Conservative date evidence: selected controls/labelled headers, not calendar tiles."""
import re
from datetime import date
from urllib.parse import parse_qs, urlparse


def has_explicit_year(value):
    return bool(re.search(r"(?<!\d)\d{4}[-/年]\d{1,2}[-/月]\d{1,2}(?!\d)", value))


def dates_in_value(value: str, reference_date: str) -> list[str]:
    text = str(value or "").strip()
    # Explicit years take precedence. A wrong year must not match a month/day suffix.
    full = re.findall(r"(?<!\d)(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})(?:日)?(?!\d)", text)
    candidates = full
    if not full:
        candidates = [(reference_date[:4], month, day) for month, day in
                      re.findall(r"(?<![\d/-])(\d{1,2})(?:月|[-/])(\d{1,2})(?:日)?(?!\d)", text)]
    result = []
    for year, month, day in candidates:
        try:
            normalized = date(int(year), int(month), int(day)).isoformat()
        except ValueError:
            continue
        if normalized not in result:
            result.append(normalized)
    return result


def date_value_matches(value: str, expected: str) -> bool:
    # A range/multiple dates is not an unambiguous selected date.
    return dates_in_value(value, expected) == [expected]


def url_role_dates(url: str, role: str, expected: str) -> list[str]:
    query = parse_qs(urlparse(url).query)
    keys = ("depdate", "ddate", "date", "dDate", "departDate", "departureDate") if role == "departure" else (
        "retdate", "rdate", "returnDate", "return_date")
    result = []
    for key in keys:
        for value in query.get(key, []):
            # Ctrip's round-trip depdate consists of ordered outbound/inbound dates.
            value = value.split("_")[0] if key == "depdate" else value
            if has_explicit_year(value):
                result.extend(dates_in_value(value, expected))
    if role == "return":
        for value in query.get("depdate", []):
            parts = value.split("_")
            if len(parts) == 2 and has_explicit_year(parts[1]):
                result.extend(dates_in_value(parts[1], expected))
    return list(dict.fromkeys(result))


def labelled_date_values(text: str, role: str) -> list[str]:
    label = r"(?:出发日期|去程日期)" if role == "departure" else r"(?:返程日期|返回日期)"
    # Restrict to a labelled selected date, not arbitrary body text/date strips.
    pattern = label + r"\s*[:：]?\s*((?:\d{4}[-/年])?\d{1,2}[-/月]\d{1,2}日?)"
    values = re.findall(pattern, text or "")
    if role == "departure":
        values += re.findall(
            r"(?:单程|往返)\s*[:：][^0-9]{0,20}((?:\d{4}[-/年])?\d{1,2}[-/月]\d{1,2}日?)",
            text or "",
        )
    return values


async def selected_control_values(page, role: str) -> list[str]:
    selectors = (
        ".modifyDate.depart-date, input[name='departDate'], input[name='departureDate'], input[aria-label*='出发日期']"
        if role == "departure" else
        ".modifyDate.return-date, .modifyDate.ret-date, input[name='returnDate'], input[aria-label*='返程日期']"
    )
    values = []

    async def read(locator):
        try:
            if not await locator.is_visible():
                return
            try:
                value = await locator.input_value(timeout=500)
            except Exception:
                inputs = locator.locator("input")
                value = ""
                for index in range(min(await inputs.count(), 2)):
                    child = inputs.nth(index)
                    if await child.is_visible():
                        value = await child.input_value(timeout=500)
                        if value:
                            break
                if not value:
                    value = await locator.inner_text(timeout=500)
            if value and value.strip() not in values:
                values.append(value.strip())
        except Exception:
            pass

    explicit = page.locator(selectors)
    for index in range(min(await explicit.count(), 6)):
        await read(explicit.nth(index))
    # Known form order: first visible date is outbound; second is inbound.
    widgets = page.locator("form#searchForm .flt-date .modifyDate")
    visible = []
    for index in range(min(await widgets.count(), 6)):
        widget = widgets.nth(index)
        if await widget.is_visible():
            visible.append(widget)
    index = 0 if role == "departure" else 1
    if len(visible) > index:
        await read(visible[index])
    return values


async def collect_date_evidence(page, expected: str, *, return_date=None, overlay_visible=False):
    evidence = {
        "expected": expected, "url": page.url, "input_values": [], "page_dates": [],
        "url_date_matched": False, "overlay_visible": overlay_visible, "matched": False,
        "matched_source": None, "matched_value": None, "status": "unknown",
    }
    # Even a selected-looking input can be a not-yet-submitted calendar edit.
    if overlay_visible:
        evidence["matched_source"] = "calendar_open"
        return evidence
    try:
        text = await page.locator("body").inner_text(timeout=2000)
    except Exception:
        text = ""
    roles = {}
    for role, day in (("departure", expected), ("return", return_date)):
        if not day:
            continue
        values = await selected_control_values(page, role)
        control_dates = list(dict.fromkeys(d for value in values for d in dates_in_value(value, day)))
        header_values = labelled_date_values(text, role)
        header_dates = list(dict.fromkeys(d for value in header_values for d in dates_in_value(value, day)))
        url_dates = url_role_dates(page.url, role, day)
        conflicting = any(d != day for d in control_dates + header_dates + url_dates)
        year_confirmed = url_dates == [day] or any(
            has_explicit_year(value) and dates_in_value(value, day) == [day]
            for value in values + header_values
        )
        confirmed = not conflicting and year_confirmed and (control_dates == [day] or header_dates == [day])
        roles[role] = {
            "expected": day, "input_values": values, "page_dates": header_dates, "url_dates": url_dates,
            "matched": confirmed, "status": "mismatch" if conflicting else "confirmed" if confirmed else "unknown",
            "year_confirmed": year_confirmed,
            "matched_source": "date_conflict" if conflicting else "visible_control" if control_dates == [day]
                              else "page_header" if header_dates == [day] else None,
        }
    departure = roles.get("departure", {})
    evidence.update(input_values=departure.get("input_values", []), page_dates=departure.get("page_dates", []),
                    url_date_matched=departure.get("url_dates") == [expected], roles=roles)
    evidence["matched"] = bool(roles) and all(item["matched"] for item in roles.values())
    evidence["status"] = ("mismatch" if any(item["status"] == "mismatch" for item in roles.values())
                          else "confirmed" if evidence["matched"] else "unknown")
    evidence["matched_source"] = departure.get("matched_source")
    if evidence["matched"]:
        evidence["matched_value"] = expected
    return evidence
