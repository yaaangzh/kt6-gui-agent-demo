# KT6 / FreeStyleCopilot Codex 交接总结

本文用于把当前项目迁移到另一个 Codex 任务或新的开发环境。接手者应先阅读本文
和 [README.md](./README.md)，再检查 Git 状态和最近提交，不要仅依据旧对话继续
修改。

## 1. 当前阶段目标

当前优先级是先稳定打通拓扑图片的完整处理链路：

```text
原始拓扑图片
├─ RapidOCR + OpenCV
│  └─ cv-result.json
│
└─ 图片 + 精简 CV 候选
   └─ CodeAgentCLI / 默认多模态模型
      ├─ model-result.json
      ├─ codeagent-events.jsonl
      └─ codeagent-stderr.log

cv-result.json + model-result.json
└─ Python 确定性融合
   └─ fused-result.json
```

当前不以单一算法达到 100% 准确率为目标。CV 提供坐标、OCR 置信度和像素连线，
模型提供语义、层级、连接和结构判断，最后通过确定性算法互补融合。

同时已经增加在线页面 DOM 路线。由于当前仓库没有 FEBS/NCE 前端源码，暂不做
嵌入式 SDK 集成；现阶段使用 Chrome/Edge 扩展 v0.5.1，由用户显式采集普通
HTTP(S) 页面中的 DOM/ARIA、iframe 上下文和稳定选择器，并自动检测可见的
Canvas/SVG/图形区域。扩展每次采集至多截取一个视觉主帧，与 DOM 证据一起提交给
本机 KT6。耗时识别由后端异步 capture job 执行，扩展把待完成 `job_id` 保存到
`chrome.storage.local`，关闭并重开弹窗后可以继续查询；同步 capture 接口仍保留。

页面可选显式暴露只读 `window.__KT6_PAGE_ADAPTER__` 快照。扩展只调用这个明确的
适配器，不会监听或拦截任意 `fetch`/XHR；`snapshot_complete` 决定快照能否作为完整
页面 API 证据，不完整快照会与像素视觉并行并作为失败回退保留。后端以 `dom_scene`、
`canvas_scene` 分治处理并另行保留 `page_api_perception`，节点用
`source.kind=dom|page_api|vision` 标明来源，并通过 `interaction.status`/`can_click_now` 明确交互资格。DOM `ui_tree` 是
带 frame 根、父子引用和省略祖先计数的语义投影，`action_binding_complete` 单独表示
动作证据是否完整。

扩展同时采集资产 ID、管理 IP、序列号、站点、版本、动作 ID 和控件归属。后端已
实现权威资产唯一解析、设备/控件双重绑定、六步 `operation_plan`、新鲜页面复核、
一次性令牌和 dry-run；计划与令牌过期可通过查询接口观察。扩展仍不直接点击页面，
服务端也没有真实设备动作通道。

用户已明确：先把链路打通，真实图片准确率、批量评测、复杂图片超长 timeout
和更深层模型优化之后再处理。

## 2. 仓库与工作目录

当前机器：

```text
开发目录：D:\yangzehui\FreeStyleCopilot
测试目录（另一台测试机）：D:\04project\FreeStyle_Copilot_KT6_demo
GitHub：git@github.com:yaaangzh/kt6-gui-agent-demo.git
分支：main
当前本地 HEAD 与远端状态：以 `git log`、`git status` 和 `git fetch` 的结果为准
```

当前开发机未挂载 `D:\04project`，不要把测试目录不存在误判为代码问题。

包含本文和 README 更新的实际最新提交应以以下命令为准：

```powershell
git log -1 --oneline --decorate
```

注意事项：

- 当前网络下 GitHub SSH 22 端口可能不可用；远端状态以 `git fetch` 后结果为准。
- 本轮 README/交接文档修改是否已提交，必须以 `git status` 和 `git log` 为准。
- `review.md` 是用户的未跟踪文件，不得加入提交。
- 用户说“提交”时，默认同时 commit 和 push，除非明确要求仅本地提交。
- 当前工作直接更新 `main`，没有使用 PR。
- 最近为了把修复合并到同一提交，多次执行过 amend 和
  `--force-with-lease`。测试环境同步旧历史时，先确认没有本地代码修改，再
  `git fetch origin main` 和 `git reset --hard origin/main`。
- 不要无依据清理 `runtime_data/`；其中可能保留耗时数分钟的真实模型结果。

## 3. 当前代码状态

分阶段链路主要文件：

