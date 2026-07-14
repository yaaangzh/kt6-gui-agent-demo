# KT6 意图驱动 UI 联动设计方案

## 1. 项目定位

KT6 的核心目标不是构建一个单独的无线诊断 Agent，而是构建一个面向复杂人机协作场景的 **意图驱动 UI 联动执行框架**。

该框架需要将用户在 LUI（Language User Interface，对话界面）中的自然语言意图，转化为 GUI 页面中的可感知、可解释、可执行、可校验的 UI 操作过程，实现 LUI 与 GUI 的双向联动。

典型业务场景：

> 用户张三昨天上午 9:00 反馈网速慢，帮忙看下是啥原因。

系统需要完成：

1. 理解用户自然语言意图。
2. 调用“用户体验保障”业务任务链。
3. 左侧立即跳转到张三所在网络拓扑图，并识别拓扑中的用户、AP、链路和指标对象。
4. 在对话侧展示分析步骤、问题原因和优化建议。
5. 在 GUI 侧自动跳转、查询、填参、执行分析，并与左侧拓扑同步高亮。
6. 在用户点击“一键执行”后，自动进入射频调优页面或 AP1 调优执行节点并执行策略。
7. 左侧同步展示策略生成、下发、生效、校验进展。
8. 将 GUI 执行状态和最终结果回写到对话侧。
9. 保证跨页面、跨轮对话、多任务执行时上下文不断连。

因此，KT6 的本质可以定义为：

> 基于界面感知和 UI 原子操作的意图执行 Runtime，实现 LUI-GUI 双向联动、可解释执行和可靠的人机协作。

## 2. 总体链路

KT6 的完整执行链路如下：

```text
用户自然语言
  -> 结构化意图
  -> 业务任务链
  -> UI 操作计划
  -> UI 原子操作序列
  -> GUI 执行
  -> 状态校验
  -> 对话回写
```

对应的核心模块如下：

```text
1. Scenario Playbook Manager
   管理场景化业务任务链，例如“用户体验保障”。

2. Intent Understanding / Task Planner
   将用户输入解析为结构化业务意图，并选择对应任务链。

3. Runtime Context Manager
   管理跨页面、跨轮对话、跨任务的上下文。

4. UI Perception & Grounding
   将 GUI 页面、拓扑图、canvas、图片和组件转换为可交互元素树，并将业务动作映射到页面元素。

5. Business Object Grounding
   将业务对象与 GUI 对象绑定，例如 AP1 <-> 拓扑节点 <-> 表格行 <-> 指标数据 <-> 可执行动作。

6. Topology / Visual Sync Adapter
   驱动左侧拓扑或图片区域进行定位、高亮、关系展示、进度展示和状态更新。

7. Intent-to-Atomic-UI-Action Planner
   将业务步骤拆解为 UI 原子操作序列。

8. Atomic Action Orchestrator
   负责原子操作执行、状态机、多任务锁、校验、失败恢复和人在环确认。

9. LUI-GUI Sync Reporter
   将 GUI 执行状态、页面跳转、操作结果同步回对话侧。
```

## 2.1 左侧拓扑/图片联动能力

KT6 不能只做后台分析和页面点击，还需要把智能体的分析结果绑定到左侧 GUI 画面中的业务对象上。

以“张三网速慢”为例：

```text
用户说完问题
  -> 左侧立即跳转张三所在站点/楼层网络拓扑图
  -> UI Perception 识别图中的张三、AP1、邻居 AP、链路和指标面板
  -> Business Object Grounding 将 AP1 绑定到业务对象 ap_001
  -> Runtime 读取用户体验、AP、射频指标
  -> Agent 判断 AP1 同频邻居干扰
  -> 左侧高亮 AP1 和同频邻居关系
  -> 用户点击一键执行
  -> 左侧切换到 AP1 射频调优执行节点
  -> 左侧同步显示策略生成、下发、生效、校验和恢复正常
```

需要强调：

```text
业务数据负责判断，左侧拓扑负责解释和呈现。
AP1 同频邻居干扰不是只靠图片识别出来，而是由用户接入记录、AP 指标、射频指标、拓扑邻居关系共同判定。
```

业务对象绑定示例：

