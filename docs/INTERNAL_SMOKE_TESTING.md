# Internal Smoke Testing With Cloudflare

This project does not use Patchright stealth mode, webdriver hiding, CAPTCHA
solving, fingerprint spoofing, or Cloudflare bypass logic.

For authorized company systems behind Cloudflare, use Cloudflare-side test
configuration instead:

- Use Turnstile test sitekeys/secret keys in development and staging.
- Use Cloudflare Access service tokens for CI or internal test runners.
- Use staging-only WAF skip rules or IP allowlists for known test runners.
- Keep production protections enabled for real users.

## Local Runner

```powershell
py -3.12 main.py internal-smoke-test `
  --url https://staging.example.com/health `
  --ready-selector "[data-testid='app-ready']" `
  --headless
```

If the site is protected by Cloudflare Access, set service-token environment
variables before running:

```powershell
$env:CF_ACCESS_CLIENT_ID="your-client-id"
$env:CF_ACCESS_CLIENT_SECRET="your-client-secret"
py -3.12 main.py internal-smoke-test --url https://staging.example.com --headless
```

The runner writes screenshot, HTML, and JSON diagnostics under:

```text
runtime/internal_smoke_tests/
```

Exit codes:

- `0`: page loaded and no verification screen was detected.
- `1`: navigation or readiness check failed.
- `2`: verification/challenge page was detected. Fix Cloudflare test policy; do not hide automation.
