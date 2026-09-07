# KT6 / FreeStyleCopilot 统一测试手册

本文适用于 `main`、三个评测方案分支、UI Graph 分支和保留的 OmniParser 历史分支。
先确认当前分支，再执行对应章节；不要在一个分支运行另一个分支才存在的执行器。

## 1. 分支与测试目标

| 分支 | 主要验证内容 |
|---|---|
| `main` | Runtime、页面感知、安全动作链、公共证据归档与报告 |
| `eval-current` | 本地 OpenCV/OCR + 通用模型 API + 融合 |
| `eval-browser-use` | Browser Use/CDP + 通用规划模型 API |
| `eval-ui-tars` | 通用规划模型 API + UI-TARS 截图定位 API |
| `ui-graph-textflow-cdp` | CDP、多源 UI Graph、操作 DAG 安全校验 |
| `feature/browser-executor` | 当前 Tab + 自然语言计划经 ScenarioRunner 派发固定 type/click 并验证结果 |
| `feature/eval-browser-harness` | 日常 Chrome 当前 Tab + Browser Harness，经 ScenarioRunner 派发固定 type/click 并验证结果 |
| `br_omniParser` | 历史回归，不纳入当前三方案结论 |

目标测试目录：`D:\04project\FreeStyle_Copilot_KT6_demo`。页面数据、截图、DOM/CDP、
模型原文和评测证据只能保存在获批测试目录或 `runtime_data/`，不得提交 Git。

## 2. 同步与接手检查

```powershell
cd D:\04project\FreeStyle_Copilot_KT6_demo
git fetch origin
git switch <目标分支>
git pull --ff-only
git status -sb
git log -3 --oneline --decorate
python --version
```

功能分支未推送时不要在测试机伪造同名分支；先确认开发机提交和远端状态。

## 3. 一次性配置 `.env`

```powershell
Copy-Item .\.env.example .\.env
notepad .\.env
```

`.env` 在切换分支时保留。后端和评测 CLI 会自动读取，终端或服务已有的同名变量优先。
配置改变后重启后端。不要把真实 key 写入 `.env.example`、命令行、suite 或报告。

### 3.1 通用规划/语义模型 API

```dotenv
KT6_MODEL_API_PROVIDER=<供应商或内网网关标识>
KT6_MODEL_API_BASE_URL=https://<获批网关>/v1
KT6_MODEL_API_KEY=<本机密钥>
KT6_MODEL_API_MODEL=<精确模型名>
KT6_MODEL_API_ALLOWED_HOSTS=<获批网关精确主机名>
```

### 3.2 可选本地 Canvas 感知

普通 DOM 页面无需配置视觉模型。只有目标页面依赖 Canvas 像素证据，且本机已经安装
RapidOCR/OpenCV 可选依赖时，才增加：

```dotenv
KT6_VISION_DRIVER=local_cv_ocr
```

这条本地补充链不调用大模型；当前 Browser Harness 主链不再要求 CodeAgent/GLM CLI
配置。

### 3.3 UI-TARS API

```dotenv
KT6_UI_TARS_API_PROVIDER=ui-tars
KT6_UI_TARS_API_BASE_URL=https://<获批UI-TARS服务>/v1
KT6_UI_TARS_API_KEY=<本机密钥>
KT6_UI_TARS_MODEL=<精确UI-TARS模型名>
KT6_UI_TARS_API_ALLOWED_HOSTS=<获批服务精确主机名>
```

正式测试区数据未经批准不得发送到公网 endpoint。

### 3.4 日常 Chrome + Browser Harness 执行层

只在 `feature/eval-browser-harness` 的隔离、可恢复页面测试：

```dotenv
KT6_BROWSER_EXECUTION_DRIVER=browser_harness
# 公网 HTTP(S) 无需逐域名配置；仅测试本机/RFC1918 页面时设为 1
KT6_EXECUTION_ALLOW_PRIVATE_NETWORKS=0
KT6_MODEL_API_PROVIDER=<供应商或内网网关标识>
KT6_MODEL_API_BASE_URL=https://<获批网关>/v1
KT6_MODEL_API_KEY=<本机密钥>
KT6_MODEL_API_MODEL=<精确模型名>
KT6_MODEL_API_ALLOWED_HOSTS=<获批网关精确主机名>
```

