# KT6 场景细化：张三网速慢诊断与一键调优

## 1. 场景目标

本场景用于细化 KT6 中“基于 UI 原子操作的意图分解与 Runtime 编排”能力。

用户通过 LUI 输入自然语言问题：

```text
用户张三昨天上午9:00反馈网速慢，帮忙看下是啥原因
```

系统需要完成：

1. 识别用户体验故障诊断意图。
2. 调用“用户体验保障”业务任务链。
3. 左侧立即跳转到张三所在站点/楼层的网络拓扑图。
4. 识别拓扑图中的张三接入链路、AP1、邻居 AP 和射频指标区域。
5. 查询张三在昨天上午 9:00 的体验指标。
6. 联动左侧拓扑分析关联 AP 和射频问题。
7. 判断根因为 AP1 同频邻居干扰，并在左侧高亮 AP1 与同频邻居关系。
8. 推荐两个解决方案。
9. 用户点击“方案1 一键执行”。
10. 左侧跳转到射频调优页面或拓扑中的 AP1 调优节点。
11. 自动生成并下发站点1/1F AP 射频调优策略。
12. 左侧同步展示策略生成、下发、生效、校验进展。
13. 校验张三体验恢复。
14. 将执行结果回写到对话侧。

## 2. 总体执行链路

```text
用户表达
  -> Agent 意图识别
  -> Runtime 创建任务
  -> Agent 选择业务任务链和拓扑定位目标
  -> Runtime 生成 LUI-GUI 联动执行计划
  -> 左侧 GUI 跳转到张三所在网络拓扑图
  -> UI Perception 识别拓扑图、AP 节点、指标面板和可交互元素
  -> Business Object Grounding 将 AP1 业务对象绑定到左侧图上节点
  -> GUI 页面跳转与 UI 原子操作执行
  -> Agent 分析数据并推荐方案
  -> Runtime 等待用户确认
  -> Runtime 驱动左侧跳到调优执行节点
  -> Runtime 执行高风险调优操作并同步左侧进度
  -> Agent 生成最终总结
  -> Runtime 回写 LUI 并归档任务
```

核心分工：

```text
Agent 负责：
- 不确定性的理解、判断、解释、推荐。
- 包括意图识别、实体抽取、任务链选择、指标解释、根因推理、方案生成。

Runtime 负责：
- 确定性的状态、跳转、执行、校验、安全控制。
- 包括上下文管理、UI 原子操作执行、状态机、锁、人在环确认、日志和回放。

UI Perception / Grounding 负责：
- 将左侧拓扑图、图片、canvas 或页面组件转换为结构化 UI 场景。
- 将图上的 AP1 节点、邻居 AP、链路、指标面板与业务对象和指标数据绑定。
- 支撑“说到哪个对象，左侧就定位哪个对象；执行到哪一步，左侧就展示哪一步”。
```

## 2.1 左侧拓扑即时联动原则

本场景中，左侧不是被动展示结果，而是任务执行的一部分。用户说完“张三网速慢”后，Runtime 应立即驱动左侧 GUI 进入张三所在网络拓扑图，并在后续分析和修复过程中持续同步状态。

关键原则：

```text
1. 先定位，再分析
   用户意图明确后，左侧先跳转到张三所在站点/楼层拓扑，建立可视化上下文。

2. 业务数据负责判断，左侧拓扑负责解释
   AP1 和同频干扰不是只靠图片猜出来，而是由用户接入记录、AP 指标、射频指标共同判断，再绑定到左侧拓扑对象上展示。

3. 图上对象必须业务对象化
   左侧图上的 AP1 不能只是一个图形节点，必须绑定 business_id、指标、状态、可执行动作。

4. 两边状态同源
   对话侧 STEP、Runtime task state、左侧拓扑节点状态必须来自同一个任务上下文。

5. 修复进展必须可视化
   一键执行后，左侧要从“故障定位视图”切换到“调优执行视图”，同步展示策略生成、下发、生效和校验。
```

左侧拓扑结构化模型示例：

```json
{
  "page": "用户体验保障",
  "visual_scene": "站点1/1F 网络拓扑图",
  "objects": [
    {
      "type": "user",
      "label": "张三",
      "business_id": "user_zhangsan",
      "position": {"x": 180, "y": 320},
      "connected_ap": "ap_001"
    },
    {
      "type": "ap_node",
      "label": "AP1",
      "business_id": "ap_001",
      "position": {"x": 420, "y": 260},
      "status": "unknown",
      "metrics": {
        "channel": 149,
        "band": "5G",
        "channel_utilization": null,
        "co_channel_neighbors": null,
        "retransmission_rate": null
      },
      "actions": ["open_detail", "show_neighbors", "rf_optimize"]
    }
  ]
}
```