```text
kt6_backend/topology_artifact_common.py
kt6_backend/topology_cv_cli.py
kt6_backend/topology_model_cli.py
kt6_backend/topology_model_contract.py
kt6_backend/topology_hybrid_cli.py
kt6_backend/topology_fusion.py
kt6_backend/topology_fusion_cli.py
kt6_backend/codeagent_canvas_vision.py
kt6_backend/page_perception.py
kt6_backend/page_capture_jobs.py
kt6_backend/asset_inventory.py
kt6_backend/dom_action_binding.py
kt6_backend/safe_dom_actions.py
kt6_backend/runtime.py
browser_extension/manifest.json
browser_extension/content-collector.js
browser_extension/popup-v2.js
browser_extension/popup.html
```

测试文件：

```text
tests/test_codeagent_canvas_vision.py
tests/test_topology_model_contract.py
tests/test_topology_artifact_clis.py
tests/test_topology_fusion.py
tests/test_page_perception.py
tests/test_page_capture_jobs.py
tests/test_asset_inventory.py
tests/test_dom_action_binding.py
tests/test_safe_dom_actions.py
tests/test_safe_dom_action_plan.py
tests/test_asset_action_integration.py
tests/test_dom_action_api.py
tests/test_browser_extension_assets.py
tests/test_hybrid_canvas_vision.py
tests/fixtures/extension_plain_page.html
tests/fixtures/extension_complex_page.html
tests/fixtures/extension_canvas_page.html
```

当前代码已包含：

- 无交互启动时移除继承的 `CI` 环境变量。
- 发送给 CodeAgent 的 stdin 保证以换行结尾。
- CodeAgent stdout 实时追加到 `codeagent-events.jsonl`。
- stderr 实时追加到 `codeagent-stderr.log`。
- 终端每 10 秒输出心跳、最后事件、stdout/stderr 字节数和空闲时间。
- timeout 或 `Ctrl+C` 时终止 CodeAgent 进程树。
- `result/success` 在截止线到达或仍在 stdout 缓冲区时优先按成功处理。
- 允许顺序重复读取同一张白名单暂存图片。
- 仍禁止读取其他路径、调用其他工具或发起未完成的并发重复读取。
- 离线模型阶段使用 `kt6.topology-model.v1` 精简协议。
- 接受纯 JSON、单个 JSON fenced block，以及 JSON 前后追加的模型分析文字。
- 在有界响应中定位唯一 `kt6.topology-model.v1` 根对象，随后执行严格协议校验。
- 同一 stream 中逐个验证 assistant/result 候选；较早候选唯一有效时不再被末尾无效文本覆盖。
- 显式围栏必须直接包含协议对象；损坏围栏、多个围栏或多个协议对象仍会拒绝。
- 离线 model/hybrid CLI 默认最多尝试 2 次，共享同一个总 timeout。
- 图片 Read 完成后默认 300 秒无 stdout 会终止停滞进程并在剩余预算内重试。
- 首次失败 events/stderr 归档为 `*.attempt-1.*`，不会被下一次尝试覆盖。
- CLI 错误 JSON 包含 `error_code`、`category` 和 `retryable`。
- 扩展按业务语义优先采集元素，并限制每个 frame 最多提交 220 个候选。
- 扩展过滤继承 `cursor:pointer` 产生的按钮子节点重复，同时保留普通文本页回退。
- 扩展采集 iframe/document 上下文、稳定 selector、业务 ID、资产身份、动作归属、
  ARIA、SVG 文本证据和 Canvas/SVG 视觉区域。
- 扩展通过 `POST /api/perception/capture-jobs` 创建后端异步任务，待完成 `job_id`
  保存在 `chrome.storage.local`；弹窗关闭不终止后端识别，重开后继续轮询。
- 同步 `POST /api/perception/captures` 仍作为兼容和诊断接口保留。
- 显式只读 `window.__KT6_PAGE_ADAPTER__` 只导出有界结构化快照；扩展不拦截任意
  `fetch`/XHR。`snapshot_complete=false` 或缺失时，页面 API 与视觉并行并可作回退。
- 大尺寸 Canvas、SVG、图形容器与嵌入区域会成为视觉 ROI；装饰性小图标被过滤。
- 每次点击只调用一次 `captureVisibleTab()`，且只提交一个最高优先级视觉主帧；
  原生 Canvas `toDataURL()` 是像素回退，不与可见截图组成多帧请求。
- 截图裁剪按实际 bitmap/viewport 比例计算；超限 PNG 会转 WebP 并降采样。
- DOM 元素太少且没有视觉区域/原生 Canvas 像素时，允许整页视觉兜底，但固定为
  `unverified` 和 analysis-only。
