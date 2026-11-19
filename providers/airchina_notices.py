"""Bounded, opt-in acknowledgements of visible query-stage travel notices.

Audit BEFORE clicking, persist through a callback, then verify the transition.
No generic confirm-button fallback, challenge interaction or booking actions.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
from urllib.parse import urlsplit, urlunsplit


NOTICE_SCRIPT = Path(__file__).with_suffix(".js").read_text(encoding="utf-8")
DIALOG_SELECTOR = '[class*="dialog_mainContent__"]:visible'
BUTTON_SELECTOR = '[class*="model_footer__"] [class*="model_btn__"]:visible'
ACK_BUTTONS = {"确认，查看下一个提示", "我知道了"}


def redact_notice(text):
    """Retain informational prose, not URL tokens, emails, phone/ID numbers."""
    def safe_url(match):
        try:
            p = urlsplit(match[0])
            return urlunsplit((p.scheme, p.hostname or "", p.path, "", ""))
        except ValueError:
            return "[链接已隐藏]"
    value = re.sub(r"https?://[^\s<>]+", safe_url, str(text or ""))
    value = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[邮箱已隐藏]", value)
    value = re.sub(r"(?<!\d)\d{11,18}[Xx]?(?!\d)", "[号码已隐藏]", value)
    value = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "[密钥已隐藏]", value)
    return value


def notice_stop(snapshot, expected_url):
    if not isinstance(snapshot, dict):
        return "needs_notice_confirmation"
    try:
        actual, expected = urlsplit(snapshot.get("url", "")), urlsplit(expected_url)
    except (ValueError, TypeError):
        return "unexpected_page"
    if (actual.scheme != "https" or actual.netloc != "www.airchina.com.cn"
            or actual.path != expected.path):
        return "unexpected_page"
    for key, state in (("verification", "needs_verification"), ("blocked", "blocked"), ("stale", "stale_results")):
        if snapshot.get(key) is True:
            return state
        if snapshot.get(key) is not False:
            return "needs_notice_confirmation"
    return None


def notice_identity(dialog):
    return sha256(json.dumps(dialog, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def can_acknowledge(dialog):
    if not isinstance(dialog, dict):
        return False
    title, body, buttons = dialog.get("title"), dialog.get("body"), dialog.get("buttons")
    if (not isinstance(title, str) or not title.strip() or not isinstance(body, str) or not body.strip()
            or dialog.get("truncated") is not False or dialog.get("has_inputs") is not False
            or dialog.get("disabled") is not False or not isinstance(buttons, list)
            or len(buttons) != 1 or buttons[0] not in ACK_BUTTONS):
        return False
    # Require the observed informational modal family, not an arbitrary alert.
    if not any(word in title for word in ("提示", "通知", "须知", "说明")):
        return False
    combined = title + "\n" + body
    if re.search(r"验证码|安全验证|人机验证|登录|授权|提交订单|确认订单|立即支付|确认支付|"
                 r"确认预订|同意购买|扣款|停留时间过长|重新查询|"
                 r"(?:本人|我)(?:已)?(?:声明|承诺|保证)|确认(?:本人|您|你).{0,12}(?:符合|满足|无|未)", combined):
        return False
    return True


class NoticeHandler:
    def __init__(self, *, enabled=False, callback=None, max_notices=10):
        self.enabled, self.callback, self.max_notices = enabled, callback, max_notices
        self.entries = []
        self.attempted = set()

    def publish(self):
        # Callback failure must prevent clicking. Never swallow disk errors.
        if self.callback:
            self.callback(deepcopy(self.entries))

    def record(self, dialog, status, reason):
        identity = notice_identity(dialog)
        existing = next((e for e in self.entries if e["notice_id"] == identity), None)
        if existing:
            return existing
        entry = {
            "notice_id": identity, "title": redact_notice(dialog.get("title"))[:300],
            "body": redact_notice(dialog.get("body"))[:16000],
            "button": redact_notice(" / ".join(dialog.get("buttons") or []))[:100],
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "status": status, "reason": reason, "truncated": bool(dialog.get("truncated")),
            "source": "airchina_visible_travel_notice",
        }
        self.entries.append(entry)
        self.publish()
        return entry

    async def handle(self, page, expected_url):
        """Return clear or a page stop state; never reload or search again."""
        script = "(" + NOTICE_SCRIPT + "\n)()"
        while len(self.entries) < self.max_notices:
            snapshot = await page.evaluate(script)
            stop = notice_stop(snapshot, expected_url)
            if stop:
                return stop
            if snapshot.get("dialog_count") == 0 and snapshot.get("dialogs") == []:
                return "clear"
            dialogs = snapshot.get("dialogs")
            if snapshot.get("dialog_count") != 1 or not isinstance(dialogs, list) or len(dialogs) != 1:
                return "needs_notice_confirmation"
            dialog = dialogs[0]
            if not isinstance(dialog, dict):
                return "needs_notice_confirmation"
            identity = notice_identity(dialog)
            allowed = self.enabled and can_acknowledge(dialog)
            entry = self.record(dialog, "recorded" if allowed else "manual_required",
                                "before_click" if allowed else "not_authorized_or_unsupported")
            if not allowed or identity in self.attempted:
                return "needs_notice_confirmation"
            root = page.locator(DIALOG_SELECTOR)
            button = root.locator(BUTTON_SELECTOR)
            if await root.count() != 1 or await button.count() != 1:
                entry.update(status="manual_required", reason="ambiguous_dom")
                self.publish()
                return "needs_notice_confirmation"
            # Re-read after persistence/locator resolution. Do not click changed
            # content under a previously recorded title or across a redirect.
            check = await page.evaluate(script)
            stop = notice_stop(check, expected_url)
            if stop or check != snapshot:
                entry.update(status="manual_required", reason="page_changed_before_click")
                self.publish()
                return stop or "needs_notice_confirmation"
            self.attempted.add(identity)
            entry.update(status="click_pending", reason="recorded_before_click")
            self.publish()
            try:
                # Bind text too: never click an arbitrary modal action.
                await button.filter(has_text=re.compile("^" + re.escape(dialog["buttons"][0]) + "$")).click(timeout=3000)
                entry.update(status="click_sent", reason="awaiting_transition")
                self.publish()
                changed = False
                for _ in range(8):
                    await page.wait_for_timeout(250)
                    after = await page.evaluate(script)
                    stop = notice_stop(after, expected_url)
                    if stop:
                        entry.update(status="unconfirmed", reason=stop)
                        self.publish()
                        return stop
                    if (after.get("dialog_count") == 0 and after.get("dialogs") == []
                            or after.get("dialog_count") == 1 and len(after.get("dialogs") or []) == 1
                            and notice_identity(after["dialogs"][0]) != identity):
                        changed = True
                        break
                entry.update(
                    status="auto_confirmed" if changed else "unconfirmed",
                    reason="notice_changed_or_closed" if changed else "no_visible_transition",
                    processed_at=datetime.now(timezone.utc).isoformat(),
                )
                self.publish()
                if not changed:
                    return "needs_notice_confirmation"
            except (Exception, asyncio.CancelledError):
                entry.update(status="unconfirmed", reason="click_or_verification_interrupted")
                self.publish()
                raise
        # Last allowed notice may have closed: allow collection but never click
        # an eleventh dialog; the budget spans all manual resumes in this task.
        snapshot = await page.evaluate(script)
        stop = notice_stop(snapshot, expected_url)
        return stop or ("clear" if snapshot.get("dialog_count") == 0 and snapshot.get("dialogs") == []
                        else "needs_notice_confirmation")