## 3. 第一轮：用户提出问题

### 3.1 用户输入

```text
用户张三昨天上午9:00反馈网速慢，帮忙看下是啥原因
```

### 3.2 Agent 需要使用的能力

1. **自然语言意图识别**
   识别该请求属于“用户体验故障诊断”类意图。

2. **实体抽取**
   抽取用户、时间、症状等关键实体。

   ```json
   {
     "user": "张三",
     "time_range": "昨天上午9:00",
     "symptom": "网速慢"
   }
   ```

3. **场景匹配**
   匹配到业务场景：用户体验保障。

4. **任务链选择**
   选择“用户体验保障”Scenario Playbook。

5. **参数补全**
   将“昨天上午9:00”转换为标准时间范围。

   示例：

   ```json
   {
     "start": "2026-07-02 08:30:00",
     "end": "2026-07-02 09:30:00"
   }
   ```

6. **初始任务规划**
   生成业务步骤：

   ```text
   STEP 1 用户指标分析
   STEP 2 关联设备问题分析
   STEP 3 问题原因分析
   STEP 4 推荐优化方案
   ```

### 3.3 Agent 输出结构化意图

```json
{
  "intent": "diagnose_user_experience",
  "scenario": "用户体验保障",
  "entities": {
    "user": "张三",
    "time_range": "昨天上午9:00",
    "symptom": "网速慢"
  },
  "task_goal": "分析张三网速慢原因并给出优化建议"
}
```

### 3.4 Runtime 需要使用的能力

1. **创建任务**

   ```text
   task_id = task_zhangsan_slow_network
   ```

2. **初始化上下文**
   保存 Conversation Context、Task Context、Business Context。

3. **加载任务链**
   加载“用户体验保障”Scenario Playbook。

4. **判断是否需要人工补充**
   如果用户、时间、症状都足够明确，则不打断用户。

5. **创建 UI 执行计划**
   将业务步骤交给 UI Action Planner 拆成原子操作。

### 3.5 Runtime 上下文

```json
{
  "task_id": "task_zhangsan_slow_network",
  "state": "planning",
  "business_context": {
    "user": "张三",
    "time_range": "昨天上午9:00",
    "symptom": "网速慢",
    "scenario": "用户体验保障"
  },
  "current_step": "prepare_user_experience_analysis"
}
```

## 4. 第二步：左侧立即跳转张三所在网络拓扑

### 4.1 UI 原子操作

```json
{
  "op": "navigate",
  "target": {
    "page": "用户体验保障",
    "view": "用户所在网络拓扑",
    "params": {
      "user": "张三",
      "time_range": "昨天上午9:00"
    }
  },
  "precondition": {
    "task_state": "planning",
    "scenario": "用户体验保障"
  },
  "postcondition": {
    "current_page": "用户体验保障",
    "current_view": "用户所在网络拓扑",
    "page_ready": true
  },
  "risk_level": "read-only"
}
```

### 4.2 Runtime 需要使用的能力

1. 页面路由能力。
2. 根据用户和时间定位站点/楼层。
3. 左侧拓扑视图跳转。
4. 页面加载状态检测。
5. UI Context 更新。
6. 执行日志记录。

### 4.3 Agent 是否参与

Agent 需要轻参与，负责把“张三昨天上午9点网速慢”转换成拓扑定位目标：

```json
{
  "visual_target": {
    "object_type": "user",
    "object_name": "张三",
    "expected_view": "用户所在网络拓扑"
  }
}
```

Runtime 负责根据该目标完成确定性跳转。

### 4.4 GUI 与 LUI 表现

GUI：

```text
左侧立即跳转到张三所在站点/楼层的网络拓扑图。
拓扑图先高亮“张三”或张三当前接入链路。
```

LUI：

```text
正在定位张三所在网络拓扑...
```

## 4.5 左侧拓扑识别与业务对象绑定

左侧跳转完成后，Runtime 需要触发 UI Perception 和 Business Object Grounding，将拓扑图中的视觉对象转换为可理解、可操作、可联动的业务对象。

### 4.5.1 UI Perception 识别内容

```text
1. 拓扑图类型
   判断当前左侧区域是站点拓扑、楼层平面图、AP 拓扑、canvas 图还是图片叠加层。

2. 可见对象
   识别用户节点、AP 节点、链路、告警标识、指标卡片、操作按钮。

3. 空间关系
   识别张三连接到哪个 AP，AP1 周围有哪些邻居 AP。

4. 视觉状态
   识别节点颜色、告警角标、连线状态、进度状态。

5. 可交互能力
   识别哪些对象可点击、可展开、可查看详情、可进入调优。
```

