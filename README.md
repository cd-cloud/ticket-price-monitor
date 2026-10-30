# Flight Price Tracker

这是一个只在本地运行的个人航班价格监控工具，基于 Python、Playwright、FastAPI 和 SQLite。它用于查询自己可访问的航班价格，保存历史记录，并在本地 Web UI 中管理航线、查看最新结果和导出报告。

## 当前能力

- 国航新增独立的「国航实验」网页任务入口：单程参考票价、进度、人工继续、
  取消和独立历史记录。复用公共浏览器与查询互斥，但不进入正式报价或甩尾比价。
  与携程查询机制的对照及限制见 [国航实验架构](docs/AIRCHINA_EXPERIMENT.md)。

- 本地 Web UI 管理单程、往返、多程、多城市组合航线。
- 支持携程查询，城市输入支持中文城市名、机场名和三字码。
- 支持舱位、直飞/中转、人数和航司偏好筛选。
- 支持手动查询单条航线、整组航线或全部航线。
- 支持每条航线开启 12 小时自动查询，不同航线之间至少间隔 10 分钟。
- 查询结果写入本地 SQLite，并生成 CSV 和 Markdown 报告。
- 凭证、浏览器 profile 和登录状态只保存在本机。
- 甩尾查询先过滤再选优，按首段航班展示多个尾段方案。
- 甩尾可一次并行查询携程和飞猪，分别展示平台状态，并统一查看、按平台筛选结果。
- 支持断点继续、刷新已发现报价、单行程复核与城市/时间/结果数预算。
- 报价区分可比基准和仅供参考，显示采集时效及人数、税费、票规待核实项。

甩尾执行模式、数据迁移和测试说明见 [docs/TAIL_DISCOVERY.md](docs/TAIL_DISCOVERY.md)。
新版甩尾工作区、任务结果隔离、断线恢复与 UI 测试见 [docs/TAIL_UI_WORKSPACE.md](docs/TAIL_UI_WORKSPACE.md)。
双平台查询的检索方式、独立预算、失败恢复与接口见 [docs/TAIL_MULTI_PROVIDER.md](docs/TAIL_MULTI_PROVIDER.md)。
国航诊断已支持单程起价与指定航班经济舱/公务舱产品读取，明确标记每成人不含税票价；**尚不支持保存正式报价或甩尾查询**。票价口径与分阶段验收见 [docs/AIRCHINA_INTEGRATION.md](docs/AIRCHINA_INTEGRATION.md)。

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

携程普通查询（单程、往返、多程）现在只保存符合实际航线、日期、舱位和航司条件的结构化行程报价。
价格与航班详情绑定为同一个快照，页面后续响应不会替换已选报价的航班。
低价日历和纯页面文字不再兜底为已确认报价；缺少证据时会报告无法确认，而不是按查询条件补全事实。
旧数据中的 `visible_row`、`network_low_price_calendar` 等来源标签仍保留，不自动升级。
查询批次隔离与迁移说明见 [docs/CTRIP_QUERY_INTEGRITY.md](docs/CTRIP_QUERY_INTEGRITY.md)。

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

## 自动测试

每次推送、创建或更新 PR 都会运行 GitHub Actions 的 `Automated tests`：

- `Python regression`：Python 3.12 全量回归，并保存 7 天的 JUnit 测试报告。
- `Frontend regression`：Node.js 24 的 JavaScript 语法检查和前端回归。

不需要配置飞猪 API key 或其他平台账号，也不安装 FlyAI CLI。
测试使用模拟平台响应；Python 测试只允许连接本机回环地址。
在仓库 **Actions → Automated tests** 查看结果，或通过 **Run workflow** 手动重跑。
详细范围和本地复现命令见 [docs/TESTING.md](docs/TESTING.md)。

## 注意事项

- 只查询自己的账号和个人使用场景。
- 避免高频刷新网站页面。
- 网站页面结构可能变化；查询失败时优先看 `runtime/flight_tracker.log`、`runtime/tail_diagnostics/` 和 `runtime/tail_traces/`。
