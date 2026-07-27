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

用户已明确：先把链路打通，真实图片准确率、批量评测、复杂图片超长 timeout
和更深层模型优化之后再处理。

## 2. 仓库与工作目录

当前机器：

```text
开发目录：D:\yangzehui\FreeStyleCopilot
测试目录：D:\04project\FreeStyle_Copilot_KT6_demo
GitHub：git@github.com:yaaangzh/kt6-gui-agent-demo.git
分支：main
功能代码基线：f16ef7f
```

包含本文和 README 更新的实际最新提交应以以下命令为准：

```powershell
git log -1 --oneline --decorate
```

注意事项：

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
```

测试文件：

```text
tests/test_codeagent_canvas_vision.py
tests/test_topology_model_contract.py
tests/test_topology_artifact_clis.py
tests/test_topology_fusion.py
```

功能代码基线 `f16ef7f` 已包含：

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
- 显式围栏必须直接包含协议对象；损坏围栏、多个围栏或多个协议对象仍会拒绝。

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
- 中心点
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

- 对齐 `GW001` 与 `GW-001` 等 ID 差异。
- 保留 CV bbox、center、OCR 置信度和像素证据。
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
```

链路另有正交字段 `relation_state=accepted|disputed|rejected`。仅高置信的明确
pair-level 负证据与低于阈值的 CV 链路组合会真正 rejected；全局
`no_connections`、孤立声明、弱负证据和高置信 CV 链路均保留为 disputed。
`display_only_links`、`disputed_links`、`rejected_links` 分别保留审计明细。

## 7. 推荐测试命令

### 7.1 自动化测试

开发环境最后一次结果：

```text
240 项通过
42 项跳过
```

跳过项来自开发环境缺少可选 RapidOCR/OpenCV 运行依赖，不是失败。

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

当前定向结果应为 43 项通过。

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
  同形态回归测试覆盖；测试环境仍需同步新提交后重新做一次端到端确认。

最后一项不要在交接时误写成“第三张图片已经通过”。

## 9. 已知限制

### 9.1 timeout

- 独立 CodeAgent Adapter 当前硬上限为 900 秒。
- 900 秒是最长等待，不是固定等待；收到完整 `result/success` 后会立即继续。
- HTTP 感知接口上限为 300 秒。
- 用户已经指出：复杂图片可能超过 900 秒。合理后续方案是将 HTTP 在线上限和
  离线文件任务上限分开，并为离线 CLI 增加可配置总 timeout / idle timeout。
- 这项尚未实现，当前阶段先保证链路打通。

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
- 目前没有正式的“从成功 events 恢复 model-result.json”CLI，只有事件保留和
  手工诊断能力。

### 9.4 准确率与执行安全

- 真实图片准确率评测尚未完成。
- 模型推断语义和未定位节点坐标不可直接用于 GUI 点击。
- 只有 CV 或渲染器提供的可验证几何信息可以参与真实定位。
- 当前 Demo 业务语义、指标和设备动作仍有 Mock 边界。

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
```

## 11. 后续建议

按当前用户优先级排序：

1. 在测试环境同步最新 `main`，重新确认三张图片的 grounded、display、semantic
   计数及 disputed/rejected 状态。
2. 建立三张真实图片的人工节点/链路真值，不再用“链接越多越好”判断准确率。
3. 后续再拆分 HTTP timeout 和离线模型 timeout，并考虑 idle timeout。
4. 增加正式 events 恢复 CLI，避免成功结果因后处理失败而需要重新调用模型。
5. 建立多图片黄金数据集，统计节点、连接、层级、厂商和型号准确率。
6. 需要 GLM 直连、多模型路由、自动重试时，再抽象
   `TopologyModelHarness`；当前无需引入大型 Harness 框架。

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
- `main` 是否与 `origin/main` 一致。
- 测试环境最新失败属于 CV、CodeAgent transport、模型协议还是融合阶段。
- 不要在没有真实事件证据时继续放宽协议。
- 不要为了准确率问题破坏已经通过的路径安全和严格 JSON 校验。
- 修改完成后运行定向测试和全量测试。
- 用户要求提交时同时推送。