- 弹窗展示扫描数、候选数、提交数、可操作数、原生 Canvas、视觉区域、可见截图、
  视觉证据、`perception_decision`、截断状态和关键元素预览。
- 后端保留 `selector`、`source_ref`、`frame_id`、`frame_url`、`document_id` 和
  `actionable`，以及 `asset_id`、管理 IP、序列号、站点、资产版本、`action_id` 和
  `owner_business_id`。
- 在线节点以 `source.kind=dom|page_api|vision` 标明来源；`interaction.status`、
  `can_click_now`、`preflight_required` 和拒绝原因明确区分可定位与可立即点击。当前
  所有节点的 `can_click_now=false`。
- DOM `ui_tree` 是每个 frame/document 有独立根节点的语义投影；节点保留
  `parent_element_id`、`children`、`parent_relation`、`omitted_ancestor_count`，并以
  `action_binding_complete` 对截断、frame 错误和 open Shadow Root fail closed。
- 后端以 `perception_decision` 标记 `dom`、`page_api`、`canvas` 三种证据的全部八种
  组合，包括 `empty`、`page_api_only`、`dom_and_canvas` 和
  `dom_and_page_api_and_canvas`；`page_api_perception`、`canvas_perception`、
  `dom_business_object_bindings` 和 `dom_action_bindings` 并行保留。
- `svg_element_texts` 只作为原始文本证据，不能自动生成业务对象或动作绑定。
- 所有 `capture_kind=visible_tab` 截图固定为 analysis-only，不能作为直接点击依据。
- 原始 DOM 绑定固定为 `observed`、不可执行；disabled 元素不会成为交互候选。
- 扩展权限保持为 `activeTab` + `scripting` + `storage`，没有申请 `<all_urls>`；
  `storage` 仅用于恢复待完成的本机 capture job。

### 3.1 在线页面 DOM + 视觉分治扩展 v0.5.1

启动本机后端：

```powershell
python -m kt6_backend.app
```

在 Chrome 的 `chrome://extensions` 或 Edge 的 `edge://extensions` 中打开开发者
模式，选择“加载已解压的扩展程序”，加载：

```text
D:\yangzehui\FreeStyleCopilot\browser_extension
```

代码更新后必须在扩展管理页点击“重新加载”。随后打开一个普通 HTTP(S) 页面，
点击扩展中的“采集当前页面”。正常结果应同时看到采集统计和关键元素预览，而不是
只有固定的 `DOM: 600`。

提交后扩展会立即获得一个后端 `job_id`。视觉模型耗时较长时可以关闭弹窗；后端任务
不会因此终止。再次打开扩展会从 `chrome.storage.local` 恢复同一任务并继续查询，
完成后显示 `capture_id`。若后端已重启或任务记录已过期，扩展会删除旧 pending 并
明确提示重新采集。若要排查兼容问题，仍可直接调用同步 captures 接口。

若目标页面能修改，可在页面中显式提供只读 `window.__KT6_PAGE_ADAPTER__`，让扩展
读取结构化对象与关系。它不是通用页面 API 抓包器：当前实现不会 monkey-patch
`fetch`/XHR，也不会自行扫描业务接口。适配器应准确声明 `snapshot_complete=true`；
否则后端保留 `page_api_perception`，同时优先继续像素视觉，视觉不可用或失败时才降级
使用这份不完整分析证据。

结果解释：

- `truncated: true` 表示候选超过预算且已明确截断，不等于采集失败。
- SVG/Canvas 页面允许原生 `Canvas: 0`，但应检测到视觉区域，并产生一张视觉主帧。
- `perception_decision` 覆盖 `dom`、`page_api`、`canvas` 的全部组合；例如
  `page_api_only` 表示只有页面显式 API 证据，`dom_and_page_api_and_canvas` 表示三路
  都已进入后端。这些状态只描述证据通道，不代表坐标可以直接执行。
- 低 DOM、无视觉区域且无原生 Canvas 像素时才启用整页兜底；它始终仅用于分析。
- 普通文本页即使没有按钮，也应通过标题/正文回退产生少量 DOM 候选。
- 视觉拓扑与 DOM 业务/动作绑定会并行保留；扩展不会执行点击，后续真实动作仍需
  业务资产绑定、重新校验和授权。
- `source.kind=dom|page_api|vision` 是来源说明，不是权限证明；应结合
  `interaction.status` 与 `can_click_now` 判断。当前没有任何节点能立即点击。
