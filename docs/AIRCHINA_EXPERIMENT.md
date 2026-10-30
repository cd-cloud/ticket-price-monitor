# 国航实验任务架构与携程对照

更新日期：2026-10-30。此功能不是正式报价/甩尾适配已验收。

## 六层链路

```text
网页「国航实验」 / JSON API
  → ExperimentalQueryService（后台任务、公共 run_lock、人工继续、取消）
  → QueryService.inspect_airchina_experiment（总时限、浏览器生命周期）
  → BrowserAutomation / BrowserSessionManager / ProviderRuntime（公共设施）
  → providers/airchina.py（一次导航及查询编排）
      → airchina_page.py（入口校验、验证码/限制/过期/提示状态）
      → airchina_result_wait.py（有界等待，两次完整 DOM 投影一致）
      → airchina_parser.py / airchina_products.py（航班/舱位/产品绑定）
      → airchina_result.py（参考结果组装，不生成正式 price）
  → PriceRepository.experimental_query_jobs（独立任务/参考结果记录）
```

没有另复制一套浏览器引擎、SQLite 连接或公共查询锁。实验查询与普通查询、
甩尾任务互斥；不读取任何平台的加密凭据或 Fliggy key。
参考结果不写入 `price_snapshots` / 甩尾结果，不参与报表、比价、路线学习和负缓存。
加载/过期/人工提示不可作为有效列表；若产品读取阶段失败，丢弃此前列表预览，
不让早先读到的价格继续冒充本次有效结果。

## 用法与生命周期

网页入口为顶部的「国航实验」。配置需包含 `airchina` 项，但保持
`enabled=false`，不需要启用普通监控。仅接受明确机场、未来单程日期、
1 成人、经济/公务舱；可选指定航班产品，否则只读列表。
总时限 240 秒，包含浏览器启动、人工等待与读取；取消/超时后关闭上下文。
遇到验证码或乘机提示，任务停在 `waiting_user`，用户在**项目浏览器**
处理后点击继续，读取同一个页面，不重新搜索。最多接受 3 次人工继续。
应用关闭会请求取消；重新启动会把遗留活动任务标为 `interrupted`，
不会假装恢复旧浏览器会话，也不会自动重新提交。
部署范围是现有单进程本地应用；不提供跨进程分布式互斥。

API：

- `POST /api/experiments/airchina`：白名单字段
  `origin / destination / departure_date / cabin / flight_no`，返回 202 和任务 ID。
- `GET /api/experiments/airchina`：最近任务；
  `GET /api/experiments/airchina/{id}`：详情。
- `POST /api/experiments/airchina/{id}/continue`：当前任务等待人工处理时有效。
- `POST /api/experiments/airchina/{id}/cancel`：取消当前任务。
- 共用现有本地 Origin 校验及可选 UI token 校验；忙时 409。
- `completed_reference` 仅表示参考票价，不是完整报价成功；
  `inconclusive` 不是“无票”。断网不自动重复 POST，须刷新确认服务端状态。

CLI 诊断仍可用，输出仍在忽略的 `runtime/airchina_probes` 中；
网页任务不依赖这些文件，不记录整页文本、截图、网络响应或任意异常消息。

## 与携程查询方式逐项对照

| 项目 | 对齐方式 | 保留的区别 |
|---|---|---|
| 浏览器启动/后端 | 共用 BrowserSessionManager 与后端注册表 | 不因验证码切换后端重新搜索 |
| 请求头注入 | 共用 `provider.extra_headers → backend.new_context` | 不复制携程 Cookie、认证头、Host、Origin、Referer |
| 浏览器环境 | 两者示例均为 `zh-CN`、`Asia/Shanghai` | 由各自配置和浏览器产生实际站点请求 |
| 通用请求头 | 两者示例均仅手动设置 `Accept-Language: zh-CN,zh;q=0.9` | UA 等保持浏览器默认，不假造国航专属签名 |
| 会话 | 共用已保存会话/持久 profile 机制 | `ctrip_state.json` 与 `airchina_state.json` 隔离；跨次有效性待实测 |
| 节流/页面超时 | 两者示例均 4–8 秒节流、45 秒页面超时 | 实验任务另有含人工等待的 240 秒总时限 |
| 入口 | 两者都有已观察的结果页直达入口 | 国航暂未实现表单 fallback，不照抄携程表单选择器 |
| 等待 | 有界等待和稳定证据，不以固定 sleep 判定成功 | 国航比较完整 DOM 投影；携程还利用响应代次/异步批次 |
| 解析 | 行程条件与航班及价格绑定 | 国航目前只校准可见 DOM，不套用携程结构化响应字段 |
| 失败 | 限流/验证/过期/上下文变化均停止或请求人工处理 | 不复制携程 profile 轮换、页面补救及通用价格兜底 |
| 结果 | 明确来源、价格类型、覆盖范围 | 国航基础票价不冒充正式 `ProviderQueryOutcome.price` |

“尽可能复用”指复用机制与校验标准，不是把携程的站点身份、
接口路径、签名字段、页面 DOM 或金额口径原样搬到国航。
仍待真实样本验证：表单查询补救、结构化响应解析、往返/联程、
税费总价、无结果状态与跨次会话复用。没有声称这些已实现。

## 测试

新增离线回归覆盖任务接收、共享锁、人工继续、取消、总时限、
资源关闭、错误脱敏、重启中断、接口校验和不进入报价表。

```sh
python -m pytest tests/test_experimental_queries.py tests/test_airchina_adapter.py -q
python tests/browser_airchina_smoke.py --output-dir /tmp/airchina-ui-smoke
```

浏览器 smoke 需要系统 Chrome，使用全请求拦截的模拟 API 验证网页；
不访问航司，截图也不是实时航司报价。
