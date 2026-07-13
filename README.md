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
  page_perception.py    Live browser DOM/canvas capture, persistence, and scene normalization
  perception.py         DOM/canvas business topology perception adapters
  perception_runtime.py Scene cache, revision, and external scene registration
  scene_store.py        Persistent versioned scene storage
  topology_change_detector.py  Node/edge/status change detection
  tools.py              Mock business tools for replaceable data access
  tool_registry.py      Tool name -> callable registry
  playbook_loader.py    Loads scenario playbooks
  models.py             Task and event models

playbooks/
  ap_offline_diagnosis.json
  user_experience_assurance.json
  rf_optimization.json
  poe_port_recovery.json

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
  test_app.py
  test_playbook_loader.py
  test_memory.py
  test_page_perception.py
  test_runtime.py

runtime_data/
  kt6_memory.sqlite3    Created at runtime. Stores persisted task traces and business memories.
  kt6_scene.sqlite3     Versioned scene cache.
  kt6_page_captures.sqlite3  Live page capture metadata.
  page_captures/        Real canvas screenshots captured by the browser.
```

## Business Thought Chain

The business thought chain is stored as executable playbooks:

```text
playbooks/ap_offline_diagnosis.json
playbooks/user_experience_assurance.json
playbooks/rf_optimization.json
```

Runtime loads the playbook after intent parsing and executes each step. Agent handles uncertain reasoning and explanation. Tools provide business data access. The frontend only consumes emitted events.

Playbook steps are fail-fast: an unknown step cannot be displayed and silently skipped. Runtime persists executed diagnosis/action step IDs under `context.executed_steps` for audit.

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

The project now supports a live page capture path in addition to the original business-data adapter:

```text
browser page
  -> live DOM/ARIA collection
  -> canvas.toDataURL() pixel capture
  -> optional canvas renderer scene adapter
  -> POST /api/perception/captures
  -> normalized Scene Graph + page_capture_id
  -> Runtime task and execution-time scene validation
```

`perception.py` exposes the business topology adapters:

```text
DomElementPerception
CanvasScreenshotPerception
HybridPerception
```

`page_perception.py` performs real DOM and canvas pixel capture. On the current page, canvas node semantics come from the renderer adapter, while pixels are captured and persisted for verification. If a page exposes only canvas pixels and no semantic adapter, the result is explicitly marked `requires_vision_model=true`; the system does not invent node bindings. OCR, CV, or a multimodal vision model remains the next adapter for those unknown canvases.

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
POST /api/perception/captures
GET  /api/perception/captures?limit={n}
GET  /api/perception/captures/{capture_id}
GET  /api/perception/cache
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
POST /api/perception/captures
GET  /api/perception/captures?limit={n}
GET  /api/perception/captures/{capture_id}
GET  /api/perception/cache
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

## Compatible Entry Points

```powershell
python main.py
# or
python run_gui.py
```

Both commands start the same Runtime-backed Web UI at `http://127.0.0.1:8787/`. The earlier standalone Tkinter prototype has been removed.