```json
{
  "business_object": {
    "type": "ap",
    "id": "ap_001",
    "name": "AP1"
  },
  "gui_bindings": {
    "topology_node": "topology.node.ap_001",
    "table_row": "associated_device_table.row.ap_001",
    "metric_panel": "radio_metric_panel.ap_001",
    "action_entry": "rf_optimization.action.ap_001"
  },
  "visual_state": {
    "position": {"x": 420, "y": 260},
    "status": "warning",
    "badges": ["同频干扰"]
  }
}
```

## 2.2 界面感知的证据分层与执行边界

拓扑界面可能同时提供 DOM、Canvas 截图、渲染器对象和 OCR 文本。KT6 不把这些来源混成一个不透明结论，而是保留来源并按可靠性选择：

```text
Renderer Scene
  > 带业务 ID 的 DOM
  > 从真实截图执行的 CanvasVisionAdapter
  > 人工文本或外部 OCR 转写
  > 未识别截图 / 普通 DOM
```

每个 Scene 都必须记录：

```text
semantic_source：语义实际来自哪里。
pixel_inference_performed：是否真的读取截图像素进行推断。
pixel_verified：像素适配链路是否成功产出受约束结果。
actionable_grounding：该定位能否用于 GUI 副作用。
```

结构化文本适合验证节点、关系、表格融合和歧义保留，但不能冒充图片识别。其 text-grid 坐标只指向文字证据，必须设置 `actionable_grounding=false`。Runtime 在动作入口执行统一门禁；不可执行的 Scene 可以参与解释和诊断，不能获取资源锁或启动动作 Playbook。

对于拓扑图中的叙述性说明，识别器遵循“显式图形/表格事实优先，说明只作注释”的原则。例如表中出现但图中无连线的汇聚设备必须保留为孤立节点，不能根据“三层架构”文字自动补边。

生产像素路径通过可配置的 `HTTPTopologyVisionAdapter` 调用外部 OCR、目标检测或多模态服务。远端只返回受约束的对象、像素框、置信度和关系；provenance 与执行资格由 PagePerception 强制生成。Scene Graph 另派生 `semantic_tree` 供 DOM-like 消费，但多父、环和并行边仍保留在 `relations/non_tree_relations` 中。

视觉模型读出的业务 ID 默认不等于资产库已绑定对象。HTTP Vision 因此固定为 analysis-only；只有后续完成生产资产库 exact binding、场景时效校验和独立授权，才允许进入可执行 grounding。

## 3. 问题一：如何得到业务的思维链

这里的“业务思维链”不应理解为大模型内部不可见、不可控的推理过程，而应产品化为 **业务任务链 / 诊断剧本 / Scenario Playbook**。

业务任务链的来源包括：

1. **专家经验**
   网优专家沉淀诊断路径，例如用户指标、关联 AP、射频干扰、漫游、认证、DHCP、出口链路等。

2. **历史工单和案例**
   从历史故障案例中归纳常见问题路径、常见根因和推荐处置方案。

3. **系统能力注册表**
   当前平台有哪些页面、接口、工具、组件，决定哪些步骤能够自动执行。

4. **规则与策略**
   包括高风险操作确认策略、推荐方案优先级、默认处置方式、审批策略等。

5. **大模型动态补全**
   在任务链不完整或用户表达模糊时，用于补齐参数、解释步骤、生成报告，但不替代任务链本身。

示例：

```json
{
  "scenario": "用户体验保障",
  "trigger_intents": ["网速慢", "体验差", "上网卡顿"],
  "steps": [
    {
      "id": "analyze_user_metrics",
      "name": "用户指标分析",
      "inputs": ["user", "time_range"],
      "outputs": ["user_kpi", "experience_score"]
    },
    {
      "id": "analyze_related_device",
      "name": "关联设备问题分析",
      "inputs": ["user_kpi"],
      "outputs": ["ap", "radio_status", "neighbor_interference"]
    },
    {
      "id": "infer_root_cause",
      "name": "问题原因分析",
      "inputs": ["radio_status"],
      "outputs": ["root_cause", "solution_candidates"]
    }
  ]
}
```

关键结论：

> 业务思维链要沉淀为可配置、可审计、可执行的任务链，由 Agent 进行选择和补全，由 Runtime 进行编排执行。

## 4. 问题二：页面跳转是否会导致上下文断连

页面跳转会带来上下文断连风险，包括：

1. 当前 DOM 和组件树变化。
2. 表单状态和筛选条件丢失。
3. 页面局部选择项丢失。
4. Agent 不知道任务执行到哪一步。
5. 对话侧和 GUI 侧状态不一致。

