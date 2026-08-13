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

## 4. 公共自动化回归

```powershell
python -m unittest discover -s tests
```

2026-08-13 开发机最近一次结果：

| 分支 | 结果 |
|---|---|
| `main` | 433 tests OK，46 skipped |
| `eval-current` | 517 tests OK，46 skipped |
| `eval-browser-use` | 455 tests OK，46 skipped |
| `eval-ui-tars` | 457 tests OK，46 skipped |

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

## 10. `br_omniParser` 历史分支

只运行全量回归和公共配置/报告测试，不新增当前三方案结果。OmniParser 结果不能与
`current/browser_use/ui_tars` 混在同一正式比较中。若公共代码同步导致回归，修复公共
兼容性，不继续扩展 OmniParser 功能。

## 11. 统一评测报告

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

## 12. 测试交付物

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
