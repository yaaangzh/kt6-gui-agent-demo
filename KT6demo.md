# KT6 意图驱动 LUI-GUI 联动框架

> 工程原型阶段 · 30 项测试通过 · 2026-07-13

---

## 一、项目定位

KT6 的目标不是构建一个单独的无线诊断 Agent，而是构建一个面向复杂人机协作场景的 **意图驱动 UI 联动执行框架（Intent-driven LUI-GUI Runtime）**。

一句话概括：**用户说一句话 → 系统选择诊断剧本 → 左侧拓扑图自动定位高亮 → 对话侧同步分析推理 → 一键执行恢复策略 → 两侧同步校验结果。**

```
用户自然语言 → 结构化意图 → Playbook 路由 → Runtime 状态机 → 事件流 → GUI 同步
                                      ↑                    ↑
                                 IntentParser         Tool Registry
                                 Diagnoser            (可替换适配器)
```

---

## 二、业务场景

当前覆盖两个完整的端到端诊断场景：

### 场景 A：用户体验保障
```
输入：用户张三昨天上午9:00反馈网速慢，帮忙看下是啥原因
 → 路由到 user_experience_assurance playbook
 → 左侧定位张三所在站点1/1F拓扑，识别AP1
 → 读取用户体验指标、关联设备、射频指标
 → 根因判定：AP1 同频邻居干扰（置信度 0.86）
 → 推荐方案：射频调优（一键执行）/ 优化信道集配置（手动）
 → 用户点击一键执行 → 确认→锁定→策略生成→下发→校验→完成
 → 左侧进度同步，右侧对话总结
```

### 场景 B：AP 离线排障
```
输入：AP3 昨晚一直离线，帮我看下
 → 路由到 ap_offline_diagnosis playbook
 → 定位 AP3 所在拓扑、高亮故障态
 → 查询 AP 心跳、交换机端口 PoE 状态
 → 根因判定：交换机端口 PoE 供电异常（置信度 0.82）
 → 推荐方案：重启 PoE 端口（确认执行）/ 派单现场检查（手动）
 → 用户确认 → 执行 → 校验 AP 在线 → 完成
```

---

