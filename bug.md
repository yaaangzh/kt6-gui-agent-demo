# KT6 Bug & 架构问题清单

> 初始 review：2026-07-10
> 一次复核：2026-07-13（29 测试通过）
> 二次深查：2026-07-13（30 测试通过，全量代码逐行验证）
> 三次补强：2026-07-14（55 测试通过，步骤注册与负路径安全补强）

---

## 最新验证矩阵（2026-07-14 三次补强）

| 项目 | 结论 | 验证方法 |
|---|---|---|
| BUG-1 空 scored IndexError | ✅ 已修复并有回归测试 | `router.py:63` + `test_router.py:42-58` |
| BUG-2 未知步骤静默跳过 | ✅ 已修复，Registry 整本预检 + fail-fast + 审计 | `step_registry.py` + `test_runtime.py` + `test_step_registry.py` |
| BUG-3 硬编码 fallback playbook_id | ✅ 已修复，缺失即拒绝 | `KT6Runtime.execute_action` + `test_action_rejected_when_task_intent_has_no_playbook_id` |
| BUG-4 import 副作用 | ✅ 已修复，工厂函数 | `app.py:35-60` + `test_app.py:12-14` |
| BUG-5 locks.clear() 误清 | ✅ 已修复，资源所有权 + try/finally | `_acquire_resources` / `_release_resources` + 锁失败回归测试 |
| BUG-6 context 无锁读写 | ✅ 已修复，统一锁 + deepcopy | `_update_context` / `get_task_snapshot` + 并发快照测试 |
| 新问题-3 solution_id 未校验 | ✅ 已修复，Playbook 白名单 + 推荐交集 | `execute_action` + `test_execute_solution_rejects_missing_or_unrecommended_solution_id` |
| 新问题-4 前端失败态卡住 | ✅ 已修复，failed/404/网络异常统一恢复 | `script.js:681-715,738-749,781-789` |
| 新问题-5 AP3 完成态残留 | ✅ 已修复，动态绑定标签 + clear_badge | `script.js:413-416,491-493,638` + `poe_port_recovery.json:62` |
| 新问题-6 业务校验失败仍 completed | ✅ 已修复，四类动作后置条件 fail-closed | `runtime.py` + `test_runtime.py` |
| 新问题-7 Canvas 外壳掩盖视觉缺口 | ✅ 已修复，按真实截图判定并回退 DOM | `page_perception.py` + `test_page_perception.py` |
| 新问题-8 链路属性与并行边漏检 | ✅ 已修复，语义属性 diff + 多边稳定匹配 | `topology_change_detector.py` + `test_topology_change_detector.py` |

---

## 二轮深查额外发现

### 🟢 观察-1: `_run_action_playbook` 旧代码从不设置 completed 状态

在引入 `try/finally` 和显式完成状态之前，`_run_action_playbook` 没有 `_set_state(task, "completed")` 调用，只依赖 `complete` 步骤自行 emit。当前修复后仅在所有 handler 成功且资源锁释放后设置状态，修复了一个**潜在的状态机死锁**（前端会永远轮询因为 `state` 永远不是 `completed`）。

### 🟢 观察-2: `_update_context` 的 `copy.deepcopy(updates)` 对大型拓扑对象可能较重

`_record_perception` 传入整个 topology dict，其中包含 perception elements 数组。当前 mock 数据规模小无影响。生产环境如果 elements 数量很大，建议改为选择性 deepcopy 关键字段。

### 🟢 观察-3: `task.context["page_capture_id"]` 赋值绕过 `_update_context`

`KT6Runtime.create_task` 中的直接赋值发生在诊断线程启动前，当前无并发风险。但为 API 一致，建议未来统一切到 `_update_context`。

### 🟢 观察-4: 页面采集成功后任务创建失败会产生孤儿 capture 记录

`script.js:722-730` — 前端 `capturePagePerception()` 成功后若 `POST /api/tasks` 失败，服务端已持久化的 page capture 记录不会被清理。原型阶段可接受，生产环境需要 capture TTL 或引用计数。

### ✅ 观察-5: 旧模块 `__pycache__` 目录残留已清理

`orchestrator/`、`domain/`、`runtime/`、`gui/` 中仅存的历史 `.pyc` 已删除；这些目录不再属于当前工程结构。`.gitignore` 已持续忽略 `__pycache__/` 和 `*.py[cod]`。

### 🟢 观察-6: 完全同构且无稳定 ID 的并行链路仍存在消歧边界

当前优先使用 `relation_id` / `edge_id` / `id`，再使用端口、信道等稳定属性匹配并行边。若多条边没有任何稳定标识且顺序也变化，只能按确定出现顺序降级匹配；生产 Scene Graph 应为多重边提供稳定 `relation_id`。

---

## 架构现状

| 架构项 | 状态 |
|--------|------|
| 两套并行系统 | ✅ 已统一 |
| God Object (KT6Runtime) | ⚠️ context/resource/failure 已抽出，步骤分派已注册表化；内置业务 handler 仍位于 Runtime |
| Playbook 声明式 vs 硬编码 | ✅ 已使用 phase + step ID Registry，type/state/必填字段在副作用前统一预检 |
| LLM 边界 | ✅ IntentParser / Diagnoser Protocol + DI |
| SQLite 连接 | ⚠️ WAL + timeout 已启用，连接仍按操作创建未复用 |
| 前端轮询 | ⏸️ 250ms 轮询，暂缓 SSE |
| Mock 边界 | ✅ PagePerceptionService 独立；tools.py 仍承担适配桥接 |

---

## 测试覆盖（55/55 通过）

```
test_app.py                       2 tests ─ import 无副作用 + WAL 验证
test_memory.py                    1 test  ─ 全链路持久化
test_page_perception.py           6 tests ─ 实时采集 + 截图失败回退 + vision fallback + 变化检测
test_perception.py                4 tests ─ DOM/Canvas hybrid + 缓存命中 + 增量 revision + 链路阻断
test_playbook_loader.py           2 tests ─ 加载 + 列表
test_router.py                    4 tests ─ 路由选择 + 排除 action playbook + 空 playbook 拒绝
test_runtime.py                  22 tests ─ 诊断/动作 + 预检 + 授权 + 后置条件 + 锁 + 并发 + 拓扑变更
test_step_registry.py             5 tests ─ phase/type/state/字段校验 + 重复/冻结 + 外部注入
test_topology_change_detector.py  9 tests ─ 节点/链路属性 + 并行边匹配 + merge/empty
```

---

## 风险评估

- **正确性**：关键路径及主要负路径均有回归；业务后置条件失败不会进入完成态或写入成功记忆
- **并发安全**：所有 context 写入统一持锁；HTTP 读取经 deepcopy 快照；诊断线程只读无冲突（同线程模型）
- **可扩展性**：可通过注入 StepHandlerRegistry 扩展新步骤；内置 handler 仍可继续从 Runtime 拆分
- **生产就绪度**：建议补齐认证、鉴权、SSE、连接池、capture TTL 后再上线