### 4.5.2 Business Object Grounding 绑定结果

```json
{
  "grounded_objects": [
    {
      "business_id": "user_zhangsan",
      "type": "user",
      "label": "张三",
      "ui_ref": "topology.node.user_zhangsan",
      "position": {"x": 180, "y": 320},
      "state": "selected"
    },
    {
      "business_id": "ap_001",
      "type": "ap",
      "label": "AP1",
      "ui_ref": "topology.node.ap_001",
      "position": {"x": 420, "y": 260},
      "state": "associated",
      "relation": {
        "connected_users": ["user_zhangsan"]
      }
    }
  ]
}
```

### 4.5.3 左侧同步动作

```json
[
  {
    "op": "focus_object",
    "target": "user_zhangsan"
  },
  {
    "op": "highlight_object",
    "target": "user_zhangsan",
    "status": "active"
  },
  {
    "op": "highlight_relation",
    "source": "user_zhangsan",
    "target": "ap_001",
    "relation_type": "connected_to"
  }
]
```

对话侧回写：

```text
已定位到张三所在网络拓扑，并识别其接入链路。
```

## 5. 第三步：填写查询条件

### 5.1 填写用户

```json
{
  "op": "fill",
  "target": {
    "semantic": "用户搜索框",
    "component_type": "input"
  },
  "value": "张三",
  "precondition": {
    "current_page": "用户体验保障",
    "element_visible": true
  },
  "postcondition": {
    "input_value": "张三"
  },
  "risk_level": "low-risk"
}
```

### 5.2 设置时间

```json
{
  "op": "set_time_range",
  "target": {
    "semantic": "时间范围选择器",
    "component_type": "date_time_picker"
  },
  "value": "昨天上午9:00",
  "normalized_value": {
    "start": "2026-07-02 08:30:00",
    "end": "2026-07-02 09:30:00"
  },
  "precondition": {
    "current_page": "用户体验保障"
  },
  "postcondition": {
    "time_range_selected": true
  },
  "risk_level": "low-risk"
}
```

### 5.3 Agent 需要使用的能力

1. 时间语义解析。
2. 时间窗口补全。
3. 当“昨天上午9点”存在歧义时生成澄清问题。

### 5.4 Runtime 需要使用的能力

1. UI 元素定位。
2. 表单填写。
3. 时间表达式标准化结果注入。
4. 填写后校验。
5. 失败重试或请求人工确认。

### 5.5 对话侧回写

```text
已定位用户张三，并设置分析时间为昨天上午 9:00 前后。
```

## 6. 第四步：触发用户指标分析

### 6.1 点击分析按钮

```json
{
  "op": "click",
  "target": {
    "semantic": "分析按钮",
    "component_type": "button"
  },
  "precondition": {
    "user_filled": true,
    "time_range_selected": true
  },
  "postcondition": {
    "analysis_task_started": true
  },
  "risk_level": "read-only"
}
```

### 6.2 等待分析完成

```json
{
  "op": "wait_for",
  "target": {
    "condition": "用户指标分析完成"
  },
  "timeout": 30000,
  "postcondition": {
    "user_metrics_panel_visible": true
  }
}
```

### 6.3 Runtime 需要使用的能力

1. 点击执行。
2. 页面 loading 监听。
3. 后台任务状态监听。
4. 超时控制。

### 6.4 对话侧回写

```text
STEP 1 用户指标分析中...
STEP 1 用户指标分析完成
```

## 7. 第五步：读取用户体验指标

### 7.1 UI 原子操作

```json
{
  "op": "read",
  "target": {
    "semantic": "用户体验指标面板",
    "component_type": "data_panel"
  },
  "outputs": [
    "experience_score",
    "throughput",
    "latency",
    "packet_loss",
    "retransmission_rate"
  ],
  "precondition": {
    "user_metrics_panel_visible": true
  },
  "risk_level": "read-only"
}
```

### 7.2 示例读取结果

```json
{
  "experience_score": "poor",
  "throughput": "low",
  "retransmission_rate": "high",
  "latency": "normal",
  "packet_loss": "slightly_high"
}
```

### 7.3 Agent 需要使用的能力

1. 指标解释。
2. 判断是否存在体验劣化。
3. 决定是否继续查关联设备。

Agent 判断：

```text
张三在该时间段存在明显无线体验劣化，表现为吞吐下降和重传率升高，需要继续关联接入设备和射频环境。
```

