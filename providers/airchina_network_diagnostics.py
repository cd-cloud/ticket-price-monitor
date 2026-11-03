"""Bounded passive metadata audit, not an API client or a response-body parser.

Attach before navigation; detach at task end even when the page is retained.
Never read bulk headers, bodies, cookies, storage or console logs. When a
rate-limit callback is supplied, only first-party HTTP 429 Retry-After is read.
A bounded script
error message is classified in memory only; no original text/stack is retained.
Requests already in flight at attachment are deliberately ignored.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import asyncio
import time
import re
from urllib.parse import urlsplit


PHASES = {
    "starting_browser", "navigating", "opening_home", "filling_form",
    "selecting_origin", "selecting_destination", "selecting_date",
    "selecting_passengers", "checking_form", "submitting_form", "result_page",
    "reading_notices", "reading_results", "reading_products", "waiting_user", "stopped",
    "checking_retained_results", "opening_results_form", "observing_refresh",
}
OUTCOMES = {
    "reference_fares_observed", "fare_products_observed", "needs_verification",
    "needs_notice_confirmation", "blocked", "stale_results", "unexpected_page",
    "query_page_unverified", "form_entry_unverified", "http_error",
    "cancelled", "error", "unknown",
}
RESOURCE_TYPES = {"document", "xhr", "fetch", "script", "stylesheet", "image", "font",
                  "media", "websocket", "manifest", "texttrack", "eventsource", "other"}
FAILURES = {
    "net::ERR_ABORTED": "aborted", "net::ERR_TIMED_OUT": "timeout",
    "net::ERR_CONNECTION_TIMED_OUT": "timeout", "net::ERR_NAME_NOT_RESOLVED": "dns",
    "net::ERR_CONNECTION_RESET": "connection", "net::ERR_CONNECTION_REFUSED": "connection",
    "net::ERR_INTERNET_DISCONNECTED": "connection", "net::ERR_FAILED": "network_other",
    "net::ERR_CERT_AUTHORITY_INVALID": "tls", "net::ERR_CERT_DATE_INVALID": "tls",
}


class NetworkDiagnostics:
    MAX_PENDING = 256
    MAX_EVENTS = 100

    def __init__(self, callback=None, *, clock=time.monotonic, rate_limit_callback=None):
        self.callback, self.clock = callback, clock
        self.rate_limit_callback = rate_limit_callback
        self.rate_limit_tasks = set()
        self.started = clock()
        self.phase = "starting_browser"
        self.outcome = "unknown"
        self.page = None
        self.listeners = []
        self.pending = {}
        self.events = []
        self.phases = []
        self.counts = Counter()
        self.statuses = Counter()
        self.failures = Counter()
        self.script_errors = {}
        self.lifecycle = {"main_document_requests": 0, "dom_content_loaded": 0, "main_frame_navigations": 0}
        self.environment = {"backend": "unknown", "storage_state_loaded": None,
                            "persistent_profile": None, "profile_role": "unknown"}
        self.attached = False
        self.closed = False
        self.audit_incomplete = False
        self.timer = None
        self.sequence = 0

    def elapsed(self):
        return max(0, round((self.clock() - self.started) * 1000))

    def snapshot(self):
        return deepcopy({
            "schema": "airchina_network_diagnostics_v1", "attached": self.attached,
            "finished": self.closed, "audit_incomplete": self.audit_incomplete,
            "elapsed_ms": self.elapsed(), "phase": self.phase, "outcome": self.outcome,
            "environment": self.environment, "counts": dict(self.counts),
            "lifecycle": self.lifecycle,
            "http_statuses": dict(self.statuses), "failure_types": dict(self.failures),
            "script_error_groups": list(self.script_errors.values()),
            "pending_at_snapshot": len(self.pending),
            "phases": self.phases, "events": self.events,
            "business_response_parsed": False,
        })

    def publish(self):
        if self.callback:
            self.callback(self.snapshot())

    def mark(self, phase):
        if phase not in PHASES or self.closed:
            return
        if not self.phases or phase != self.phase:
            self.phase = phase
            self.phases.append({"phase": phase, "at_ms": self.elapsed()})
            self.phases[:] = self.phases[-40:]
        self.publish()

    def set_environment(self, automation, context):
        # Only existing metadata, never open a profile or read its contents.
        try:
            metadata = automation._session_metadata_for_context(context)
            backend = metadata.browser_backend
            self.environment.update(
                backend=backend if backend in {"chrome", "playwright", "patchright", "rebrowser"} else "unknown",
                storage_state_loaded=metadata.storage_state_loaded if type(metadata.storage_state_loaded) is bool else None,
                persistent_profile=metadata.persistent_profile_loaded if type(metadata.persistent_profile_loaded) is bool else None,
            )
            profile = automation.session_manager.profile_name_for_context(context)
            if profile == "airchina-v2":
                self.environment["profile_role"] = "primary"
            elif profile in {"airchina-v2-recovery", "airchina-v2-recovery-2", "airchina-v2-recovery-3"}:
                self.environment["profile_role"] = "recovery"
        except Exception:
            # Test adapters/other runtimes may not expose session metadata.
            pass
        self.publish()

    def attach(self, page):
        self.page = page
        try:
            for event, handler in (
                ("request", self.request), ("response", self.response),
                ("requestfinished", self.finished), ("requestfailed", self.failed),
                ("pageerror", self.page_error),
                ("domcontentloaded", self.document_loaded), ("framenavigated", self.frame_navigated),
            ):
                def guarded(value=None, handler=handler):
                    if self.closed:
                        return
                    try:
                        handler(value)
                    except Exception:
                        self.audit_incomplete = True
                    self.schedule_publish()
                page.on(event, guarded)
                self.listeners.append((event, guarded))
            self.attached = True
        except Exception:
            self.audit_incomplete = True
            self.detach()
        self.publish()

    def schedule_publish(self):
        if self.timer is None and not self.closed:
            self.timer = asyncio.get_running_loop().call_later(.75, self.checkpoint)

    def checkpoint(self):
        self.timer = None
        try:
            self.publish()
        except Exception:
            # Do not emit raw exceptions from an asynchronous event callback.
            # Final explicit publication still propagates persistence failures.
            self.audit_incomplete = True

    def append(self, event):
        self.events.append(event)
        if len(self.events) > self.MAX_EVENTS:
            del self.events[0]
            self.counts["events_dropped"] += 1

    def request(self, request):
        self.counts["requests"] += 1
        if request.resource_type == "document" and request.frame == self.page.main_frame:
            self.lifecycle["main_document_requests"] += 1
        if len(self.pending) >= self.MAX_PENDING:
            self.counts["requests_untracked"] += 1
            return
        url = urlsplit(request.url)
        host = url.hostname or ""
        scope = ("official" if host == "www.airchina.com.cn" else
                 "airchina_subdomain" if host == "airchina.com.cn" or host.endswith(".airchina.com.cn") else "external")
        resource = request.resource_type
        resource = resource if resource in RESOURCE_TYPES else "other"
        # A URL keyword is only a hint, NEVER proof this is a fare API.
        candidate = scope != "external" and resource in {"xhr", "fetch"} and any(
            word in url.path.lower() for word in ("flight", "search", "query", "fare", "price"))
        self.sequence += 1
        row = {"id": self.sequence, "phase": self.phase, "at_ms": self.elapsed(),
               "scope": scope, "resource": resource, "query_candidate": candidate}
        self.pending[request] = row
        self.counts["query_candidates"] += int(candidate)
        self.append({**row, "event": "request"})

    def response(self, response):
        row = self.pending.get(response.request)
        if row is None:
            return
        status = response.status
        status = status if type(status) is int and 100 <= status <= 599 else None
        if status is not None:
            self.statuses[str(status)] += 1
        self.counts["responses"] += 1
        self.append({**row, "event": "response", "event_phase": self.phase,
                     "status": status, "elapsed_ms": max(0, self.elapsed() - row["at_ms"])})
        if status == 429 and row["scope"] != "external" and self.rate_limit_callback:
            self.rate_limit_callback(None)
            # Pause immediately even if the single allowlisted header cannot
            # be read. Never fetch all headers, cookies or response bodies.
            if len(self.rate_limit_tasks) < 8:
                task = asyncio.create_task(self._retry_after(response))
                self.rate_limit_tasks.add(task)
            else:
                self.audit_incomplete = True

    async def _retry_after(self, response):
        from airchina_query_gate import retry_after_seconds
        try:
            async with asyncio.timeout(1):
                value = await response.header_value("retry-after")
            seconds = retry_after_seconds(value)
            if seconds is not None:
                self.rate_limit_callback(seconds)
        except Exception:
            self.audit_incomplete = True

    async def finish_rate_limits(self):
        tasks, self.rate_limit_tasks = self.rate_limit_tasks, set()
        if tasks:
            try:
                await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    def end_request(self, request, event, failure=None):
        row = self.pending.pop(request, None)
        if row is None:
            return
        self.counts[event] += 1
        self.append({**row, "event": event, "event_phase": self.phase,
                     "elapsed_ms": max(0, self.elapsed() - row["at_ms"]),
                     **({"failure": failure} if failure else {})})
        if failure:
            self.failures[failure] += 1

    def finished(self, request):
        self.end_request(request, "finished")

    def failed(self, request):
        self.end_request(request, "failed", FAILURES.get(request.failure, "network_other"))

    def page_error(self, error):
        self.counts["page_errors"] += 1
        # Inspect a bounded message only in memory; never persist message, stack,
        # URL, identifiers or a reproducible hash of private text.
        name = getattr(error, "name", None)
        kind = name if isinstance(name, str) and name in {"TypeError", "ReferenceError", "SyntaxError", "RangeError",
                                "URIError", "EvalError", "Error"} else "other"
        message = getattr(error, "message", "")
        message = message[:4096].lower() if isinstance(message, str) else ""
        category = "other"
        for label, pattern in (
            ("chunk_load", r"loading chunk .* failed|chunkloaderror|failed to fetch dynamically imported module"),
            ("hydration", r"hydration|hydrating|server rendered html"),
            ("undefined_access", r"cannot read propert|cannot set propert"),
            ("not_callable", r"is not a function"),
            ("undefined_symbol", r"is not defined"),
            ("syntax", r"unexpected token|invalid or unexpected token"),
        ):
            if re.search(pattern, message):
                category = label
                break
        key = (kind, category)
        # Fixed allowlists bound this to 8 * 7 groups, independently of page input.
        group = self.script_errors.setdefault(key, {
            "kind": kind, "category": category, "count": 0,
            "first_at_ms": self.elapsed(), "last_at_ms": self.elapsed(),
            "first_phase": self.phase, "last_phase": self.phase,
        })
        group.update(count=group["count"] + 1, last_at_ms=self.elapsed(), last_phase=self.phase)

    def document_loaded(self, _value=None):
        self.lifecycle["dom_content_loaded"] += 1

    def frame_navigated(self, frame):
        if frame == self.page.main_frame:
            # Includes same-document history changes; not a reload count.
            self.lifecycle["main_frame_navigations"] += 1

    def detach(self):
        for event, handler in self.listeners:
            try:
                self.page.remove_listener(event, handler)
            except Exception:
                self.audit_incomplete = True
        self.listeners.clear()

    def close(self, outcome):
        self.closed = True
        self.outcome = outcome if outcome in OUTCOMES else "unknown"
        if self.timer:
            self.timer.cancel()
            self.timer = None
        self.detach()
        try:
            self.publish()
        finally:
            self.pending.clear()
            self.page = None