规划模型只使用 `KT6_MODEL_API_*`。Browser Harness 提供当前 Tab 的实时 DOM/CDP 感知
和固定 type/click 传输，不需要 CodeAgent/GLM CLI；Canvas 像素证据需要时才显式启用
上述本地 `local_cv_ocr` 补充，没有 `execution_fixture` 专用识别器。公网
HTTP(S) 目标无需逐域名配置；每次初始导航、当前页面、重定向和新标签绑定都会重新做
DNS/网络范围校验，并兼容代理常用的 `198.18.0.0/15` synthetic DNS（仅域名解析结果，
直接输入该网段 IP 仍拒绝）。本机和 RFC1918 页面默认拒绝，仅隔离测试时可设置
`KT6_EXECUTION_ALLOW_PRIVATE_NETWORKS=1`；链路本地和保留地址始终拒绝。未显式启用
`browser_harness` 时，`dry_run=false` 继续 fail closed。先安装
`requirements-browser-executor.txt`，正常打开日常 Chrome，并在
`chrome://inspect/#remote-debugging` 开启当前实例远程调试；不需要专用 profile、固定
CDP 端口或 Chrome 启动参数。扩展只选择当前 Tab，Browser Harness 负责感知和动作。

## 4. 公共自动化回归

```powershell
python -m unittest discover -s tests
```

各分支开发机最近一次结果（日期见有单独标注的行，其余为 2026-08-13）：

| 分支 | 结果 |
|---|---|
| `main` | 451 tests OK，46 skipped |
| `eval-current` | 518 tests OK，46 skipped |
| `eval-browser-use` | 456 tests OK，46 skipped |
| `eval-ui-tars` | 457 tests OK，46 skipped |
| `ui-graph-textflow-cdp` | 508 tests OK，46 skipped |
| `feature/browser-executor` | 2026-08-29：当前 Chrome 扩展与执行主链定向 123 tests OK；最近全量结果见下文 |
| `feature/eval-browser-harness` | 2026-09-07：生命周期、执行链和扩展定向 68 项通过；实机历史记录见第 10 节 |
| `br_omniParser` | 461 tests OK，46 skipped |

测试数量会随公共同步增加，以当前命令最终 `OK` 为准。公共配置和报告定向测试：

```powershell
python -m unittest tests.test_env_config
python -m unittest `
  tests.test_evaluation_artifacts `
  tests.test_evaluation_report
```

## 5. `main` 公共框架测试

`main` 不运行 Browser Use 或 UI-TARS 实验执行器，重点验证公共契约：

```powershell
python -m unittest `
  tests.test_evaluation_artifacts `
  tests.test_evaluation_report `
  tests.test_app
```

## 6. `eval-current` 测试

```powershell
python -m unittest `
  tests.test_openai_compatible_api `
  tests.test_openai_compatible_topology_model `
  tests.test_openai_compatible_ui_graph_reasoner `
  tests.test_hybrid_canvas_vision `
  tests.test_vision_cache_coordinator `
  tests.test_app
```

检查点：OpenCV/OCR 本地执行；模型请求不包含截图、Base64、本地路径和完整页面 URL；
provider/model 进入证据；模型输出先严格校验再融合。

## 7. `eval-browser-use` 测试

使用独立 Python 3.11-3.13 环境：

```powershell
py -3.12 -m venv .venv-browser-use
.\.venv-browser-use\Scripts\Activate.ps1
python -m pip install -r .\requirements-evaluation-browser-use.txt
python -m browser_use install
```

```powershell
python -m unittest `
  tests.test_browser_use_evaluation `
  tests.test_evaluation_executor `
  tests.test_openai_compatible_api
```

检查点：Browser Use 使用任务 host 白名单；规划模型不接收截图；危险工具被排除；成功由
本地页面断言决定；DOM、逐步 planner、action trace 和 validation 证据完整。

## 8. `eval-ui-tars` 测试

```powershell
py -3.12 -m venv .venv-ui-tars
.\.venv-ui-tars\Scripts\Activate.ps1
python -m pip install -r .\requirements-evaluation-ui-tars.txt
python -m playwright install chromium
```

```powershell
python -m unittest `
  tests.test_ui_tars_evaluation `
  tests.test_evaluation_executor `
  tests.test_openai_compatible_api
