# KT6 Bug & 架构问题清单

> 初始 review：2026-07-10  
> 一次复核：2026-07-13（29 测试通过）  
> 二次深查：2026-07-13（30 测试通过，全量代码逐行验证）

---

## 最终验证矩阵（2026-07-13 二轮深查）

| 项目 | 结论 | 验证方法 |
|---|---|---|
| BUG-1 空 scored IndexError | ✅ 已修复并有回归测试 | `router.py:63` + `test_router.py:42-58` |
| BUG-2 未知步骤静默跳过 | ✅ 已修复，白名单 + ValueError + 审计 | `runtime.py:18-45,379-380,668-669,371-375` + `test_runtime.py:155-171` |
| BUG-3 硬编码 fallback playbook_id | ✅ 已修复，缺失即拒绝 | `runtime.py:108-110` + `test_runtime.py:118-130` |
| BUG-4 import 副作用 | ✅ 已修复，工厂函数 | `app.py:35-60` + `test_app.py:12-14` |
| BUG-5 locks.clear() 误清 | ✅ 已修复，资源所有权 + try/finally | `runtime.py:63,147-165,660-671` + `test_runtime.py:209-231` |
| BUG-6 context 无锁读写 | ✅ 已修复，统一锁 + deepcopy | `runtime.py:167-173` + `test_runtime.py:173-194` |
| 新问题-3 solution_id 未校验 | ✅ 已修复，Playbook 白名单 + 推荐交集 | `runtime.py:119-128` + playbook `solution_ids` + `test_runtime.py:132-153` |
| 新问题-4 前端失败态卡住 | ✅ 已修复，failed/404/网络异常统一恢复 | `script.js:681-715,738-749,781-789` |
| 新问题-5 AP3 完成态残留 | ✅ 已修复，动态绑定标签 + clear_badge | `script.js:413-416,491-493,638` + `poe_port_recovery.json:62` |

---

## 二轮深查额外发现

### 🟢 观察-1: `_run_action_playbook` 旧代码从不设置 completed 状态

在引入 `try/finally/else` 之前，`_run_action_playbook` 没有 `_set_state(task, "completed")` 调用，只依赖 `complete` 步骤自行 emit。当前修复后显式在 finally 之后设置状态，修复了一个**潜在的状态机死锁**（前端会永远轮询因为 `state` 永远不是 `completed`）。

### 🟢 观察-2: `_update_context` 的 `copy.deepcopy(updates)` 对大型拓扑对象可能较重

`_record_perception` 传入整个 topology dict，其中包含 perception elements 数组。当前 mock 数据规模小无影响。生产环境如果 elements 数量很大，建议改为选择性 deepcopy 关键字段。

### 🟢 观察-3: `task.context["page_capture_id"]` 赋值绕过 `_update_context`

`runtime.py:70` — `create_task` 中的直接赋值发生在诊断线程启动前，当前无并发风险。但为 API 一致，建议未来统一切到 `_update_context`。

### 🟢 观察-4: 页面采集成功后任务创建失败会产生孤儿 capture 记录

`script.js:722-730` — 前端 `capturePagePerception()` 成功后若 `POST /api/tasks` 失败，服务端已持久化的 page capture 记录不会被清理。原型阶段可接受，生产环境需要 capture TTL 或引用计数。

### 🟢 观察-5: 旧模块 `__pycache__` 目录残留

`orchestrator/`、`domain/`、`runtime/`、`gui/` 目录仅剩 `__pycache__/*.pyc` 文件，源码已全部删除。建议 `git clean -fd` 清理或加 `.gitignore`。

---

## 架构现状

| 架构项 | 状态 |
|--------|------|
| 两套并行系统 | ✅ 已统一 |
| God Object (KT6Runtime) | ⚠️ context/resource/failure 已抽出；if-else 步骤执行有白名单保护暂未做注册表 |
| Playbook 声明式 vs 硬编码 | ⚠️ 新增 step 必须加白名单 + if-else 分支；type 字段已有但未用于自动派发 |
| LLM 边界 | ✅ IntentParser / Diagnoser Protocol + DI |
| SQLite 连接 | ⚠️ WAL + timeout 已启用，连接仍按操作创建未复用 |
| 前端轮询 | ⏸️ 250ms 轮询，暂缓 SSE |
| Mock 边界 | ✅ PagePerceptionService 独立；tools.py 仍承担适配桥接 |

---

## 测试覆盖（30/30 通过）

```
test_app.py                       2 tests ─ import 无副作用 + WAL 验证
test_memory.py                    1 test  ─ 全链路持久化
test_page_perception.py           3 tests ─ 实时采集 + vision model fallback + 变化检测
test_perception.py                3 tests ─ DOM/Canvas hybrid + 缓存命中 + 增量 revision
test_playbook_loader.py           2 tests ─ 加载 + 列表
test_router.py                    4 tests ─ 路由选择 + 排除 action playbook + 空 playbook 拒绝
test_runtime.py                  15 tests ─ 诊断流程 + AP 离线 + 缺参 + 动作授权 + 锁 + 并发 + 拓扑变更
```

---

## 风险评估

- **正确性**：关键路径均由测试覆盖，状态机、锁、solution_id 校验均有回归
- **并发安全**：所有 context 写入统一持锁；HTTP 读取经 deepcopy 快照；诊断线程只读无冲突（同线程模型）
- **可扩展性**：新增 playbook/step 仍需改 runtime.py，是下一步重构主目标
- **生产就绪度**：建议补齐认证、鉴权、SSE、连接池、capture TTL 后再上线
