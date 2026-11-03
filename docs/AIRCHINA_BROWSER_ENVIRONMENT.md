# 国航浏览器环境模拟与网络切换

本功能只配置国航实验使用的系统 Chrome。不是免验证码或全套指纹伪装：
国航默认注入固定 CPU 8 核 / 设备内存 8 GiB 暴露值脚本，可显式关闭；
WebGL 标识覆盖仍需显式配置，默认保持原生 GPU 标识。
不修改 Canvas 像素或音频，不新增 stealth 参数，不自动轮换 IP/档案。
使用 Playwright 浏览器上下文参数和有限的新文档脚本，不修改系统代理、个人浏览器或其他查询源。

## 默认行为及环境配置

配置加载规则仅对国航生效，不改变携程、飞猪：

- `providers.airchina.browser_environment` **缺失**或为 `{}` 时，
  默认使用 `{"mode":"fixed-v1","hardware_concurrency":8,"device_memory":8}`。
- 环境对象未填写 `fingerprint` 时使用上述默认值；显式指纹对象完整覆盖默认值，不隐式补全字段。
- `browser_environment: null` 完全关闭环境覆盖，恢复旧命名空间及原有请求头行为。
- `browser_environment.fingerprint: null` 仅关闭指纹脚本，保留显示/语言/网络设置。
- 默认改变不会自动启用国航正式报价源、启动查询或解除已有暂停。

如需额外配置显示参数，在本地 `config.json` 的 `providers.airchina` 中添加：

```json
"browser_environment": {
  "viewport": {"width": 1440, "height": 900},
  "screen": {"width": 1920, "height": 1080},
  "device_scale_factor": 1,
  "locale": "zh-CN",
  "timezone_id": "Asia/Shanghai",
  "color_scheme": "light",
  "network": {"mode": "system"}
}
```

- 可选项：`viewport`、`screen`、`device_scale_factor`、`locale`、
  `timezone_id`、`color_scheme`、`has_touch`、`user_agent`、`preset`、`fingerprint` 和 `network`。
- `screen` 必须与 `viewport` 一起设置且不小于后者；宽高 320–7680，
  缩放 0.5–4；主题允许 light/dark/no-preference；时区需为有效 IANA 名称。
- 未指定语言/时区时继承供应商配置；未指定 UA 时使用真实 Chrome UA（推荐）。
  自定义 UA 只用于兼容性测试，不会同步伪造系统、TLS、Canvas 或所有 Client Hints。
- 启用本功能时移除额外请求头里的手写 UA、Accept-Language 和三种常见 UA
  Client Hint 覆盖，让浏览器生成，避免与上下文参数相互矛盾。
- `system` 表示不覆盖 Chrome 的网络配置，**不是强制直连**：
  系统代理或 VPN 仍可能生效；本功能不验证出口 IP。
- 不接受任意浏览器启动参数、自定义 JS 代码或脚本路径，未知字段会报错。

## 固定指纹实验（默认开启，可关闭）

默认值由配置加载器应用；底层浏览器接口不会自行补全，必须使用配置解析后的选项。
下面是**自定义合成测试参数**，不是匹配真实设备的建议；
其中 WebGL 字符串不属于默认配置，不应直接作为真实 GPU 身份使用：

```json
"browser_environment": {
  "preset": "desktop-cn-v1",
  "fingerprint": {
    "mode": "fixed-v1",
    "hardware_concurrency": 8,
    "device_memory": 8,
    "webgl": {
      "vendor": "Synthetic test vendor",
      "renderer": "Synthetic test renderer"
    }
  },
  "network": {"mode": "system"}
}
```

- `mode` 必须是 `fixed-v1`，另外至少指定一个字段。CPU 允许整数 1–64；
  内存允许 0.25、0.5、1、2、4、8；WebGL 必须同时提供非空 vendor/renderer，
  单项最长 256 字符且不能含控制字符。不接受随机模式、任意注入脚本或失败自动切换。
- CPU/内存只覆盖浏览器已有的 JS getter，不新增浏览器原本不支持的 API。
  WebGL1/2 只覆盖实际支持、已启用的 `WEBGL_debug_renderer_info` 扩展标识。
  扩展不可用时不伪造扩展；不改变真实 GPU、绘制像素、其他 WebGL 能力或错误语义。
- 脚本在新文档加载时安装，覆盖 Window 和 iframe；第一张查询页会新建，
  而不是直接使用安装前恢复的旧文档。安装失败会报错并清理，不静默退回未覆盖环境。
- **Worker（包括其中的 OffscreenCanvas）、Service Worker、字体、音频、TLS、
  操作系统、UA-CH 都没有相应伪装**。Window 与 Worker 的 CPU 等值可能不同，脚本可被检测，
  不保证整体指纹一致、绕过风控或减少验证码。没有隐藏补丁源码。
