# Flight Price Tracker

这是一个只在本地运行的个人航班价格监控工具，基于 Python、Playwright、FastAPI 和 SQLite。它用于查询自己可访问的航班价格，保存历史记录，并在本地 Web UI 中管理航线、查看最新结果和导出报告。

## 当前能力

- 本地 Web UI 管理单程、往返、多程、多城市组合航线。
- 支持携程查询，城市输入支持中文城市名、机场名和三字码。
- 支持舱位、直飞/中转、人数和航司偏好筛选。
- 支持手动查询单条航线、整组航线或全部航线。
- 支持每条航线开启 12 小时自动查询，不同航线之间至少间隔 10 分钟。
- 查询结果写入本地 SQLite，并生成 CSV 和 Markdown 报告。
- 凭证、浏览器 profile 和登录状态只保存在本机。

## 常用命令

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
python main.py init-config
python main.py serve-ui --host 127.0.0.1 --port 8765
```

打开：

```text
http://127.0.0.1:8765
```

手动运行查询：

```powershell
python main.py run-once --provider ctrip
```

生成报告：

```powershell
python main.py report
```

导出 CSV：

```powershell
python main.py export-csv
```

## 浏览器后端策略

默认使用本机 Chrome，并使用持久化 profile 保存站点状态。备用后端是 Playwright Chromium。配置项在 `config.json` 的 `defaults.browser_backend` 和 `defaults.browser_backend_fallbacks`；如果默认后端是 Chrome 但没有显式配置 fallback，程序会自动补 `playwright` 作为备用。

携程流程会优先用 Chrome persistent profile 直接打开结果页。Ctrip 不再强制先走账号密码登录，因为实际查询页可以在未预登录状态下完成；如果站点需要人工处理登录或验证，可以先通过 session bootstrap/手动浏览器 profile 让状态保留下来。

诊断接口：

```text
GET /api/browser-session/diagnostics
```

它会返回当前默认 backend、每个 provider 的 backend 顺序、Chrome profile 路径和是否需要登录提示。

## 查询可信度

每条结果的 `raw_payload` 会记录解析来源：

- `parser`: 例如 `ctrip_single_network`、`ctrip_single_visible`、`ctrip_multi_city_network`。
- `parser_confidence`: `high`、`medium` 或 `low`。
- `detail_quality`: `complete`、`partial` 或 `price_only`。
- `detail_source`: 例如 `network_exact`、`network_low_price_calendar`、`visible_row`。

多程查询会优先打开携程多程结果页，例如 `multi-bjs-ctu-ctu-sin`，并优先使用 network itinerary。拿不到多程 itinerary 时，不再误用单程低价日历价格。

## 目录结构

```text
main.py                    # CLI 入口
web_ui.py                  # FastAPI 路由
app_service.py             # 应用编排
browser_automation.py      # 浏览器自动化协调
backends/                  # 浏览器后端
providers/                 # 各平台查询与解析
static/                    # Web UI JS/CSS
templates/                 # Web UI 模板
data_storage.py            # SQLite 读写
analyzer.py                # CSV 和 Markdown 报告
runtime/                   # 本地运行数据，不应提交
output/                    # 导出报告，不应提交
```

## 注意事项

- 只查询自己的账号和个人使用场景。
- 避免高频刷新网站页面。
- 网站页面结构可能变化；查询失败时优先看 `runtime/flight_tracker.log`、`runtime/tail_diagnostics/` 和 `runtime/tail_traces/`。
