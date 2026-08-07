# KT6 / FreeStyleCopilot UI Graph A/B 测试手册

## 1. 测试目标

本手册用于比较现有页面感知方案与新 UI Graph 方案，重点验证：

1. 节点来源是否完整、可追溯。
2. DOM/CDP 点击候选是否准确，disabled、临时引用和非可信来源是否会被拒绝。
3. 页面主动暴露的只读 API Adapter 是否能进入统一图。
4. 内部 GLM5.1 是否能生成结构合法、目标可绑定的操作 DAG。
5. Playwright/CDP 采集是否能稳定保留 DOM、AX、iframe 和 Shadow DOM 结构。
6. 父子、归属、业务对象和动作关系是否能支持后续联动规划。
7. 与基线方案相比，识别耗时、准确率和资源消耗是否值得接受。

当前 A/B 分支如下：

| 分组 | 分支 | 提交 | 用途 |
|---|---|---|---|
| A 组 | `main` | `dd0aca6` | 当前基线：已有 DOM、Canvas、OpenCV/OCR 与安全动作链 |
| B 组 | `ui-graph-textflow-cdp` | `63f66ed` | 新增 Playwright/CDP、多源 UI Graph、内部 GLM 规划和 DAG 校验 |

`br_omniParser` 不参与本轮测试。当前 B 组只生成并校验 dry-run 操作计划，不执行真实点击。

开始前确认版本：

```powershell
git show-ref --heads main ui-graph-textflow-cdp
git rev-list --left-right --count main...ui-graph-textflow-cdp
```

预期两个分支相差一个提交，输出计数为 `0 1`。

## 2. 测试边界

- 所有页面数据、CDP 快照和 GLM 请求必须留在测试区内。
- CDP 调试地址只能监听 loopback，例如 `127.0.0.1:9222`。
- GLM endpoint 必须是测试区内部地址；非 loopback 地址必须使用 HTTPS。
- `interaction.candidate=true` 只代表“可进入实时预检的候选”，不代表现在可点击。
- UI Graph 和计划结果必须始终保持：

  - `analysis_only=true`
  - `dry_run_only=true`（计划响应）
  - `safe_for_execution=false`

- 当前不验收真实鼠标、键盘或 Playwright 点击执行。
- 自动化测试使用合成 DOM/CDP 和假 reasoner，不能替代真实 Chrome、NCE/FEBS 页面和内部 GLM 联调。

## 3. 环境准备

需要准备：

- Windows PowerShell。
- 项目所需 Python 环境。
- Node.js 和 npm。
- Chrome、Edge 或测试区批准的 Chromium。
- 可登录的真实测试页面。
- 内部 GLM5.1 endpoint、主机白名单和可选凭证。
- 如需验证页面 API，目标页面需主动暴露只读 `window.__KT6_PAGE_ADAPTER__`。

后端默认地址：

```text
http://127.0.0.1:8787/
```

健康检查：

```powershell
Invoke-RestMethod -Uri 'http://127.0.0.1:8787/api/health'
```

未配置内部 GLM 时，健康检查中的 `ui_graph_reasoning.configured` 应为 `false`。

### 推荐的 A/B 隔离方式

推荐使用独立 Git worktree，避免两个分支共用 `runtime_data`、SQLite、缓存和测试输出。
当前根目录保留 B 组，再创建一个 A 组目录：

```powershell
cd D:\yangzehui\FreeStyleCopilot
git worktree add ..\FreeStyleCopilot_AB_main main
```

得到：

| 分组 | 建议目录 |
|---|---|
| A 组 | `D:\yangzehui\FreeStyleCopilot_AB_main` |
| B 组 | `D:\yangzehui\FreeStyleCopilot` |

两个后端默认都使用 8787 端口，因此应顺序测试：停止 A 后再启动 B，不要同时启动。

如果测试区不允许创建 worktree，也可以在同一目录顺序切换分支，但必须：

1. 先停止后端。
2. 归档本轮结果。
3. 切换分支。
4. 确认没有复用上一组的 `runtime_data`、CDP 输出和浏览器采集结果。

## 4. 自动化回归

### 4.1 A 组全量回归

```powershell
cd D:\yangzehui\FreeStyleCopilot_AB_main
python -m unittest discover -s tests
```

### 4.2 B 组全量回归

```powershell
cd D:\yangzehui\FreeStyleCopilot
git switch ui-graph-textflow-cdp
python -m unittest discover -s tests
```

提交 `63f66ed` 的参考结果是 444 项测试通过、46 项按环境条件跳过。后续测试数量可能变化，
验收应以当前命令返回 `OK` 为准，不要只核对固定数量。

