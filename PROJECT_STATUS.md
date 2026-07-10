# Project Status

## What Is Complete

- Runtime-backed business demo, served by `kt6_backend.app`.
- Scenario playbooks stored outside Runtime code under `playbooks/`.
- Intent Agent and Diagnosis Agent separated from Runtime.
- Mock business tools and mock data separated from Runtime.
- Tool registry maps playbook tool names to callable implementations.
- Frontend consumes backend event stream instead of hardcoding the business flow.
- Left GUI is now an irregular `canvas` topology. Nodes are not DOM elements; Runtime events drive canvas grounding, camera focus, and highlights.
- Unit tests cover playbook loading and Runtime happy path.

## What Is Mocked

- User experience metrics.
- Associated AP lookup.
- Radio metrics and co-channel neighbor count.
- Negative checks for egress, RADIUS, DHCP.
- RF strategy generation and dispatch.
- GUI perception is represented by structured Runtime UI events.
- Canvas object perception is mocked through `data/mock_topology.json` coordinates and `visual_grounding`.

## What To Replace For Real Business Integration

- Replace `MockBusinessTools` in `kt6_backend/tools.py` with connectors to real systems.
- Keep tool names stable through `kt6_backend/tool_registry.py`.
- Keep playbooks stable unless the business flow changes.
- Replace canvas topology rendering or grounding with a real topology/canvas/page adapter if integrating an existing GUI.

## Current Boundary

This is a complete engineering prototype, not production software. It proves the architecture:

```text
User intent -> Agent -> Playbook -> Runtime -> Tools -> UI events -> LUI-GUI sync
```

Production hardening still needs persistence, authentication, real connector error handling, distributed locking, and browser/page perception adapters.
