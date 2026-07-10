# KT6 LUI-GUI Runtime Demo

This project is a Runtime-backed KT6 prototype for intent-driven LUI-GUI linkage.

It is not a pure frontend animation. The frontend calls backend Runtime APIs; the backend loads scenario playbooks, invokes Agent and mock business tools, emits runtime/UI/chat events, and the GUI consumes those events to synchronize the topology view.

## Project Structure

```text
kt6_backend/
  app.py                HTTP API and static frontend server
  runtime.py            Task state machine and playbook executor
  router.py             Intent-to-playbook router for multi-scenario selection
  agent.py              Intent parsing, diagnosis reasoning, recommendation
  memory.py             SQLite-backed task/event/checkpoint/business memory
  perception.py         Mock UI perception adapter for existing canvas scenes
  tools.py              Mock business tools for replaceable data access
  tool_registry.py      Tool name -> callable registry
  playbook_loader.py    Loads scenario playbooks
  models.py             Task and event models

playbooks/
  ap_offline_diagnosis.json
  user_experience_assurance.json
  rf_optimization.json

data/
  mock_topology.json
  mock_ap_status.json
  mock_switch_port.json
  mock_user_experience.json
  mock_associated_device.json
  mock_radio_metrics.json
  mock_negative_checks.json
  mock_rf_strategy.json

demo/
  index.html
  styles.css
  script.js

tests/
  test_playbook_loader.py
  test_memory.py
  test_runtime.py

runtime_data/
  kt6_memory.sqlite3    Created at runtime. Stores persisted task traces and business memories.
```

## Business Thought Chain

The business thought chain is stored as executable playbooks:

```text
playbooks/ap_offline_diagnosis.json
playbooks/user_experience_assurance.json
playbooks/rf_optimization.json
```

Runtime loads the playbook after intent parsing and executes each step. Agent handles uncertain reasoning and explanation. Tools provide business data access. The frontend only consumes emitted events.

The Runtime is not tied to one Zhangsan scenario anymore. `PlaybookRouter` selects a playbook from `playbooks/*.json` based on intent hints and `trigger_intents`.

```text
用户张三昨天上午9:00反馈网速慢，帮忙看下是啥原因
  -> user_experience_assurance

AP3 昨晚一直离线，帮我看下
  -> ap_offline_diagnosis
```

## UI Perception And Grounding

KT6 supports two perception paths behind one structured scene graph contract.

```text
DOM element perception
  -> For ordinary pages where nodes, buttons, tables, and cards expose DOM/ARIA/data-* attributes.

Canvas screenshot perception
  -> For irregular topology canvases where AP/user/link nodes are pixels, not DOM elements.

Hybrid perception
  -> Prefer DOM when confidence is high; fall back to canvas when DOM is missing or incomplete.
```

Both paths produce the same output shape:

```text
existing business UI
  -> kt6_backend/perception.py
  -> scene graph + business object bindings
  -> Runtime UI events
  -> frontend canvas pan / zoom / highlight / progress sync
```

`perception.py` is still a mock adapter. It now exposes:

```text
DomElementPerception
CanvasScreenshotPerception
HybridPerception
```

Real integration should replace these mock capture/perceive functions with browser DOM snapshots, accessibility trees, topology engine APIs, canvas screenshots, OCR, CV, or multimodal vision models. Runtime and playbooks consume the structured scene graph and business bindings, so this layer can be replaced without rewriting the orchestration flow.

```text
data/mock_topology.json
  canvas size
  irregular node coordinates
  business object IDs
  visual grounding refs consumed by mock perception
  links and co-channel relations
```

## Run KT6 Business Demo

Start the Runtime-backed demo:

```powershell
python -m kt6_backend.app
```

Then open:

```text
http://127.0.0.1:8787/
```

The frontend calls the backend Runtime APIs:

```text
POST /api/tasks
GET  /api/tasks
GET  /api/tasks/{task_id}
GET  /api/tasks/{task_id}/events
POST /api/tasks/{task_id}/actions
GET  /api/memory
```

The business data is mocked under `data/`, but the runtime flow is real:

```text
张三网速慢 -> 左侧拓扑定位 -> AP1 同频干扰分析 -> 一键射频调优 -> 左侧进度同步 -> 体验恢复
```

## Runtime APIs

```text
GET  /api/health
GET  /api/playbooks
GET  /api/playbooks/{scenario_id}
GET  /api/tools
GET  /api/memory?limit={n}
POST /api/tasks
GET  /api/tasks?limit={n}
GET  /api/tasks/{task_id}
GET  /api/tasks/{task_id}/events?since={event_id}
POST /api/tasks/{task_id}/actions
```

Example:

```powershell
$task = Invoke-RestMethod -Method Post `
  -Uri 'http://127.0.0.1:8787/api/tasks' `
  -ContentType 'application/json' `
  -Body '{"query":"用户张三昨天上午9:00反馈网速慢，帮忙看下是啥原因"}'

Invoke-RestMethod -Uri "http://127.0.0.1:8787/api/tasks/$($task.task_id)/events?since=0"

Invoke-RestMethod -Uri 'http://127.0.0.1:8787/api/tasks?limit=5'

Invoke-RestMethod -Uri 'http://127.0.0.1:8787/api/memory?limit=10'
```

## Runtime Memory

This is no longer only an in-browser demonstration. The backend persists:

```text
tasks          task id, query, state, context, locks
events         runtime/chat/ui/solution event stream
checkpoints    high-risk action checkpoint before execution
memories       completed business incidents and resolution result
```

The current memory layer is local SQLite. It is still not a production knowledge base, but it gives the project a real persistence boundary that can later be replaced by a platform database, vector store, or enterprise memory service.

## Run Tests

```powershell
python -m unittest discover -s tests
```

## Replace Mock Data With Real Systems

Keep the Runtime and Playbooks stable. Replace implementations in:

```text
kt6_backend/tools.py
```

The expected tool names are registered in:

```text
kt6_backend/tool_registry.py
```

## Run CLI

```powershell
python main.py
```

## Run GUI

```powershell
python run_gui.py
```
