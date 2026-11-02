"""Real public notification text; synthetic browser states, no airline requests."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from providers.airchina_notices import NoticeHandler, can_acknowledge, redact_notice
from providers.airchina_page import classify_page
from providers import airchina
from test_airchina_adapter import snapshot as fare_snapshot, TARGET, ROUTE
from test_experimental_queries import env, REQUEST, wait


def notices():
    rows = json.loads((Path(__file__).parent / "fixtures/airchina_travel_notices.json").read_text())["notices"]
    return [{**row, "disabled": False, "has_inputs": False, "truncated": False} for row in rows]


def state(dialogs=None, **changes):
    dialogs = dialogs or []
    return {"url": TARGET, "verification": False, "blocked": False, "stale": False,
            "dialog_count": len(dialogs), "dialogs": dialogs, **changes}


class FakePage:
    def __init__(self, states, callback=None):
        self.evaluate = AsyncMock(side_effect=states)
        self.wait_for_timeout = AsyncMock()
        self.button = SimpleNamespace(count=AsyncMock(return_value=1), click=AsyncMock(side_effect=callback))
        self.button.filter = Mock(return_value=self.button)
        self.root = SimpleNamespace(count=AsyncMock(return_value=1), locator=Mock(return_value=self.button))
        self.locator = Mock(return_value=self.root)


def test_all_three_observed_notices_are_recorded_before_click_and_confirmed():
    a, b, c = [state([n]) for n in notices()]
    updates = []
    def on_click(**kw):
        assert updates[-1][-1]["status"] == "click_pending"
        assert updates[-1][-1]["body"]
    page = FakePage([a, a, b, b, b, c, c, c, state(), state()], on_click)
    handler = NoticeHandler(enabled=True, callback=lambda rows: updates.append(rows))
    assert asyncio.run(handler.handle(page, TARGET)) == "clear"
    assert page.button.click.await_count == 3
    assert all(n["status"] == "auto_confirmed" for n in handler.entries)
    assert [n["button"] for n in handler.entries] == ["确认，查看下一个提示"] * 2 + ["我知道了"]
    assert len(updates[0]) == 1  # Callback gets copies, not mutated later.
    assert updates[0][0]["status"] == "recorded"


@pytest.mark.parametrize("enabled", [False, True])
def test_last_i_know_button_detected_without_read_next_text(enabled):
    item = notices()[-1]
    assert classify_page(TARGET, item["body"] + "\n我知道了", TARGET)["state"] == "needs_notice_confirmation"
    assert can_acknowledge(item)
    page = FakePage([state([item])] * 2 + [state(), state()])
    result = asyncio.run(NoticeHandler(enabled=enabled).handle(page, TARGET))
    assert result == ("clear" if enabled else "needs_notice_confirmation")
    assert page.button.click.await_count == int(enabled)


@pytest.mark.parametrize("changes", [
    {"buttons": ["立即支付"]}, {"buttons": ["确认预订"]}, {"buttons": ["确认"]},
    {"buttons": ["我知道了", "取消"]}, {"buttons": []},
    {"has_inputs": True}, {"disabled": True}, {"truncated": True},
    {"title": None}, {"body": ""}, {"body": "本人承诺符合特殊乘机条件"},
    {"body": "请登录授权后继续"}, {"body": "本次将自动扣款"},
    {"body": "确认您符合上述要求"}, {"title": "订单提交"}, {"body": "请完成验证码"},
])
def test_unknown_personal_or_financial_prompts_never_clicked(changes):
    dialog = {**notices()[0], **changes}
    page = FakePage([state([dialog])])
    handler = NoticeHandler(enabled=True)
    assert asyncio.run(handler.handle(page, TARGET)) == "needs_notice_confirmation"
    page.button.click.assert_not_awaited()
    assert handler.entries[0]["status"] == "manual_required"


@pytest.mark.parametrize(("changes", "expected"), [
    ({"verification": True}, "needs_verification"),
    ({"blocked": True}, "blocked"),
    ({"stale": True}, "stale_results"),
    ({"url": "https://evil.test/"}, "unexpected_page"),
    ({"url": TARGET.replace("2099-01-01", "2099-01-02")}, "unexpected_page"),
    ({"verification": None}, "needs_notice_confirmation"),
])
def test_challenge_or_redirect_never_read_or_clicked(changes, expected):
    page = FakePage([state(notices()[:1], **changes)])
    handler = NoticeHandler(enabled=True)
    assert asyncio.run(handler.handle(page, TARGET)) == expected
    assert handler.entries == []
    page.button.click.assert_not_awaited()


def test_multiple_dialogs_are_not_dismissed_arbitrarily():
    page = FakePage([state(notices()[:2])])
    assert asyncio.run(NoticeHandler(enabled=True).handle(page, TARGET)) == "needs_notice_confirmation"
    page.button.click.assert_not_awaited()


def test_changed_content_before_click_never_uses_previous_authorization():
    a, b = [state([n]) for n in notices()[:2]]
    page = FakePage([a, b])
    handler = NoticeHandler(enabled=True)
    assert asyncio.run(handler.handle(page, TARGET)) == "needs_notice_confirmation"
    assert handler.entries[0]["reason"] == "page_changed_before_click"
    page.button.click.assert_not_awaited()


def test_recording_failure_prevents_click():
    def fail(_rows):
        raise OSError("no space")
    page = FakePage([state(notices()[:1])])
    with pytest.raises(OSError):
        asyncio.run(NoticeHandler(enabled=True, callback=fail).handle(page, TARGET))
    page.button.click.assert_not_awaited()


def test_no_transition_is_not_claimed_as_confirmed_or_reclicked():
    a = state(notices()[:1])
    page = FakePage([a] * 11)
    handler = NoticeHandler(enabled=True)
    assert asyncio.run(handler.handle(page, TARGET)) == "needs_notice_confirmation"
    assert handler.entries[0]["status"] == "unconfirmed"
    assert asyncio.run(handler.handle(page, TARGET)) == "needs_notice_confirmation"
    page.button.click.assert_awaited_once()


def test_cycles_do_not_reclick_identical_notice():
    a, b = [state([n]) for n in notices()[:2]]
    page = FakePage([a, a, b, b, b, a, a])
    handler = NoticeHandler(enabled=True)
    assert asyncio.run(handler.handle(page, TARGET)) == "needs_notice_confirmation"
    assert page.button.click.await_count == 2


def test_budget_stops_before_extra_notice():
    a, b = [state([n]) for n in notices()[:2]]
    page = FakePage([a, a, b, b])
    handler = NoticeHandler(enabled=True, max_notices=1)
    assert asyncio.run(handler.handle(page, TARGET)) == "needs_notice_confirmation"
    page.button.click.assert_awaited_once()


@pytest.mark.parametrize("exception", [RuntimeError("secret-token"), asyncio.CancelledError()])
def test_click_interrupt_preserves_uncertain_record_and_does_not_retry(exception):
    a = state(notices()[:1])
    page = FakePage([a, a])
    page.button.click.side_effect = exception
    handler = NoticeHandler(enabled=True)
    with pytest.raises(type(exception)):
        asyncio.run(handler.handle(page, TARGET))
    assert handler.entries[0]["status"] == "unconfirmed"
    assert "secret-token" not in json.dumps(handler.entries)
    page.button.click.assert_awaited_once()


def test_redaction_keeps_prose_but_removes_url_tokens_email_and_number():
    result = redact_notice("通知 https://example.test/path?token=secret#private a@b.com 13812345678")
    assert "通知 https://example.test/path" in result
    for secret in ("token", "secret", "private", "a@b.com", "13812345678"):
        assert secret not in result


def test_adapter_auto_processing_keeps_one_navigation_and_no_manual_pause():
    a = state(notices()[:1])
    page = FakePage([a, a, state(), state(), fare_snapshot(), fare_snapshot()])
    page.url = TARGET
    page.goto = AsyncMock(return_value=SimpleNamespace(status=200))
    original = page.locator
    page.locator = lambda selector: (
        SimpleNamespace(inner_text=AsyncMock(return_value="确认，查看下一个提示" if page.evaluate.await_count == 0 else "选择航班"))
        if selector == "body" else original(selector)
    )
    manual = AsyncMock()
    report = asyncio.run(airchina.inspect(
        SimpleNamespace(throttle=AsyncMock()), SimpleNamespace(timeout_ms=1000), ROUTE, page,
        auto_confirm_notices=True, manual_handler=manual,
    ))
    assert report["state"] == "reference_fares_observed"
    assert report["notices"][0]["status"] == "auto_confirmed"
    manual.assert_not_awaited()
    page.goto.assert_awaited_once()


@pytest.mark.parametrize("terminal", ["timeout", "error"])
def test_task_retains_record_even_when_result_absent(env, terminal, monkeypatch):
    service, repo, control, page, automation, query = env
    async def fake(route, **kwargs):
        assert kwargs["auto_confirm_notices"] is True
        kwargs["notice_callback"]([{
            "notice_id": "sample", "title": "测试提示", "body": "已记录内容",
            "status": "auto_confirmed", "observed_at": repo.now_iso(),
        }])
        raise TimeoutError() if terminal == "timeout" else RuntimeError("private-token")
    monkeypatch.setattr(query, "inspect_airchina_experiment", fake)
    job = service.submit({**REQUEST, "auto_confirm_notices": True})
    result = wait(service, job["job_id"])
    assert result["status"] == ("timed_out" if terminal == "timeout" else "failed")
    assert result["result"] is None
    assert result["notices"][0]["body"] == "已记录内容"
    assert repo.count_snapshots() == 0


@pytest.mark.parametrize("value", ["true", 1, None, {}, []])
def test_authorization_requires_boolean(env, value):
    service, *_ = env
    with pytest.raises(ValueError):
        service.submit({**REQUEST, "auto_confirm_notices": value})
