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
已推送融合基线：41583d6；当前最新提交以 `git log` 为准
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

- 模型提示明确把无规律散点、无可见连线的物理拓扑视为合法输入；不得按行列、距离
  或网格外观臆造连接和层级，也不得把侧边资源树、导航栏或工具栏当作主画布节点。
- 仅当 Local CV 已成功，且模型阶段最终错误属于明确的模型响应白名单时，离线
  hybrid CLI 才允许返回 `status=degraded`。白名单仅含
  `ambiguous_model_response`、`invalid_model_response`、`missing_final_text`、
  `step_incomplete` 和 `final_step_incomplete`。
- 降级还要求融合结果至少有一个 Local CV 像素落地节点；否则仍按失败退出。
- 降级时保留 Local CV 节点、`fused-result.json` 和 `model-error.json`，不伪造
  `model-result.json`；CV 链路统一标为 `disputed` 且不可交互。
- 缺少 Read 证明、Read 后空闲或总预算耗尽、进程异常退出、transport/pipe 失败，
  以及启动、权限、路径、文件清理和完整性错误不允许降级。
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
- 支持模型逐对明确否决 CV 误连；全局 `no_connections` 不再无条件硬删除 CV 链路。
- 无连线散点图仍要求模型核查每条 supplied CV link，并用 `negative_edges` 返回明确
  误检；保留负证据置信度，以 `accepted`、`disputed`、`rejected` 三态决策。
- 裁剪、遮挡、画面边界截断，或画外/隐藏中间节点只能记为无法确认，不能作为
  `negative_edges`；模型报告的 `path_equivalent` 间接关系不可当作直接可见连线。
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

链路另有正交字段 `relation_state=accepted|disputed|rejected`。全局
`no_connections` 只争议模型实际覆盖端点的 CV 链路；孤立声明、弱负证据和无置信度
负证据也保留为 disputed。高置信、明确的 pair-level 负证据可拒绝方向探测等
启发式 CV 误线；若 CV 同时有足够置信度和可追踪的直接像素路径，则保留为 disputed
供复核，而不是只比较两个阈值。模型同一节点对同时给出正边和负边属于协议矛盾，
在模型契约及直接 fusion 入口都会 fail-closed。`display_only_links`、
`disputed_links`、`rejected_links` 分别保留审计明细，争议、拒绝、间接和 unresolved
链路均不可交互。

## 7. 推荐测试命令

### 7.1 自动化测试

开发环境最后一次结果：

```text
262 项执行，其中 220 项通过
42 项跳过，0 项失败
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
  tests.test_topology_artifact_clis `
  tests.test_topology_fusion
