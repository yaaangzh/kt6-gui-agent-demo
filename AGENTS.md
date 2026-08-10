# AGENTS.md

本文件供在 `D:\yangzehui\FreeStyleCopilot` 工作的开发 Agent 使用。开始修改前，先读
`README.md`、`CODEX_HANDOFF.md` 和 `test.md`，再执行 Git 状态检查。不要只依据旧对话、
历史哈希或缓存的远端状态继续工作。

## 1. 项目定位

KT6 / FreeStyleCopilot 是无线网络运维 PoC，目标是把自然语言意图、页面感知、拓扑
理解、业务 Playbook、人在环确认和受控操作计划串成一条可审计链路。

当前主要分支：

| 分支 | 用途 |
|---|---|
| `main` | 现有 DOM、Canvas、OpenCV/OCR、Runtime 和安全动作链基线 |
| `ui-graph-textflow-cdp` | Playwright/CDP、多源 UI Graph、操作 DAG 规划实验分支 |
| `br_omniParser` | 已停止作为当前 A/B 方案的 OmniParser 试点，不要混入 UI Graph 分支 |

新 UI Graph 方案必须在独立分支完成真实测试后再决定是否合入 `main`。未经用户明确
要求，不要合并、变基、删除分支、清理 stash、创建 PR 或改写远端历史。

## 2. 项目架构

### 2.1 Runtime 与业务链路

```text
用户自然语言
→ Intent Agent
→ Playbook Router
→ Runtime 状态机
→ Tool Registry / Business Adapter
→ Scene / Page Perception
→ 人在环确认
→ dry-run 安全动作计划
```

关键文件：

```text
kt6_backend/app.py
kt6_backend/runtime.py
kt6_backend/agent.py
kt6_backend/router.py
kt6_backend/playbook_loader.py
kt6_backend/tool_registry.py
kt6_backend/tools.py
```

### 2.2 页面感知

浏览器扩展负责一次采集中的 DOM/ARIA 与 Canvas/SVG 分治：

```text
browser_extension/
  content-collector.js    DOM/ARIA、selector、frame、业务 ID、视觉 ROI
  popup-v2.js             提交异步 capture job、恢复任务进度
  manifest.json           Chrome/Edge 扩展权限与版本
```

后端处理入口：

```text
kt6_backend/page_perception.py
kt6_backend/page_capture_jobs.py
```

Canvas 视觉链路：

```text
Canvas/SVG 截图
→ OpenCV/OCR 提取文字、坐标和像素连线
→ codeagent.bat 调用测试区内部 GLM5.1 补充语义
→ 确定性融合
→ vision/text 证据
```

相关文件：

```text
kt6_backend/local_cv_canvas_vision.py
kt6_backend/codeagent_canvas_vision.py
kt6_backend/hybrid_canvas_vision.py
kt6_backend/topology_fusion.py
```

### 2.3 Playwright/CDP Sidecar

`browser_sidecar/capture-ui-graph.mjs` 通过 loopback CDP 只读采集：

- DOMSnapshot
- Accessibility Tree
- iframe/frame/document
- Shadow DOM
- backend node id

Sidecar 不点击、不输入、不拦截网络，也不调用 `Runtime.evaluate`。扩展 capture 与 CDP
Sidecar capture 当前是独立 capture，不要误称已经实时合并。

### 2.4 UI Graph

`kt6_backend/ui_graph.py` 把以下来源转为统一 JSON 图协议：

```text
dom / cdp / page_api / vision / text
```

节点保留来源、role、name、frame/document、稳定引用、disabled 和 interaction；边表达：

```text
parent_of
owner_of
claims_business_object
supports_action
semantic_relation
```

UI Graph 是结构化数据模型，不一定是独立文件。完整图可通过
`GET /api/ui-graphs/{capture_id}` 获取；发送给模型时使用有界 JSON/文本投影。

### 2.5 操作 DAG

```text
UI Graph
→ Reasoner 提出 locate/click/wait/verify DAG
→ ui_operation_graph.py 确定性校验
→ dry-run 计划
```

相关文件：

```text
kt6_backend/ui_graph_reasoner.py
kt6_backend/ui_graph_planning.py
kt6_backend/ui_operation_graph.py
```

重要现状：当前 UI Graph Reasoner 只实现 `HTTPUIGraphReasoner`。测试区内部 GLM5.1
实际通过 `D:\03CodeAgent\CodeAgentCLI\codeagent.bat` 使用，目前没有 UI Graph
CodeAgent CLI Adapter。因此：

