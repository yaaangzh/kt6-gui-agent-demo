# KT6 / FreeStyleCopilot 新分支测试手册

适用分支：`ui-graph-textflow-cdp`

测试目录：`D:\04project\FreeStyle_Copilot_KT6_demo`

测试环境：OpenCV/OCR + CodeAgentCLI（内部配置 GLM5.1）

## 1. 本轮测试范围

本轮不修改代码，只验证当前新分支：

1. 扩展同时采集 DOM/ARIA 和 Canvas/SVG。
2. Canvas 使用 OpenCV/OCR + `codeagent.bat` 混合识别。
3. Playwright/CDP 采集 DOMSnapshot、AXTree、iframe 和 Shadow DOM。
4. DOM、CDP、page_api、vision、text 结果进入 UI Graph。
5. UI Graph 正确标记来源、父子关系、控件归属和点击候选。
6. 所有结果保持 analysis-only，不执行真实点击。

当前边界：UI Graph 操作规划只实现了 HTTP Reasoner，尚未通过 `codeagent.bat` 调用
测试区内部 GLM5.1。本轮不配置 HTTP Reasoner；`/api/ui-operations/plan` 返回 503
属于预期结果。自动化测试中的 Fake Reasoner 只验证操作 DAG 契约和安全校验。

扩展采集与 CDP Sidecar 当前产生独立 capture，本轮分别验收，不能描述成一张实时
合并的五源图。

## 2. 安全要求

- 页面数据、截图、CDP 快照和 CodeAgent 输出只能保存在测试区。
- CodeAgent 入口固定为测试区本机 `codeagent.bat`。
- CDP 只能监听 loopback 地址。
- 使用测试专用浏览器 profile。
- Sidecar 只读采集，不点击、不输入、不拦截网络。
- UI Graph 必须始终 `safe_for_execution=false`。
- 测试产物不得提交 Git 或上传外部服务。

## 3. 同步分支并检查环境

```powershell
cd D:\04project\FreeStyle_Copilot_KT6_demo

git fetch origin ui-graph-textflow-cdp
git switch ui-graph-textflow-cdp
git pull
git status --short
git log -3 --oneline --decorate

python --version
node --version
npm --version
Test-Path 'D:\03CodeAgent\CodeAgentCLI\codeagent.bat'
```

最后一项必须返回 `True`。`br_omniParser` 不参与本轮测试。

安装并检查 Sidecar：

```powershell
cd .\browser_sidecar
npm install
npm run check
```

`npm install` 使用测试区批准的内部源或预置依赖。

## 4. 自动化回归

建议在未设置真实 Vision/CodeAgent 环境变量的新 PowerShell 中运行：

```powershell
cd D:\04project\FreeStyle_Copilot_KT6_demo
python -m unittest discover -s tests
```

开发环境参考结果：

```text
Ran 444 tests
OK (skipped=46)
```

UI Graph/CDP 定向回归：

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

开发环境参考结果：`57 tests OK`。测试数量可能变化，以当前命令最终返回 `OK` 为准。

## 5. 配置视觉识别并启动后端

另开 PowerShell，设置当前真实测试配置：

```powershell
$env:KT6_VISION_DRIVER = 'hybrid'
$env:KT6_HYBRID_MODEL_DRIVER = 'codeagent_cli'
$env:KT6_CODEAGENT_EXECUTABLE = `
  'D:\03CodeAgent\CodeAgentCLI\codeagent.bat'
$env:KT6_VISION_TIMEOUT_SECONDS = '300'

cd D:\04project\FreeStyle_Copilot_KT6_demo
python -m kt6_backend.app
```

不要设置：

```text
KT6_UI_GRAPH_REASONER_ENDPOINT
KT6_UI_GRAPH_REASONER_ALLOWED_HOSTS
KT6_UI_GRAPH_REASONER_API_KEY
```

健康检查：

```powershell
$health = Invoke-RestMethod `
  -Uri 'http://127.0.0.1:8787/api/health'

$health.ui_graph_reasoning
```

预期 `configured=false`。这表示真实 CodeAgent UI Graph Adapter 尚未接入，不影响本轮
视觉识别和 UI Graph 建图测试。

## 6. 扩展 DOM + Canvas 测试

在 Chrome/Edge 扩展管理页加载并重新加载：

```text
D:\04project\FreeStyle_Copilot_KT6_demo\browser_extension
```

打开目标页面，点击“采集当前页面”，检查：

