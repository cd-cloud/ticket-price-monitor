# Flight Price Tracker Architecture

This project is a local-only flight price tracker. It stores credentials, browser sessions,
query results, reports, and schedule state on the user's machine.

## Current Modules

- `main.py`: command-line entry point for credentials, session bootstrap, manual query runs,
  reports, recurring scheduler, and the local Web UI.
- `core/`: foundational persistence, configuration, and utilities (`config_manager.py`,
  `data_storage.py`, `private_files.py`, `airport_aliases.py`, `html_text_cleaner.py`,
  `local_session_state_store.py`, `analyzer.py`).
- `services/`: application orchestration and domain services (`app_service.py`,
  `query_service.py`, `route_service.py`, `route_expander.py`, `report_service.py`,
  `run_summary.py`, `browser_profile_service.py`).
- `web/`: FastAPI routing layer (`web_ui.py`) and dashboard payload builder (`dashboard_builder.py`).
- `scheduling/`: recurring execution (`auto_worker.py` for 12-hour per-route auto-query with
  at-least-10-minute spacing, and `scheduler.py` for CLI scheduled runs).
- `browser/`: browser automation coordinator and runtime helpers (`browser_automation.py`,
  `browser_page_operations.py`, `browser_session_manager.py`, `browser_session_policy.py`,
  `browser_environment.py`, `browser_environment_audit.py`, `browser_fingerprint.py/.js`,
  `browser_query_result.py`, `browser_retry_policy.py`, `browser_use_adapter.py`,
  `adaptive_selectors.py`, `provider_runtime.py`).
- `tail/`: tail-discovery domain models, batch/job management, history, validation, quoting,
  and scheduling (`tail_discovery_service.py`, `tail_discovery_manager.py`, `tail_jobs.py`,
  `tail_quotes.py`, `tail_schedule_service.py`, `tail_service.py`, `tail_models.py`, etc.).
- `experiments/`: isolated Air China experimental workflow (`experimental_query_service.py`,
  `experimental_session_service.py`, `experimental_browser_session.py`, `airchina_probe.py`,
  `airchina_query_gate.py`).
- `tools/`: local/internal smoke runners and utility scripts (`local_smoke_test.py`,
  `internal_smoke_test.py`, `replay_tail_history.py`).
- `providers/ctrip_runtime.py`: Ctrip-specific runtime extensions used only by Ctrip handlers.
- `providers/ctrip_browser_operations.py`: Ctrip ordinary-query forms, dates, and itinerary helpers.
- `providers/ctrip_tail_operations.py`: Ctrip tail browser, verification, trace, and diagnostic helpers.
  `BrowserAutomation` explicitly composes these adapters. `CtripTailDiscoveryRunner` receives the
  core coordinator and Ctrip adapters separately, without mixin inheritance or dynamic forwarding.
- `backends/base.py`: `BrowserBackend` ABC. Implementations return Playwright-compatible
  `BrowserContext` and `Page` objects.
- `backends/chrome.py`: default backend. It launches the user's installed Chrome with a
  persistent provider-specific profile under `runtime/browser_profiles/chrome/`.
- `backends/playwright.py`: Playwright Chromium fallback backend.
- `backends/rebrowser.py`, `backends/patchright.py`, and `backends/camoufox.py`: optional
  anti-detect backends. They are disabled by default and require
  `FLIGHT_TRACKER_ENABLE_ANTI_DETECT_BACKENDS=1`; disabled backends cannot be selected or instantiated.
- `backends/fallback.py`: `BackendFallbackManager` manages backend startup fallback and
  provider-level backend switching.
- `providers/base.py`: provider contracts and normalized result assembly types.
- `providers/ctrip.py`: Ctrip query orchestration, including direct result-page flow,
  one-way/round-trip form fallback, and direct-first multi-city flow.
- `providers/ctrip_page.py`: Ctrip Playwright page actions, including form filling,
  city/date selection, result-page date correction, and native cabin-filter attempts.
- `providers/ctrip_parser.py`: Ctrip visible result parsing.
- `providers/ctrip_network_parser.py`: Ctrip network payload parsing for low-price calendar,
  exact itinerary, multi-city itinerary, and diagnostics.
- `providers/ctrip_result.py`: Ctrip result assembly with parser confidence and detail quality.
- `providers/ctrip_tail_discovery.py`: Ctrip tail-discovery page opening, warming, diagnostics,
  and network/page match helpers.
- `providers/ctrip_tail_parser.py`: Ctrip tail-discovery card/text parsing.
- `providers/ctrip_support.py`: Ctrip city-code, route, and cabin helper data.
- `providers/feizhu.py` and `providers/priceline.py`: additional provider query handlers.
- `providers/registry.py`: provider capability metadata used to reject unsupported route/filter combinations before provider I/O.
- `static/app/api.js`: shared fetch wrapper, request timeouts, and response error parsing.
- `static/app/store.js`: explicit dashboard, workspace, and browser-profile state, plus
  `tailState.form`, `job`, `results`, and `connection` groups. `window.tailCandidateNames`
  remains a compatibility mirror for `airport_ui.js`, owned by `tailState.form`.