```

检查点：规划 API 与 UI-TARS API 独立记录；每步截图绑定独立 artifact ID；坐标严格缩放
并拒绝越界；默认 dry-run；只有显式 `--execute-actions` 加确定性终态验证才可计成功。

## 9. `ui-graph-textflow-cdp` 测试

```powershell
cd .\browser_sidecar
npm install
npm run check
cd ..
```

```powershell
python -m unittest `
  tests.test_cdp_sidecar_assets `
  tests.test_cdp_snapshot `
  tests.test_page_perception_cdp `
  tests.test_page_perception_ui_graph `
  tests.test_ui_graph `
  tests.test_ui_graph_reasoner `
  tests.test_ui_operation_graph `
  tests.test_ui_graph_planning `
  tests.test_ui_graph_api
```

真实 CDP 只连接 loopback；Sidecar 只读，不点击、不输入、不拦截网络。必须检查：

- `analysis_only=true`、`execution_authorized=false`、`safe_for_execution=false`；
- DOM/CDP 点击候选具有稳定 ref/backend node id；
- disabled、page_api、vision、text、`@capture:` 不能越权；
- iframe、Shadow DOM、parent/owner 关系符合页面事实；
- 未配置真实 Reasoner 时规划接口 503 是当前预期。

## 10. `feature/eval-browser-harness` 测试

2026-09-07 生命周期修复的最小定向回归（不访问真实浏览器或模型）：

```powershell
.\.venv-browser-executor\Scripts\python.exe -m unittest `
  tests.test_browser_harness_lifecycle `
  tests.test_execution_scenario `
  tests.test_browser_executor `
  tests.test_browser_extension_assets
```

结果：68 项通过（0.382 秒），其中新增专项 13 项。第一次在 Windows 受限沙箱中有
4 项被临时目录权限阻断；正常本机权限重跑上述同一命令全部通过。本轮未跑全量、未调用
模型或操作真实 Chrome。代码更新后需要重启后端，并在 `chrome://extensions` 重新加载
扩展。人工复测重点：

1. 一个面板正在规划/执行时，从另一个面板提交任务，应立即提示 busy，原任务的目标不变。
2. Chrome 连接失败时，任务应结束为 failed，下一次新任务可重新连接，不持续占用 running。
3. 相同输入或相同菜单选项再次执行，新 capture 仍满足目标时应验证通过。
4. 有延迟的页面跳转应先观察到 URL 改变，再采集验证；未跳转仍应明确失败。

互斥不会增加模型调用；DOM 动作仍保留两次动作前 capture 和一次动作后验证。
导航等待只读 `Page.getFrameTree`，不重复全页识别；重连仅发生在新绑定边界，不重放动作。

当前固定动作词表只测试 `type` 与 `click`，不支持滚动、快捷键、任意 CDP 或 JavaScript。
`type` 只能操作普通 INPUT/TEXTAREA，拒绝 password/file/hidden、disabled 和 readonly，
并必须紧跟同目标、同文本的 `input_value` 确定性验证。可使用独立 Python 3.12 环境：

```powershell
py -3.12 -m venv .venv-browser-executor
.\.venv-browser-executor\Scripts\Activate.ps1
python -m pip install -r requirements-browser-executor.txt
```

先跑不需要真实浏览器的自动化回归：

```powershell
python -m unittest `
  tests.test_execution_scenario `
  tests.test_browser_executor `
  tests.test_outcome_verifier `
  tests.test_execution_e2e `
  tests.test_safe_dom_actions `
  tests.test_safe_dom_action_plan `
  tests.test_dom_action_api `
  tests.test_ui_graph `
  tests.test_ui_operation_graph `
  tests.test_app
```

实机闭环是“任意公网 URL + 自然语言任务 → 统一页面感知 → LLM 生成 `kt6.action-plan.v1`
→ 实时 Grounding → 受控 type/click → 重新感知 → 确定性 Verify”。计划中没有 UI Graph、
backend node id、selector 或坐标，也不再依赖固定测试页或规则解析器：

```powershell
.\scripts\start-browser-executor.ps1

# 可选：启动后端后顺便在现有 Chrome 打开目标标签页
.\scripts\start-browser-executor.ps1 -InitialTargetUrl "https://www.baidu.com/"

# 首次执行前按 Chrome 提示允许远程调试；连接后 browser_harness.ready=true
Invoke-RestMethod http://127.0.0.1:8787/api/execution/health
```