- `ui_tree.tree_scope=semantic_projection` 表示它是受预算约束的语义树，不是完整原始
  HTML 镜像；查看 `frame_roots`、父子引用、省略祖先计数和
  `action_binding_complete` 判断结构与动作证据覆盖。


### 3.2 设备资产绑定与执行前复核

当前完成的是可测试、默认拒绝的 dry-run 安全骨架：

1. `AssetResolver` 按资产 ID、规范化 IP、保留标点的序列号唯一解析资产；仅名称
   匹配必须附带站点/楼层 scope。重复身份、证据冲突和 scope 冲突全部拒绝。
2. `DOMActionBindingService` 校验顶层页面与 frame origin 白名单，在相同
   frame/document 内分别绑定设备主体和其 DOM 后代动作控件。重复 ref/selector、
   伪 selector、全局按钮和伪造 owner 均拒绝。
3. `shutdown_ap` 等高风险动作必须有严格机器 ID（如 `data-kt6-action="ap.shutdown"`），
   不能只根据“关闭”“disable”等文字或近义 ID 猜测。
4. `prepare` 生成六步 `operation_plan`：`bind_target`、`confirm_and_authorize`、
   `fresh_capture_revalidation`、`final_revalidation`、`execute`、`verify_outcome`。
5. `preflight` 要求不同且新鲜的二次页面采集、准确的 asset/action 确认、精确权限、
   资产状态/版本和完整 DOM 目标指纹一致。
6. 复核通过后签发 15 秒有效、只能消费一次的随机令牌；并发消费只有一个成功。
   `GET /api/dom-actions/plans/{plan_id}` 会显示步骤推进、阻断和令牌到期后的过期状态。
7. `execute` 当前只支持 dry-run；`dry_run=false` 固定返回
   `live_execution_channel_unavailable`。所有层级的 `safe_for_execution` 均为
   `false`，复核结果只用 `preflight_verified=true` 表示；`verify_outcome` 在真实执行器
   接入前不会伪装为已完成。

接口：

```text
POST /api/dom-actions/prepare
POST /api/dom-actions/preflight
POST /api/dom-actions/execute
GET  /api/dom-actions/plans/{plan_id}
GET  /api/dom-actions/audit
```

Demo 资产来自 `data/mock_assets.json`。生产必须将 `JSONAssetInventoryAdapter`
替换为经过认证的 NCE/FEBS 查询 Adapter，并把权限、用户、scope 与页面采集来源
绑定到服务端认证会话。当前 capture 和 `permissions` 都是客户端测试输入，不能
作为生产授权凭据；最终复核读取的是存储快照，不是原子 live DOM，因此在接入受控
浏览器执行器或设备 API 前必须继续保持 dry-run。
查看后端最近一次页面采集：

```powershell
Invoke-RestMethod `
  -Uri 'http://127.0.0.1:8787/api/perception/captures?limit=1' |
  ConvertTo-Json -Depth 10
```

当前没有 FEBS/NCE 前端源码，因此不能把本扩展描述成“已经嵌入目标系统”。拿到
源码后可复用相同采集协议改成页面内 SDK。

## 4. CodeAgentCLI 运行事实

测试环境中确认可用的入口：

```text
D:\03CodeAgent\CodeAgentCLI\codeagent.bat
D:\03CodeAgent\CodeAgentCLI\codeagentcli.bat
```

两者版本：

```text
1.2605.03-IN.2 (codeAgentCLI)
```

`codeagentcli.bat` 会设置企业 CA、清空代理变量，然后启动：

```text
D:\03CodeAgent\CodeAgentCLI\bin\codeagentcli.exe
```

不要改用：

```text
D:\03CodeAgent\CodeAgentCLI\bin\codeagent.exe
```

该文件会拉起旧版本 CodeAgent。

KT6 当前以 Python subprocess 启动以下逻辑参数：

```text
codeagent -p
  --output-format stream-json
  --input-format text
  --verbose
  --tools Read
  --allowedTools Read
  --permission-mode dontAsk|bypassPermissions
  --no-session-persistence
  --disable-slash-commands