### 4.3 B 组定向回归

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

Sidecar 语法检查：

```powershell
cd D:\yangzehui\FreeStyleCopilot\browser_sidecar
npm install
npm run check
```

如果测试区不能访问公网 npm，请使用内部 npm 源或预置 `node_modules`。不要为了安装依赖把页面数据带出测试区。

## 5. 建议测试用例

所有用例必须在 A、B 两组使用相同页面、相同初始状态、相同缩放比例和相同操作指令。

| 编号 | 场景 | 重点检查 |
|---|---|---|
| T01 | 原生 DOM 按钮 | B 组是否产生 `source.kind=dom/cdp` 的稳定候选 |
| T02 | disabled/aria-disabled 按钮 | 不得成为可通过校验的 click 目标 |
| T03 | 多个同名“关闭/保存”按钮 | `parent_of/owner_of` 是否能区分所属设备或容器 |
| T04 | 表格、树、菜单、折叠面板 | 父子链、展开前后状态和联动计划是否正确 |
| T05 | iframe、Shadow DOM | `iframe_document/shadow_root` 关系是否完整 |
| T06 | 页面 API Adapter | 是否产生 `source.kind=page_api`，且不能自行授权点击 |
| T07 | Canvas/OpenCV/OCR 页面 | 视觉/文本证据是否进入图，但不越权成为可信点击目标 |
| T08 | 页面状态变化 | graph/content hash 是否变化，旧 capture 是否不会混入新计划 |
| T09 | 大页面或大量节点 | 超限时是否 fail closed，`truncated` 图不得进入规划 |
| T10 | 临时 DOM 引用 | `@capture:` 及大小写变体不得成为可重绑定点击目标 |

每个用例至少执行：

- 1 次冷启动采集。
- 4 次热采集。
- 1 次页面状态变化后的重新采集。

冷启动和热缓存数据分开统计，不要混合计算平均值。

## 6. A 组基线测试

### 6.1 启动 A 组

```powershell
cd D:\yangzehui\FreeStyleCopilot_AB_main
python -m kt6_backend.app
```

也可以使用兼容入口：

```powershell
python main.py
```

### 6.2 加载基线扩展

1. 在 Chrome 的 `chrome://extensions` 或 Edge 的 `edge://extensions` 打开开发者模式。
2. 加载 A 组目录下的 `browser_extension`。
3. 打开目标测试页面并完成登录。
4. 点击扩展采集按钮。
5. 记录 capture 结果、DOM/Canvas/OCR 数量、耗时和最终目标识别结果。

### 6.3 A 组记录项

- 页面 URL 和测试用例编号。
- 冷/热采集耗时。
- DOM 元素数量。
- Canvas/截图/OCR 区域数量。
- 目标文本是否识别。
- 目标业务对象是否识别。
- 同名控件是否选对所属对象。
- 现有安全动作链是否能正确 prepare/preflight。
- 错误信息和人工判断。

A 组没有 `/api/ui-graphs/*` 和 `/api/ui-operations/plan`，不要把缺少这些接口记为缺陷；
它们是 B 组新增能力。

完成后停止后端，再开始 B 组。

## 7. B 组真实页面联调

### 7.1 启动测试浏览器的 CDP

使用测试区批准的 Chromium 启动方式，至少包含：

```text
--remote-debugging-port=9222
--remote-debugging-address=127.0.0.1
--user-data-dir=<测试区专用浏览器目录>
```

要求：

- 只能绑定 `127.0.0.1`。
- 使用测试专用 profile，不要复用个人浏览器数据目录。
- 打开并登录目标测试页面。
- 保证 `--page-url` 能唯一匹配目标标签页。

### 7.2 使用 Sidecar 采集

```powershell
cd D:\yangzehui\FreeStyleCopilot\browser_sidecar
npm install

node .\capture-ui-graph.mjs `
  --cdp-url http://127.0.0.1:9222 `
  --page-url nce `
  --output .\cdp-snapshot-case01.json
```

预期标准输出包含：

```json
{"status":"captured","output":"...","frames":1}
```

注意：

- `--page-url` 是 URL 子串，应改成目标页面能唯一匹配的内容。
- Sidecar 使用独占创建，输出文件已存在时会失败。重测时使用新文件名或先人工归档旧文件。
- CDP 快照可能包含页面文本，必须作为测试敏感数据保存。
- Sidecar 只采集，不点击、不输入、不拦截网络，也不调用 `Runtime.evaluate`。

### 7.3 配置内部 GLM5.1

如果本轮只测试建图，可以暂不配置 GLM。此时计划接口返回 HTTP 503 是预期行为。