### 7.4 Runtime 需要使用的能力

1. 页面数据读取。
2. UI 数据结构化。
3. 将读取结果写入 Task Context。

## 8. 第六步：关联设备问题分析

### 8.1 切换到关联设备分析

```json
{
  "op": "click",
  "target": {
    "semantic": "关联设备分析",
    "component_type": "tab"
  },
  "precondition": {
    "experience_score": "poor"
  },
  "postcondition": {
    "related_device_panel_visible": true
  },
  "risk_level": "read-only"
}
```

### 8.2 读取关联 AP

```json
{
  "op": "read",
  "target": {
    "semantic": "接入设备信息",
    "component_type": "data_table"
  },
  "outputs": [
    "ap_name",
    "site",
    "floor",
    "band",
    "rssi",
    "snr",
    "channel"
  ]
}
```

### 8.3 示例读取结果

```json
{
  "ap": "AP1",
  "site": "站点1",
  "floor": "1F",
  "band": "5G",
  "channel": 149,
  "rssi": "-72dBm",
  "snr": "18dB"
}
```

### 8.4 Agent 需要使用的能力

1. 关联设备判断。
2. 根据用户指标定位可能问题设备。
3. 判断是否需要继续分析射频。

### 8.5 Runtime 需要使用的能力

1. 页面 tab 切换。
2. 表格读取。
3. 数据归一化。
4. 关联输出写入上下文。

### 8.6 对话侧回写

```text
STEP 2 关联设备问题分析完成
已关联到接入设备 AP1，位置为站点1/1F。
```

### 8.7 左侧拓扑联动

读取到关联 AP 后，Runtime 不只把 AP1 写入上下文，还要把 AP1 绑定到左侧拓扑图上的节点。

```json
{
  "event": "associated_ap_identified",
  "task_id": "task_zhangsan_slow_network",
  "business_context_update": {
    "associated_ap": {
      "id": "ap_001",
      "name": "AP1",
      "site": "站点1",
      "floor": "1F"
    }
  },
  "ui_sync": [
    {
      "op": "focus_object",
      "target": "ap_001"
    },
    {
      "op": "highlight_object",
      "target": "ap_001",
      "status": "warning_candidate"
    },
    {
      "op": "show_badge",
      "target": "ap_001",
      "text": "张三接入 AP"
    }
  ]
}
```

左侧表现：

```text
拓扑图从张三节点平滑聚焦到 AP1。
张三到 AP1 的接入链路被高亮。
AP1 显示“张三接入 AP”标识。
```

## 9. 第七步：问题原因分析

### 9.1 进入射频环境分析

```json
{
  "op": "click",
  "target": {
    "semantic": "射频环境分析",
    "component_type": "tab"
  },
  "precondition": {
    "ap": "AP1"
  },
  "postcondition": {
    "radio_analysis_panel_visible": true
  },
  "risk_level": "read-only"
}
```

### 9.2 读取射频分析结果

```json
{
  "op": "read",
  "target": {
    "semantic": "射频干扰分析结果",
    "component_type": "data_panel"
  },
  "outputs": [
    "co_channel_interference",
    "neighbor_ap_count",
    "channel_utilization"
  ]
}
```

### 9.3 示例证据

```json
{
  "co_channel_interference": "high",
  "neighbor_ap_count": 6,
  "channel_utilization": "high",
  "root_cause": "AP1 同频邻居干扰"
}
```

### 9.4 Agent 需要使用的能力

1. 根因推理。
2. 多证据归因。
3. 生成自然语言解释。
4. 生成优化方案候选。

Agent 输出：

```text
由于接入 AP1 出现同频邻居干扰问题，引起用户体验劣化。
```

### 9.4.1 同频邻居干扰判定逻辑

“AP1 同频邻居干扰”不能只靠左侧图片猜测，需要由业务指标、拓扑关系和页面数据共同判定。

判定条件示例：

```text
1. 张三在昨天上午 9:00 接入 AP1。
2. AP1 工作在 5G 信道 149。
3. AP1 周边同信道邻居 AP 数量达到阈值，例如 >= 4。
4. AP1 信道利用率高。
5. 张三在该时间段吞吐下降、重传率升高。
6. 出口链路、认证、DHCP 未发现明显异常。
```

结构化根因结果：

```json
{
  "root_cause": "co_channel_interference",
  "root_cause_text": "AP1 同频邻居干扰",
  "confidence": 0.86,
  "affected_object": {
    "type": "ap",
    "id": "ap_001",
    "name": "AP1"
  },
  "evidence": [
    "张三在 09:00 接入 AP1",
    "AP1 工作在 5G 信道 149",
    "AP1 周边同信道邻居 AP 数量为 6",
    "AP1 信道利用率高",
    "张三重传率升高且吞吐下降",
    "出口链路、认证、DHCP 未见异常"
  ]
}
```

