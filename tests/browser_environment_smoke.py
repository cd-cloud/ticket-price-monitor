"""Real Chrome + loopback-only proxy fixtures. No external request forwarding."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backends.chrome import SystemChromeBackend
from browser_environment import parse_browser_environment


def proxy(label):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send_error(self, *args, **kwargs):
            try:
                super().send_error(*args, **kwargs)
            except (BrokenPipeError, ConnectionResetError):
                pass  # Chrome may cancel a refused background request.

        def do_CONNECT(self):
            self.send_error(502)  # Never tunnel/forward any external connection.

        def do_GET(self):
            if self.path != "http://airchina-env-test.invalid/":
                self.send_error(502)
                return
            self.server.hits += 1
            body = f"<html lang='en'><body><h1>{label}</h1></body></html>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.hits = 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


async def run():
    one, t1 = proxy("Network A")
    two, t2 = proxy("Network B")
    old = os.environ.get("SMOKE_LOCAL_PROXY")
    backend = SystemChromeBackend()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            await backend.start(True, None, profile_root=Path(tmp))
            for server, label, marker in [(one, "Network A", "A"), (two, "Network B", "B"), (one, "Network A", "A")]:
                os.environ["SMOKE_LOCAL_PROXY"] = f"http://127.0.0.1:{server.server_port}"
                environment = parse_browser_environment({
                    "viewport": {"width": 1440, "height": 900},
                    "screen": {"width": 1920, "height": 1080},
                    "device_scale_factor": 2,
                    "locale": "en-GB", "timezone_id": "Europe/London",
                    "color_scheme": "dark", "user_agent": "OfflineEnvironmentSmoke/1",
                    "network": {"mode": "proxy", "server_env": "SMOKE_LOCAL_PROXY"},
                })
                options = environment.context_options()
                context = await backend.new_context(
                    profile_name=f"env-{environment.key}",
                    locale=options.pop("locale"), timezone_id=options.pop("timezone_id"),
                    browser_environment_options=options)
                try:
                    page = await backend.new_page(context)
                    await page.goto("http://airchina-env-test.invalid/", timeout=10000)
                    assert await page.locator("h1").inner_text() == label
                    state = await page.evaluate("""() => ({
                        width: innerWidth, height: innerHeight, screen: screen.width,
                        scale: devicePixelRatio, locale: navigator.language,
                        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
                        dark: matchMedia('(prefers-color-scheme: dark)').matches,
                        ua: navigator.userAgent, marker: localStorage.getItem('synthetic-marker')
                    })""")
                    assert state == dict(width=1440, height=900, screen=1920, scale=2,
                        locale="en-GB", timezone="Europe/London", dark=True,
                        ua="OfflineEnvironmentSmoke/1", marker=marker if one.hits == 2 else None), state
                    await page.evaluate("(marker) => localStorage.setItem('synthetic-marker', marker)", marker)
                finally:
                    await backend.close_context(context)
            assert one.hits == 2 and two.hits == 1
            await backend.stop()
    finally:
        await backend.stop()
        one.shutdown()
        two.shutdown()
        one.server_close()
        two.server_close()
        t1.join()
        t2.join()
        if old is None:
            os.environ.pop("SMOKE_LOCAL_PROXY", None)
        else:
            os.environ["SMOKE_LOCAL_PROXY"] = old
    print("PASS: real Chrome emulation, local proxy A/B/A switching, isolated persistent state; no external forwarding.")


if __name__ == "__main__":
    asyncio.run(run())