```

暂存图片位于：

```text
<workdir>\runtime_data\codeagent_jobs\kt6-vision-*\frame-*.png
```

CodeAgent 使用自身配置的默认模型。KT6 不在代码中写死 GLM 型号；测试期间默认
模型为 GLM-5.1。

## 5. 两份输入协议

### 5.1 CV 文件

`cv-result.json` 由本地 RapidOCR/OpenCV 生成，重点字段：

```text
objects[].business_id
objects[].bbox
objects[].center
objects[].confidence
objects[].attributes
links[].source
links[].target
links[].confidence
links[].attributes
```

CV 负责真实像素坐标和可追溯像素证据。

### 5.2 模型文件

`model-result.json` 使用：

```text
schema_version = kt6.topology-model.v1
```

核心字段：

```text
nodes
links
structure_templates
negative_edges
no_connections
confidence
```

模型提示只携带精简 CV 候选：

- 节点 ID
- 类型与标签
- 画布 ID
- 中心点（CV 只有 bbox 时由 KT6 本地派生）
- 置信度
- 候选连接

不会把 bbox、OCR polygon、像素路径等完整 CV 属性再次塞给模型，也不要求模型
重新输出坐标。

模型响应校验：

- 响应可以是纯 JSON，也可以在自然语言说明中包含单个 JSON fenced block。
- 使用 `JSONDecoder.raw_decode` 定位唯一 `kt6.topology-model.v1` 根对象。
- JSON 前后的自然语言分析可以忽略。
- 显式围栏必须直接包含协议对象；第二个 fenced block、第二个协议对象、损坏
  围栏、重复 key、NaN、无限值、未知字段和超限数组仍会拒绝。
- schema marker 和候选起点扫描均有上限；嵌套属性中的同名 marker 不算协议根。
- 节点、连接、模板、置信度和属性均有类型及数量边界。

## 6. 融合逻辑

融合完全由 Python 确定性执行，不再调用模型。

已实现：

- 全局一对一对齐模型节点和 CV 节点：精确匹配优先，其次仅接受唯一紧凑 ID
  匹配，例如 `GW001` 与 `GW-001`。
- 歧义紧凑 ID、前缀相似 ID 和模型内部 compact-key 冲突均 fail-closed，绝不
  猜测绑定坐标。
- 保留 CV bbox、派生 center、canvas_id、OCR 置信度和像素证据。
- 通过 `node_coordinate_mappings` 显式审计语义节点、模型节点、CV 节点和坐标
  的对应关系。
- 补充模型角色、厂商、型号、层级和连接。
- 支持直接连接和多跳路径等价。
- 保留 `star`、`layered` 结构模板。
- 支持模型明确否决 CV 误连，但全局 `no_connections` 不再无条件硬删除 CV 链路。
- 保留负证据置信度，并以 `accepted`、`disputed`、`rejected` 三态决策。
- 对仅用于渲染的未定位节点推断坐标。
- 同时输出 grounded、display 和 semantic 三层图。
- 推断坐标及 display-only 链路不可用于真实 GUI 点击。

常见 `fusion_status`：

```text
confirmed
cv_only
model_only
path_equivalent
structurally_derived
llm_rejected
spatially_inferred
```

融合顶层视图：

```text
result / grounded_graph  真实像素落地图，保持既有消费者兼容
display_graph            grounded + 可渲染推断节点/链路，始终不可交互
semantic_graph           完整语义并集，可包含未定位节点和 unresolved 链路
node_coordinate_mappings 节点 ID 与 canvas/bbox/center 的显式审计表
```

`node_coordinate_mappings` 的 `mapping_status` 为 `matched`、`cv_only` 或
`unmatched`，`match_method` 为 `exact`、`compact_unique` 或 `none`。推断坐标
保持 `unmatched` 和 `rendering_only=true`。该表不授予点击权限，所有映射的
`interaction_eligible` 固定为 `false`；真实动作仍需 PagePerception 资产绑定、
适配器能力和动作授权共同通过。

链路另有正交字段 `relation_state=accepted|disputed|rejected`。仅高置信的明确
pair-level 负证据与低于阈值的 CV 链路组合会真正 rejected；全局
`no_connections`、孤立声明、弱负证据和高置信 CV 链路均保留为 disputed。
`display_only_links`、`disputed_links`、`rejected_links` 分别保留审计明细。

## 7. 推荐测试命令

### 7.1 自动化测试

2026-08-04 开发环境全量结果为 362 项通过、42 项跳过；后续通过、跳过和失败数量
仍以当前命令输出为准。跳过项来自开发环境缺少可选 RapidOCR/OpenCV 运行依赖，
不是测试失败。

全量命令：

```powershell
python -m unittest discover -s tests
```

拓扑链路定向命令：

```powershell
python -m unittest `
  tests.test_topology_model_contract `
  tests.test_codeagent_canvas_vision `
  tests.test_topology_artifact_clis
```

定向测试数量会随回归用例增长，以命令实际输出为准。

页面感知、资产绑定与扩展定向命令：

