# 自动回归测试

## 触发和查看结果

工作流 `.github/workflows/tests.yml` 在所有分支推送、PR 创建/更新及手动触发时运行。
进入 GitHub 仓库的 **Actions → Automated tests**，查看两个检查：

1. **Python regression**：现有 Python 测试，包括查询锁、自动调度、报价校验、
   历史排序、预算及任务恢复。
2. **Frontend regression**：前端语法和 Node 原生测试，包括展示、筛选与提交参数。

同一分支或 PR 的新运行会取消旧的未完成运行。任务有超时限制。
Python 测试报告只上传 `test-results/python.xml`，保留 7 天；
前端结果在步骤日志中查看。不会上传本地配置、数据库、浏览器 profile 或运行目录。

失败时展开红色步骤查看具体测试和报错。修复并推送后会重新测试。
这只是自动检查，不会自动修复、部署或修改代码。
是否将检查设为合并前必需，需要另外配置 GitHub 分支保护，本次不修改仓库设置。

## 无需平台密钥

- 工作流不引用任何仓库 Secret，不需要飞猪 API key、携程账号或登录态。
- 不安装 FlyAI CLI，不下载浏览器，不执行真实平台查询脚本。
- Python 使用 `pytest-socket` 只允许 `127.0.0.1`、`::1` 和 Unix socket，
  供本地冒烟服务、异步运行时使用；意外的外部 Python socket 连接会使测试失败。
- 这不是操作系统网络沙箱，不能限制独立子进程。当前平台调用在测试中被模拟，
  新测试也必须保持该约束，不能加入真实 CLI 或浏览器查询。
- 安装依赖和 GitHub Actions 上传报告仍需要网络；“离线”指测试不查询航班平台。

## 本地复现

在项目目录中，使用 Python 3.12 和 Node.js 24：

```sh
python -m pip install -r requirements-dev.txt
python -m pytest tests -q -ra --allow-hosts=127.0.0.1,::1 --allow-unix-socket --junitxml=test-results/python.xml
for file in static/app.js static/app/*.js static/airchina_experiment.js; do node --check "$file"; done
node --test tests/test_tail_ui.cjs tests/test_app_infrastructure.cjs
```

测试报告目录已加入 Git 忽略规则。无需安装浏览器即可运行以上测试。
可选的 patchright、rebrowser-playwright 未安装时，对应后端测试会跳过。

## 甩尾折叠组件样式构建

只有修改 `tools/tail-accordion.scss` 或 Bootstrap 版本时需要重新构建：

```sh
npm ci
npm run build:tail-ui
```

- `bootstrap@5.3.8` 与 `sass@1.107.1` 固定于 `package.json` 和 `package-lock.json`，
  只用于构建；`node_modules/` 已加入 Git 忽略规则。
- 产物 `static/vendor/tail-accordion.css` 随代码交付，运行时只读取本地静态文件，
  不访问 CDN，不需要 npm 或 Bootstrap JS。MIT 文本位于 `static/vendor/LICENSE-bootstrap.txt`。
- 连续两次构建应产生相同内容。既有 Python/Node 测试不需要安装这些构建依赖，
  但修改源样式后应先重新构建，再运行浏览器冒烟。
- `tests/browser_tail_smoke.py` 同时展开多个设置区，验证 1280、390、320px 的
  通栏布局、字段分栏、箭头样式、无横向溢出，以及手机操作栏不遮挡输入项。
- 可选高数据量检查使用 100 条任务、240 条报价及 20 个历史任务，覆盖快速筛选、
  分页、轮询中刷新、慢响应视图切换、详情抽屉键盘导航和 1280/768/390/320px 布局，
  仍不访问真实平台：

  ```sh
  python tests/browser_ui_stress.py --output-dir ./runtime/ui-stress
  ```

## 本轮范围与限制

- CI 使用 Ubuntu 24.04、Python 3.12、Node.js 24；不是全操作系统兼容性认证。
- 本轮固定运行时系列，没有锁定全部 Python 依赖；依赖锁定另行处理。
- 桌面 Chrome 视觉冒烟脚本仍为可选手动检查，本轮未加入 CI。
- 测试通过意味着已覆盖规则未回归，不证明真实平台可用、库存完整或排序收益提高。
- 新工作流推送后才会在 GitHub 运行；本地通过不能代替首次远程运行结果。