解决方式是引入独立的 **Runtime Context Manager**，将上下文从页面中抽离出来，由 Runtime 统一托管。

上下文分为四类：

```text
Conversation Context
用户说了什么、当前意图、历史对话、人工修正记录。

Task Context
任务 ID、当前步骤、步骤输入输出、任务状态、执行计划。

UI Context
当前页面、元素树、选中项、表单值、页面跳转来源和目标。

Business Context
张三、昨天上午 9:00、AP1、站点1/1F、根因、推荐方案。
```

页面跳转前后需要建立 checkpoint：

```json
{
  "task_id": "task_zhangsan_slow_network",
  "current_step": "execute_rf_optimization",
  "from_page": "用户体验保障",
  "to_page": "射频调优",
  "business_context": {
    "user": "张三",
    "time_range": "昨天上午9:00",
    "ap": "AP1",
    "site": "站点1/1F",
    "root_cause": "同频邻居干扰",
    "selected_solution": "射频调优"
  }
}
```

关键结论：

> 页面可以跳转，但任务上下文不能依赖页面本身。任务连续性必须由 Runtime 托管，并在页面跳转前后完成状态校验和上下文恢复。

## 5. 问题三：如何和其他 KT 连接

KT6 是整体框架中的执行联动层，承接 KT1-KT5 的产物，并把 GUI 执行结果反哺给其他 KT。

```text
KT1：人机交互复杂意图理解
  输入给 KT6：结构化意图、任务目标、约束条件。
  KT6 使用：决定进入哪个业务场景、执行哪些 UI 操作。

KT2：用户偏好意图修正与问题生成推荐
  输入给 KT6：用户画像、偏好、历史行为、默认策略。
  KT6 使用：决定推荐哪个方案、是否默认展开细节、确认粒度。

KT3：Copilot 人在环 Runtime
  输入给 KT6：人工确认、修正、暂停、继续、回滚能力。
  KT6 使用：高风险 UI 操作前触发确认，失败时请求人工介入。

KT4：场景化 UI 生成
  输入给 KT6：场景化组件、动态 UI、UI 语义标注。
  KT6 使用：驱动 UI 组件随任务状态变化，生成合适交互界面。

KT5：意图拓扑生成
  输入给 KT6：业务拓扑、网络拓扑、任务拓扑。
  KT6 使用：联动拓扑节点高亮、跳转、筛选、定位故障设备。

KT6：意图驱动 UI 联动
  输出给其他 KT：执行轨迹、页面状态、用户反馈、操作结果、失败原因。
```

关键结论：

> KT6 不是孤立模块，而是把 KT1 的意图、KT2 的个性化、KT3 的人在环、KT4 的动态 UI、KT5 的拓扑能力统一落到 GUI 操作执行中。

## 6. 问题四：原子操作反复交互时如何保证多任务编排可靠

多任务编排的可靠性不能依赖 prompt 约束，而要依赖 Runtime 机制。

### 6.1 任务状态机

每个任务都必须有明确状态：

```text
created
 -> planning
 -> waiting_user
 -> executing
 -> verifying
 -> completed

异常分支：
paused / failed / cancelled / rollback_required
```

### 6.2 原子操作前置条件和后置校验

每个 UI 原子操作都需要声明执行条件和执行后的验证条件。

```json
{
  "op": "click",
  "target": "方案1 一键执行",
  "precondition": {
    "page": "用户体验保障",
    "root_cause_exists": true,
    "solution_status": "recommended"
  },
  "postcondition": {
    "page": "射频调优",
    "strategy_panel_visible": true
  }
}
```

### 6.3 任务锁和资源锁

需要引入多类锁，避免并发任务相互干扰：

```text
UI 页面锁
防止两个任务同时控制同一个页面。

业务资源锁
防止两个任务同时修改 AP1、站点1/1F、射频策略等资源。

用户会话锁
防止同一个用户问题被多个任务重复处理。

操作风险锁
高风险动作必须等待人工确认。
```

### 6.4 操作风险分级

原子操作按风险等级划分：

```text
read-only
跳转、搜索、读取、查看。

low-risk
填写、筛选、高亮、展开面板。

medium-risk
生成策略、保存草稿、预检查。

high-risk
下发配置、重启设备、修改网络参数。
```

高风险操作必须接入 KT3 的人在环确认能力。

### 6.5 执行日志和可回放轨迹

每个原子操作都需要记录执行日志：

