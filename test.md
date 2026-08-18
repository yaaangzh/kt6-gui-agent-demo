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
| `feature/browser-executor` | SafeDOMAction 授权后通过 Browser Harness 派发 click |
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

### 3.1 内部 CodeAgent/GLM 路线

```dotenv
KT6_VISION_DRIVER=hybrid
KT6_HYBRID_MODEL_DRIVER=codeagent_cli
KT6_CODEAGENT_EXECUTABLE=D:\03CodeAgent\CodeAgentCLI\codeagent.bat
KT6_VISION_TIMEOUT_SECONDS=300
```

### 3.2 通用规划/语义模型 API

```dotenv
KT6_MODEL_API_PROVIDER=<供应商或内网网关标识>
KT6_MODEL_API_BASE_URL=https://<获批网关>/v1
KT6_MODEL_API_KEY=<本机密钥>
KT6_MODEL_API_MODEL=<精确模型名>
KT6_MODEL_API_ALLOWED_HOSTS=<获批网关精确主机名>
```

### 3.3 UI-TARS API

```dotenv
KT6_UI_TARS_API_PROVIDER=ui-tars
KT6_UI_TARS_API_BASE_URL=https://<获批UI-TARS服务>/v1
KT6_UI_TARS_API_KEY=<本机密钥>
KT6_UI_TARS_MODEL=<精确UI-TARS模型名>
KT6_UI_TARS_API_ALLOWED_HOSTS=<获批服务精确主机名>
```

正式测试区数据未经批准不得发送到公网 endpoint。

### 3.4 Browser Harness 执行层

只在 `feature/browser-executor` 的隔离、可恢复页面测试：

```dotenv
KT6_BROWSER_EXECUTION_DRIVER=browser_harness
KT6_BROWSER_HARNESS_CDP_URL=http://127.0.0.1:9222
```

未设置这两个变量时，`dry_run=false` 继续 fail closed。CDP 地址只允许 loopback。

## 4. 公共自动化回归

```powershell
python -m unittest discover -s tests
```

2026-08-13 开发机最近一次结果：

| 分支 | 结果 |
|---|---|
| `main` | 451 tests OK，46 skipped |
| `eval-current` | 518 tests OK，46 skipped |
| `eval-browser-use` | 456 tests OK，46 skipped |
| `eval-ui-tars` | 457 tests OK，46 skipped |
| `ui-graph-textflow-cdp` | 508 tests OK，46 skipped |
| `feature/browser-executor` | 525 tests OK，47 skipped |
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

## 10. `feature/browser-executor` 测试

第一阶段只测试 click，不测试输入、滚动、键盘、任意 CDP 或 JavaScript。使用独立
Python 3.12 环境安装可选依赖：

```powershell
py -3.12 -m venv .venv-browser-executor
.\.venv-browser-executor\Scripts\Activate.ps1
python -m pip install -r .\requirements-browser-executor.txt
browser-harness --doctor
```

先跑不需要真实浏览器的自动化回归：

```powershell
python -m unittest `
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

先跑仓库自带的真实页面 DOM 闭环。Fixture 只提供“打开 AP_001 详情”目标，不提供
UI Graph、backend node id 或坐标；命令会启动测试页、现场采集、生成三次 UI Graph、
执行 click 并验证新 capture：

```powershell
python -m kt6_backend.execution_e2e_cli
```

成功输出位于：

```text
runtime_data/execution_e2e/<UTC时间>/
  initial-ui-graph.json
  fresh-ui-graph.json
  after-ui-graph.json
  result.json
```

`result.json` 必须同时满足 `execution_status=executed_pending_verification`、
`verification_status=verified`、`outcome_verified=true`。若希望把真实浏览器场景纳入
unittest，可在根目录 `.env` 增加 `KT6_RUN_BROWSER_E2E=1`；未配置时只跳过这一项实机
用例，其余契约测试照常运行。当前开发机只有 Python 3.14，尚未安装要求的 Python 3.12
和 Browser Harness，因此本机只能完成自动化契约回归，实机闭环需在准备好的环境运行。

实机只使用可恢复的“打开 AP_001 详情”类任务，步骤如下：

1. Chrome/Edge 以 loopback remote debugging 启动，NCE 标签页已登录且页面状态可恢复。
2. 用现有扩展采集 DOM，用 `browser_sidecar/capture-ui-graph.mjs` 采集同一页面的 CDP
   snapshot；第一阶段将两份证据放进同一次 `POST /api/perception/captures` 请求，其中
   Sidecar JSON 位于 `cdp_snapshot` 字段。不要声称扩展与 Sidecar 已自动实时合并。
3. `POST /api/ui-operations/plan`，检查 click 指向带正整数 backend node id 的 CDP
   candidate；DAG 本身仍为 `dry_run_only=true`。
4. 依次调用 `/api/dom-actions/prepare` 与 `/api/dom-actions/preflight`，使用不同的新鲜
   capture、准确 asset/action 确认和所需权限取得一次性 token。
5. 调用 `/api/dom-actions/execute`，传入 token、`dry_run=false`、该 fresh capture 的
   `graph_id` 和已经验证的 `target_node_id`。只有 CDP 节点的 `#id`、owner、action 与
   DOM 绑定完全一致，并且执行瞬间的 frame、DOM 属性和 hit-test 仍匹配时才会交给
   Browser Harness。
6. 预期 HTTP 202，状态为 `executed_pending_verification`。这只证明 click 已派发；
   重新进行 KT6 capture，再调用 `POST /api/dom-actions/verify`；只有确定性验证返回
   `verified` 后，才能在业务评测中记为成功。

执行回执写入内存审计接口 `GET /api/dom-actions/audit`，计划进度通过
`GET /api/dom-actions/plans/{plan_id}` 查看；Browser Harness 隔离工作区位于
`runtime_data/browser_harness_workspace/`。当前代码不自动生成执行录像，也不把点击回执
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
