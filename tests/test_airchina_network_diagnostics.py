"""Passive event audit tests: no browser/network/state-file access."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from providers.airchina_network_diagnostics import NetworkDiagnostics


class Page:
    def __init__(self):
        self.handlers = {}

    def on(self, event, callback):
        self.handlers.setdefault(event, []).append(callback)

    def remove_listener(self, event, callback):
        self.handlers[event].remove(callback)

    def emit(self, event, value):
        for callback in list(self.handlers.get(event, [])):
            callback(value)


class Request:
    resource_type = "fetch"
    url = "https://www.airchina.com.cn/api/flight/SECRET_PATH?token=SECRET_QUERY"
    failure = "net::ERR_NAME_NOT_RESOLVED"

    @property
    def headers(self):
        raise AssertionError("never read headers")

    @property
    def post_data(self):
        raise AssertionError("never read body")


class Response:
    def __init__(self, request, status=200):
        self.request, self.status = request, status

    @property
    def headers(self):
        raise AssertionError("never read headers")

    def json(self):
        raise AssertionError("never parse unknown response")

    def text(self):
        raise AssertionError("never read response body")


def test_scoped_passive_events_redacted_and_http_errors_not_network_failures():
    async def run():
        saved = []
        audit = NetworkDiagnostics(saved.append)
        page = Page()
        audit.attach(page)
        audit.mark("submitting_form")
        request = Request()
        page.emit("request", request)
        audit.mark("result_page")
        page.emit("response", Response(request, 403))
        page.emit("requestfinished", request)
        page.emit("pageerror", RuntimeError("SECRET_ERROR"))
        audit.close("needs_verification")
        report = saved[-1]
        assert report["finished"] and report["attached"]
        assert report["http_statuses"] == {"403": 1}
        assert report["counts"]["query_candidates"] == 1
        assert report["counts"].get("failed", 0) == 0
        assert report["counts"]["page_errors"] == 1
        assert report["events"][1]["phase"] == "submitting_form"
        assert report["events"][1]["event_phase"] == "result_page"
        assert report["outcome"] == "needs_verification"
        assert "SECRET" not in json.dumps(report)
        assert all(not handlers for handlers in page.handlers.values())
        assert not audit.pending and audit.page is None
        assert audit.timer is None
    asyncio.run(run())


def test_late_old_response_ignored_and_listener_not_duplicated_on_reuse():
    async def run():
        page = Page()
        request = Request()
        first = NetworkDiagnostics()
        first.attach(page)
        page.emit("request", request)
        first.close("reference_fares_observed")
        second = NetworkDiagnostics()
        second.attach(page)
        page.emit("response", Response(request))
        page.emit("requestfinished", request)
        assert not second.events
        next_request = Request()
        page.emit("request", next_request)
        page.emit("response", Response(next_request))
        page.emit("requestfinished", next_request)
        assert second.counts["requests"] == second.counts["responses"] == 1
        second.close("reference_fares_observed")
        assert all(not handlers for handlers in page.handlers.values())
    asyncio.run(run())


def test_document_events_count_separately_from_same_document_navigation():
    async def run():
        page = Page()
        page.main_frame = object()
        audit = NetworkDiagnostics()
        audit.attach(page)
        page.emit("framenavigated", page.main_frame)
        page.emit("framenavigated", object())  # iframe
        assert audit.snapshot()["lifecycle"] == {
            "main_document_requests": 0, "dom_content_loaded": 0, "main_frame_navigations": 1,
        }
        request = Request()
        request.resource_type, request.frame = "document", page.main_frame
        page.emit("request", request)
        page.emit("domcontentloaded", None)
        report = audit.snapshot()
        assert report["lifecycle"]["main_document_requests"] == 1
        assert report["lifecycle"]["dom_content_loaded"] == 1
        assert "SECRET" not in json.dumps(report)
        audit.close("reference_fares_observed")
        assert all(not handlers for handlers in page.handlers.values())
    asyncio.run(run())

@pytest.mark.parametrize("code,expected", [
    ("net::ERR_NAME_NOT_RESOLVED", "dns"),
    ("net::ERR_CERT_DATE_INVALID", "tls"),
    ("net::ERR_ABORTED", "aborted"),
    ("net::ERR_TIMED_OUT", "timeout"),
    ("net::ERR_CONNECTION_RESET", "connection"),
    ("failed at https://example.com/SECRET", "network_other"),
])
def test_network_failure_allowlist(code, expected):
    audit = NetworkDiagnostics()
    request = Request()
    request.failure = code
    audit.request(request)
    audit.failed(request)
    assert audit.snapshot()["failure_types"] == {expected: 1}
    assert "SECRET" not in json.dumps(audit.snapshot())


def test_event_and_pending_budgets_and_untrusted_host_never_classified_official():
    audit = NetworkDiagnostics()
    audit.MAX_PENDING, audit.MAX_EVENTS = 3, 2
    for _ in range(10):
        request = Request()
        request.url = "https://www.airchina.com.cn.evil.example/api/flight?secret"
        audit.request(request)
    report = audit.snapshot()
    assert len(audit.pending) == 3
    assert len(report["events"]) == 2
    assert report["counts"]["requests_untracked"] == 7
    assert report["counts"]["events_dropped"] == 1
    assert report["counts"]["query_candidates"] == 0
    assert all(e["scope"] == "external" for e in report["events"])
    audit.close("unknown")


def test_partial_attach_failure_detaches_already_registered_handlers():
    page = Page()
    normal = page.on
    def on(event, callback):
        if event == "requestfinished":
            raise RuntimeError("SECRET")
        normal(event, callback)
    page.on = on
    audit = NetworkDiagnostics()
    audit.attach(page)
    assert not audit.attached and audit.audit_incomplete
    assert all(not handlers for handlers in page.handlers.values())
    assert "SECRET" not in json.dumps(audit.snapshot())


def test_session_metadata_is_not_authentication_or_page_reuse_proof():
    audit = NetworkDiagnostics()
    automation = SimpleNamespace(
        _session_metadata_for_context=Mock(return_value=SimpleNamespace(
            browser_backend="chrome", storage_state_loaded=False, persistent_profile_loaded=True)),
        session_manager=SimpleNamespace(profile_name_for_context=Mock(return_value="airchina-v2")),
    )
    audit.set_environment(automation, "context")
    assert audit.environment == {"backend": "chrome", "storage_state_loaded": False,
                                 "persistent_profile": True, "profile_role": "primary"}
    automation.session_manager.profile_name_for_context.return_value = "/SECRET/PERSONAL"
    other = NetworkDiagnostics()
    other.set_environment(automation, "context")
    assert other.environment["profile_role"] == "unknown"
    assert "SECRET" not in json.dumps(other.snapshot())


def test_checkpoint_failure_sanitized_but_final_persistence_failure_propagates():
    def fail(_):
        raise RuntimeError("private database error")
    audit = NetworkDiagnostics(fail)
    audit.checkpoint()
    assert audit.audit_incomplete
    assert "private" not in json.dumps(audit.snapshot())
    with pytest.raises(RuntimeError):
        audit.close("error")
    assert audit.closed and audit.page is None


@pytest.mark.parametrize("message,category", [
    ("Cannot read properties of undefined (reading 'SECRET')", "undefined_access"),
    ("SECRET is not a function", "not_callable"),
    ("SECRET is not defined", "undefined_symbol"),
    ("Loading chunk 123 failed: https://SECRET/", "chunk_load"),
    ("Hydration failed SECRET", "hydration"),
    ("Unexpected token SECRET", "syntax"),
    ("https://SECRET/?token=SECRET", "other"),
])
def test_script_errors_persist_only_fixed_categories_and_group_counts(message, category):
    audit = NetworkDiagnostics()
    error = SimpleNamespace(name="TypeError", message=message, stack="SECRET")
    audit.mark("selecting_origin")
    audit.page_error(error)
    audit.mark("waiting_user")
    audit.page_error(error)
    report = audit.snapshot()
    assert report["counts"]["page_errors"] == 2
    assert len(report["script_error_groups"]) == 1
    group = report["script_error_groups"][0]
    assert group["category"] == category and group["count"] == 2
    assert group["first_phase"] == "selecting_origin" and group["last_phase"] == "waiting_user"
    assert "SECRET" not in json.dumps(report)


def test_script_errors_unknown_names_are_bounded_not_raw_dedup_keys():
    audit = NetworkDiagnostics()
    for i in range(1000):
        audit.page_error(SimpleNamespace(name=f"SECRET{i}", message=f"SECRET{i}"))
    assert len(audit.script_errors) == 1
    assert audit.snapshot()["script_error_groups"][0]["kind"] == "other"
    assert "SECRET" not in json.dumps(audit.snapshot())