```powershell
python -m unittest `
  tests.test_page_capture_jobs `
  tests.test_browser_extension_assets `
  tests.test_asset_inventory `
  tests.test_dom_action_binding `
  tests.test_safe_dom_actions `
  tests.test_safe_dom_action_plan `
  tests.test_asset_action_integration `
  tests.test_dom_action_api `
  tests.test_page_perception `
  tests.test_hybrid_canvas_vision `
  tests.test_app
```

### 7.2 测试环境端到端命令

每张图片使用独立 `source-id` 和输出目录：

```powershell
cd D:\04project\FreeStyle_Copilot_KT6_demo

$imageName = "3.png"
$id = "topology-3-" + (Get-Date -Format "yyyyMMddHHmmss")
$out = ".\runtime_data\$id"

python -m kt6_backend.topology_hybrid_cli `
  "..\topo_pic_data\$imageName" `
  --source-id $id `
  --out-dir $out `
  --timeout 900 `
  --permission-mode bypassPermissions `
  --executable "D:\03CodeAgent\CodeAgentCLI\codeagent.bat"

$LASTEXITCODE
```

`bypassPermissions` 只用于隔离测试环境，不应在代码中设为默认值。

成功条件：

```text
终端 JSON 中 status = ok
进程退出码 = 0
cv-result.json 存在
model-result.json 存在
codeagent-events.jsonl 存在
codeagent-stderr.log 存在
fused-result.json 存在
```

融合摘要：

```powershell
$f = Get-Content "$out\fused-result.json" `
  -Raw -Encoding UTF8 | ConvertFrom-Json

$f.summary | Format-List
```

至少确认：

```text
cv_object_count > 0
model_object_count > 0
fused_object_count > 0
```

### 7.3 复用 CV

模型失败但 CV 已完成时，可以把 `cv-result.json` 放入新的 `$out` 后执行：

```powershell
python -m kt6_backend.topology_hybrid_cli `
  "..\topo_pic_data\3.png" `
  --source-id $id `
  --out-dir $out `
  --timeout 900 `
  --permission-mode bypassPermissions `
  --executable "D:\03CodeAgent\CodeAgentCLI\codeagent.bat" `
  --reuse-cv
```

不要把另一张图片的 CV 文件复用于当前图片。

## 8. 真实图片验证记录

测试环境不是公用电脑。

已观察到：

- 两张真实拓扑图片已经完成 CV、CodeAgent、模型 JSON 解析和融合，终端返回成功。
- 一张样例的融合摘要曾得到 21 个 CV 节点、22 个模型节点、21 个最终可定位节点、
  20 条融合连接；其中有 1 个模型独有未定位节点和 1 条未解析连接。这属于准确率
  分析，不是链路失败。
- CodeAgent 处理困难图片时可能顺序读取同一图片两次，事件日志中的图片 Base64
  因此可能从约 1 MB 增至 2 MB 以上。
- 第三张图片最近一次运行中，CodeAgent 已返回 `result/success`；最终文本长
  22177 字符，前 1329 字符是英文分析，随后才是单个完整 JSON fenced block。
  旧解析器因要求 JSON 位于响应开头而失败。当前版本已改为提取唯一协议根，并用
  同形态回归测试覆盖；测试环境仍需同步后重新做一次端到端确认。
- `1.png` 在 2026-07-27 11:28 的运行已完成 Read，但旧代码最终只得到无效模型
  协议。当前版本会验证 stream 中全部候选、避免有效 assistant JSON 被末尾无效
  `result.result` 覆盖，并对真正无效的模型响应在剩余总预算内重试一次。
- `2.png` 在 2026-07-27 14:09 的运行完成 Read 后 860 秒无 stdout，最终命中
  900 秒总超时。当前离线 CLI 默认在图后空闲 300 秒时终止该进程并重试一次。

上述三张图片都需要在测试环境同步当前版本后复测，不要提前写成已经通过。

## 9. 已知限制

### 9.1 timeout 与有限重试

- 独立 CodeAgent Adapter 当前硬上限为 900 秒。
- 离线 model/hybrid CLI 的 `--timeout` 是所有模型尝试共享的总预算，不是每次
  尝试各等待相同时间；默认最多 2 次尝试。
- Read 完成后的默认 idle timeout 为 300 秒，只在离线 CLI 启用；核心 Runner
  默认仍关闭，HTTP/嵌入式调用不受影响。
- `--idle-timeout 0 --max-attempts 1` 可恢复为单次、无 idle watchdog 的行为。
- 收到完整 `result/success` 后会立即继续；截止线已缓冲的终止事件仍优先处理。
- HTTP 感知接口上限为 300 秒。
- 复杂图片超过 900 秒的离线总预算拆分仍未实现。

### 9.2 CodeAgent 非确定性

- 同一图片的 Read 和推理时间可能显著波动。
- 模型可能重复 Read 同一张图。
- 模型可能用 fenced JSON，或在 JSON 前后追加说明。
- 模型成功不代表语义识别一定正确。

### 9.3 事件文件

- `codeagent-events.jsonl` 包含原始 stream-json。
- `Read` 的 `tool_result` 可能内含图片 Base64，文件会很大。
- 不要在终端直接完整输出，也不要未经确认上传包含真实拓扑图片的事件文件。
- 当前 stdout 上限为 8 MB；大图片、多次重复 Read 可能触及上限。
- 发生自动重试时，首次日志保存为 `codeagent-events.attempt-1.jsonl` 和
  `codeagent-stderr.attempt-1.log`，最终一次仍使用标准文件名。
- 目前没有正式的“从成功 events 恢复 model-result.json”CLI，只有事件保留和
  手工诊断能力。

### 9.4 准确率与执行安全

- 真实图片准确率评测尚未完成。
- 模型推断语义、未定位节点坐标和页面自报的业务 ID 不可直接用于 GUI 点击。
- 只有 CV 或渲染器提供的可验证几何信息可以参与真实定位。
- DOM 安全链路已完成资产解析、双重绑定、复核、令牌和 dry-run，但没有真实点击。
- 当前 capture、权限、资产数据、业务语义、指标和设备动作仍有 Mock/测试边界。
- 生产必须接入服务端身份授权、可信浏览器会话和点击前原子 live DOM 复核，或优先
  使用以 canonical asset_id 为参数的受控设备 API。

### 9.5 在线 DOM 扩展

- 只支持普通 HTTP(S) 页面；不能采集 `chrome://`、浏览器扩展商店、内部 PDF 或
  `file://` 页面。