`start-browser-executor.ps1` 是 Windows 当前环境的统一入口：启动或复用 8787 后端，再用
`chrome.exe --new-tab` 在已经运行的日常 Chrome 打开目标 URL。脚本不创建专用 profile，
不传 `--remote-debugging-port`、`--user-data-dir` 或 `--load-extension`，也不固定 9222。
脚本优先使用
`.venv-browser-executor`，也兼容 `runtime_data/tools/python312` 和系统 Python；可通过
`-PythonPath` 或 `-ChromePath` 显式指定。

扩展首次安装步骤：

1. 在现有 Chrome 打开 `chrome://extensions`，开启开发者模式。
2. 点击“加载已解压的扩展程序”，选择仓库的 `browser_extension/`。
3. 代码更新后在扩展管理页点击“重新加载”，无需替换或重启 Chrome。
4. 打开 `chrome://inspect/#remote-debugging`，勾选允许当前浏览器实例远程调试。
5. 回到脚本打开的新标签页，点击工具栏中的 “KT6 Browser Agent”。
6. 输入自然语言流程，点“生成执行计划”；Chrome 首次询问时点击 Allow。
7. 检查语义步骤后勾选确认并执行。

成功输出位于：

```text
runtime_data/execution_scenarios/<run_id>/
  action-plan.json
  capture-*-ui-graph.json
  result.json
```

`result.json` 必须为 `status=success`，每个 step 均为 `completed`，每个 type/click 的后续
verify/wait 都必须由新 capture 通过确定性 Verifier。真实浏览器验收只从 Side Panel
发起；自动化测试覆盖精确 Tab 选择、Browser Harness 适配、计划契约、Grounding、安全
执行和 Verifier，不维护独立 Runner 页面。

Side Panel 通过 `chrome.tabs.query` 取得 active Tab，再用 `chrome.debugger.getTargets()`
按 `tabId` 唯一映射 Target。扩展不调用 `chrome.debugger.attach`，也不代发 CDP。计划和
执行 API 只携带 `browser_target_id`；后端通过 Browser Harness 再次核对 Target ID 与
URL。临时 Target ID 不进入 Action Plan。

实机步骤如下：

1. 日常 Chrome 已打开，扩展 v0.8.0 已加载，目标标签页已登录且页面状态可恢复；已在
   `chrome://inspect/#remote-debugging` 开启当前实例远程调试。
2. `POST /api/execution/plans` 传入 `start_url`、`user_request`，Side Panel 还会传
   `browser_target_id`；先通过 URL Safety
   Policy 校验，再感知页面并调用 LLM 生成计划；确认没有坐标、selector 和 backend node id。
3. `POST /api/execution/runs` 必须带 `confirmed=true`，Side Panel 再次确认当前 Tab 的
   URL/Target ID 未变，立即返回 run_id；前端轮询
   `GET /api/execution/runs/{run_id}`，避免阻塞页面。
4. 每个 click 执行“capture → Grounding（DOM/CDP 优先，缺失时回退 Vision）→
   fresh capture → live frame/identity/hit-test → click”；随后用 verify/wait 再次感知。
   无 DOM `id` 的 CDP 候选必须有正整数 backend node id 和语义/属性指纹；0×0 可点击
   容器只能落到唯一可见直接子节点，live hit-test 命中也必须仍在同一授权 DOM 子树。
   链接还需在点击前校验解析后的目标 `href`；`target=_blank` 把本次新增且 URL/opener
   匹配的 popup target 绑定为后续 capture 的页面。
5. 每个 type 执行两次 capture 与 DOM Grounding 指纹复核，随后固定调用 `DOM.focus`、
   Ctrl+A、Backspace、`Input.insertText`；随后重新采集 AX/DOM，并用新 UI Graph 的
   `attributes.value` 做 `input_value` 验证。模型不能生成按键序列。
6. Vision 目标使用本次截图识别的 bbox 比例，点击前重新读取 live Canvas box 并
   做 hit-test，不复用第一步坐标。
7. 最后新 capture 必须满足对应 Verifier（element_visible、element_disappeared、
   element_selected、selected、text_present、input_value、url_changed 或 page_changed），才能返回
   SUCCESS。失败会按 planner_failed / target_not_found / target_ambiguous /
   perception_failed / execution_failed / verify_failed / page_changed 归类，便于统计
   KT6 GUI Agent 具体卡在哪个环节。