这里的分工是：

```text
Runtime：
负责读取用户接入记录、AP 指标、射频指标、拓扑邻居关系，并将这些数据结构化。

Agent：
负责基于结构化证据进行归因、解释和方案生成。

左侧拓扑：
负责把 AP1、同频邻居 AP、干扰关系和证据位置可视化。
```

### 9.5 Runtime 需要使用的能力

1. 将 root_cause 写入 Business Context。
2. 将 STEP 3 标记完成。
3. 触发方案推荐阶段。

### 9.6 对话侧回写

```text
STEP 3 问题原因分析完成
由于接入 AP1 出现同频邻居干扰问题，引起用户体验劣化。
```

### 9.7 左侧拓扑同步根因

当 Runtime 收到根因结果后，需要把诊断结论同步到左侧拓扑。

```json
{
  "event": "root_cause_identified",
  "task_id": "task_zhangsan_slow_network",
  "root_cause": "co_channel_interference",
  "ui_sync": [
    {
      "op": "highlight_object",
      "target": "ap_001",
      "status": "warning"
    },
    {
      "op": "highlight_related_objects",
      "target": "ap_001",
      "relation": "same_channel_neighbors"
    },
    {
      "op": "show_relation",
      "source": "ap_001",
      "targets": ["ap_002", "ap_003", "ap_004", "ap_005", "ap_006", "ap_007"],
      "relation_type": "co_channel_interference"
    },
    {
      "op": "show_badge",
      "target": "ap_001",
      "text": "同频干扰"
    },
    {
      "op": "show_metric_panel",
      "target": "ap_001",
      "metrics": ["channel", "channel_utilization", "co_channel_neighbors", "retransmission_rate"]
    }
  ]
}
```

左侧表现：

```text
AP1 被标记为 warning。
AP1 周围同信道邻居 AP 被高亮。
AP1 与邻居 AP 之间显示“同频干扰”关系。
AP1 旁边展示信道、信道利用率、同频邻居数量、重传率等指标。
```

## 10. 第八步：推荐解决方案

### 10.1 Agent 需要使用的能力

1. 方案生成。
2. 方案排序。
3. 风险判断。
4. 根据 KT2 用户偏好决定推荐表达。

### 10.2 Agent 输出方案

```json
[
  {
    "solution_id": "rf_optimization",
    "name": "射频调优",
    "execution_mode": "one_click",
    "risk_level": "high-risk",
    "description": "系统自动分析站点1/1F AP射频调优策略，并适时自动下发。"
  },
  {
    "solution_id": "channel_set_optimization",
    "name": "优化信道集配置",
    "execution_mode": "manual",
    "risk_level": "medium-risk",
    "description": "在5G调优信道集中增加信道149、153、157、161、165。"
  }
]
```

### 10.3 Runtime 需要使用的能力

1. 将方案写入上下文。
2. 判断方案1为高风险操作。
3. 将任务状态切换为 `waiting_user`。
4. 渲染对话侧方案卡片。

### 10.4 Runtime 状态

```json
{
  "task_state": "waiting_user",
  "waiting_for": "solution_selection",
  "available_actions": ["方案1 一键执行", "方案2 手动配置"]
}
```

### 10.5 对话侧显示

```text
针对该问题 AI 为您推荐以下两种解决方案：

方案1：射频调优
一键执行。系统自动分析站点1/1F AP 射频调优策略，并适时自动下发。

方案2：优化信道集配置
手动配置。在 5G 调优信道集中增加如下信道：149、153、157、161、165。
```

## 11. 第九步：用户点击“一键执行”

### 11.1 用户动作

用户可能点击按钮：

```text
方案1 一键执行
```

也可能输入自然语言：

```text
执行第一个方案
```

或：

```text
帮我一键调优
```

### 11.2 Agent 需要使用的能力

1. 用户动作识别。
2. 将“一键执行 / 第一个方案 / 帮我调优”映射到 `solution_id = rf_optimization`。
3. 判断用户是否具备执行权限。
4. 判断是否需要二次确认。

### 11.3 Runtime 需要使用的能力

1. 从 `waiting_user` 恢复任务。
2. 校验 `selected_solution` 是否存在。
3. 校验风险等级。
4. 触发 KT3 人在环确认。
5. 加业务资源锁：AP1 / 站点1/1F / 射频配置。
6. checkpoint 当前上下文。