- `activeTab` 权限下，跨域 iframe 的脚本注入可能受页面和浏览器安全策略限制。
- iframe 内视觉区域无法可靠换算为顶层截图坐标时，只保留顶层 viewport 兜底并标记
  `unverified`，不能用于动作定位。
- closed Shadow DOM 无法从外部扩展读取。
- 纯 Canvas/SVG 的业务对象没有原生业务 DOM 节点，需要继续走视觉识别；SVG
  `<text>` 只作文字证据。
- 整页兜底可能包含页面中的非目标信息；当前只在 DOM 极少且没有其他像素证据时
  启用，并受截图大小、后端持久化和部署环境隐私策略约束。
- open Shadow Root 会被检测并计入覆盖统计；其内部当前不作为完整动作绑定证据，
  `action_binding_complete=false`，不能因外层 DOM 看似完整而继续执行。
- 当前尚未用真实 FEBS/NCE 在线页面完成扩展验证，不能把通用网页测试结果等同于
  目标系统已经适配。
- 目标页面需要提供强资产身份；高风险动作还需要严格 `data-kt6-action`。无法修改
  页面时，应由受信任的站点专用 Adapter 提供等价证据，不能靠按钮文字猜测。
- 顶层页面和目标 iframe origin 都必须在资产允许列表中。
- 原始 `dom_action_bindings` 始终只是观察证据，`actionable_grounding=false`，
  所有安全链路结果也保持 `safe_for_execution=false`。视觉截图不绕过该限制。

## 10. 近期已解决问题

