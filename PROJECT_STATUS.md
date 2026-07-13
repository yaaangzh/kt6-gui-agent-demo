# Project Status

## What Is Complete

- Runtime-backed business demo, served by `kt6_backend.app`.
- Scenario playbooks stored outside Runtime code under `playbooks/`.
- Intent Agent and Diagnosis Agent separated from Runtime.
- Mock business tools and mock data separated from Runtime.
- Tool registry maps playbook tool names to callable implementations.
- Agent boundaries expose injectable `IntentParser` and `Diagnoser` protocols.
- App services are created lazily through a factory; importing the HTTP module no longer creates SQLite state.
- Runtime records executed Playbook steps and rejects unknown steps instead of silently skipping them.
- Runtime-level resource ownership prevents overlapping tasks from executing against the same declared resource.
- Task context writes and HTTP snapshots are synchronized; SQLite stores use WAL and busy timeouts.
- Frontend consumes backend event stream instead of hardcoding the business flow.
- Left GUI is now an irregular `canvas` topology. Nodes are not DOM elements; Runtime events drive canvas grounding, camera focus, and highlights.
- Browser-side page capture collects live DOM/ARIA elements and real canvas pixels before task creation and action execution.
- Page captures are persisted and linked to Runtime tasks through `page_capture_id`.
- Canvas renderer semantics, scene cache, revisions, and topology change detection share one Scene Graph contract.
- Unit tests cover routing, playbooks, Runtime, memory, app construction, concurrency, resource locks, scene caching, topology changes, and live page perception.

## What Is Mocked

- User experience metrics.
- Associated AP lookup.
- Radio metrics and co-channel neighbor count.
- Negative checks for egress, RADIUS, DHCP.
- RF strategy generation and dispatch.
- Business metrics, topology data, and device actions remain mocked.
- The current canvas node semantics come from the live page renderer adapter. The screenshot is real, but unknown-canvas OCR/CV/multimodal recognition is not yet connected.

## What To Replace For Real Business Integration

- Replace `MockBusinessTools` in `kt6_backend/tools.py` with connectors to real systems.
- Keep tool names stable through `kt6_backend/tool_registry.py`.
- Keep playbooks stable unless the business flow changes.
- Install the live page sensor or an equivalent browser extension into the target business page.
- Prefer a topology engine/renderer adapter when the real canvas exposes graph data; otherwise connect a vision model for screenshot-only pages.

## Current Boundary

This is a complete engineering prototype, not production software. It proves the architecture:

```text
User intent -> Agent -> Playbook -> Runtime -> Tools -> UI events -> LUI-GUI sync
```

Production hardening still needs authentication, real connector error handling, distributed locking, external-page deployment of the capture sensor, vision-model fallback, retention policies, and observability.