## 三、系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                        前端 (demo/)                          │
│  ┌──────────┐  ┌──────────────┐  ┌───────────────────────┐  │
│  │ Canvas   │  │ 对话/方案面板 │  │ 实时页面采集传感器      │  │
│  │ 拓扑渲染  │  │ 路由/步进板   │  │ DOM + Canvas 截图       │  │
│  └──────────┘  └──────────────┘  └───────────────────────┘  │
│         ↑              ↑                   │                 │
│         │   事件流 (250ms 轮询)             │ POST /captures  │
├─────────┼──────────────┼───────────────────┼─────────────────┤
│         │              │                   ↓                 │
│  ┌──────────────────────────────────────────────────────┐    │
│  │                  HTTP API (app.py)                    │    │
│  │  create_services() 工厂 · ThreadingHTTPServer          │    │
│  │  13 个 REST 端点 · JSON 协议                           │    │
│  └──────────────────────────────────────────────────────┘    │
│                              │                               │
│  ┌───────────────────────────┴──────────────────────────┐    │
│  │                   KT6Runtime (runtime.py)             │    │
│  │  ┌─────────┐ ┌──────────┐ ┌────────┐ ┌───────────┐  │    │
│  │  │ Router  │ │ 状态机    │ │ 步骤   │ │ 资源锁    │  │    │
│  │  │ 多候选  │ │ 13 状态   │ │ 白名单  │ │ 所有权    │  │    │
│  │  │ 评分    │ │ lifecycle │ │ 审计   │ │ 跨task    │  │    │
│  │  └─────────┘ └──────────┘ └────────┘ └───────────┘  │    │
│  │  ┌──────────────┐ ┌────────────┐ ┌───────────────┐  │    │
│  │  │ IntentParser │ │ Diagnoser  │ │ ToolRegistry  │  │    │
│  │  │ (Protocol)   │ │ (Protocol) │ │ name→callable │  │    │
│  │  └──────────────┘ └────────────┘ └───────────────┘  │    │
│  └──────────────────────────────────────────────────────┘    │
│                              │                               │
│  ┌───────────┐ ┌────────────┴──────┐ ┌──────────────────┐    │
│  │ Playbook  │ │   MockBusiness    │ │  Perception 管道  │    │
│  │ Loader    │ │   Tools           │ │  Scene Store     │    │
│  │ 4 个 JSON │ │   data/*.json     │ │  Change Detector │    │
│  └───────────┘ └───────────────────┘ │  Page Capture    │    │
│                                      └──────────────────┘    │
│  ┌──────────────────────────────────────────────────────┐    │
│  │              持久层 (SQLite × 3, WAL 模式)            │    │
│  │  kt6_memory.sqlite3     — tasks/events/checkpoints   │    │
│  │  kt6_scene.sqlite3      — scene snapshots/changes    │    │
│  │  kt6_page_captures.sqlite3 — 实时页面采集元数据       │    │
│  └──────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
```

---

## 四、核心模块

### 4.1 Runtime 引擎 (`kt6_backend/runtime.py`)
- **13 状态任务状态机**：`created → planning → waiting_input → locating → perceiving → reasoning → waiting_user → confirming → executing → verifying → completed / failed / replanning`
- **步骤执行**：白名单机制，未知步骤立即 `ValueError`，已执行步骤写入 `executed_steps` 审计轨迹
- **资源锁**：Runtime 级 `resource_owners` 字典，跨 task 互斥，所有权校验，try/finally 保证释放
- **并发安全**：`threading.RLock` 保护所有 context 写入和 HTTP 读取（deepcopy 快照）

### 4.2 Playbook 剧本系统 (`playbooks/*.json`)
- 4 个 JSON 剧本：`user_experience_assurance` / `ap_offline_diagnosis` / `rf_optimization` / `poe_port_recovery`
- 每个剧本包含：触发词、必填槽位、诊断步骤、UI 操作序列、可执行动作
- 动作授权：`solution_ids` 白名单 + 推荐交集双重校验

### 4.3 路由系统 (`kt6_backend/router.py`)
- 多候选评分（触发词匹配 + 首选链路加权）
- 自动排除 action-only playbook（无 `create_context` 步骤）
- 空候选时显式 `RuntimeError`

### 4.4 Agent 接口 (`kt6_backend/agent.py`)
- `IntentParser` Protocol — 意图解析，当前 `IntentAgent` 用正则实现
- `Diagnoser` Protocol — 根因推理 + 方案推荐，当前 `DiagnosisAgent` 用规则实现
- 通过构造函数 DI 注入，可替换为 LLM 实现

### 4.5 感知管道
```
业务数据适配器路径 (perception.py):
  mock_topology.json → DomElementPerception / CanvasScreenshotPerception
  → HybridPerception → PerceptionRuntime.resolve() → Scene Cache

实时页面采集路径 (page_perception.py):
  浏览器 DOM/ARIA → Canvas.toDataURL() → 渲染器适配器
  → PagePerceptionService.ingest() → PerceptionRuntime.register_external()
  → SQLite 持久化 → 结构化的 Scene Graph
```

- **Scene Store**：版本化的场景快照（InMemory + SQLite 双实现）
- **Topology Change Detector**：节点增删移动 + 边变化 + 属性 diff → 阻塞/重绑定分类
- **实时采集**：前端 `capturePagePerception()` 采集 DOM+Canvas，POST 到服务端落盘

### 4.6 工具注册表 (`kt6_backend/tool_registry.py`)
- 15 个已注册的工具名称 → callable 映射
- 分类：topology / experience / wireless / radio / network / rf_optimization

### 4.7 持久层 (`kt6_backend/memory.py` + `scene_store.py` + `page_perception.py`)
- 3 个 SQLite 数据库，全部 WAL 模式 + 5s busy timeout
- 存储：tasks / events / checkpoints / memories / scene_snapshots / scene_changes / page_captures
- 真实 Canvas 截图落盘到 `runtime_data/page_captures/`

---

## 五、API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/health` | 健康检查 |
| GET | `/api/playbooks` | 列出所有剧本 |
| GET | `/api/playbooks/{id}` | 剧本详情 |
| GET | `/api/tools` | 列出所有工具 |
| GET | `/api/topology` | 获取全量拓扑 |
| GET | `/api/memory?limit=n` | 历史记忆列表 |
| GET | `/api/perception/cache?limit=n` | 场景缓存列表 |
| POST | `/api/perception/captures` | 提交实时页面采集 |
| GET | `/api/perception/captures?limit=n` | 采集记录列表 |
| GET | `/api/perception/captures/{id}` | 采集详情 |
| POST | `/api/tasks` | 创建诊断任务 |
| GET | `/api/tasks?limit=n` | 任务列表 |
| GET | `/api/tasks/{id}` | 任务详情（深拷贝快照） |
| GET | `/api/tasks/{id}/events?since=N` | 事件流（增量轮询） |
| POST | `/api/tasks/{id}/actions` | 提交用户动作（方案执行） |

---

## 六、当前进展

### ✅ 已完成

| 功能 | 状态 |
|------|------|
| HTTP API 服务（13 端点） | 完成 |
| Task 状态机（13 状态） | 完成 |
| Playbook 路由（多候选评分） | 完成 |
| 4 个 Playbook 剧本 | 完成 |
| 步骤白名单 + fail-fast + 审计轨迹 | 完成 |
| Runtime 级资源锁（跨 task 互斥） | 完成 |
| 线程安全（锁 + deepcopy 快照） | 完成 |
| SQLite 持久化（WAL 模式，3 个 DB） | 完成 |
| Scene Graph 版本管理（哈希 + 缓存） | 完成 |
| 拓扑变化检测（节点/边/属性 diff） | 完成 |
| 实时页面采集（DOM + Canvas 截图） | 完成 |
| 前端 Canvas 拓扑渲染引擎 | 完成 |
| LUI-GUI 事件联动（轮询驱动） | 完成 |
| IntentParser / Diagnoser Protocol（LLM 可替换） | 完成 |
| 工厂函数（import 无副作用） | 完成 |
| 旧系统清理（统一到 kt6_backend） | 完成 |
| 单元测试 30 项（路由/状态机/并发/锁/拓扑变更/实时采集） | 完成 |

### ⚠️ 部分完成

| 功能 | 现状 | 计划 |
|------|------|------|
| 步骤执行派发 | 硬编码 if-else（有白名单保护） | 注册表/策略模式 |
| KT6Runtime 拆分 | context/resource/failure 已抽出 | 步骤执行器独立 |
| SQLite 连接管理 | WAL + timeout 已启用，按操作建连 | 连接池或线程本地连接 |
| Mock 边界 | PagePerceptionService 已独立 | 业务工具桥接进一步收窄 |

### ⏸️ 暂缓

| 功能 | 原因 |
|------|------|
| SSE / WebSocket | 原型阶段 250ms 轮询够用 |
| LLM 接入 | Protocol 已就绪，等待真实后端 |
| 认证 / 鉴权 | 原型无外部暴露 |
| 分布式锁 | 单机 SQLite |
| Canvas 视觉模型 | 已标记 `requires_vision_model`，等待模型接入 |

---

## 七、工程边界

### 真实部分（非 mock）
- 编排引擎（Runtime 状态机）
- 路由系统（多候选评分）
- 并发模型（锁 + 快照）
- 持久化（3 个 SQLite DB + 文件存储）
- 场景版本管理（哈希 + diff）
- 拓扑变化检测
- 实时页面采集管道（DOM + Canvas 截图落盘）
- 前端渲染引擎（Canvas 2D）

### Mock 部分（替换即可接入真实系统）
- 业务数据（`data/*.json` → 替换 `tools.py` 对接真实 API）
- 意图解析 + 根因推理（`agent.py` 正则/规则 → 接入 LLM）
- Canvas 语义识别（渲染器适配器已有，未知 Canvas 标记 `requires_vision_model` → 接入视觉模型）

---

## 八、目录结构

```
FreeStyleCopilot/
├── kt6_backend/              主系统
│   ├── app.py                 HTTP 服务 + 工厂函数
│   ├── runtime.py             核心 Runtime（状态机 + 编排）
│   ├── agent.py               意图解析 + 根因推理（Protocol + 实现）
│   ├── router.py              Playbook 多候选路由
│   ├── memory.py              SQLite 任务/事件/记忆存储
│   ├── models.py              Task / RuntimeEvent 数据模型
│   ├── tools.py               Mock 业务工具（可替换）
│   ├── tool_registry.py       工具名 → callable 注册表
│   ├── playbook_loader.py     JSON Playbook 加载器
│   ├── perception.py          DOM/Canvas 感知适配器
│   ├── perception_runtime.py  场景缓存 + 版本管理
│   ├── page_perception.py     实时页面采集 + 落盘
│   ├── scene_store.py         版本化场景快照（InMemory + SQLite）
│   └── topology_change_detector.py  拓扑 diff 引擎
│
├── playbooks/                 场景剧本（4 个 JSON）
├── data/                      Mock 数据（8 个 JSON）
├── demo/                      前端（Canvas + 对话 + 实时采集）
├── tests/                     30 项单元测试
├── runtime_data/              运行时数据（SQLite + 截图文件）
│
├── main.py                    CLI 入口
├── run_gui.py                 Web 启动入口
├── DESIGN.md                  架构设计文档
├── README.md                  项目说明
├── PROJECT_STATUS.md          项目状态
├── bug.md                     问题清单与修复记录
└── KT6demo.md                 本文档
```

---

## 九、运行方式

```powershell
# 启动服务
python -m kt6_backend.app

# 浏览器打开
http://127.0.0.1:8787/

# 运行全部测试
python -m unittest discover -s tests
```

---

## 十、下一步

1. **步骤执行器注册表** — 消除 680 行 if-else，playbook `type` 字段自动派发
2. **SSE 事件推送** — 替换 250ms 轮询，实现真正的实时联动
3. **连接池** — SQLite / 未来 HTTP 客户端的连接复用
4. **LLM 接入** — 基于 IntentParser / Diagnoser Protocol 实现真实 LLM 推理
5. **真实数据连接器** — 将 `MockBusinessTools` 替换为真实 NMS/控制器 API
