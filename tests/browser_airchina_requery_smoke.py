"""Real Chrome, synthetic result/form DOM; every request intercepted offline."""
import asyncio
from datetime import date, timedelta
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playwright.async_api import async_playwright
from config_manager import RouteQuery
from providers.airchina_form import validate_entry
from providers.airchina_requery import ResultsRequery
from providers.airchina_result_wait import wait_for_results
from providers.airchina_network_diagnostics import NetworkDiagnostics


def html(route, scenario):
    d = date.fromisoformat(route.departure_date)
    return f"""<!doctype html><html lang="zh"><meta charset="utf-8"><body data-submissions="0">
    <div class="style_resultInfoBox__mock">
      <div class="style_cityTest__mock">北京首都(PEK) 上海虹桥(SHA)</div>
      <div class="style_dateBox__mock">{d.year}年{d.month}月{d.day}日</div>
      <div class="style_tripType__mock">单程</div><div class="style_passengerBox__mock">1成人</div>
    </div>
    <div id="toggle">重新查询</div><section id="form" hidden>
      <div id="tabCurrent" data-id="oneWay">单程</div>
      <div class="input_inputContainer__mock"><span class="input_imageMargin__mock">出发机场</span>
        <input value="北京首都国际机场"><span class="input_text__mock">PEK</span></div>
      <div class="input_inputContainer__mock"><span class="input_imageMargin__mock">到达机场</span>
        <input value="上海虹桥机场"><span class="input_text__mock">SHA</span></div>
      <input placeholder="出发日期" readonly value="{route.departure_date}">
      <div class="select_container__mock"><div class="select_title__mock">乘客类型</div>
        <div class="select_contentContainer__mock">1成人0儿童0婴儿</div></div>
      <div class="select_container__mock"><div class="select_title__mock">伤残军警乘客</div>
        <div class="select_contentContainer__mock">0残疾军人/伤残人民警察</div></div>
      <div class="searchCore_searchButton__mock">查询</div>
    </section>
    <div class="searchTitle_peiceTips__mock">以下价格为每位成人旅客不含税票价</div>
    <div class="cabinTitle_first__mock">头等舱</div>
    <div class="cabinTitle_business__mock">公务舱</div>
    <div class="cabinTitle_superEconomy__mock">超级经济舱</div>
    <div class="cabinTitle_economy__mock">经济舱</div>
    <div id="list"><div class="style_singleFightWrapper__mock">
      <span class="shared-flights_number__mock">CA1549</span>
      <div class="baseContent_departure__mock">16:30</div>
      <div class="baseContent_arrive__mock">18:45</div>
      <div class="baseContent_airport-departure__mock">PEK T3</div>
      <div class="baseContent_airport-arrive__mock">SHA T2</div>
      <div class="baseContent_totalTime__mock">2h15m</div>
      <div class="baseContent_between__mock"></div>
      <div class="cabinContent_first__mock">--</div>
      <div class="cabinContent_business__mock">--</div>
      <div class="cabinContent_superEconomy__mock">--</div>
      <div class="cabinContent_economy__mock"><span class="cabinContent_price__mock">￥600起</span></div>
    </div></div><div id="status"></div>
    <script>
    const scenario = {json.dumps(scenario)};
    const form = document.querySelector('#form'), list = document.querySelector('#list');
    document.querySelector('#toggle').onclick = e => {{
      form.hidden = false; e.target.textContent = "收起查询";
    }};
    if (scenario === "wrong_conditions")
      document.querySelector('input[placeholder="出发日期"]').value = "2099-01-01";
    document.querySelector('.searchCore_searchButton__mock').onclick = () => {{
      document.body.dataset.submissions = String(Number(document.body.dataset.submissions) + 1);
      if (scenario === "no_transition") return;
      if (scenario === "challenge") {{
        document.querySelector('#status').textContent = "请完成验证"; return;
      }}
      const previous = list.innerHTML;
      list.replaceChildren();
      if (scenario === "loading")
        document.querySelector('#status').textContent = "正在加载中，请稍候";
      setTimeout(() => {{
        list.innerHTML = previous; document.querySelector('#status').textContent = "";
      }}, 350);
    }};
    </script></body></html>"""


async def run():
    route = RouteQuery("PEK", "SHA", (date.today() + timedelta(days=10)).isoformat())
    target = validate_entry(route, "results")
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        for scenario in ("loading", "cleared", "no_transition", "challenge", "wrong_conditions"):
            context = await browser.new_context()
            requests = []
            async def intercept(request):
                requests.append(request.request.url)
                await request.fulfill(content_type="text/html", body=html(route, scenario))
            await context.route("**/*", intercept)
            page = await context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(target)
            audit = NetworkDiagnostics()
            audit.attach(page)
            result = await ResultsRequery(page, route).run(15000)
            assert len(requests) == 1, requests  # no reload or second document
            assert not errors, errors
            expected = ("query_submitted" if scenario in {"loading", "cleared"} else
                        "needs_verification" if scenario == "challenge" else "form_entry_unverified")
            assert result["state"] == expected, result
            count = await page.locator("body").get_attribute("data-submissions")
            assert count == ("0" if scenario == "wrong_conditions" else "1")
            if expected == "query_submitted":
                waited = await wait_for_results(page, route, expected_url=target, timeout_ms=3000, poll_ms=100)
                assert waited["state"] == "reference_fares_observed", waited
                assert waited["wait_evidence"]["stable_samples"] == 2
            else:
                assert not result["entry_evidence"]["refresh_observed"]
            assert audit.snapshot()["lifecycle"]["main_document_requests"] == 0
            audit.close("unknown")
            await context.close()
        await browser.close()
    print("Air China requery offline smoke passed: retained document, one visible submit, "
          "loading/clearing required before equal-price read, no-op/challenge/mismatch fail closed.")


if __name__ == "__main__":
    asyncio.run(run())