### 11.4 Runtime checkpoint

```json
{
  "task_id": "task_zhangsan_slow_network",
  "current_step": "execute_rf_optimization",
  "selected_solution": "rf_optimization",
  "business_context": {
    "user": "张三",
    "ap": "AP1",
    "site": "站点1",
    "floor": "1F",
    "root_cause": "同频邻居干扰"
  },
  "locks": ["resource:site1_1f_ap_radio"]
}
```

### 11.5 人在环确认

如果需要确认，对话侧显示：

```text
射频调优涉及策略下发，是否确认继续执行？
```

用户确认后 Runtime 继续执行。

## 12. 第十步：跳转射频调优页面

### 12.1 UI 原子操作

```json
{
  "op": "navigate",
  "target": {
    "page": "射频调优"
  },
  "precondition": {
    "selected_solution": "rf_optimization",
    "user_confirmed": true,
    "resource_lock_acquired": true
  },
  "postcondition": {
    "current_page": "射频调优",
    "page_ready": true
  },
  "risk_level": "read-only"
}
```

### 12.2 Runtime 需要使用的能力

1. 跨页面跳转。
2. 上下文保持。
3. 页面加载检测。
4. UI Context 重建。

### 12.3 Agent 是否参与

通常不参与。若页面跳转失败，Agent 可参与解释失败原因或选择替代路径。

### 12.4 GUI 与 LUI 表现

GUI：

```text
左侧页面从用户体验保障拓扑视图跳转到射频调优页面，或在同一拓扑中切换到 AP1 的射频调优执行节点。
AP1 保持高亮，作为本次调优对象。
```

LUI：

```text
正在进入射频调优页面，并定位站点1/1F AP1。
```

### 12.5 左侧执行视图同步

一键执行后，左侧不应只跳到普通页面，而应进入“执行态视图”。该视图需要承接前面诊断阶段的 AP1 上下文。

```json
{
  "event": "enter_optimization_view",
  "task_id": "task_zhangsan_slow_network",
  "from_view": "用户体验保障拓扑",
  "to_view": "射频调优执行视图",
  "context_handoff": {
    "target_ap": "ap_001",
    "site": "站点1",
    "floor": "1F",
    "root_cause": "co_channel_interference",
    "selected_solution": "rf_optimization"
  },
  "ui_sync": [
    {
      "op": "focus_object",
      "target": "ap_001"
    },
    {
      "op": "highlight_object",
      "target": "ap_001",
      "status": "running"
    },
    {
      "op": "show_progress",
      "target": "ap_001",
      "step": "enter_optimization_view",
      "text": "准备射频调优"
    }
  ]
}
```

## 13. 第十一步：定位站点和 AP

### 13.1 选择站点

```json
{
  "op": "select",
  "target": {
    "semantic": "站点选择器",
    "component_type": "tree_selector"
  },
  "value": "站点1/1F",
  "precondition": {
    "current_page": "射频调优"
  },
  "postcondition": {
    "site_selected": "站点1/1F"
  },
  "risk_level": "low-risk"
}
```

### 13.2 定位 AP

```json
{
  "op": "highlight",
  "target": {
    "semantic": "AP1",
    "component_type": "topology_node"
  },
  "precondition": {
    "site_selected": "站点1/1F"
  },
  "postcondition": {
    "ap_highlighted": "AP1"
  },
  "risk_level": "read-only"
}
```

### 13.3 Runtime 需要使用的能力

1. 树选择器操作。
2. 拓扑节点定位。
3. 拓扑联动。
4. 页面元素高亮。

### 13.4 与 KT5 的连接

```text
KT5 提供站点/AP 拓扑。
KT6 根据任务上下文高亮 AP1，并驱动拓扑页面跳转定位。
```

## 14. 第十二步：生成调优策略

### 14.1 点击生成调优策略

```json
{
  "op": "click",
  "target": {
    "semantic": "生成调优策略",
    "component_type": "button"
  },
  "precondition": {
    "current_page": "射频调优",
    "site_selected": "站点1/1F",
    "ap_highlighted": "AP1"
  },
  "postcondition": {
    "strategy_generation_started": true
  },
  "risk_level": "medium-risk"
}
```

### 14.2 等待策略生成

```json
{
  "op": "wait_for",
  "target": {
    "condition": "调优策略生成完成"
  },
  "timeout": 60000,
  "postcondition": {
    "strategy_preview_visible": true
  }
}
```

### 14.3 读取策略预览

```json
{
  "op": "read",
  "target": {
    "semantic": "调优策略预览",
    "component_type": "strategy_panel"
  },
  "outputs": [
    "target_ap",
    "channel_plan",
    "power_plan",
    "expected_improvement"
  ]
}
```