- 同一配置值固定；指纹参数和脚本版本纳入环境标识，用于隔离档案、备份和缓存。
  更改脚本版本会产生新命名空间；不迁移 Cookie，不改变共享限流/暂停。
- 没有新增 UI 开关；按下文手动切换步骤编辑本地配置并重启，再运行本地检测。
  配置检查只输出字段启用状态和 CPU/内存数值，不输出 GPU 字符串。
- 旧配置未写环境选项的，升级后将默认进入新环境命名空间，旧档案/备份仍保留但不复制。
  旧配置若显式为 `null`，则继续尊重其关闭设置；需要启用时删除该字段或改为环境对象。
- 注入发生在本项目自动化 Chrome 上下文；“直接打开档案”的独立 Chrome 窗口
  没有脚本注入，不等同于自动化查询环境。需要一致环境时使用“国航实验”的会话准备入口。

## 固定预设与本地一致性检测

两个版本化桌面预设：`desktop-cn-v1`（zh-CN / Asia/Shanghai）和
`desktop-gb-v1`（en-GB / Europe/London）。共同参数是视口 1440×900、
屏幕 1920×1080、缩放 1、浅色、无触控；不指定 UA，不伪装操作系统或 Chrome 版本。
同一预设不会随机变化，允许显式字段覆盖；`has_touch` 只接受布尔值。

```json
"browser_environment": {
  "preset": "desktop-cn-v1",
  "network": {"mode": "system"}
}
```

优先级：显式环境字段 > 预设字段 > 供应商语言/时区默认值。
预设展开为实际选项后计算环境标识；等效的完整配置与预设使用相同命名空间。
启用预设属于一次明确的环境变化，仍按前述流程关闭会话、修改本地配置、重启服务。
应用是否已使用新配置取决于服务重启；修改配置文件不会替换已存在的浏览器上下文。

新增检测命令（在项目目录执行）：

```sh
python main.py --config /absolute/path/to/config.json airchina-environment-audit
```

默认有头 Chrome，与国航实验的浏览器显示模式一致；可加 `--headless` 用于自动测试，
报告会记录启动模式及其与配置的差异，不能把无头结果视为正式有头会话的证据。
检测期间会短暂打开一个临时 Chrome 窗口，结束即关闭。

- 使用临时档案与内存拦截的合成页面，做两次导航采样；不访问国航，也不连接配置中的代理。
- 比较视口、屏幕、缩放、语言、时区、主题、触控能力及可选 UA；
  比较合成请求的 UA/语言与页面 JS 值，检查 UA / navigator.platform /
  低熵 UA-CH 的粗粒度平台类别。缺失或无法识别的信号标记“不可验证”，不是通过。
- 时区别名、主题 no-preference 等可能被浏览器规范化；差异只供排查，不是风控判定。
- 不使用正式档案，不读取 Cookie/token/账号/历史，不枚举字体、
  Canvas 像素或音频特征；不调用高熵 UA-CH 接口。
  解析后的配置启用 `fingerprint` 时安装固定脚本，并核对配置中的 CPU/内存/WebGL 标识。
  默认只核对 CPU/内存；只有显式设置 WebGL 才读取扩展标识。
  GPU 字符串只在页面内比较，报告只记录匹配结果；不支持的 API 标为“不可验证”。
- 请求读取仅限身份类头字段白名单，报告不保存 UA 原文、代理地址或凭据。
  所有**检测页面请求**均在本地 fulfill/abort；此机制不是操作系统级网络沙箱，
  不承诺阻止 Chrome 自身的更新等后台网络行为。
- 输出在配置的 `runtime_dir/browser_environment_audits/<唯一编号>/report.html` 和
  `report.json`，仅本机保存。退出码 0 表示已检查项目未发现差异，
  1 表示存在差异，2 表示检测/配置失败；未配置或不可验证项目仍需阅读报告。
- 检测不读写查询暂停状态、不验证代理出口/IP/TLS，也不证明免验证码或“指纹通过”。

2026-11-03 实测：

- 当前本地测试配置在临时有头、无头 Chrome 中均完成检测，已检查项目未发现差异。
  该配置未启用预设，显示尺寸等未明确配置的项目仍标为“未配置”。
- 两个预设在真实无头 Chrome 的合成页面测试中通过；刻意设置的请求 UA /
  JS UA 冲突被识别，原始 UA 不进入报告。
- 全量 Python 回归 1144 项通过、2 项跳过、58 个子测试通过。
- 修正旧代理冒烟测试传给 Chrome 的临时目录参数，从无效的 `profile_root`
  改为 `user_data_dir`，确保测试真正使用临时档案。没有删除历史遗留测试档案。

同日加入固定指纹后的补充验证：

