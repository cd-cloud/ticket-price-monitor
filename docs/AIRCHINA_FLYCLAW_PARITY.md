# Fliggy MCP → 国航 Source：迁移清单

基于分支 `feature/flyclaw-airchina` 的 `07bab22551ecb14bc6f98cf932989b8ef0a0779b`，
2026-11-03 对照上游 `sources/fliggy_mcp.py`。这是**部分功能迁移，不是全部能力已上线**。
上游来源及 Apache-2.0 许可保留在 `vendor/flyclaw/NOTICE.md` 和 `LICENSE`。

## 逐项对应

| 上游功能 | 国航实现/限制 |
| --- | --- |
| `query_by_route` 同步接口 | 保留；另有异步接口。异步调用方必须 await，防止嵌套事件循环 |
| IATA、中文/英文城市输入 | 本地机场索引展开，不再只取首机场；机场别名也支持。显式机场码仍只查该机场，不扩大为整座城市 |
| 城市全机场覆盖 | 串行查询机场对，默认最多 4 对，构造参数可设 1–16；超预算明确拒绝、不截断机场。遇阻立即停止，标记不完整覆盖 |
| `cabin` 四舱位 | economy / premium / business / first；premium 映射 premium_economy。使用原国航已识别的四个价格列，不更改产品展开能力 |
| `stops=0/1/2/any` | 作为最大中转次数接收；目前可验证结果仍只有直飞子集。`connection_coverage=unsupported`，不宣称检索到了中转全量 |
| `limit` | 正整数校验；完成可用机场对查询后全局按参考价排序，再截取；记录截断前数量 |
| `timeout` 覆盖 | 整个城市查询共用一个 deadline，各机场对使用剩余预算；不永久改变 Source 默认值。超时取消当前协程并等待清理，不继续下一个机场 |
| 失败返回空列表 | 运行错误/超时没有结果时返回 []；另外提供固定原因的 `last_query`。输入不合法显式抛错，不能伪装无票；取消传播给调用方 |
| `query_by_flight` | 与上游一样不做仅航班号查询；新增明确 unsupported 诊断。已有指定航线/日期的 flight_no 功能仍只适用已验证的舱位产品 |
| FlightRecord | 航班、机场/城市、时间、时长、CNY 参考价、中文舱位、source、segments、trip_type 等。拒绝无效日期、非有限/非正价格、条件冲突 |
| 直飞 segments / layover 字段 | 一个真实可校验航段；经停城市/时长列表为空，max_layover_minutes=0；不把未知中转时间伪造为 0 |
| 多航段、经停机场与等待时长 | **未完成真实采集**：国航现有可验证 DOM 只接受直飞行，不根据两个端点猜中间航段 |
| `return_date`、返程各航段及往返票价 | **未完成真实采集**：显式拒绝；CLI 修复此前不传 return_date、静默变单程的问题。不用两个单程相加冒充官网往返报价 |
| 国际时区/跨日 | 仍受现有 DOM 解析器限制；不能宣称有上游的国际联程覆盖能力 |
| JSON-RPC tools/call、MCP text 解包 | 飞猪传输协议；国航继续使用公共 QueryService → 官网浏览器 → 校验报告，不假设存在相同 MCP 服务 |
| HMAC 签名、Authorization、AES/gzip 上下文、device ID | 飞猪专有协议，不迁移为国航身份、不复制密钥、不模拟其设备指纹 |

## 结果与诊断

- `last_report`：最近一次原生国航实验报告，兼容已有实验 UI。
- `last_query`：整个请求的计划机场对、已尝试机场对、停止原因、截断信息和能力范围；
  此对象不保存浏览器异常原文、Cookie、令牌或请求体。
- `airport_search_complete` 仅表示所有计划机场对已完成可验证参考结果读取，
  不代表全部航班、库存、舱位或票价覆盖；`coverage_complete` 仍为 false。
- 若第二个机场对被验证码阻塞，保留第一个机场对的参考结果，但明确 partial，
  不自动换浏览器/档案或继续第三个机场对。
- 价格保持 `starting_fare / per_adult / tax_included=false / reference_only=true`；
  不能直接和含税最终价比价，更不构成可出票承诺。
- 同一个 Source 不允许并发覆盖诊断。独立国航 CLI 等待 Source 清理完成，不使用
  通用线程池的提前返回，避免命令已结束而浏览器仍在后台查询。

## 离线验收及下一步

`tests/test_airchina_source_contract.py` 覆盖输入、城市预算、四舱位、最大中转过滤语义、
全局排序限制、失败即停、部分覆盖、总超时、取消、并发、损坏记录及报价口径。
`tests/test_flyclaw_integration.py` 覆盖 CLI 城市原文/timeout 传递和往返/人数不静默丢弃。
这些是离线 mock/fixture 验证，**不是新增国航真实查询成功的证据**。

要完成余下能力，需取得脱敏、可核对的国航中转详情及往返选择页样本，包含每航段
机场、日期/时间、舱位、完整行程价格归属和税费说明，再扩展 DOM schema、解析器、
稳定等待和表单流程。当前缺少这类证据，不能通过复制飞猪 JSON 字段来补造。

示例（实际查询仍可能要求人工验证，命令默认遇阻停止）：

```sh
python flyclaw_cli.py search --source airchina --from PEK --to 上海 \
  --date 2026-12-13 --cabin premium --stops 0 --limit 5 --timeout 120
```