测试规划时，在启动后端前设置：

```powershell
$env:KT6_UI_GRAPH_REASONER_ENDPOINT = `
  'https://glm-gateway.example.internal/v1/ui-plan'
$env:KT6_UI_GRAPH_REASONER_ALLOWED_HOSTS = `
  'glm-gateway.example.internal'
$env:KT6_UI_GRAPH_REASONER_API_KEY = `
  '<internal-token>'
$env:KT6_UI_GRAPH_REASONER_TIMEOUT_SECONDS = '60'
```

要求：

- 把示例地址替换成测试区真实内部地址。
- API key 可选；不要把真实值写进文档、截图或 Git。
- 非 loopback endpoint 必须使用 HTTPS。
- `KT6_UI_GRAPH_REASONER_ALLOWED_HOSTS` 必须是精确主机名，不接受通配符或完整 URL。
- 超时必须大于 0 且不超过 300 秒。

### 7.4 启动 B 组后端

```powershell
cd D:\yangzehui\FreeStyleCopilot
git switch ui-graph-textflow-cdp
python -m kt6_backend.app
```

另开 PowerShell 检查：

```powershell
$health = Invoke-RestMethod `
  -Uri 'http://127.0.0.1:8787/api/health'

$health.ui_graph_reasoning
```

配置 GLM 后，`configured` 应为 `true`。健康检查中不应出现 endpoint 或 API key。

### 7.5 提交 CDP 快照并生成 UI Graph

在项目根目录执行：

```powershell
$snapshot = Get-Content `
  .\browser_sidecar\cdp-snapshot-case01.json `
  -Raw | ConvertFrom-Json

$captureBody = @{
  page = @{
    url   = $snapshot.page.url
    title = $snapshot.page.title
  }
  cdp_snapshot = $snapshot
} | ConvertTo-Json -Depth 100

$captureElapsed = Measure-Command {
  $capture = Invoke-RestMethod `
    -Method Post `
    -Uri 'http://127.0.0.1:8787/api/perception/captures' `
    -ContentType 'application/json' `
    -Body $captureBody
}

$captureElapsed.TotalMilliseconds
$capture.capture_id
$capture.summary
$capture.ui_graph_ref
```

`page.url` 和 `cdp_snapshot.page.url` 应来自同一次页面采集。至少 scheme、host、path 必须一致，
建议完整 URL 一致，避免把旧页面状态的快照提交到新页面。

成功预期：

- HTTP 201。
- 返回非空 `capture_id`。
- `summary.cdp_snapshot_available=true`。
- `summary.cdp_node_count`、`cdp_relation_count` 有合理数值。
- `ui_graph_ref.graph_id` 非空。
- `summary.ui_graph_truncated=false`。
- 普通 capture 响应不重复内嵌完整 UI Graph。

读取完整图：

```powershell
$graph = Invoke-RestMethod `
  -Uri "http://127.0.0.1:8787/api/ui-graphs/$($capture.capture_id)"

$graph.schema_version
$graph.graph_id
$graph.stats
```

预期：

- `schema_version=kt6.ui-graph.v1`。
- `graph.capture_id` 与请求 capture 一致。
- `analysis_only=true`。
- `execution_authorized=false`。
- `safe_for_execution=false`。

不存在的 capture 应返回 HTTP 404。

### 7.6 检查节点来源

```powershell
$graph.nodes |
  Group-Object { $_.source.kind } |
  Select-Object Name, Count

$graph.nodes |
  Select-Object id, role, name, disabled,
    @{Name='source'; Expression={$_.source.kind}},
    @{Name='candidate'; Expression={$_.interaction.candidate}},
    @{Name='source_ref'; Expression={$_.source.source_ref}},
    @{Name='backend_node_id'; Expression={$_.source.backend_node_id}} |
  Format-Table -AutoSize
```

检查：

- 来源只应是 `dom/cdp/page_api/vision/text` 或明确的派生类型。
- 相同对象的不同来源应保留为独立证据，不应静默覆盖。
- DOM 节点应能回溯 selector/ref。
- CDP 节点应能回溯 frame 和 backend node ID。
- bbox、名称、disabled 等字段发生冲突时，应能从 provenance 中看出来源。

### 7.7 检查点击候选和安全不变量

```powershell
$candidates = @(
  $graph.nodes |
    Where-Object { $_.interaction.candidate -eq $true }
)

$candidates |
  Select-Object id, name, disabled,
    @{Name='source'; Expression={$_.source.kind}},
    @{Name='source_ref'; Expression={$_.source.source_ref}},
    @{Name='backend_node_id'; Expression={$_.source.backend_node_id}}

