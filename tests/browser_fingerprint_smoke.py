"""Real Chrome, temporary profiles, intercepted fixtures only; no airline queries."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backends.chrome import SystemChromeBackend
from browser_environment import parse_browser_environment
from browser_environment_audit import audit_provider, write_report
from browser_fingerprint import fingerprint_script


ORIGIN = "http://127.0.0.1:8769"
DOCUMENT = '<!doctype html><title>Local fixed-property test</title><iframe src="/frame"></iframe>'
WORKER = "postMessage({cpu: navigator.hardwareConcurrency, memory: navigator.deviceMemory ?? null});"
SAMPLE = """() => {
  const result = {
    cpu: navigator.hardwareConcurrency,
    memory: navigator.deviceMemory ?? null,
    ua: navigator.userAgent, platform: navigator.platform, webdriver: navigator.webdriver
  };
  const getter = Object.getOwnPropertyDescriptor(Navigator.prototype, 'hardwareConcurrency').get;
  try { getter.call({}); result.invalidReceiver = false; }
  catch (e) { result.invalidReceiver = e instanceof TypeError; }
  result.gl = {};
  for (const kind of ['webgl', 'webgl2']) {
    const gl = document.createElement('canvas').getContext(kind);
    if (!gl) { result.gl[kind] = null; continue; }
    const before = gl.getParameter(37445);
    const beforeError = gl.getError();
    const ext = gl.getExtension('WEBGL_debug_renderer_info');
    let invalidReceiver = false;
    try { gl.getParameter.call({}, gl.VERSION); }
    catch (e) { invalidReceiver = e instanceof TypeError; }
    result.gl[kind] = {
      before, beforeError, invalidReceiver,
      version: gl.getParameter(gl.VERSION),
      maxTexture: gl.getParameter(gl.MAX_TEXTURE_SIZE),
      debug: ext ? {
        vendor: gl.getParameter(ext.UNMASKED_VENDOR_WEBGL),
        renderer: gl.getParameter(ext.UNMASKED_RENDERER_WEBGL)
      } : null
    };
    gl.getExtension('WEBGL_lose_context')?.loseContext();
  }
  return result;
}"""
READ_WORKER = """() => new Promise((resolve, reject) => {
  const worker = new Worker('/worker.js');
  const timer = setTimeout(() => { worker.terminate(); reject(Error('worker timeout')); }, 5000);
  worker.onmessage = event => { clearTimeout(timer); worker.terminate(); resolve(event.data); };
  worker.onerror = () => { clearTimeout(timer); worker.terminate(); reject(Error('worker failed')); };
})"""


async def run(*, headless=True, report_root=None):
    visited = []
    async def intercept(route):
        url = route.request.url
        visited.append(url)
        if url == ORIGIN + "/main":
            await route.fulfill(content_type="text/html", body=DOCUMENT)
        elif url == ORIGIN + "/frame":
            await route.fulfill(content_type="text/html", body="<!doctype html><title>Local frame</title>")
        elif url == ORIGIN + "/worker.js":
            await route.fulfill(content_type="application/javascript", body=WORKER)
        else:
            await route.abort()

    with tempfile.TemporaryDirectory(prefix="fixed-properties-smoke-") as folder:
        backend = SystemChromeBackend()
        try:
            await backend.start(headless, None, user_data_dir=Path(folder))
            async def open_page(profile, fixed=None):
                env = parse_browser_environment({"fingerprint": fixed})
                context = await backend.new_context(profile_name=profile,
                    browser_environment_options=env.context_options())
                await context.route("**/*", intercept)
                page = await backend.new_page(context)
                await page.goto(ORIGIN + "/main", wait_until="load")
                return context, page

            context, page = await open_page("native")
            baseline = await page.evaluate(SAMPLE)
            worker = await page.evaluate(READ_WORKER)
            await backend.close_context(context)
            # Deliberately artificial values: a test fixture, not a hardware claim.
            fixed = {
                "mode": "fixed-v1",
                "hardware_concurrency": 3 if baseline["cpu"] != 3 else 7,
                "device_memory": 0.5 if baseline["memory"] != 0.5 else 1,
                "webgl": {
                    "vendor": "Synthetic test vendor",
                    "renderer": 'Synthetic renderer "); globalThis.INJECTED = true; // 中文',
                },
            }

            def check(actual):
                assert actual["cpu"] == fixed["hardware_concurrency"]
                assert actual["memory"] == fixed["device_memory"] if baseline["memory"] is not None \
                    else actual["memory"] is None
                assert actual["invalidReceiver"] is True
                for key in ("ua", "platform", "webdriver"):
                    assert actual[key] == baseline[key], key
                for kind in ("webgl", "webgl2"):
                    native, changed = baseline["gl"][kind], actual["gl"][kind]
                    assert (native is None) == (changed is None)
                    if native is None:
                        continue
                    for key in ("before", "beforeError", "invalidReceiver", "version", "maxTexture"):
                        assert native[key] == changed[key], (kind, key)
                    if native["debug"] is None:
                        assert changed["debug"] is None
                    else:
                        assert changed["debug"] == fixed["webgl"], kind

            for round_number in range(2):  # Close/reopen the same isolated test profile.
                context, page = await open_page("fixed", fixed)
                for _ in range(2):
                    check(await page.evaluate(SAMPLE))
                    frame = next(f for f in page.frames if f.url == ORIGIN + "/frame")
                    check(await frame.evaluate(SAMPLE))
                    assert await page.evaluate(READ_WORKER) == worker, "Workers must remain native"
                    assert await page.evaluate("typeof globalThis.INJECTED") == "undefined"
                    # Reinstallation must not stack overrides or alter values.
                    await page.evaluate(fingerprint_script(fixed))
                    check(await page.evaluate(SAMPLE))
                    await page.goto(ORIGIN + "/main", wait_until="load")
                await backend.close_context(context)
            context, page = await open_page("native")
            assert await page.evaluate(SAMPLE) == baseline, "Disabled/native profile must remain unchanged"
            await backend.close_context(context)
            assert all(u.startswith(ORIGIN + "/") for u in visited), "Unexpected page request"
        finally:
            await backend.stop()

    provider = SimpleNamespace(locale="zh-CN", timezone="Asia/Shanghai", extra_headers={},
        browser_environment=parse_browser_environment({"preset": "desktop-cn-v1", "fingerprint": fixed}))
    report = await audit_provider(provider, headless=headless, configured_headless=False)
    assert report["result"] == "no_observed_differences"
    assert "Synthetic" not in json.dumps(report)
    if report_root:
        report["test_fixture"] = True
        report["limitations"].insert(0, "这是合成测试配置，未启用到正式查询配置。")
        folder = write_report(report, report_root)
        print(f"Local synthetic report: {folder / 'report.html'}")
    supported = sum(bool(v and v["debug"]) for v in baseline["gl"].values())
    print(f"PASS: fixed CPU/memory, {supported}/2 WebGL APIs available and checked; "
          "iframe, two loads/restarts, native worker, opt-out, redacted audit. No airline query.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--report-root", type=Path)
    args = parser.parse_args()
    asyncio.run(run(headless=not args.headed, report_root=args.report_root))