- Canvas 视觉可以真实使用 CodeAgent/GLM5.1。
- UI Graph 建图和 DAG 验证可以自动化测试。
- 真实 CodeAgent/GLM5.1 UI Graph 操作编排尚未接通。
- 未配置 HTTP Reasoner 时计划接口返回 503 是当前预期。

不要在文档、汇报或测试结论中把上述边界写成“内部 GLM 操作编排已实机完成”。

### 2.6 DOM 安全动作链

`asset_inventory.py`、`dom_action_binding.py`、`safe_dom_actions.py` 提供资产唯一解析、
控件归属、新鲜页面复核、一次性令牌和审计。当前只有 dry-run，没有真实浏览器点击或
设备下发通道。

## 3. 关键约束

### 3.1 数据与模型

- 测试区页面数据不能传给 Codex、公共模型、外部 SaaS 或未经批准的 endpoint。
- 测试区内部 GLM5.1 的实际入口是 `codeagent.bat`，不是独立 HTTP 服务。
- CodeAgent events、CDP 快照、UI Graph 和截图可能包含敏感页面数据，不得提交 Git。
- 不要在日志、健康检查、文档或回复中输出 API key、token、图片 Base64 或完整敏感 URL。

### 3.2 UI Graph 与执行安全

- UI Graph、节点、边和操作计划必须保持 `safe_for_execution=false`。
- 计划必须保持 `dry_run_only=true`，除非未来有独立、经过批准的执行链变更。
- `interaction.candidate=true` 只表示允许进入校验，不表示可立即点击或已授权。
- 只有 DOM/CDP 节点可以成为点击候选。
- DOM 候选必须有稳定 selector/source ref。
- CDP 候选必须有正整数 backend node id。
- page_api、vision、text 只能辅助理解，不能授权点击。
- disabled/blocked 节点不能成为有效候选。
- `@capture:` 及任意大小写变体不能成为稳定点击目标。
- 原始 `actionable=true`、business_id 或 element_id 不能绕过候选门禁。
- graph/capture 绑定、模型投影范围和 DAG 依赖必须 fail closed。

### 3.3 页面 API 与 CDP

- 页面 API 只读取页面显式提供的 `window.__KT6_PAGE_ADAPTER__`。
- 不 monkey-patch 或拦截任意 `fetch`/XHR，不自行扫描业务 REST API。
- CDP 地址只能是 `localhost`、`127.0.0.1` 或 `::1`。
- Sidecar 输出文件独占创建；重测使用新文件名，不覆盖旧快照。

### 3.4 Git 与工作区

- `review.md` 是用户未跟踪文件，除非用户明确要求，否则不得修改、暂存或提交。
- OmniParser WIP 保存在 stash；用 `git stash list` 核对，不要自动 apply/drop。
- `runtime_data/` 可能保存耗时模型结果和敏感测试产物，不要无依据清理。
- 工作区可能包含用户修改；只暂存当前任务明确涉及的文件，避免 `git add -A`。
- 不使用 `git reset --hard`、`git checkout --` 或强制删除来清理用户改动。
- 远端状态必须先 fetch 再判断；不要沿用文档中的历史哈希。
- GitHub SSH 22 端口在当前网络可能长时间无响应；必要时使用已认证 HTTPS push，
  但不要未经授权改写持久 remote 配置。

## 4. 常用命令

### 4.1 接手检查

```powershell
cd D:\yangzehui\FreeStyleCopilot

git status -sb
git branch --show-current
git branch -vv
git log -5 --oneline --decorate
git stash list
```

### 4.2 全量与定向测试

在未配置真实 Vision/CodeAgent 环境变量的干净 PowerShell 中运行：

```powershell
python -m unittest discover -s tests
```

当前 B 组参考基线：444 项通过、46 项按环境跳过；以当前输出 `OK` 为准。

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

定向参考基线：57 项通过。

### 4.3 配置真实 Canvas 混合识别

```powershell
$env:KT6_VISION_DRIVER = 'hybrid'
$env:KT6_HYBRID_MODEL_DRIVER = 'codeagent_cli'
$env:KT6_CODEAGENT_EXECUTABLE = `
  'D:\03CodeAgent\CodeAgentCLI\codeagent.bat'
$env:KT6_VISION_TIMEOUT_SECONDS = '300'
```

环境变量必须在启动后端前设置；修改后重启后端。

### 4.4 启动后端

```powershell
python -m kt6_backend.app
```

```powershell
Invoke-RestMethod -Uri 'http://127.0.0.1:8787/api/health' |
  ConvertTo-Json -Depth 10