2026-08-20 本机实机复核：严格语义计划 `点击百度热搜 → verify url_changed` 运行成功；
移除执行目标逐域名白名单后，`example.com → 点击 Learn more → IANA` 的跨域实机运行也
成功。Action Plan、三次 capture 和 `result.json` 均写入忽略提交的
`runtime_data/execution_scenarios/<run_id>/`。相关回归 61 项通过、1 项按真实浏览器环境
开关跳过。百度首次复核时配置 Planner 曾返回 HTTP 402，所以该次只证明执行与验证链；
后续 `example.com` 复核已由正式 `/api/execution/plans` 调用 `deepseek-v4-pro` 生成计划，
再把返回计划原样提交 `/api/execution/runs` 并得到 `status=success`，完整 URL + 自然语言
Planner E2E 已通过。

2026-08-21 统一启动链复核：从 CDP、Harness、后端均未就绪的状态运行
`start-browser-executor.ps1` 后，`/api/execution/health` 返回 `ready=true`；再次运行脚本
幂等复用全部组件。正式 `deepseek-v4-pro` 生成 `example.com → Learn more → url_changed`
计划，真实执行 `run_2bbfb40b1b6d46c4` 为 `success`。本次相关专项 110 项通过、1 项按
真实浏览器开关跳过。全量 558 项中 557 项通过/跳过，唯一失败为未修改的
`VisionFrameMatcherOpenCVTests.test_small_translation_with_blank_new_border_is_reusable`；
该用例在本机 Python 3.12 与 3.14 环境均稳定返回 `insufficient_transform_inliers`，需作为
独立视觉算法/依赖问题处理，不能计入本次启动链回归。

2026-08-21 Browser Agent v0.6.0 实机复核：统一脚本启动的 Chrome 命令行已包含
`--load-extension=<repo>/browser_extension`，`/api/execution/health` 为 `ready=true`。
Side Panel 同路线传入百度 active Tab 的精确 Target ID，正式模型生成四步
`type → input_value → click → page_changed` 计划，且 Target ID 未进入 Action Plan。
真实运行 `run_48b7041ddf7844bf` 的 type 与 `input_value` 两步均 completed/verified；百度
当前新版页面在输入后自行进入搜索结果页，结果页 UI Graph 超过 2000 节点并标记
`truncated=true`，所以第三步 click 按安全规则拒绝为 `grounding_ui_graph_invalid`，没有
伪报整条工作流成功。扩展/执行链专项回归为 121 passed、1 skipped；全量 564 项为
562 passed、1 skipped，唯一 failure 仍是上述未修改的 OpenCV 用例。

以上 2026-08-20/21 记录是旧的专用 CDP Chrome + Browser Harness 实机证据，不代表当前
v0.7.0 现有 Chrome 扩展传输已经实机验收。v0.7.0 改为扩展 service worker attach 当前
Tab，并用本机长轮询把固定 CDP 命令交给后端现有安全执行逻辑；完成本轮单元/接口回归后，
仍需按本节重新做一次真实 Chrome 页面验收并记录新 run_id。

2026-08-24 v0.7.0 自动化复核：扩展 runtime 中继、HTTP 注册/鉴权、精确
runtime/Target/URL 绑定、固定 CDP 参数、Side Panel 载荷和启动脚本共 65 项通过，1 项
真实浏览器用例按开关跳过；全量 570 项为 568 passed、1 skipped、1 failed。唯一失败仍是
`VisionFrameMatcherOpenCVTests.test_small_translation_with_blank_new_border_is_reusable`
返回 `insufficient_transform_inliers`，与本次执行链文件无关。Windows PowerShell 5.1 与
Node/Python 静态语法检查、`git diff --check` 均通过。真实 Chrome E2E 尚待重新验收。

2026-08-29 删除旧 popup/content collector、独立 Runner 页面和仓库内置的 Browser Harness
daemon/CLI 后，现有 Chrome 扩展 runtime、计划、grounding、ScenarioRunner、Verifier、HTTP API
与公共 App 工厂定向回归共 123 项通过；`background.js`、`sidepanel.js` 语法检查、Python
编译和 `git diff --check` 均通过。此结果只覆盖自动化回归，真实 Chrome 页面仍按本节手工
流程验收。