```text
无进度、600 秒无 stdout
→ 修复 CI 环境变量和 stdin 换行

权限确认或路径入口不确定
→ 支持显式 permission-mode 和 executable

终端看似卡死
→ 实时 events/stderr + 10 秒心跳

Ctrl+C/timeout 残留进程
→ 终止 CodeAgent 进程树

result/success 在截止线仍报 timeout
→ 成功事件优先，并在管道清理后复查缓冲事件

模型重复 Read 同一图片
→ 允许已完成后的顺序重复，仍限制路径和工具

模型返回 fenced JSON
→ 兼容单个 JSON fenced block，关闭围栏不计作第二个代码块

模型在 JSON 前后追加分析
→ 定位唯一 `kt6.topology-model.v1` 根对象，再进行严格协议校验

模型返回多个协议对象或损坏围栏
→ 拒绝歧义响应，不绕过严格协议校验

模型独有节点有推断坐标但链路在主结果中不可见
→ 保持 grounded 点击安全，同时在 display_graph 输出 display-only 节点和链路

模型用 no_connections 全局否决 CV 链路
→ 改为 disputed；只有高置信明确负边与较弱 CV 证据组合才真正 rejected

模型 ID 与多个 CV ID 紧凑化后相同，或仅为前缀相似
→ `node_coordinate_mappings` 保持 unmatched/cv_only，不猜测坐标绑定

模型 negative_edges 引用未列入 nodes 的 CV 端点别名
→ 仅在精确或唯一紧凑候选时安全解析，但不会伪造 model-node 坐标绑定

有效 assistant 模型 JSON 被末尾无效 result 文本覆盖
→ 逐候选严格验证并按规范化 payload 去重；多个不同有效对象仍拒绝

图片 Read 完成后长时间无 stdout，直到 900 秒才失败
→ 离线 CLI 默认 300 秒图后 idle watchdog，并在共享总预算内有限重试

任意网页采集固定显示 DOM 600，无法判断采集了什么
→ 改成语义优先候选、每 frame 220 上限、显式截断统计和关键元素预览

一个按钮因子节点继承 cursor:pointer 被重复采集
→ 过滤已有可操作祖先的非语义子节点，同时保留真正独立的交互元素

纯文本页面没有可操作控件时采集为空
→ 增加标题和正文文本回退，不再把“无按钮”等同于“无页面语义”

纯 Canvas 页面 DOM 为零
→ 保留 Canvas 像素证据并交给视觉路线，DOM 为零不再自动判定失败

SVG/地图页面原生 Canvas 为零，扩展只提交 DOM
→ 自动检测大尺寸 SVG/图形区域，单次可见标签页截图并裁剪为视觉主帧；视觉拓扑和
DOM 绑定在后端并行保留

扩展弹窗绑定同步长请求，关闭后丢失进度
→ 后端异步 capture job 持续执行，`chrome.storage.local` 保存 job_id，弹窗重开恢复；
同步接口继续保留给兼容调用
```

## 11. 后续建议

按当前用户优先级排序：

1. 在真实 NCE 在线页面重新加载扩展 v0.5.1，确认导航/表格走 DOM，地图/拓扑区域
   产生一个视觉主帧，关闭并重开弹窗后同一异步任务能恢复；同时记录
   `source.kind`、`interaction`、`ui_tree.action_binding_complete`、
   `perception_decision`、frame/document/origin 和 ROI 状态。设备容器仍需强资产身份，
   动作控件仍需明确 DOM 祖先归属和高风险 `data-kt6-action`。
2. 将 `JSONAssetInventoryAdapter` 替换成经过认证的 NCE/FEBS 资产查询，把权限、
   用户和 scope 换成服务端身份会话；完成受控执行器与回滚前继续保持 dry-run。
3. 在测试环境同步当前修改，先跑资产绑定/API 定向测试，再依次复测
   `1.png`、`2.png`、`3.png`，确认自动重试、
   grounded/display/semantic 计数、disputed/rejected 状态及坐标映射。
4. 建立三张真实图片的人工节点/链路真值，不再用“链接越多越好”判断准确率。
5. 后续再拆分 HTTP timeout 和超过 900 秒的离线任务总预算。
6. 增加正式 events 恢复 CLI，避免成功结果因后处理失败而重新调用模型。
7. 建立多图片黄金数据集，统计节点、连接、层级、厂商和型号准确率。
8. 需要 GLM 直连或多模型路由时，再抽象 `TopologyModelHarness`；当前无需引入
   大型 Harness 框架。

## 12. 新 Codex 接手检查清单

新任务开始后先执行：

```powershell
cd D:\yangzehui\FreeStyleCopilot
git status --short
git log -5 --oneline --decorate
python -m unittest discover -s tests
```

然后确认：

- `review.md` 是否仍为未跟踪文件。
- 扩展 `manifest.json` 是否为 v0.5.1，并在浏览器扩展管理页完成重新加载。
- 目标系统源码目前并不在仓库中，不要误称已经完成 FEBS/NCE 页面内嵌集成。
- 当前提交/推送状态以 `git log`、`git status` 为准，不沿用本文中的历史哈希。
- `main` 是否与 `origin/main` 一致。
- 测试环境最新失败属于 CV、CodeAgent transport、模型协议还是融合阶段。
- 不要在没有真实事件证据时继续放宽协议。
- 不要为了准确率问题破坏已经通过的路径安全和严格 JSON 校验。
- 修改完成后运行定向测试和全量测试。
- 用户要求提交时同时推送。