```

当前定向结果应为 78 项通过。

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

允许降级的模型错误不会再让整条识别报废。此时条件为：

```text
终端 JSON 中 status = degraded
degraded_to = local_cv
进程退出码 = 0
cv-result.json、fused-result.json、model-error.json 存在
model-result.json 不存在且终端字段 model = null
fused.summary.model_object_count = 0
所有保留的 CV 链路 relation_state = disputed
```

`status=degraded` 只是可用性降级，不是识别准确率通过。把结果交给人工复核前，还应
确认 `cv_object_count > 0`、`fused_object_count > 0`，并检查模型错误码、节点
置信度和全部 `disputed_links`；任一计数为零，或尚未对照真实图片人工真值时，
不得因为退出码为 `0` 就记作图片识别通过。依赖模型补充的角色、层级、厂商、型号和
间接关系在降级结果中均未得到本次确认。

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

以下记录严格区分三种证据：真实产物实测、用户确认的人工真值，以及合成事件回归。
三者不能互相替代。

### 8.1 真实产物实测

- 两张真实拓扑图片已经完成 CV、CodeAgent、模型 JSON 解析和融合，终端返回成功。
- 一张样例的融合摘要曾得到 21 个 CV 节点、22 个模型节点、21 个最终可定位节点、
  20 条融合连接；其中有 1 个模型独有未定位节点和 1 条未解析连接。这属于准确率
  分析，不是链路失败。
- CodeAgent 处理困难图片时可能顺序读取同一图片两次，事件日志中的图片 Base64
  因此可能从约 1 MB 增至 2 MB 以上。
- `3.png` 已于 2026-07-27 16:35 使用提交 `2968011` 对应逻辑复测成功：约 270 秒
  收到 `result/success`，终端 `status=ok`，`cv-result.json`、`model-result.json`、
  `codeagent-events.jsonl`、`codeagent-stderr.log` 和 `fused-result.json` 均已生成。
  这证明前置分析后协议 JSON 的解析链路已实际通过，但不等同于语义准确率验收。
- `1.png` 在 2026-07-27 11:28 的运行已完成 Read，但旧代码最终只得到无效模型
  协议。当前版本会验证 stream 中全部候选、避免有效 assistant JSON 被末尾无效
  `result.result` 覆盖，并对真正无效的模型响应在剩余总预算内重试一次。
- `2.png` 在 2026-07-27 14:09 的运行完成 Read 后 860 秒无 stdout，最终命中
  900 秒总超时。当前离线 CLI 默认在图后空闲 300 秒时终止该进程并重试一次。

### 8.2 用户确认的人工真值

- 用户确认 `1.png` 是由原图放大得到，视觉上未接上的 `AP-007` 不能据此判定为
  孤立节点。该图的人工真值为 22 个已知节点、21 条语义关系：

```text
GW-001 (ZTE)
└─ CORE-001 (Huawei)
   └─ AGG-003 (Huawei USG6391E)
      ├─ ACC-010 ─┬─ AP-022
      │           ├─ AP-029
      │           └─ AP-048
      ├─ ACC-017 ─── AP-050
      ├─ ACC-022 ─── AP-006
      ├─ ACC-006 ─┬─ AP-013
      │           └─ AP-043
      ├─ ACC-012 ─┬─ AP-034
      │           └─ AP-061
      ├─ ACC-015 ─┬─ AP-026
      │           ├─ AP-039
      │           └─ AP-053
      └─ (间接，未知中间层) ─── AP-007