- DOM、ARIA、selector、frame/document 和业务 ID 被采集。
- Canvas/SVG/地图/拓扑区域产生视觉 ROI。
- Canvas 经 OpenCV/OCR 和 `codeagent.bat` 混合识别。
- DOM 与视觉结果并行保留，不互相覆盖。
- 关闭弹窗后异步任务继续，重新打开可恢复进度。
- 完成后显示 `capture_id`。

记录扩展返回的 capture ID：

```powershell
$extensionCaptureId = '<capture_id>'

$extensionCapture = Invoke-RestMethod `
  -Uri "http://127.0.0.1:8787/api/perception/captures/$extensionCaptureId"

$extensionGraph = Invoke-RestMethod `
  -Uri "http://127.0.0.1:8787/api/ui-graphs/$extensionCaptureId"

$extensionGraph.nodes |
  Group-Object { $_.source.kind } |
  Select-Object Name, Count
```

根据页面实际内容，扩展图可能包含 `dom/page_api/vision/text`。页面没有 Canvas 时不要求
产生 vision；页面没有 Adapter 时不要求产生 page_api。

## 7. CDP 结构采集测试

使用测试专用 profile 启动 Chrome：

```powershell
chrome.exe `
  --remote-debugging-port=9222 `
  --remote-debugging-address=127.0.0.1 `
  --user-data-dir=D:\04project\kt6-chrome-profile
```

登录目标页面，关闭无关标签页，然后采集：

```powershell
cd D:\04project\FreeStyle_Copilot_KT6_demo\browser_sidecar

$snapshotPath = '.\cdp-snapshot-' + `
  (Get-Date -Format 'yyyyMMdd-HHmmss') + '.json'

node .\capture-ui-graph.mjs `
  --cdp-url http://127.0.0.1:9222 `
  --page-url nce `
  --output $snapshotPath
```

把 `nce` 改成能唯一匹配目标标签页的 URL 子串。输出文件已存在时使用新文件名。

## 8. 提交 CDP 快照并生成 UI Graph

```powershell
$snapshot = Get-Content $snapshotPath -Raw | ConvertFrom-Json

$captureBody = @{
  page = @{
    url   = $snapshot.page.url
    title = $snapshot.page.title
  }
  cdp_snapshot = $snapshot
} | ConvertTo-Json -Depth 100

$elapsed = Measure-Command {
  $cdpCapture = Invoke-RestMethod `
    -Method Post `
    -Uri 'http://127.0.0.1:8787/api/perception/captures' `
    -ContentType 'application/json' `
    -Body $captureBody
}

$cdpGraph = Invoke-RestMethod `
  -Uri "http://127.0.0.1:8787/api/ui-graphs/$($cdpCapture.capture_id)"

$elapsed.TotalMilliseconds
$cdpCapture.summary
$cdpGraph.stats
```

预期：

- `schema_version=kt6.ui-graph.v1`。
- capture 与 graph ID 对应。
- `analysis_only=true`。
- `execution_authorized=false`。
- `safe_for_execution=false`。
- `stats.truncated=false`。

## 9. 检查来源、父子关系和点击候选

```powershell
$cdpGraph.nodes |
  Group-Object { $_.source.kind } |
  Select-Object Name, Count

$cdpGraph.edges |
  Where-Object {
    $_.type -in @(
      'parent_of',
      'owner_of',
      'claims_business_object',
      'supports_action',
      'semantic_relation'
    )
  } |
  Select-Object type, source, target, relation_type

$candidates = @(
  $cdpGraph.nodes |
    Where-Object { $_.interaction.candidate -eq $true }
)

$candidates |
  Select-Object id, role, name, disabled,
    @{Name='source'; Expression={$_.source.kind}},
    @{Name='source_ref'; Expression={$_.source.source_ref}},
    @{Name='backend_node_id'; Expression={$_.source.backend_node_id}}
```

必须满足：

- DOM 候选有稳定 selector/source ref。
- CDP 候选有正整数 backend node id。
- disabled 节点不能成为有效候选。
- `@capture:` 及大小写变体不能成为稳定目标。
- page_api、vision、text 不能自行成为点击候选。
- 所有节点 `can_click_now=false`、`safe_for_execution=false`。
- 同名按钮能通过 parent/owner 关系区分所属设备或容器。
- iframe 和 Shadow DOM 父链符合页面实际结构。

快速安全检查：

```powershell
$bad = @(
  $cdpGraph.nodes |
    Where-Object {
      ($_.disabled -eq $true -and $_.interaction.candidate -eq $true) -or
      $_.can_click_now -eq $true -or
      $_.safe_for_execution -eq $true -or
      ($_.interaction.candidate -eq $true -and
       $_.source.kind -notin @('dom', 'cdp')) -or
      ($_.interaction.candidate -eq $true -and
       ([string]$_.source.source_ref).ToLowerInvariant().Contains('@capture:'))
    }
)