- `static/app/common.js`: formatting, escaping, toast, in-app confirmation modal, and shared text helpers.
- `static/app/route_form.js` and `static/app/dashboard.js`: route editing and ordinary-query dashboard.
- `static/app/browser_profiles.js`: browser-profile controls and rendering.
- `static/app/tail_form.js`, `tail_jobs.js`, and `tail_results.js`: tail form, job lifecycle, and quote views.
- `static/app/workspace.js`: workspace navigation; `static/app.js` only binds events and starts the UI.
- `static/app.css`, `templates/index.html`: local dashboard styling and script-load order.

## Tail-discovery execution

Tail providers enumerate offers; `TailDiscoveryService` filters and validates them before ranking.
`tail_quotes.py` owns itinerary/first-leg identities, quote freshness and comparable baseline rules.
`TailDiscoveryJobManager` provides in-process lifecycle and explicit resume/refresh/explore actions.
Per-destination results, attempts and completion checkpoints commit together through
`PriceRepository.transaction()`. SQLite, not the diagnostic queue JSON, is authoritative for recovery.
Feizhu tail discovery calls the CLI directly and does not start a browser. Rate limits create a
persisted provider cooldown instead of triggering backend hopping.
See [TAIL_DISCOVERY.md](TAIL_DISCOVERY.md) for API, migration, quote semantics and test commands.

## Browser Backend Policy

The default backend is Chrome. Provider execution order is Chrome first, then Playwright
Chromium fallback. If `defaults.browser_backend` is `chrome` and no fallback list is configured,
`config_manager.py` automatically supplies `["playwright"]`.

Chrome runs with a persistent profile per provider. `BrowserSessionManager` checks profile health
before use, including stale singleton locks and corrupted preferences. If a profile looks unsafe,
the Chrome backend can switch to a recovery profile instead of reusing a broken one.

The browser session diagnostics API is:

```text
GET /api/browser-session/diagnostics
```

It returns the effective backend order, Chrome profile path, profile health, and login hints.

## Ctrip Result Policy

New ordinary Ctrip queries (one-way, round-trip and multi-city) require structured itinerary
evidence matching route, dates, cabin, airline and direct/transfer conditions. Price and details are selected
together and retained in `OfferDetails.matched_quote`; outcome assembly never reparses the
mutable response list. Legacy saved `raw_payload` records may still contain:

- `parser`: which parser produced the result.
- `parser_confidence`: `high`, `medium`, or `low`.
- `detail_quality`: `complete`, `partial`, or `price_only`.
- `detail_source`: `network_exact`, `network_low_price_calendar`, `visible_row`, or `price_only`.

Calendar and visible-only prices are no longer accepted as confirmed ordinary quotes.
`ResponseStore` tracks request generations and rejects both late responses and late body parsing
from previous searches. Direct navigation, form submission and cabin-correction navigation reset
the generation. Complete response bodies remain in memory only; persisted diagnostics contain sanitized URLs and bounded structural summaries. Browser, CLI and tail result URLs are normalized again at the persistence boundary so query strings, fragments and token-like path segments cannot enter SQLite or CSV exports. The same sanitizer is applied to dashboard derivatives, in-memory job responses, runtime queues, attempts and legacy rows during repository initialization. Ctrip tail discovery loads results independently of ordinary fare selection and keeps its service-level validation; empty partial searches remain retryable and are not negative cached.
See [CTRIP_QUERY_INTEGRITY.md](CTRIP_QUERY_INTEGRITY.md) for compatibility details and limitations.

## Local Runtime Data

These paths are intentionally local-only and should not be committed:

- `.env`
- `runtime/credentials.enc.json`
- `runtime/*_state.json`
- `runtime/browser_profiles/`
- `runtime/flight_prices.db`
- `runtime/flight_tracker.log`
- `runtime/inspections/`
- `runtime/internal_smoke_tests/`
- `runtime/tail_diagnostics/`
- `runtime/tail_traces/`
- `output/`
- `vendor/flyclaw/config.local.yaml`

On POSIX, runtime/output directories are hardened to `0700` where possible and sensitive files to `0600`; existing `.env`, provider storage state, encrypted credential and FlyClaw local-config files are migrated when loaded. Ordinary-query full-page captures and Ctrip tail traces require `FLIGHT_TRACKER_CAPTURE_DEBUG_PAGES=true` (explicit inspect/probe commands have separate opt-ins). Route writes use an interprocess lock plus a file fingerprint; a stale writer receives a conflict and must reload instead of replacing another process's routes.

## Git Boundary

This directory is its own Git repository. Local runtime, output, credentials, browser profiles and FlyClaw local configuration must remain outside commits.

## Known Cleanup Priorities

1. Add provider modules for any provider that still relies on generic fallback behavior.
2. Continue slimming `app_service.py` by moving route CRUD helpers or legacy CLI paths into smaller modules.
3. Remove or quarantine legacy execution paths after confirming they are no longer needed:
   `run_once_via_cli`, `start_run_cycle_via_cli`, `run_cycle_worker.cmd`, and possibly `scheduler.py`.
4. Clean historical encoding artifacts in old config selectors and any remaining old template text.
5. Archive or delete old debug artifacts in `runtime/`, especially `multi_city_probe_*` and large
   live-result JSON files.

## Refactor Rule

Prefer behavior-preserving refactors first. After each move, verify:

- `python -m py_compile ...`
- Web UI responds with HTTP 200
- Dashboard loads
- A Ctrip single-route query still works
- A Ctrip multi-city query saves only reliable multi-city results