```

- 21 条语义关系的计数口径是：`GW-001→CORE-001` 和
  `CORE-001→AGG-003` 共 2 条，`AGG-003→6 个 ACC` 共 6 条，6 个 ACC 到
  12 个明确 AP 共 12 条，另加 `AGG-003` 到 `AP-007` 的 1 条间接关系。
  `AP-007` 的中间节点身份未知，协议和测试不得把它伪造成一条直接边，也不得凭空
  创建中间节点；应保留为待补全的间接语义关系。
- 现有 `enterprise_topology_ocr.txt` 夹具把 `AGG-003` 只放在设备详情表，不能替代
  上述原图人工真值。当前修改尚未用真实 `1.png` 产物证明 22 节点、21 关系全部
  正确，因此不得再写成“语义完整”或“识别通过”。
- 用户提供的四图对比截图报告：`2.png` 的 Model 输出了 `no_connections`，
  `3.png`、`4.png` 的 Fused 表现较好，`1.png` 存在未落地关系。该截图没有附
  原始 CV/Model/Fused JSON，只能作为问题线索，不能据此确认 3 条 CV 候选均为
  误检、还原证据字段或完成准确率验收。

### 8.3 合成事件回归与尚未复测的散点图

- 用户另提供了一张无规律散点“产品物理拓扑”截图。CodeAgent `Read` 已产生包含
  约 34 个 OSS、CameraRoot、CommonSubnet、Subnet、Name 和 V2SN 节点的
  `vlDescription`，但用户报告整次识别失败；当前机器没有生产测试目录和最终错误
  JSON，无法确认其精确 `error_code`。
- 本次新增的是模拟 CodeAgent 事件结构的回归：两条合成 stream 都包含 `Read` 的
  `tool_result.vlDescription`；一条随后返回严格模型 JSON，验证该字段不会被误当成
  最终协议，另一条不返回最终 JSON，验证会得到 `missing_final_text`。另有独立的
  hybrid 测试验证该白名单错误在非空 CV 下产生明示降级。以上测试没有复用用户事件
  中的完整 Base64/描述，也不证明真实散点图已识别成功或节点数准确。
- `1.png` 人工真值已编码为确定性融合回归：断言 22 个节点、21 条语义关系、
  `AGG-003` 的 8 个相邻关系，以及 `AP-007` 的 `path_equivalent` 间接语义；这仍是
  合成 fixture，不是对真实图片重新识别后的准确率证明。
- 散点反馈同时促成确定性逐边回归：无连线场景仍返回
  `no_connections=true`，但每条已核查的 CV 误连必须写入 `negative_edges`；
  完整逐边证据可拒绝全部测试候选，部分证据只拒绝对应配对，其余保持 disputed。
  `no_connections=true` 与非空正 `links`/星型派生边按协议矛盾拒绝；同一节点对
  同时出现在正边和负边中也会 fail-closed。这里描述的是合成单元测试覆盖，不是
  `2.png` 真实产物验收。

`3.png` 已通过；`1.png`、`2.png` 和这张散点图仍需在测试环境同步当前修改后复测。

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

- `status=degraded` 只表示 Local CV 节点仍可用；模型角色、层级、厂商、型号均未
  得到本次模型确认，链路也必须按 `disputed` 复核。

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
→ 全局结论只产生 disputed；高置信 pair-level 负边可拒绝启发式 CV 误线，
  但具有足够置信度和直接像素路径的 CV 边仍保留为 disputed

模型 ID 与多个 CV ID 紧凑化后相同，或仅为前缀相似
→ `node_coordinate_mappings` 保持 unmatched/cv_only，不猜测坐标绑定

模型 negative_edges 引用未列入 nodes 的 CV 端点别名
→ 仅在精确或唯一紧凑候选时安全解析，但不会伪造 model-node 坐标绑定

有效 assistant 模型 JSON 被末尾无效 result 文本覆盖
→ 逐候选严格验证并按规范化 payload 去重；多个不同有效对象仍拒绝

图片 Read 完成后长时间无 stdout，直到 900 秒才失败
→ 离线 CLI 默认 300 秒图后 idle watchdog，并在共享总预算内有限重试

无规律散点物理拓扑没有可见连线，模型未产出协议时整条流水线失败
→ 提示明确允许空 links/templates；有限重试仍失败时仅对白名单模型错误明示降级，
  保留 Local CV 节点并把未确认链路标为 disputed
```

## 11. 后续建议

按当前用户优先级排序：

1. 在测试环境同步当前修改，复测 `1.png`、`2.png` 和新散点图，确认正常成功或
   明示降级、grounded/display/semantic 计数、逐对 `negative_edges`、
   disputed/rejected 状态及坐标映射。
2. 将用户确认的 `1.png` 22 节点/21 语义关系固化为独立黄金真值，特别验证
   `CORE-001→AGG-003→ACC` 层级和 `AP-007` 间接关系；不要用现有 OCR 文本夹具
   代替原图真值，也不要用“链接越多越好”判断准确率。
3. 后续再拆分 HTTP timeout 和超过 900 秒的离线任务总预算。
4. 增加正式 events 恢复 CLI，避免成功结果因后处理失败而重新调用模型。
5. 建立多图片黄金数据集，统计节点、连接、层级、厂商和型号准确率。
6. 需要 GLM 直连或多模型路由时，再抽象 `TopologyModelHarness`；当前无需引入
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
- `main` 是否与 `origin/main` 一致。
- 测试环境最新失败属于 CV、CodeAgent transport、模型协议还是融合阶段。
- 不要在没有真实事件证据时继续放宽协议。
- 不要为了准确率问题破坏已经通过的路径安全和严格 JSON 校验。
- 修改完成后运行定向测试和全量测试。
- 用户要求提交时同时推送。
