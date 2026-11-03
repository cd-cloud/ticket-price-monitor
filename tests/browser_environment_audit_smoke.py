"""Real Chrome on synthetic intercepted pages; no airline queries."""
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from browser_environment import parse_browser_environment
from browser_environment_audit import audit_provider


async def run():
    for preset in ("desktop-cn-v1", "desktop-gb-v1"):
        provider = SimpleNamespace(locale="zh-CN", timezone="Asia/Shanghai", extra_headers={},
            browser_environment=parse_browser_environment({"preset": preset}))
        report = await audit_provider(provider, headless=True)
        assert report["result"] == "no_observed_differences", report
        for row in report["rows"]:
            if row["name"] in {"视口尺寸", "屏幕尺寸", "像素缩放", "语言", "时区", "主题", "触控能力",
                               "请求 UA 与 JS UA", "请求语言与 JS 语言", "同一上下文两次采样"}:
                assert row["status"] == "pass", row
    provider = SimpleNamespace(locale="zh-CN", timezone="Asia/Shanghai",
                              extra_headers={"User-Agent": "DeliberateHeaderMismatch/1"},
                              browser_environment=None)
    mismatch = await audit_provider(provider, headless=True)
    assert mismatch["result"] == "differences_found"
    assert next(r for r in mismatch["rows"] if r["name"] == "请求 UA 与 JS UA")["status"] == "mismatch"
    assert "DeliberateHeaderMismatch" not in str(mismatch)
    print("PASS: both fixed presets, two-load consistency, deliberate request/JS UA mismatch, redacted reports.")


if __name__ == "__main__":
    asyncio.run(run())
