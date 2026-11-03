# FlyClaw provenance and local integration

- Upstream: https://github.com/AI4MSE/FlyClaw
- Imported commit: `c50341bf57449ed71545ff6418e8197a77406b96`
- License: Apache License 2.0; see `LICENSE`.

The complete upstream file tree is included. Local integration preserves the original
CLI, source modules, configuration, caches, documentation and images, with these
adaptations:

- Skiplagged uses a normal HTTP client rather than browser-fingerprint impersonation.
- Fliggy MCP is enabled only with explicit operator-provided configuration; upstream
  embedded signing credentials and device-fingerprint context are not used by default.
- `sources/airchina.py` is a local addition. It reuses the host project's Air China
  reference-fare adapter and never upgrades starting fares to final or bookable quotes.
- Optional `--compare` modules referenced by upstream but absent from its repository
  remain unavailable and are not represented as verified functionality.