```json
{
  "task_id": "task_001",
  "step_id": "execute_rf_optimization",
  "op": "click",
  "target": "下发策略按钮",
  "before_state": "策略待确认",
  "after_state": "策略已下发",
  "result": "success"
}
```

关键结论：

> 多任务编排的可靠性来自状态机、锁、幂等性、前后置校验、风险分级和执行日志，而不是让 Agent 自由发挥。

## 7. UI 原子操作定义

KT6 中的 UI 原子操作应作为稳定协议存在，用于连接任务规划和 GUI 执行。

建议原子操作包括：

```text
navigate(page)
跳转到指定页面。

click(element)
点击指定页面元素。

fill(element, value)
填写输入框。

select(element, option)
选择下拉选项、树节点或列表项。

set_time_range(element, value)
设置时间范围。

wait_for(condition)
等待页面、数据或任务状态满足条件。

read(element)
读取页面元素内容。

highlight(element)
高亮页面元素或拓扑节点。

confirm(action)
触发人工确认。

submit(form)
提交表单。

checkpoint(context)
保存任务上下文。

rollback(action)
回滚或撤销操作。

handoff_to_human(reason)
转人工处理。

report_to_chat(content)
将执行状态或结果回写到 LUI。
```

原子操作不只描述“做什么”，还要描述：

```text
target：操作目标。
source：操作来自哪个任务步骤。
precondition：执行前条件。
postcondition：执行后校验。
risk_level：风险等级。
timeout：超时时间。
retry_policy：重试策略。
fallback：失败后的降级策略。
```

## 8. 无线用户体验保障示例

### 8.1 用户输入

```text
用户张三昨天上午9:00反馈网速慢，帮忙看下是啥原因
```

### 8.2 结构化意图

```json
{
  "intent": "diagnose_user_experience",
  "user": "张三",
  "time_range": "昨天上午9:00",
  "symptom": "网速慢",
  "scenario": "用户体验保障"
}
```

### 8.3 业务任务链

```text
STEP 1 用户指标分析
STEP 2 关联设备问题分析
STEP 3 问题原因分析
STEP 4 推荐优化方案
STEP 5 用户确认执行方案
STEP 6 执行射频调优
STEP 7 校验用户体验恢复
```

### 8.4 UI 原子操作序列

```text
1. navigate 用户体验保障页面
2. fill 用户搜索框 = 张三
3. set_time_range = 昨天上午9:00
4. click 分析按钮
5. wait_for 用户指标分析完成
6. read 用户体验指标
7. read 关联设备 AP1
8. read 干扰分析结果
9. report_to_chat 问题原因为 AP1 同频邻居干扰
10. render 推荐方案卡片
11. wait_user_click 方案1 一键执行
12. checkpoint 当前上下文
13. navigate 射频调优页面
14. select 站点1/1F
15. locate AP1
16. generate 射频调优策略
17. confirm 下发策略
18. wait_for 调优完成
19. verify 张三体验恢复
20. report_to_chat 站点1/1F AP 射频调优已完成，用户张三体验恢复正常
```

### 8.5 对话侧输出示例

```text
智慧体调用“用户体验保障”任务链，分析用户张三网速慢的原因及优化建议如下：

STEP 1 用户指标分析完成
STEP 2 关联设备问题分析完成
STEP 3 问题原因分析完成

由于接入 AP1 出现同频邻居干扰问题，引起用户体验劣化。

针对该问题 AI 为您推荐以下两种解决方案：

方案1：射频调优
一键执行。系统自动分析站点1/1F AP 射频调优策略，并适时自动下发。

方案2：优化信道集配置
手动配置。在 5G 调优信道集中增加如下信道：149、153、157、161、165。
```

用户点击“一键执行”后：

```text
调优策略执行中。

站点1/1F AP 射频调优已完成，用户张三体验恢复正常。
```

## 9. 建议方案名称

中文名称：

> 基于 UI 原子操作的意图分解与多任务执行编排方案

更完整的中文名称：

> 面向 LUI-GUI 联动的 UI 原子操作意图分解与 Runtime 编排机制

英文名称：

> Intent-to-Atomic-UI-Action Decomposition and Runtime Orchestration for LUI-GUI Collaboration

## 10. 后续细化方向

下一步建议继续细化以下内容：

1. UI 原子操作协议字段定义。
2. Runtime 上下文模型。
3. 多任务状态机和锁机制。
4. UI 感知元素树结构。
5. 无线用户体验保障场景的端到端执行样例。
