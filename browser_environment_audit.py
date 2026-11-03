"""Local-only browser consistency audit. No live site, profile or access-gate use."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import html
import json
from pathlib import Path
import re
import tempfile
from uuid import uuid4

from backends.chrome import SystemChromeBackend
from browser_environment import environment_summary


AUDIT_URL = "http://127.0.0.1:8769/browser-environment-audit"
PAGE = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy"
 content="default-src 'none'; style-src 'unsafe-inline'; connect-src 'none'">
<title>本地浏览器环境采样</title><body><h1>本地浏览器环境采样</h1>
<p>合成页面；请求被本地拦截，不连接航空公司或代理。</p></body></html>"""
# Bounded public browser properties only, never cookies/storage/account fields,
# font enumeration, canvas/audio rendering or high-entropy UA-CH requests.
SAMPLE = """() => ({
  viewport: {width: innerWidth, height: innerHeight},
  screen: {width: screen.width, height: screen.height},
  device_scale_factor: devicePixelRatio,
  has_touch: navigator.maxTouchPoints > 0,
  locale: navigator.language,
  timezone_id: Intl.DateTimeFormat().resolvedOptions().timeZone,
  color_scheme: matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' :
    matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'no-preference',
  user_agent: navigator.userAgent.slice(0, 512),
  platform: navigator.platform.slice(0, 64),
  ua_platform: navigator.userAgentData?.platform?.slice(0, 64) || null
})"""
HEADERS = ("user-agent", "accept-language", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform")

# Only run for explicit fixed overrides; GPU strings never leave the page.
FINGERPRINT_SAMPLE = """config => {
  const result = {};
  if (config.hardware_concurrency !== undefined)
    result.hardware_concurrency = navigator.hardwareConcurrency ?? null;
  if (config.device_memory !== undefined)
    result.device_memory = navigator.deviceMemory ?? null;
  if (config.webgl) {
    result.webgl = {};
    for (const kind of ['webgl', 'webgl2']) {
      const gl = document.createElement('canvas').getContext(kind);
      const ext = gl?.getExtension('WEBGL_debug_renderer_info');
      result.webgl[kind] = ext ? {
        vendor_matches: gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) === config.webgl.vendor,
        renderer_matches: gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) === config.webgl.renderer
      } : null;
      gl?.getExtension('WEBGL_lose_context')?.loseContext();
    }
  }
  return result;
}"""


def platform_family(value):
    text = str(value or "").lower()
    for family, tokens in (("Android", ("android",)), ("iOS", ("iphone", "ipad")),
                           ("Windows", ("windows", "win32", "win64")),
                           ("macOS", ("macintosh", "macintel", "macos", "mac os")),
                           ("Linux", ("linux",))):
        if any(token in text for token in tokens):
            return family
    return None


def language(value):
    # Return only a language tag, never arbitrary header or configuration text.
    token = str(value or "").split(",")[0].split(";")[0].strip()
    return token if re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{2,8})*", token) else None


