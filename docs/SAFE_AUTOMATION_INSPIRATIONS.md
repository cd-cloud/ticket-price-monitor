# Safe Automation Inspirations

This project may borrow engineering patterns from scraping and browser automation
projects, but it must not borrow stealth, CAPTCHA bypassing, fingerprint
manipulation, proxy rotation, or anti-bot evasion.

## Rebrowser / Patchright

Useful idea: keep browser backends behind an explicit abstraction boundary.

Not adopted: stealth/undetected browser patches. The tracker keeps Playwright as
the deterministic default and treats any non-Playwright backend as diagnostics
only unless it is explicitly reviewed.

## Scrapling

Useful idea: adaptive selector recovery. If a class name changes, try a small
ranked set of ordinary selectors and pick the candidate with visible route/card
evidence.

Implemented locally in:

- `adaptive_selectors.py`
- `providers/ctrip_tail_parser.py`

## Pydoll

Useful idea: event-driven diagnostics and typed extraction. We borrow the shape,
not the stealth claims:

- page-state classification lives in `browser_use_adapter.py`
- strict itinerary schema lives in `tail_structured_extractor.py`
- Playwright network/trace artifacts remain the source of truth for debugging

## Guardrails

- Do not add CAPTCHA solving.
- Do not add Cloudflare/anti-bot bypass code.
- Do not rotate proxies or fingerprints.
- Prefer lower query volume, longer intervals, saved sessions, manual verification,
  and clear failure states.