- 临时有头、无头 Chrome 均通过 CPU/内存和 WebGL1/2 固定标识检查；
  包括 iframe、重复注入、两次导航、同档案关闭重开和原生档案不受影响。
- Worker 保持原生值，已在测试中确认，因此不声明全局指纹一致。
- 合成固定参数的检测报告通过，GPU 字符串未写入报告；该轮测试未改动运行配置。
- 全量 Python 回归 1182 项通过、2 项跳过、58 个子测试通过；
  保留一条既有 Starlette 依赖弃用警告。JS 语法检查通过。

默认开启后的补充验证：

- 默认值、显式关闭、自定义覆盖、其他供应商不受影响均有回归覆盖；
  修正默认后端包含 Playwright 回退时的处理，只使用首选 Chrome，启动失败仍禁止回退。
- 全量 Python 回归 1191 项通过、2 项跳过、58 个子测试通过，保留既有依赖弃用警告。
- 本地运行配置已明确写入 CPU 8 / 内存 8，临时有头 Chrome 检测通过；
  对应服务已重启并核对新档案命名空间，既有查询暂停和浏览器查询次数未变。

## 指定代理网络

把 `network` 改成：

```json
"network": {
  "mode": "proxy",
  "server_env": "AIRCHINA_PROXY_SERVER",
  "username_env": "AIRCHINA_PROXY_USERNAME",
  "password_env": "AIRCHINA_PROXY_PASSWORD"
}
```

在运行服务的环境变量或配置文件旁的本机 `.env` 中设置对应值。
代理地址需要显式协议和端口，例如 `http://127.0.0.1:8080`；
支持 HTTP/HTTPS 代理及无认证 SOCKS5，SOCKS5 认证在本路径中拒绝。
无认证代理删除 username/password 两个引用；有认证时两者必须同时提供。
URL 中不得嵌入账号密码、路径、查询参数或片段。

代理变量缺失/格式不合法时在配置校验阶段报错；代理启动/连接失败不会
自动退回系统网络、其他后端或另一个代理。不要提交 `.env`、密码或档案；
代理能看到相应网络元数据，HTTP 明文内容也可能被其读取，仅使用你信任的服务。

此功能不采购代理、不自动获取免费代理，也不保证地址变更后实际出口 IP 不同。
本次只验证了两个本机 HTTP 测试代理，不代表任何外部代理或 SOCKS 服务实测成功。

## 手动切换步骤

1. 等当前任务结束，关闭空闲项目会话，再正常停止本地服务。
2. 修改本地环境配置或代理变量（可从代理 A 切到代理 B，也可改回 `system`）。
3. 在项目目录运行以下只读配置检查，不会打开浏览器或查询：

   ```sh
   python main.py --config /absolute/path/to/config.json airchina-environment-check
   ```

   输出网络模式、语言/时区、显示参数及固定指纹启用摘要，不输出代理地址、
   账号、密码、UA 原文或 WebGL 字符串。
4. 重新启动服务。环境变量由进程启动时加载，编辑 `.env` 后不能依赖已有进程热更新。
   UI 仍使用原来的“国航实验”入口，本版不提供网页在线修改代理密码的表单。

环境配置摘要用于生成独立的持久档案/备份命名空间，以及参考缓存键。
切换不会复制旧 Cookie、删除旧档案、清除验证状态；切回完全相同配置可使用原档案。
更改代理凭据也会得到不同命名空间。已保留的上下文不能跨环境复用。
服务运行时若配置变化导致备份命名空间不一致，会拒绝会话准备并要求重启。

**暂停、限流间隔和跨进程查询锁仍按同一 runtime_dir 共享，不按环境分开。**
遇验证码后的暂停不会因为换 UA、代理、档案或重启服务而解除。
会话准备是用户手动检查入口，不是自动重发航班查询；恢复仍需明确人工确认。

## 验证

- 自动测试覆盖配置边界、凭据脱敏、启动参数传递、禁止后端回退、
  档案/备份/缓存隔离、保留上下文失效和切换后暂停仍有效。
- `python tests/browser_fingerprint_smoke.py` 用临时 Chrome 和拦截的合成页面，
  比较原生/固定属性、iframe、重复导航和重启；验证 Worker 保持原生、
  关闭覆盖后不污染原生档案，以及检测报告不保存 GPU 字符串。可加 `--headed`；
  不支持的 WebGL API 计入不可验证数量，不当作成功伪造。
- `python tests/browser_environment_smoke.py` 使用真实 Chrome 和两个仅监听本机的
  HTTP 测试代理，代理不转发任何外网请求。验证视口/屏幕/缩放、语言/时区、
  主题/UA，以及 A → B → A 网络切换与本地状态隔离。
- 检测不会使用外部代理查询国航或解除验证码暂停；运行配置的改动需单独应用并重启服务。