### 14.4 Agent 需要使用的能力

1. 解释策略内容。
2. 判断策略是否和根因匹配。
3. 判断是否存在异常风险。

### 14.5 Runtime 需要使用的能力

1. 执行生成策略操作。
2. 等待策略生成。
3. 读取策略预览。
4. 执行前风险校验。

### 14.6 左侧策略生成进度同步

```json
{
  "event": "strategy_generation_started",
  "task_id": "task_zhangsan_slow_network",
  "ui_sync": [
    {
      "op": "show_progress",
      "target": "ap_001",
      "step": "strategy_generation",
      "status": "running",
      "text": "正在生成射频调优策略"
    },
    {
      "op": "show_badge",
      "target": "ap_001",
      "text": "策略生成中"
    }
  ]
}
```

策略生成完成后：

```json
{
  "event": "strategy_generated",
  "task_id": "task_zhangsan_slow_network",
  "ui_sync": [
    {
      "op": "show_strategy_preview",
      "target": "ap_001",
      "strategy": {
        "target": "AP1",
        "action": "射频调优",
        "expected_effect": "降低同频干扰，改善用户吞吐和重传率"
      }
    },
    {
      "op": "show_progress",
      "target": "ap_001",
      "step": "strategy_generation",
      "status": "success",
      "text": "调优策略已生成"
    }
  ]
}
```

## 15. 第十三步：下发射频调优策略

该步骤属于高风险动作，必须受 Runtime 安全控制。

### 15.1 确认下发

```json
{
  "op": "confirm",
  "target": {
    "semantic": "下发策略确认框"
  },
  "precondition": {
    "strategy_preview_visible": true,
    "risk_level": "high-risk",
    "user_confirmed": true
  },
  "postcondition": {
    "dispatch_confirmed": true
  },
  "risk_level": "high-risk"
}
```

### 15.2 点击下发策略

```json
{
  "op": "click",
  "target": {
    "semantic": "下发策略按钮",
    "component_type": "button"
  },
  "precondition": {
    "dispatch_confirmed": true,
    "resource_lock_acquired": true
  },
  "postcondition": {
    "optimization_started": true
  },
  "risk_level": "high-risk"
}
```

### 15.3 Runtime 需要使用的能力

1. 高风险操作控制。
2. 人在环确认。
3. 资源锁保持。
4. 操作幂等校验。
5. 下发状态监听。
6. 失败回滚或转人工。

### 15.4 Agent 需要使用的能力

1. 下发前解释风险。
2. 下发失败时生成原因解释和替代建议。

### 15.5 对话侧回写

```text
调优策略执行中...
```

### 15.6 左侧下发进度同步

下发开始：

```json
{
  "event": "optimization_dispatch_started",
  "task_id": "task_zhangsan_slow_network",
  "ui_sync": [
    {
      "op": "highlight_object",
      "target": "ap_001",
      "status": "running"
    },
    {
      "op": "show_progress",
      "target": "ap_001",
      "step": "dispatch",
      "status": "running",
      "text": "策略下发中"
    }
  ]
}
```

下发生效中：

```json
{
  "event": "optimization_applying",
  "task_id": "task_zhangsan_slow_network",
  "ui_sync": [
    {
      "op": "show_progress",
      "target": "ap_001",
      "step": "applying",
      "status": "running",
      "text": "策略生效校验中"
    },
    {
      "op": "show_metric_panel",
      "target": "ap_001",
      "metrics": ["channel_utilization", "co_channel_neighbors", "retransmission_rate"]
    }
  ]
}
```

## 16. 第十四步：等待调优完成并验证效果

### 16.1 等待射频调优完成

```json
{
  "op": "wait_for",
  "target": {
    "condition": "射频调优完成"
  },
  "timeout": 120000,
  "postcondition": {
    "optimization_status": "completed"
  }
}
```

### 16.2 返回用户体验保障页面

```json
{
  "op": "navigate",
  "target": {
    "page": "用户体验保障"
  },
  "precondition": {
    "optimization_status": "completed"
  },
  "postcondition": {
    "current_page": "用户体验保障"
  },
  "risk_level": "read-only"
}
```

### 16.3 重新读取用户体验状态

```json
{
  "op": "read",
  "target": {
    "semantic": "用户体验恢复状态"
  },
  "inputs": {
    "user": "张三",
    "time_range": "调优后"
  },
  "outputs": [
    "experience_score",
    "throughput",
    "retransmission_rate"
  ]
}
```

### 16.4 Agent 需要使用的能力