if ($bad.Count -ne 0) {
  throw "发现 $($bad.Count) 个违反安全边界的节点"
}
```

对 `$extensionGraph` 重复相同检查。

## 10. 页面 API Adapter 测试

仅在受控页面主动提供 `window.__KT6_PAGE_ADAPTER__` 时测试。重新用扩展采集后检查：

```powershell
$extensionGraph.nodes |
  Where-Object { $_.source.kind -eq 'page_api' } |
  Select-Object id, name, business_id,
    @{Name='candidate'; Expression={$_.interaction.candidate}}
```

预期 page_api 只参与结构和业务推理，不能授权点击。当前实现不拦截任意 `fetch`/XHR。

## 11. 操作规划接口的当前预期

本轮不配置 HTTP Reasoner，调用：

```text
POST /api/ui-operations/plan
```

预期返回 HTTP 503。测试结论统一写为：

> UI Graph 建图、模型投影和操作 DAG 确定性验证已通过自动化测试；测试区真实
> CodeAgent/GLM5.1 操作编排尚未接入，未配置 Reasoner 时接口按预期返回 503。

不得写成“内部 GLM5.1 已经基于 UI Graph 完成真实操作编排”。

## 12. 建议用例

| 编号 | 场景 | 检查点 |
|---|---|---|
| T01 | 原生 DOM 按钮 | role、selector 和候选正确 |
| T02 | disabled 按钮 | 不得成为有效候选 |
| T03 | 多个同名按钮 | parent/owner 区分归属 |
| T04 | 表格、树、菜单 | 父子链和动作归属正确 |
| T05 | iframe、Shadow DOM | frame/document/宿主关系完整 |
| T06 | 页面 API Adapter | page_api 不授权点击 |
| T07 | Canvas/SVG 拓扑 | CV/OCR + CodeAgent 识别成功 |
| T08 | DOM + Canvas 混合页 | 两路结果并行保留 |
| T09 | 页面状态变化 | 新旧 capture/graph hash 变化 |
| T10 | 大页面 | 超限截断并保持 fail closed |
| T11 | `@capture:` 变体 | 不得成为稳定目标 |
| T12 | CodeAgent 超时/无效 JSON | 有限重试并保留日志 |

## 13. 记录指标

| 用例 | 页面类型 | DOM 数 | Canvas/ROI | CV/OCR ms | CodeAgent ms | CDP ms | 图节点/边 | 候选数 | 目标正确 | 父子/owner 正确 | 备注 |
|---|---|---:|---:|---:|---:|---:|---|---:|---|---|---|
| T01 | DOM |  |  |  |  |  |  |  |  |  |  |
| T07 | Canvas |  |  |  |  |  |  |  |  |  |  |
| T08 | 混合 |  |  |  |  |  |  |  |  |  |  |

重点汇总目标命中率、同名控件误选率、父子/owner 准确率、CodeAgent 成功/超时率、
UI Graph 大小以及冷启动、热缓存和 P95 耗时。

## 14. 通过条件

- 全量和 UI Graph/CDP 定向测试返回 `OK`。
- `npm run check` 通过。
- 扩展能采集真实 DOM 和 Canvas 页面。
- Canvas 能走 hybrid + codeagent_cli 视觉路线。
- CDP 能采集真实 DOMSnapshot 和 AXTree。
- UI Graph 来源、父子和 owner 关系符合页面事实。
- disabled、临时 ref、page_api、vision、text 均不能越权。
- 图和节点始终不授予执行能力。
- 页面数据和 CodeAgent 输出不离开测试区。

当前计划接口返回 503 不作为失败；真实 CodeAgent/GLM5.1 操作 DAG 不属于本轮通过条件。

## 15. 测试交付物

每轮使用独立目录保存：

```text
git-version.txt
unittest-full.log
unittest-ui-graph.log
extension-capture.json
cdp-snapshot.json
extension-ui-graph.json
cdp-ui-graph.json
codeagent-events.jsonl
codeagent-stderr.log
metrics.csv
issues.md
conclusion.md
```

快照、图、截图和 events 可能包含真实页面数据，不得提交 Git。

测试结论至少写明：分支、提交、页面版本、自动化结果、DOM/Canvas 结果、CDP 结果、
UI Graph 结构结果、安全检查结果、当前 503 边界，以及“可继续测试/需修复/不建议合入”。