def build_report(provider, samples, header_samples, *, headless, configured_headless):
    options = (provider.browser_environment.context_options()
               if getattr(provider, "browser_environment", None) else {})
    expected = {**{"locale": provider.locale, "timezone_id": provider.timezone}, **options}
    rows = []

    def row(name, status, wanted, actual, note=""):
        rows.append(dict(name=name, status=status, expected=wanted, observed=actual, note=note))

    labels = {"viewport": "视口尺寸", "screen": "屏幕尺寸", "device_scale_factor": "像素缩放",
              "locale": "语言", "timezone_id": "时区", "color_scheme": "主题", "has_touch": "触控能力"}
    first = samples[0]
    for key, label in labels.items():
        wanted, actual = expected.get(key), first.get(key)
        # Equality is against what the engine reports. Timezone aliases and
        # no-preference may normalize; mismatch is not proof of website risk.
        status = "unconfigured" if wanted is None else "pass" if wanted == actual else "mismatch"
        row(label, status, wanted, actual,
            "浏览器可能规范化时区别名或 no-preference；差异不等于风险判定。" if key in {"timezone_id", "color_scheme"} else "")
    ua = first.get("user_agent")
    row("自定义 UA", "unconfigured" if "user_agent" not in options else
        "pass" if options["user_agent"] == ua else "mismatch",
        "自定义（原文不保存）" if "user_agent" in options else "浏览器默认",
        "已读取（原文不保存）")
    # Check both loads, not only the first snapshot.
    ua_pairs = [(s.get("user_agent"), h.get("user-agent")) for s, h in zip(samples, header_samples)]
    row("请求 UA 与 JS UA", "unavailable" if any(not a or not b for a, b in ua_pairs) else
        "pass" if all(a == b for a, b in ua_pairs) else "mismatch",
        "两次均一致", "原文不保存", "只读取拦截到的合成页面请求，不代表经过代理后的真实请求头。")
    locale_pairs = [(language(s.get("locale")), language(h.get("accept-language")))
                    for s, h in zip(samples, header_samples)]
    row("请求语言与 JS 语言", "unavailable" if any(not a or not b for a, b in locale_pairs) else
        "pass" if all(a.lower() == b.lower() for a, b in locale_pairs) else "mismatch",
        [a for a, _ in locale_pairs], [b for _, b in locale_pairs])
    families = {
        "UA": platform_family(ua), "JS": platform_family(first.get("platform")),
        "UA-CH": platform_family(first.get("ua_platform")),
        "请求 UA-CH": platform_family(header_samples[0].get("sec-ch-ua-platform")),
    }
    known = {v for v in families.values() if v}
    row("平台类别一致性", "mismatch" if len(known) > 1 else
        "unavailable" if any(v is None for v in families.values()) else "pass",
        "可识别类别相同", families, "只比较粗粒度类别；缺少 UA-CH 或无法识别时不标为通过。")
    row("同一上下文两次采样", "pass" if samples[0] == samples[1] else "mismatch",
        "稳定", "稳定" if samples[0] == samples[1] else "存在差异",
        "两次合成页面导航，不是跨重启、跨网络或跨网站稳定性测试。")
    fingerprint = options.get("fingerprint")
    if fingerprint:
        observed = [s.get("fingerprint", {}) for s in samples]
        for key, label in (("hardware_concurrency", "固定 CPU 核数"),
                           ("device_memory", "固定设备内存")):
            if key not in fingerprint:
                continue
            values = [s.get(key) for s in observed]
            row(label, "unavailable" if any(v is None for v in values) else
                "pass" if all(v == fingerprint[key] for v in values) else "mismatch",
                fingerprint[key], values, "只覆盖 Window/iframe；Worker 未修改。")
        if "webgl" in fingerprint:
            for kind in ("webgl", "webgl2"):
                values = [s.get("webgl", {}).get(kind) for s in observed]
                row(f"固定 {kind.upper()} 标识",
                    "unavailable" if any(v is None for v in values) else
                    "pass" if all(v.get("vendor_matches") is True and
                                  v.get("renderer_matches") is True for v in values) else "mismatch",
                    "两次 vendor/renderer 均匹配", values,
                    "仅比较扩展标识，不保存 GPU 字符串；不修改渲染结果、Worker 或硬件。")
    failed = any(r["status"] == "mismatch" for r in rows)
    return {
        "schema": "browser_environment_audit_v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "result": "differences_found" if failed else "no_observed_differences",
        "headless": headless, "configured_headless": configured_headless,
        "launch_mode_matches_config": headless == configured_headless,
        "environment": environment_summary(provider),
        "temporary_profile": True, "proxy_applied": False,
        "live_site_queried": False, "rows": rows,
        "limitations": [
            "本报告只验证本机 Chrome 参数和有限的一致性，不是“指纹通过”或免验证码证明。",
            "使用临时档案，不读取/复制正式档案、Cookie、token、历史或国航暂停状态。",
            "代理配置不用于检测；不测试出口 IP、TLS、真实请求链路或服务器风控。",
            "不枚举字体或采集 Canvas 像素、音频指纹；只有显式配置时才注入固定属性脚本并比较 WebGL 标识。",
            "固定属性只覆盖新文档的 Window/iframe，Worker 等环境未修改，可能存在可检测差异；不保证免验证码。",
            "headless 模式与正式有头国航实验不相同时，结果不能直接代表正式会话。",
        ],
    }


def render_report(report):
    def value(v):
        return html.escape("未指定 / 不可用" if v is None else
                           json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v)
    states = {"pass": "一致", "mismatch": "有差异", "unconfigured": "未配置",
              "unavailable": "不可验证"}
    rows = "".join(
        f"<tr><th>{value(r['name'])}</th><td>{states[r['status']]}</td>"
        f"<td>{value(r['expected'])}</td><td>{value(r['observed'])}</td>"
        f"<td>{value(r['note'])}</td></tr>" for r in report["rows"])
    limits = "".join(f"<li>{html.escape(t)}</li>" for t in report["limitations"])
    result = "发现配置/暴露值差异" if report["result"] == "differences_found" else "已检查项目未发现差异"
    return f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>本地浏览器环境一致性检测</title><style>
