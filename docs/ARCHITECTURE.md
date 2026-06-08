# Flight Price Tracker Architecture

This project is a local-only flight price tracker. It stores credentials, browser sessions,
query results, reports, and schedule state on the user's machine.

## Current Modules

- `main.py`: command-line entry point for credentials, session bootstrap, manual query runs,
  reports, recurring scheduler, and the local Web UI.
- `web_ui.py`: FastAPI routing layer for the local dashboard and JSON APIs.
- `app_service.py`: application orchestration. It coordinates config, storage, browser
  automation, reporting, route CRUD, and high-level worker entry points.
- `dashboard_builder.py`: dashboard payload construction and display-oriented parsing helpers.
- `scheduling/auto_worker.py`: per-route auto-query worker, including 12-hour recurrence and
  at-least-10-minute route spacing.
- `browser_automation.py`: browser automation coordinator. It owns backend selection,
  provider dispatch, session reuse, response capture, and final raw payload assembly.
- `backends/base.py`: `BrowserBackend` ABC. Implementations return Playwright-compatible
  `BrowserContext` and `Page` objects.
- `backends/chrome.py`: default backend. It launches the user's installed Chrome with a
  persistent provider-specific profile under `runtime/browser_profiles/chrome/`.
- `backends/playwright.py`: Playwright Chromium fallback backend.
- `backends/rebrowser.py`: Rebrowser drop-in replacement.
- `backends/patchright.py`: Patchright drop-in replacement.
- `backends/fallback.py`: `BackendFallbackManager` manages backend startup fallback and
  provider-level backend switching.
- `browser_session.py`: provider profile health checks and recovery-profile selection.
- `browser_use_adapter.py`: optional browser-use diagnostic handoff. It records page-state
  artifacts for abnormal states but does not replace the deterministic Playwright flow.
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
- `config_manager.py`: configuration loading, encrypted credentials, route models, and defaults.
- `data_storage.py`: SQLite schema and repository operations.
- `analyzer.py`: CSV export and markdown reports.
- `static/app.js`, `static/app.css`, `templates/index.html`: local dashboard UI.

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

Single one-way and round-trip queries may use exact network itinerary data, low-price calendar
data, or visible result rows. The saved `raw_payload` records:

- `parser`: which parser produced the result.
- `parser_confidence`: `high`, `medium`, or `low`.
- `detail_quality`: `complete`, `partial`, or `price_only`.
- `detail_source`: `network_exact`, `network_low_price_calendar`, `visible_row`, or `price_only`.

Multi-city queries are stricter. They first open the Ctrip multi-city result URL directly,
for example `multi-bjs-ctu-ctu-sin`, and prefer exact network itinerary data. If exact multi-city
network data is unavailable, the code only accepts a visible result row with usable text; it no
longer reuses single-route low-price calendar extraction for multi-city routes.

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

## Git Boundary

The Git root on this machine is `C:\Users\x1462\Documents\Playground`, not this project directory.
That means `git status` from inside this project can show unrelated sibling projects and documents
from the parent workspace.

## Known Cleanup Priorities

1. Continue slimming `browser_automation.py` by moving provider-specific logic into provider modules.
2. Add provider modules for any provider that still relies on generic fallback behavior.
3. Keep Web UI state rendering small and explicit; prefer small helper functions over inline script.
4. Continue slimming `app_service.py` by moving route CRUD helpers or legacy CLI paths into smaller modules.
5. Remove or quarantine legacy execution paths after confirming they are no longer needed:
   `run_once_via_cli`, `start_run_cycle_via_cli`, `run_cycle_worker.cmd`, and possibly `scheduler.py`.
6. Clean historical encoding artifacts in old config selectors and any remaining old template text.
7. Archive or delete old debug artifacts in `runtime/`, especially `multi_city_probe_*` and large
   live-result JSON files.

## Refactor Rule

Prefer behavior-preserving refactors first. After each move, verify:

- `python -m py_compile ...`
- Web UI responds with HTTP 200
- Dashboard loads
- A Ctrip single-route query still works
- A Ctrip multi-city query saves only reliable multi-city results