```

### 4.5 Sidecar 检查与采集

```powershell
cd .\browser_sidecar
npm install
npm run check
```

```powershell
$snapshotPath = '.\cdp-snapshot-' + `
  (Get-Date -Format 'yyyyMMdd-HHmmss') + '.json'

node .\capture-ui-graph.mjs `
  --cdp-url http://127.0.0.1:9222 `
  --page-url nce `
  --output $snapshotPath
```

### 4.6 快速查看 JSON

```powershell
$graph = Get-Content .\ui-graph.json -Raw | ConvertFrom-Json
$graph | Select-Object graph_id, capture_id, analysis_only, safe_for_execution
$graph.stats

$graph.nodes |
  Group-Object { $_.source.kind } |
  Select-Object Name, Count
```

读取 API 图：

```powershell
$graph = Invoke-RestMethod `
  -Uri 'http://127.0.0.1:8787/api/ui-graphs/<capture_id>'
```

### 4.7 文档修改检查

```powershell
git diff --check
git diff --stat
git status -sb
```

文档变更不需要重复跑全部代码测试，但必须检查 Markdown 围栏、相对链接和过时的
分支/测试状态。

## 5. 容易踩坑的地方

### 5.1 把两种 GLM 调用方式混为一谈

Canvas 视觉已经通过 `codeagent.bat` 使用内部 GLM5.1；UI Graph Reasoner 当前仍是 HTTP
实现。不要配置一个不存在的内部 GLM endpoint，也不要把 Fake Reasoner 测试当成实机联调。

### 5.2 把候选误认为可点击

`role=button` 或 `interaction.candidate=true` 不等于已授权。当前所有节点仍应
`can_click_now=false`、`safe_for_execution=false`。

### 5.3 把 Canvas/OCR 坐标当成 DOM 点击依据

视觉和 OCR 只提供语义、坐标和关系证据，不能直接授权点击。真实动作必须重新绑定到
可信 DOM/CDP 节点并经过原有安全动作链。

### 5.4 扩展采集与 CDP capture 混淆

扩展一次任务可以同时处理 DOM 与 Canvas/SVG；CDP Sidecar 是另一条只读采集通道。
当前两者生成独立 capture，测试和汇报时必须分开说明。

### 5.5 大页面截断

UI Graph 当前上限为 2000 节点、8000 边。图被截断时 planning 应 fail closed；不要为
追求成功率直接放宽上限或绕过截断检查。先在真实页面记录来源占比和目标祖先链。

### 5.6 多 frame 同名 alias

多 iframe 出现相同 selector/ref 时，DOM action binding 可能保守地标为 unresolved。
这是安全拒绝，不是误点击；需要通过 frame/document scope 精确修复，不能全局猜测。

### 5.7 扩展更新未生效

修改扩展文件后必须在 `chrome://extensions` 或 `edge://extensions` 点击“重新加载”。弹窗
关闭不会终止异步 capture job，重开后应恢复，而不是重复提交。

### 5.8 环境变量继承

后端只在启动时读取环境变量。在另一个 PowerShell 设置变量无效；改完配置必须重启
后端。自动化单元测试应在未配置真实 driver 的干净窗口运行。

### 5.9 沙箱与临时目录权限

部分 Python 测试需要在系统临时目录创建 SQLite 和 assets。受限沙箱可能报
`PermissionError`，应在获准后提升权限重跑，不能把权限错误当成代码失败。

### 5.10 CodeAgent 非确定性与大日志

CodeAgent 可能超时、重复读取图片或返回带说明的 JSON。保留 events/stderr 和尝试日志；
`codeagent-events.jsonl` 可能包含图片 Base64，不要完整打印或上传。

## 6. 修改与交付准则

- 诊断请求只报告原因，不主动实施修复；用户明确要求修改时才改代码。
- 修改应保持最小范围，优先复用现有 Adapter、契约和测试 fixture。
- 安全相关修改必须补正向和反向回归，特别是来源、稳定 ref、disabled、大小写变体、
  graph/capture 绑定和 DAG 依赖。
- 代码变更至少运行相关定向测试；高风险或跨模块修改运行全量测试。
- 只在用户明确要求时 commit/push；推送成功前不能说“已上传”。
- A/B 真实页面结果出来前，不把 `ui-graph-textflow-cdp` 合入 `main`。

## 7. 权威文档

- `README.md`：项目能力、架构、API 和运行入口。
- `CODEX_HANDOFF.md`：当前状态、历史问题、测试事实和接手清单。
- `test.md`：当前测试区可执行流程和真实边界。
- `docs/ui-graph-architecture.md`：UI Graph 契约、安全边界和设计说明。

文档与实际代码或 Git 状态冲突时，以当前代码、命令输出和真实测试证据为准，并在同一
任务中修正文档。
