# 国航浏览器环境模拟与网络切换

本功能只配置国航实验使用的系统 Chrome。不是免验证码或全套指纹伪装：
不注入 Canvas/WebGL/音频等伪造脚本，不新增 stealth 参数，不自动轮换 IP/档案。
使用 Playwright 浏览器上下文参数，不修改系统代理、个人浏览器或其他查询源。

## 默认行为及环境配置

`providers.airchina.browser_environment` 缺失或为 `null` 时完全保留原有环境和档案。
如需启用，在本地 `config.json` 的 `providers.airchina` 中添加：

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
  `timezone_id`、`color_scheme`、`user_agent` 和 `network`。
- `screen` 必须与 `viewport` 一起设置且不小于后者；宽高 320–7680，
  缩放 0.5–4；主题允许 light/dark/no-preference；时区需为有效 IANA 名称。
- 未指定语言/时区时继承供应商配置；未指定 UA 时使用真实 Chrome UA（推荐）。
  自定义 UA 只用于兼容性测试，不会同步伪造系统、TLS、Canvas 或所有 Client Hints。
- 启用本功能时移除额外请求头里的手写 UA、Accept-Language 和三种常见 UA
  Client Hint 覆盖，让浏览器生成，避免与上下文参数相互矛盾。
- `system` 表示不覆盖 Chrome 的网络配置，**不是强制直连**：
  系统代理或 VPN 仍可能生效；本功能不验证出口 IP。
- 不接受任意浏览器启动参数、JS 注入或配置路径，未知字段会报错。

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

   输出仅有网络模式、语言/时区和显示参数，不输出代理地址、账号、密码或 UA 原文。
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
- `python tests/browser_environment_smoke.py` 使用真实 Chrome 和两个仅监听本机的
  HTTP 测试代理，代理不转发任何外网请求。验证视口/屏幕/缩放、语言/时区、
  主题/UA，以及 A → B → A 网络切换与本地状态隔离。
- 未使用外部代理查询国航，未解除当前的验证码暂停，未修改现有本地服务配置。