$badDisabled = @(
  $graph.nodes |
    Where-Object {
      $_.disabled -eq $true -and
      $_.interaction.candidate -eq $true
    }
)

$unsafeNodes = @(
  $graph.nodes |
    Where-Object {
      $_.can_click_now -eq $true -or
      $_.safe_for_execution -eq $true
    }
)

if ($badDisabled.Count -ne 0) {
  throw 'disabled 节点被错误标成候选'
}
if ($unsafeNodes.Count -ne 0) {
  throw 'UI Graph 错误授予了执行能力'
}
```

人工检查：

- DOM 候选有稳定 ref/selector。
- `@capture:`、`@CAPTURE:` 等临时引用不能通过。
- CDP 候选有正整数 `backend_node_id`。
- disabled/blocked/analysis-only 节点不能成为有效 click 目标。
- page_api、vision、text 可以辅助定位，但不能单独让 click 计划通过验证。

### 7.8 检查父子和业务关系

```powershell
$graph.edges |
  Where-Object {
    $_.type -in @(
      'parent_of',
      'owner_of',
      'claims_business_object',
      'supports_action',
      'semantic_relation'
    )
  } |
  Select-Object type, source, target, relation_type |
  Format-Table -AutoSize
```

至少人工核对：

1. 同名按钮是否属于正确设备/表格行/菜单。
2. iframe 文档是否挂在正确 iframe 节点下。
3. Shadow DOM 节点是否保留宿主关系。
4. 操作控件是否通过 `owner_of` 或 `supports_action` 关联正确业务对象。
5. 候选节点的父链是否进入发给 GLM 的模型投影。

### 7.9 请求内部 GLM 生成操作 DAG

```powershell
$planBody = @{
  page_capture_id = $capture.capture_id
  instruction = '定位 AP1，在其操作区域找到关闭按钮并验证状态'
} | ConvertTo-Json

$planElapsed = Measure-Command {
  $plan = Invoke-RestMethod `
    -Method Post `
    -Uri 'http://127.0.0.1:8787/api/ui-operations/plan' `
    -ContentType 'application/json' `
    -Body $planBody
}

$planElapsed.TotalMilliseconds
$plan.status
$plan.validation
$plan.validation.plan.steps
```

有效计划预期：

- HTTP 200。
- `status=planned`。
- 返回 `graph_id` 与 UI Graph 一致。
- `dry_run_only=true`。
- `safe_for_execution=false`。
- `validation.valid=true`。
- 步骤只包含 `locate/click/wait/verify`。
- `depends_on` 不存在循环。
- click 只引用实际发送给 GLM 的 DOM/CDP 候选节点。

常见预期状态：

| HTTP | 含义 |
|---|---|
| 200 | 计划通过确定性校验 |
| 400 | 请求字段或长度非法 |
| 404 | capture/UI Graph 不存在 |
| 422 | 图截断、投影失败或模型计划未通过校验 |
| 502 | GLM 网络、响应 JSON、大小或 `graph_id` 错误 |
| 503 | 内部 reasoner 未配置 |

内部 GLM 响应必须是严格 JSON，至少包含：

```json
{
  "schema_version": "kt6.ui-operation-plan.v1",
  "graph_id": "<请求中的同一个 graph_id>",
  "steps": [
    {
      "id": "locate-ap1",
      "op": "locate",
      "target_node_id": "<图内节点 ID>"
    }
  ]
}
```

不允许脚本、自由表达式、未知操作或引用图投影之外的节点。

### 7.10 页面 API Adapter 测试

只在受控测试页面使用。目标页面需要主动暴露：

```javascript
window.__KT6_PAGE_ADAPTER__ = {
  snapshot() {
    return {
      ui_version: "adapter-test-v1",
      canvas: { width: 1200, height: 800 },
      objects: [
        {
          business_id: "ap-001",
          type: "ap",
          label: "AP-001",
          x: 100,
          y: 100,
          width: 120,
          height: 60
        }
      ],
      links: []
    };
  }
};
```

然后使用 B 组浏览器扩展重新采集，检查：

```powershell
$graph.nodes |
  Where-Object { $_.source.kind -eq 'page_api' } |
  Select-Object id, name, business_id,
    @{Name='candidate'; Expression={$_.interaction.candidate}}