1. 调优前后对比分析。
2. 判断体验是否恢复。
3. 生成最终总结。

### 16.5 Runtime 需要使用的能力

1. 跨页面恢复上下文。
2. 调用验证步骤。
3. 释放资源锁。
4. 标记任务 `completed`。
5. 记录完整执行轨迹。

### 16.6 左侧完成态同步

调优完成后，左侧需要从“执行中”切换为“恢复正常”。

```json
{
  "event": "optimization_completed",
  "task_id": "task_zhangsan_slow_network",
  "ui_sync": [
    {
      "op": "highlight_object",
      "target": "ap_001",
      "status": "success"
    },
    {
      "op": "clear_relation",
      "source": "ap_001",
      "relation_type": "co_channel_interference"
    },
    {
      "op": "show_badge",
      "target": "ap_001",
      "text": "体验恢复"
    },
    {
      "op": "show_before_after_metrics",
      "target": "ap_001",
      "before": {
        "channel_utilization": "high",
        "retransmission_rate": "high",
        "experience_score": "poor"
      },
      "after": {
        "channel_utilization": "normal",
        "retransmission_rate": "normal",
        "experience_score": "normal"
      }
    }
  ]
}
```

左侧表现：

```text
AP1 从 running 变为 success。
同频干扰连线和告警标识消失。
AP1 上显示“体验恢复”。
指标面板展示调优前后对比。
```

## 17. 第十五步：最终对话回写

### 17.1 对话侧输出

```text
站点1/1F AP 射频调优已完成。

已重新校验用户张三的体验指标：
- 用户体验评分恢复正常
- 重传率下降
- 吞吐恢复正常

用户张三体验恢复正常。
```

### 17.2 Runtime 最终状态

```json
{
  "task_id": "task_zhangsan_slow_network",
  "state": "completed",
  "final_result": {
    "root_cause": "AP1 同频邻居干扰",
    "executed_solution": "射频调优",
    "result": "用户体验恢复正常"
  },
  "locks_released": true
}
```

## 18. 完整链路简表

| 阶段 | 用户或系统动作 | Agent 能力 | Runtime 能力 |
| --- | --- | --- | --- |
| 1 | 用户输入问题 | 意图识别、实体抽取、场景匹配 | 创建任务、初始化上下文 |
| 2 | 左侧跳转张三所在拓扑 | 输出拓扑定位目标 | 页面跳转、拓扑加载、UI Context 更新 |
| 3 | 识别左侧拓扑对象 | 轻参与 | UI 感知、对象识别、业务对象绑定 |
| 4 | 填写用户和时间 | 时间语义解析 | UI 元素定位、表单填写、后置校验 |
| 5 | 触发用户指标分析 | 轻参与 | 点击、等待、读取状态 |
| 6 | 分析用户指标 | 指标解释、判断体验劣化 | 读取页面数据、写入上下文 |
| 7 | 关联设备分析 | 判断 AP 与用户体验关系 | 切换页面区域、读取设备数据、左侧高亮 AP1 |
| 8 | 问题原因分析 | 根因推理、生成解释 | 保存 root cause、左侧标记同频干扰关系 |
| 9 | 推荐方案 | 生成方案、排序、风险判断 | 渲染方案卡片、进入 waiting_user |
| 10 | 用户点击一键执行 | 识别用户选择 | 恢复任务、风险确认、加资源锁 |
| 11 | 左侧跳转调优执行节点 | 异常时参与 | 跨页面跳转、上下文恢复、执行视图同步 |
| 12 | 定位站点和 AP | 轻参与 | 选择站点、高亮 AP、拓扑联动 |
| 13 | 生成调优策略 | 解释策略、判断风险 | 点击生成、等待完成、读取策略、左侧显示策略生成进度 |
| 14 | 下发策略 | 解释风险和失败原因 | 人在环确认、高风险执行、状态监听、左侧显示下发进度 |
| 15 | 验证效果 | 前后指标对比、生成总结 | 重新查询、释放锁、完成任务、左侧显示恢复正常 |
| 16 | 回写结果 | 生成自然语言总结 | 同步 LUI、记录日志、任务归档 |

## 19. 核心设计结论

本场景中，Agent 与 Runtime 的边界应保持清晰：

```text
Agent 负责：
不确定性的理解、判断、解释、推荐。

Runtime 负责：
确定性的状态、跳转、执行、校验、安全控制。
```

因此，KT6 不应被设计为简单的 browser use 或单一 Agent，而应被设计为：

> 面向 LUI-GUI 联动的 UI 原子操作意图分解与 Runtime 编排机制。
