"""Real Chrome session reuse on synthetic intercepted pages; no airline I/O."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from playwright.async_api import async_playwright
from experimental_browser_session import ExperimentalSessionRunner


def run():
    requests, opened, closed = [], [], []
    # This is our own offline fixture. No real site's storage or tokens are read.
    html = """<!doctype html><meta charset="utf-8"><p id="counter"></p><script>
      const count = Number(sessionStorage.getItem('offline_demo_count') || 0) + 1;
      sessionStorage.setItem('offline_demo_count', String(count));
      document.getElementById('counter').textContent = String(count);
    </script>"""
    @asynccontextmanager
    async def factory(config, credentials):
        assert credentials == {}
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            context = await browser.new_context()
            async def intercept(route):
                requests.append(route.request.url)
                await route.fulfill(content_type="text/html", body=html)
            await context.route("**/*", intercept)
            page = await context.new_page()
            opened.append(page)
            async def new_page(provider):
                return page, context
            async def close_context(ctx):
                await ctx.close()
            try:
                yield SimpleNamespace(_new_page=new_page, _close_context_quietly=close_context)
            finally:
                await browser.close()
                closed.append(page)

    with tempfile.TemporaryDirectory() as tmp:
        cfg = SimpleNamespace(runtime_dir=Path(tmp), browser_backend="chrome", browser_backend_fallbacks=[],
                              browser_channel=None, automation_backend="playwright", headless=True)
        provider = SimpleNamespace(name="airchina")
        runner = ExperimentalSessionRunner()
        audits = []
        async def task(pool, mode):
            async with pool.lease(cfg, provider, factory, mode=mode, callback=audits.append) as lease:
                await lease.page.goto("https://offline-airchina.test/")
                count = await lease.page.locator("#counter").inner_text()
                lease.keep = True  # synthetic success; no claim about real fare parsing
                return lease.page, lease.context, asyncio.get_running_loop(), count
        try:
            first = runner.run(lambda pool: task(pool, "reuse"))
            second = runner.run(lambda pool: task(pool, "reuse"))
            assert all(a is b for a, b in zip(first[:3], second[:3]))
            assert (first[3], second[3]) == ("1", "2")
            assert len(opened) == 1 and not closed
            assert audits[-1]["reused"] and audits[-1]["use_number"] == 2
            # Explicit cold control disposes the retained owner before reopening.
            third = runner.run(lambda pool: task(pool, "fresh"))
            assert third[0] is not first[0] and third[3] == "1"
            assert len(opened) == len(closed) == 2
            fourth = runner.run(lambda pool: task(pool, "persistent"))
            fifth = runner.run(lambda pool: task(pool, "persistent"))
            assert fourth[0] is not third[0] and fifth[0] is fourth[0]
            assert (fourth[3], fifth[3]) == ("1", "2")
            assert len(opened) == 3 and len(closed) == 2
            assert audits[-1]["reused"] and audits[-1]["use_number"] == 2
            assert len(requests) == 5  # exactly one navigation per explicit task
        finally:
            runner.close()
        assert not runner._thread.is_alive()
    print("Offline Chrome session smoke passed: reuse and persistent modes keep the same page/context/loop, "
          "fresh control closes before reopening, all requests intercepted, no background queries.")


if __name__ == "__main__":
    run()