```

预期：

- 出现 `source.kind=page_api` 的节点。
- 页面 API 中的敏感字段和执行声明被剥离。
- page_api 只参与结构/业务推理，不能自行让 click 计划通过。

这不是任意 REST API 抓取，也不监听 fetch/XHR；没有 Adapter 的页面应正常回退到 DOM/CDP/视觉路径。

## 8. A/B 指标记录

识别效率和规划效率分开统计：

```text
感知耗时 = 页面采集 + 后端 ingest/建图
规划耗时 = UI Graph 投影 + 内部 GLM + 确定性校验
总耗时   = 感知耗时 + 规划耗时
```

A 组没有 GLM 规划阶段，因此主要比较“感知耗时、目标识别和业务归属”；B 组的规划耗时单独列出，
不要把它混入识别耗时后直接与 A 组比较。

建议记录表：

| 用例 | 分支 | 冷/热 | 采集 ms | 后端感知 ms | 图节点/边 | 候选数 | 目标正确 | 父子/owner 正确 | GLM ms | validation | 备注 |
|---|---|---|---:|---:|---|---:|---|---|---:|---|---|
| T01 | main | 冷 |  |  | N/A | N/A |  |  | N/A | N/A |  |
| T01 | ui-graph-textflow-cdp | 冷 |  |  |  |  |  |  |  |  |  |

建议额外汇总：

- 目标节点召回率。
- 错误目标/同名控件误选率。
- disabled 节点误候选率。
- 父子和 owner 关系准确率。
- GLM 计划通过率与拒绝原因分布。
- UI Graph UTF-8 大小和模型投影大小。
- 冷启动中位数、热缓存中位数和 P95。

## 9. 必须满足的通过条件

以下安全条件任一失败，本轮 B 组不得进入真实执行联调：

1. 图或节点出现 `safe_for_execution=true`。
2. 计划响应不是 `dry_run_only=true`。
3. disabled 节点能通过 click 校验。
4. `@capture:` 临时引用能通过 click 校验。
5. page_api、vision 或 text 节点可以自行授权 click。
6. GLM 可以引用未发送给它的节点。
7. `capture_id/graph_id` 不匹配仍能生成有效计划。
8. 截断图仍能进入规划。
9. CDP 地址可以绑定非 loopback。
10. 页面数据被发送到测试区外部 endpoint。

功能验收至少应满足：

- UI Graph 来源统计与页面实际来源一致。
- 同名控件能依靠父子/owner 关系区分。
- iframe/Shadow DOM 测试页面的父链完整。
- 页面状态变化后 graph/content hash 有变化。
- GLM DAG 的依赖顺序符合操作意图。
- 所有自动化回归通过。

性能阈值不在代码中写死，由测试负责人根据真实页面和机器基线确定。

## 10. 常见问题

### `npm install` 失败

使用测试区内部 npm 源或预置依赖。`playwright-core` 不下载浏览器，但仍需要安装 npm 包。

### Sidecar 提示页面匹配不唯一

关闭无关标签页，或把 `--page-url` 改成更长、更唯一的 URL 子串。

### Sidecar 提示输出文件已存在

输出使用独占创建。换一个带用例编号或时间戳的文件名，或先人工归档旧文件。

### capture 返回 400 page route mismatch

确认请求 `page.url` 和 CDP 快照来自同一页面。不要把上一轮页面的快照提交给当前页面。

### 计划接口返回 503

内部 GLM reasoner 未配置，或环境变量是在后端启动之后设置的。设置后重新启动后端。

### 计划接口返回 502

检查内部 GLM：

- endpoint 是否可达。
- HTTPS/TLS 是否正确。
- 主机是否在精确白名单。
- 返回是否为严格 JSON。
- 是否回显相同 `graph_id`。
- 响应是否超过 64 KiB。

### 计划接口返回 422

查看响应中的投影或 validation 错误。常见原因包括图被截断、循环依赖、节点不存在、
click 目标不是可信候选、模型引用了投影外节点。

### UI Graph 没有点击候选

检查目标是否 disabled、是否缺少稳定 DOM ref、是否只有 AX 虚拟节点、是否只有 OCR/page_api 证据。
没有可信候选时保持 0 个候选是正确的 fail-closed 行为。

## 11. 测试交付物

每轮至少保存：

- 分支名和完整 commit hash。
- 测试机器、浏览器和 Python/Node 版本。
- 测试用例编号和页面版本。
- A/B 指标表。
- 后端日志。
- capture ID、graph ID 和计划 validation 结果。
- 失败用例的最小复现步骤。
- CDP 快照的受控存放位置，不要把原始快照提交 Git。

最终结论应分别回答：

1. B 组是否提高了结构识别准确率。
2. 增加的 CDP/建图耗时是多少。
3. 父子和业务归属是否减少了同名控件误选。
4. 内部 GLM 是否能稳定生成通过校验的 DAG。
5. 是否存在任何点击安全边界失败。
6. 是否建议继续开发真实执行器，或仅保留为分析/规划能力。