2026-09-01 `feature/eval-browser-harness` 当前提交前定向复核：Browser Harness 适配、
Side Panel、Action Plan、ScenarioRunner、DOM API 与启动入口共 57 项通过；两个扩展脚本
`node --check`、后端 `compileall` 和 `git diff --check` 均通过。百度现有 Chrome 页面已由
用户完成计划生成、输入、Grounding、点击和结果验证手工验收。

目标 Tab 真绑定（Side Panel + Browser Harness）：

- Side Panel 通过精确 Target ID 消除同 URL 多 Tab 歧义；后端要求 Browser Harness 看到的
  Target ID 与 URL 完全一致，否则返回 `browser_target_binding_mismatch`。
- 计划生成后若用户切换 Tab 或导航页面，执行前检查会要求重新生成计划；Action Plan
  本身不保存 Target ID、selector 或坐标。
- 后续
  `Page.getFrameTree`、`DOMSnapshot.captureSnapshot`、`Accessibility.getFullAXTree`、
  `Page.captureScreenshot` 与 `DOM.describeNode`、`DOM.getBoxModel`、
  `DOM.getNodeForLocation`、type/click 都通过 Browser Harness 已绑定的同一 Tab；KT6
  适配器拒绝模型生成的任意按键序列、JavaScript、Python 和 raw CDP。

双 Tab 实机验收至少检查：

1. Side Panel 所在 Tab 与 Browser Harness 绑定的 Target 唯一对应；
2. 只有 Side Panel 选中的 Target Tab 被感知和操作；
3. Capture/UI Graph 中的 URL、DOM、截图都来自该 Target Tab；
4. BrowserExecutor 的 type/click 真正发生在该 Target Tab；
5. click 后重新感知，Verifier 能判断成功或明确失败；
6. 结果落到 SUCCESS 或明确失败类别，而不是线程卡死 / 状态一直 running。

DOM 执行回执写入内存审计接口 `GET /api/dom-actions/audit`，计划进度通过
`GET /api/dom-actions/plans/{plan_id}` 查看。当前代码不自动生成执行录像，也不把点击回执
当作评测成功证据。

## 11. `br_omniParser` 历史分支

只运行全量回归和公共配置/报告测试，不新增当前三方案结果。OmniParser 结果不能与
`current/browser_use/ui_tars` 混在同一正式比较中。若公共代码同步导致回归，修复公共
兼容性，不继续扩展 OmniParser 功能。

## 12. 统一评测报告

初始化示例：

```powershell
$evalDir = '.\runtime_data\evaluation\nce-simple-query'

python -m kt6_backend.evaluation_report_cli init `
  --out "$evalDir\suite.json" `
  --suite-id nce-ip-simple-query-28 `
  --title 'NCE-IP简单查询类任务' `
  --task-count 28 `
  --repetitions 3 `
  --step-limit 10 `
  --planner-provider '<实际供应商或内网网关>' `
  --planner-model '<精确模型名>' `
  --environment-id '<统一测试环境编号>'
```

三组必须使用同一 suite、任务提示、规划模型、环境和重复次数。每次运行归档 Manifest、
动作轨迹、确定性验证，以及方案要求的截图、DOM/CDP、CV/模型/路由/融合或 UI-TARS
响应。模型超时和无效响应也必须记为失败运行，不能从完成率中删除。

```powershell
python -m kt6_backend.evaluation_report_cli validate `
  --suite "$evalDir\suite.json" `
  --runs "$evalDir\runs.jsonl"

$reportDir = "$evalDir\report-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
python -m kt6_backend.evaluation_report_cli report `
  --suite "$evalDir\suite.json" `
  --runs "$evalDir\runs.jsonl" `
  --out-dir $reportDir
```

证据不完整、哈希改变、公平性不满足或存在安全违规时不得自动排名。详细角色和字段见
`docs/evaluation-reporting.md`。

## 13. 测试交付物

```text
git-version.txt
unittest-full.log
scheme-run-index.jsonl
artifacts/<scheme>/<task>/rNNN/manifest.json
metrics.csv
issues.md
conclusion.md
```

领导版报告不得嵌入截图、页面 DOM、模型原文、完整 URL、API key 或失败自由文本。