body{{font:16px/1.6 system-ui,sans-serif;background:#f4f7fa;color:#172c3c;margin:32px auto;max-width:1200px;padding:0 20px}}
h1{{font-size:28px}}section{{background:white;padding:24px;border-radius:12px;margin:20px 0}}
.table{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;font-size:14px}}
th,td{{text-align:left;padding:12px;border-bottom:1px solid #dce4e9;overflow-wrap:anywhere}}
th{{min-width:120px}}small{{color:#526777}}</style>
<h1>本地浏览器环境一致性检测</h1><small>{value(report['created_at'])} · 合成页面 · 不访问国航</small>
<section><h2>{result}</h2><p>采样模式：{"无头" if report["headless"] else "有头"}；
使用临时档案；代理未连接。此报告不是全套指纹验证。</p></section>
<section class="table"><table><thead><tr><th>检测项</th><th>状态</th><th>预期</th><th>实际</th><th>说明</th></tr></thead>
<tbody>{rows}</tbody></table></section><section><h2>检测边界</h2><ul>{limits}</ul></section></html>"""


async def audit_provider(provider, *, headless=False, configured_headless=False):
    """All page requests fulfilled/aborted locally, independent of live sessions."""
    backend = SystemChromeBackend()
    samples, headers = [], []
    with tempfile.TemporaryDirectory(prefix="browser-environment-audit-") as folder:
        try:
            await backend.start(headless, None, user_data_dir=Path(folder))
            options = (provider.browser_environment.context_options()
                       if getattr(provider, "browser_environment", None) else {})
            options.pop("proxy", None)  # Never connect even a configured proxy.
            locale = options.pop("locale", provider.locale)
            timezone_id = options.pop("timezone_id", provider.timezone)
            # Preserve only browser-identity header overrides for comparison.
            # No auth/cookie/custom headers are sent even to the synthetic page.
            selected_headers = {k: v for k, v in provider.extra_headers.items() if k.lower() in HEADERS}
            context = await backend.new_context(profile_name="audit",
                locale=locale, timezone_id=timezone_id, extra_http_headers=selected_headers or None,
                browser_environment_options=options)

            async def intercept(route):
                if route.request.url != AUDIT_URL:
                    await route.abort()
                    return
                # Read only the exact allowlist; never all_headers() or body.
                headers.append({name: await route.request.header_value(name) for name in HEADERS})
                await route.fulfill(status=200, content_type="text/html", body=PAGE)

            await context.route("**/*", intercept)
            page = await backend.new_page(context)
            for _ in range(2):
                await page.goto(AUDIT_URL, wait_until="load", timeout=10000)
                sample = await page.evaluate(SAMPLE)
                if options.get("fingerprint"):
                    sample["fingerprint"] = await page.evaluate(FINGERPRINT_SAMPLE, options["fingerprint"])
                samples.append(sample)
            if len(headers) != 2:
                raise RuntimeError("audit_samples_incomplete")
            return build_report(provider, samples, headers, headless=headless,
                                configured_headless=configured_headless)
        finally:
            await backend.stop()


def write_report(report, output_root):
    folder = Path(output_root) / uuid4().hex
    folder.mkdir(parents=True, mode=0o700)
    for name, body in (("report.json", json.dumps(report, ensure_ascii=False, indent=2)),
                       ("report.html", render_report(report))):
        path = folder / name
        # New unique report folder: no arbitrary overwrite.
        with path.open("x", encoding="utf-8") as handle:
            handle.write(body)
        path.chmod(0o600)
    return folder


async def run_audit(config, *, headless=False):
    provider = config.providers.get("airchina")
    if provider is None:
        print("配置中缺少国航；未启动检测。")
        return 2
    try:
        async with asyncio.timeout(60):
            report = await audit_provider(provider, headless=headless, configured_headless=config.headless)
        folder = write_report(report, config.runtime_dir / "browser_environment_audits")
    except Exception:
        print("本地环境检测失败；请检查 Chrome 和环境配置。未连接代理或查询国航。")
        return 2
    print(f"本地检测完成：{folder.resolve() / 'report.html'}")
    print("存在差异" if report["result"] == "differences_found" else "已检查项目未发现差异（非免验证码证明）")
    return 1 if report["result"] == "differences_found" else 0
