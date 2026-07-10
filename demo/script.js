const API_BASE = "";
const DEFAULT_STEP_LABELS = ["定位张三所在拓扑", "识别 AP1 与指标", "判断同频邻居干扰", "推荐并执行调优", "验证体验恢复"];

const state = {
  running: false,
  taskId: null,
  lastEventId: 0,
  pollTimer: null,
  playbookId: null,
  availableViews: new Set(["experience"]),
};

const PLAYBOOK_VIEW_CONFIG = {
  default: {
    toolbar: {
      experience: "诊断视图",
      rf: "执行视图",
      verify: "校验视图",
    },
    titles: {
      experience: "Canvas 全量拓扑 / 待选择思维链",
      rf: "Canvas 聚焦 / 待执行",
      verify: "Canvas 校验 / 待校验",
    },
  },
  user_experience_assurance: {
    toolbar: {
      experience: "用户体验保障",
      rf: "射频调优",
      verify: "体验校验",
    },
    titles: {
      experience: "Canvas 全量拓扑 / 用户体验保障",
      rf: "Canvas 聚焦 / AP1 射频调优执行",
      verify: "Canvas 校验 / 张三体验恢复",
    },
  },
  ap_offline_diagnosis: {
    toolbar: {
      experience: "AP 离线排障",
      rf: "PoE 恢复",
      verify: "在线校验",
    },
    titles: {
      experience: "Canvas 全量拓扑 / AP 离线排障",
      rf: "Canvas 聚焦 / AP3 PoE 恢复执行",
      verify: "Canvas 校验 / AP3 在线恢复",
    },
  },
};

const canvasState = {
  topology: null,
  objectMap: new Map(),
  camera: { x: 700, y: 450, scale: 0.72 },
  targetCamera: { x: 700, y: 450, scale: 0.72 },
  highlights: new Map(),
  focused: null,
  relationHighlights: new Set(),
  interferenceVisible: false,
  badge: null,
  perceptionMode: "raw",
  boundObjects: new Set(),
  tick: 0,
};

const el = {
  form: document.querySelector("#query-form"),
  input: document.querySelector("#query-input"),
  reset: document.querySelector("#reset-demo"),
  runtimeState: document.querySelector("#runtime-state"),
  playbookPill: document.querySelector("#playbook-pill"),
  routePanel: document.querySelector("#route-panel"),
  routeSelected: document.querySelector("#route-selected"),
  routeToggle: document.querySelector("#route-toggle"),
  routeCandidates: document.querySelector("#route-candidates"),
  sceneTitle: document.querySelector("#scene-title"),
  focusObject: document.querySelector("#focus-object"),
  metricUser: document.querySelector("#metric-user"),
  metricAp: document.querySelector("#metric-ap"),
  metricChannel: document.querySelector("#metric-channel"),
  metricNeighbor: document.querySelector("#metric-neighbor"),
  metricExperience: document.querySelector("#metric-experience"),
  canvas: document.querySelector("#topology-canvas"),
  progressCard: document.querySelector("#progress-card"),
  progressTitle: document.querySelector("#progress-title"),
  clarificationPanel: document.querySelector("#clarification-panel"),
  clarificationMessage: document.querySelector("#clarification-message"),
  solutionPanel: document.querySelector("#solution-panel"),
  solutionList: document.querySelector("#solution-list"),
  progress1: document.querySelector("#progress-step-1"),
  progress2: document.querySelector("#progress-step-2"),
  progress3: document.querySelector("#progress-step-3"),
  perceptionCapture: document.querySelector("#perception-capture"),
  perceptionScene: document.querySelector("#perception-scene"),
  perceptionBinding: document.querySelector("#perception-binding"),
  cursor: document.querySelector("#automation-cursor"),
  guiActionText: document.querySelector("#gui-action-text"),
  guiLog: document.querySelector("#gui-log-list"),
  scenePhase: document.querySelector("#scene-phase"),
  sceneHeadline: document.querySelector("#scene-headline"),
  sceneProgressBar: document.querySelector("#scene-progress-bar"),
  steps: [...document.querySelectorAll(".step")],
  toolButtons: [...document.querySelectorAll(".tool-button")],
  exampleSelect: document.querySelector("#example-select"),
};

const ctx = el.canvas.getContext("2d");

function setRuntime(text) {
  el.runtimeState.textContent = `Runtime: ${text}`;
}

function resizeQueryInput() {
  el.input.style.height = "auto";
  const nextHeight = Math.min(el.input.scrollHeight, 112);
  el.input.style.height = `${Math.max(42, nextHeight)}px`;
}

function setPlaybook(playbook) {
  if (!playbook) return;
  state.playbookId = playbook.scenario_id;
  el.playbookPill.textContent = `Playbook: ${playbook.name} (${playbook.scenario_id})`;
  updateToolbarLabels();
}

function currentViewConfig() {
  return PLAYBOOK_VIEW_CONFIG[state.playbookId] || PLAYBOOK_VIEW_CONFIG.default;
}

function updateToolbarLabels() {
  const labels = currentViewConfig().toolbar;
  el.toolButtons.forEach((button) => {
    button.textContent = labels[button.dataset.view] || button.textContent;
    button.classList.toggle("hidden", !state.availableViews.has(button.dataset.view));
  });
}

function setRouteDecision(routeDecision) {
  if (!routeDecision) return;
  const selected = routeDecision.selected || {};
  el.routePanel.classList.remove("hidden");
  el.routePanel.classList.remove("expanded");
  el.routeToggle.textContent = "展开";
  el.routeSelected.textContent = `选中：${selected.name || "-"} / 置信分 ${selected.confidence ?? 0}`;
  el.routeCandidates.innerHTML = (routeDecision.candidates || [])
    .map((candidate) => {
      const triggers = candidate.matched_triggers?.length ? candidate.matched_triggers.join("、") : "未命中";
      const slots = candidate.required_slots?.length ? candidate.required_slots.join(" / ") : "-";
      return `
        <div class="route-candidate ${candidate.selected ? "selected" : ""}">
          <div>
            <strong>${candidate.name}</strong>
            <span>${candidate.scenario_id}</span>
          </div>
          <p>分数 ${candidate.score}；触发词：${triggers}</p>
          <small>所需槽位：${slots}</small>
        </div>
      `;
    })
    .join("");
}

function setGuiAction(text) {
  if (!text) return;
  el.guiActionText.textContent = text;
  addGuiLog(text);
}

function setScene(scene) {
  if (!scene) return;
  el.scenePhase.textContent = scene.phase;
  el.sceneHeadline.textContent = scene.headline;
  el.sceneProgressBar.style.width = `${scene.progress}%`;
}

function addGuiLog(text) {
  [...el.guiLog.children].forEach((item) => item.classList.remove("latest"));
  const item = document.createElement("li");
  item.textContent = text;
  item.className = "latest";
  el.guiLog.appendChild(item);
  while (el.guiLog.children.length > 6) el.guiLog.removeChild(el.guiLog.firstElementChild);
}

function resizeCanvas() {
  const rect = el.canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  el.canvas.width = Math.floor(rect.width * ratio);
  el.canvas.height = Math.floor(rect.height * ratio);
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  fitAllIfNeeded();
}

function fitAllIfNeeded() {
  if (!canvasState.topology || canvasState.focused) return;
  const world = canvasState.topology.canvas;
  const rect = el.canvas.getBoundingClientRect();
  const scale = Math.min(rect.width / world.width, rect.height / world.height) * 0.86;
  canvasState.camera = { x: world.width / 2, y: world.height / 2, scale };
  canvasState.targetCamera = { ...canvasState.camera };
}

function worldToScreen(x, y) {
  const rect = el.canvas.getBoundingClientRect();
  return {
    x: rect.width / 2 + (x - canvasState.camera.x) * canvasState.camera.scale,
    y: rect.height / 2 + (y - canvasState.camera.y) * canvasState.camera.scale,
  };
}

function screenCursorForObject(id) {
  const object = canvasState.objectMap.get(id);
  if (!object) return null;
  const point = worldToScreen(object.x, object.y);
  const canvasRect = el.canvas.getBoundingClientRect();
  const stageRect = el.canvas.parentElement.getBoundingClientRect();
  return {
    x: canvasRect.left - stageRect.left + point.x + 8,
    y: canvasRect.top - stageRect.top + point.y + 8,
  };
}

function moveCursorToObject(id, click = false) {
  const point = screenCursorForObject(id);
  if (!point) return;
  el.cursor.classList.remove("clicking");
  el.cursor.style.transform = `translate(${point.x}px, ${point.y}px) rotate(-18deg)`;
  if (click) window.setTimeout(() => el.cursor.classList.add("clicking"), 80);
}

function setView(view) {
  state.availableViews.add(view);
  if (view === "verify") state.availableViews.add("rf");
  updateToolbarLabels();
  const labels = currentViewConfig().titles;
  el.sceneTitle.textContent = labels[view] || labels.experience;
  el.toolButtons.forEach((button) => button.classList.toggle("active", button.dataset.view === view));
}

function setStep(index, status) {
  const step = el.steps[index - 1];
  if (!step) return;
  step.classList.remove("running", "done");
  if (status) step.classList.add(status);
}

function setStepLabels(labels = []) {
  el.steps.forEach((step, index) => {
    const label = labels[index];
    if (label) step.querySelector("p").textContent = label;
    step.classList.toggle("hidden", !label && index >= labels.length);
  });
}

function setPerceptionStep(step, status) {
  const map = {
    capture: el.perceptionCapture,
    scene: el.perceptionScene,
    binding: el.perceptionBinding,
  };
  const item = map[step];
  if (!item) return;
  item.classList.remove("running", "done");
  if (status) item.classList.add(status);
}

function resetPerceptionSteps() {
  [el.perceptionCapture, el.perceptionScene, el.perceptionBinding].forEach((item) => {
    item.classList.remove("running", "done");
  });
}

function resetSteps() {
  el.steps.forEach((step) => step.classList.remove("running", "done"));
}

function setMetricPanel(values = {}) {
  if (values.focus) el.focusObject.textContent = values.focus;
  if (values.user) el.metricUser.textContent = values.user;
  if (values.ap) el.metricAp.textContent = values.ap;
  if (values.channel) el.metricChannel.textContent = values.channel;
  if (values.neighbor) el.metricNeighbor.textContent = values.neighbor;
  if (values.experience) el.metricExperience.textContent = values.experience;
}

async function loadInitialTopology() {
  const response = await fetch(`${API_BASE}/api/topology`);
  if (!response.ok) return;
  const topology = await response.json();
  loadCanvasTopology(topology);
}

function loadCanvasTopology(topology) {
  canvasState.topology = topology;
  canvasState.objectMap = new Map(topology.objects.map((object) => [object.business_id, object]));
  canvasState.highlights.clear();
  canvasState.relationHighlights.clear();
  canvasState.interferenceVisible = false;
  canvasState.badge = null;
  canvasState.perceptionMode = "raw";
  canvasState.boundObjects.clear();
  canvasState.focused = null;
  fitAllIfNeeded();
}

function focusObject(id) {
  const object = canvasState.objectMap.get(id);
  if (!object) return;
  canvasState.focused = id;
  canvasState.targetCamera = { x: object.x, y: object.y, scale: 1.22 };
  setTimeout(() => moveCursorToObject(id, true), 420);
}

function highlightObject(id, status) {
  canvasState.highlights.set(id, status);
}

function highlightRelation(source, target) {
  canvasState.relationHighlights.add(`${source}->${target}`);
}

function showInterference() {
  canvasState.interferenceVisible = true;
  const relations = canvasState.topology?.co_channel_relations || [];
  relations.forEach((relation) => {
    canvasState.relationHighlights.add(`${relation.source}->${relation.target}`);
    canvasState.highlights.set(relation.target, "danger");
  });
}

function clearInterference() {
  canvasState.interferenceVisible = false;
  canvasState.badge = null;
  (canvasState.topology?.co_channel_relations || []).forEach((relation) => {
    canvasState.relationHighlights.delete(`${relation.source}->${relation.target}`);
    canvasState.highlights.delete(relation.target);
  });
}

function showBadge(target, text) {
  canvasState.badge = { target, text };
}

function setProgressStep(target, status) {
  const map = { strategy: el.progress1, dispatch: el.progress2, verify: el.progress3 };
  const item = map[target];
  if (!item) return;
  item.classList.remove("running", "done");
  if (status === "running") item.classList.add("running");
  if (status === "done") item.classList.add("done");
}

function setProgressMode(mode) {
  if (mode === "ap_recovery") {
    el.progressTitle.textContent = "AP 恢复进展";
    el.progress1.textContent = "确认端口";
    el.progress2.textContent = "重启 PoE";
    el.progress3.textContent = "在线校验";
    return;
  }
  el.progressTitle.textContent = "射频调优进展";
  el.progress1.textContent = "策略生成";
  el.progress2.textContent = "策略下发";
  el.progress3.textContent = "生效校验";
}

function captureCanvas() {
  canvasState.perceptionMode = "capturing";
  setPerceptionStep("capture", "running");
  setGuiAction("UI Perception：捕获左侧业务 Canvas 图层");
}

function perceiveScene() {
  canvasState.perceptionMode = "perceived";
  setPerceptionStep("capture", "done");
  setPerceptionStep("scene", "running");
  setGuiAction("UI Perception：生成拓扑 Scene Graph 和候选元素框");
  window.setTimeout(() => setPerceptionStep("scene", "done"), 260);
}

function bindBusinessObject(id) {
  canvasState.perceptionMode = "bound";
  canvasState.boundObjects.add(id);
  setPerceptionStep("binding", "running");
  window.setTimeout(() => setPerceptionStep("binding", "done"), 360);
}

function resetTopology() {
  updateToolbarLabels();
  setView("experience");
  setRuntime("idle");
  canvasState.highlights.clear();
  canvasState.relationHighlights.clear();
  canvasState.interferenceVisible = false;
  canvasState.badge = null;
  canvasState.perceptionMode = "raw";
  canvasState.boundObjects.clear();
  canvasState.focused = null;
  fitAllIfNeeded();
  el.progressCard.classList.add("hidden");
  el.clarificationPanel.classList.add("hidden");
  el.clarificationMessage.textContent = "请补充必要输入。";
  el.solutionPanel.classList.add("hidden");
  el.solutionList.innerHTML = "";
  el.guiLog.innerHTML = "";
  setGuiAction("等待任务");
  setScene({ phase: "等待用户输入", headline: "左侧是不规则 Canvas 全量拓扑，等待 LUI 意图触发聚焦", progress: 0 });
  el.cursor.style.transform = "translate(84px, 84px) rotate(-18deg)";
  el.focusObject.textContent = "未绑定对象";
  el.metricUser.textContent = "-";
  el.metricAp.textContent = "-";
  el.metricChannel.textContent = "-";
  el.metricNeighbor.textContent = "-";
  el.metricExperience.textContent = "-";
  [el.progress1, el.progress2, el.progress3].forEach((item) => item.classList.remove("running", "done"));
  setProgressMode("rf");
  resetPerceptionSteps();
  setStepLabels(DEFAULT_STEP_LABELS);
  resetSteps();
}

function resetDemo() {
  if (state.pollTimer) window.clearInterval(state.pollTimer);
  state.running = false;
  state.taskId = null;
  state.lastEventId = 0;
  state.pollTimer = null;
  state.playbookId = null;
  state.availableViews = new Set(["experience"]);
  el.input.value = "";
  el.input.placeholder = "输入用户问题，Runtime 会自动选择对应思维链";
  resizeQueryInput();
  el.playbookPill.textContent = "Playbook: 未选择";
  el.routePanel.classList.add("hidden");
  el.routePanel.classList.remove("expanded");
  el.routeSelected.textContent = "等待用户输入选择思维链";
  el.routeToggle.textContent = "展开";
  el.routeCandidates.innerHTML = "";
  resetTopology();
}

function showSolutionPanel(solutions = []) {
  if (!solutions.length) return;
  el.solutionPanel.classList.remove("hidden");
  el.solutionList.innerHTML = solutions
    .map((solution, index) => {
      const executable = solution.execution_mode === "one_click" || solution.execution_mode === "manual_confirm";
      const button = executable
        ? `<button type="button" class="solution-panel-button" data-solution-id="${solution.solution_id}">${solution.execution_mode === "one_click" ? "一键执行" : "确认执行"}</button>`
        : "";
      return `
        <div class="solution-mini">
          <strong>方案${index + 1}：${solution.name}</strong>
          <p>${solution.description}</p>
          <span>${solution.execution_mode} / ${solution.risk_level}</span>
          ${button}
        </div>
      `;
    })
    .join("");
  el.solutionList.querySelectorAll(".solution-panel-button").forEach((button) => {
    button.addEventListener("click", () => executeOptimization(button.dataset.solutionId, button));
  });
}

function showClarificationPanel(message) {
  el.clarificationPanel.classList.remove("hidden");
  el.clarificationMessage.textContent = message || "请补充必要输入后重新提交。";
}

function applyUiAction(action) {
  if (action.op === "capture_canvas") captureCanvas();
  if (action.op === "load_canvas_topology" && action.topology) loadCanvasTopology(action.topology);
  if (action.op === "perceive_scene") perceiveScene();
  if (action.op === "bind_business_object") bindBusinessObject(action.target);
  if (action.op === "focus_object") focusObject(action.target);
  if (action.op === "highlight_object") highlightObject(action.target, action.status);
  if (action.op === "highlight_relation") highlightRelation(action.source, action.target);
  if (action.op === "show_interference") showInterference();
  if (action.op === "clear_interference") clearInterference();
  if (action.op === "show_badge") showBadge(action.target, action.text);
  if (action.op === "set_progress_mode") setProgressMode(action.target);
  if (action.op === "show_progress_card") el.progressCard.classList.remove("hidden");
  if (action.op === "set_progress_step") setProgressStep(action.target, action.status);
}

function playUiActions(actions) {
  actions.forEach((action, index) => {
    window.setTimeout(() => applyUiAction(action), index * 220);
  });
}

function applyEvent(event) {
  state.lastEventId = Math.max(state.lastEventId, event.id);
  if (event.topology) loadCanvasTopology(event.topology);
  if (event.runtime_state) setRuntime(event.runtime_state);
  if (event.playbook) setPlaybook(event.playbook);
  if (event.route_decision) setRouteDecision(event.route_decision);
  if (event.scene) setScene(event.scene);
  if (event.gui_action) setGuiAction(event.gui_action);
  if (event.step_labels) setStepLabels(event.step_labels);
  if (event.step) setStep(event.step.index, event.step.status);
  if (event.metrics) setMetricPanel(event.metrics);
  if (event.view) setView(event.view);

  const uiActions = [...(event.actions || []), ...(event.ui_actions || [])];
  playUiActions(uiActions);

  if (event.type === "solutions") {
    showSolutionPanel(event.solutions);
  }
  if (event.type === "clarification") showClarificationPanel(event.message);
  if (event.type === "step") setStep(event.index, event.status);
}

async function pollEvents() {
  if (!state.taskId) return;
  const response = await fetch(`${API_BASE}/api/tasks/${state.taskId}/events?since=${state.lastEventId}`);
  if (!response.ok) return;
  const payload = await response.json();
  payload.events.forEach(applyEvent);
  if (payload.state === "completed") {
    state.running = false;
    window.clearInterval(state.pollTimer);
    state.pollTimer = null;
    el.input.placeholder = "可以继续输入新的问题";
  }
  if (payload.state === "waiting_input") {
    state.running = false;
    window.clearInterval(state.pollTimer);
    state.pollTimer = null;
    el.input.placeholder = "请补充缺失信息后重新输入完整问题";
  }
}

async function runDiagnosis(query) {
  if (state.running) return;
  resetDemo();
  state.running = true;
  const response = await fetch(`${API_BASE}/api/tasks`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query }),
  });
  if (!response.ok) {
    setGuiAction("任务创建失败，请确认后端服务已启动");
    state.running = false;
    return;
  }
  const payload = await response.json();
  state.taskId = payload.task_id;
  el.input.value = "";
  resizeQueryInput();
  el.input.placeholder = "当前任务执行中，完成后可继续输入新的问题";
  state.pollTimer = window.setInterval(pollEvents, 250);
  await pollEvents();
}

async function executeOptimization(solutionId, button) {
  if (!state.taskId || button.disabled) return;
  button.disabled = true;
  const response = await fetch(`${API_BASE}/api/tasks/${state.taskId}/actions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: "execute_solution", solution_id: solutionId }),
  });
  if (!response.ok) setGuiAction("执行动作被 Runtime 拒绝，可能任务状态已变化或资源锁不可用");
}

function drawGrid(width, height) {
  ctx.save();
  ctx.fillStyle = "#f8fbff";
  ctx.fillRect(-200, -200, width + 400, height + 400);
  ctx.strokeStyle = "#e5edf7";
  ctx.lineWidth = 1;
  for (let x = -200; x <= width + 200; x += 80) {
    ctx.beginPath();
    ctx.moveTo(x, -200);
    ctx.lineTo(x, height + 200);
    ctx.stroke();
  }
  for (let y = -200; y <= height + 200; y += 80) {
    ctx.beginPath();
    ctx.moveTo(-200, y);
    ctx.lineTo(width + 200, y);
    ctx.stroke();
  }
  ctx.restore();
}

function drawBusinessCanvasFrame(width, height) {
  ctx.save();
  ctx.fillStyle = "rgba(255, 255, 255, 0.74)";
  roundRect(64, 58, width - 128, height - 116, 18);
  ctx.fill();
  ctx.strokeStyle = "#cbd5e1";
  ctx.lineWidth = 2;
  ctx.stroke();

  ctx.fillStyle = "#eff6ff";
  roundRect(96, 94, 290, 90, 10);
  ctx.fill();
  ctx.fillStyle = "#334155";
  ctx.font = "800 24px Microsoft YaHei UI";
  ctx.fillText("站点1 / 1F 无线网络拓扑", 118, 132);
  ctx.fillStyle = "#64748b";
  ctx.font = "700 15px Microsoft YaHei UI";
  ctx.fillText("Canvas 图层：AP / 终端 / 链路 / 射频热区", 118, 162);

  const heatZones = [
    [470, 430, 250, 210, "rgba(220, 38, 38, 0.11)"],
    [820, 350, 290, 230, "rgba(217, 119, 6, 0.1)"],
    [180, 560, 230, 210, "rgba(14, 165, 233, 0.1)"],
  ];
  heatZones.forEach(([x, y, w, h, fill]) => {
    ctx.fillStyle = fill;
    roundRect(x, y, w, h, 28);
    ctx.fill();
  });
  ctx.restore();
}

function objectRadius(object) {
  if (object.type === "core") return 34;
  if (object.type === "aggregation") return 30;
  if (object.type === "user") return 24;
  return 28;
}

function drawLink(source, target, type, highlighted = false) {
  const a = canvasState.objectMap.get(source);
  const b = canvasState.objectMap.get(target);
  if (!a || !b) return;
  ctx.save();
  ctx.beginPath();
  ctx.moveTo(a.x, a.y);
  const cx = (a.x + b.x) / 2 + (type === "access" ? -38 : 24);
  const cy = (a.y + b.y) / 2 + (type === "access" ? 22 : -18);
  ctx.quadraticCurveTo(cx, cy, b.x, b.y);
  ctx.lineWidth = highlighted ? 5 : 2;
  ctx.strokeStyle = highlighted ? "#2563eb" : type === "access" ? "#b6c4d6" : "#d6e0ec";
  if (type === "access" && !highlighted) ctx.setLineDash([8, 8]);
  ctx.stroke();
  ctx.restore();
}

function drawInterference(source, target) {
  const a = canvasState.objectMap.get(source);
  const b = canvasState.objectMap.get(target);
  if (!a || !b) return;
  ctx.save();
  ctx.beginPath();
  ctx.moveTo(a.x, a.y);
  const wave = Math.sin(canvasState.tick / 10) * 22;
  ctx.quadraticCurveTo((a.x + b.x) / 2, (a.y + b.y) / 2 + wave, b.x, b.y);
  ctx.lineWidth = 4;
  ctx.strokeStyle = "#dc2626";
  ctx.setLineDash([12, 8]);
  ctx.lineDashOffset = -canvasState.tick;
  ctx.stroke();
  ctx.restore();
}

function drawNode(object) {
  const status = canvasState.highlights.get(object.business_id);
  const focused = canvasState.focused === object.business_id;
  const rawMode = canvasState.perceptionMode === "raw" || canvasState.perceptionMode === "capturing";
  const bound = canvasState.boundObjects.has(object.business_id);
  const radius = objectRadius(object);
  const colors = {
    active: ["#eff6ff", "#2563eb"],
    warning: ["#fff7ed", "#d97706"],
    danger: ["#fef2f2", "#dc2626"],
    running: ["#eff6ff", "#2563eb"],
    success: ["#ecfdf5", "#0f9f6e"],
  };
  const [fill, stroke] = rawMode ? ["#ffffff", "#b8c2d3"] : colors[status] || ["#ffffff", "#94a3b8"];

  ctx.save();
  if (focused) {
    ctx.strokeStyle = "#2563eb";
    ctx.lineWidth = 3;
    ctx.setLineDash([12, 8]);
    ctx.strokeRect(object.x - radius - 18, object.y - radius - 18, (radius + 18) * 2, (radius + 18) * 2);
    ctx.fillStyle = "#2563eb";
    ctx.font = "700 15px Microsoft YaHei UI";
    ctx.textAlign = "center";
    ctx.fillText("Runtime Focus", object.x, object.y - radius - 30);
  }

  ctx.beginPath();
  ctx.arc(object.x, object.y, radius, 0, Math.PI * 2);
  ctx.shadowColor = "rgba(15, 23, 42, 0.18)";
  ctx.shadowBlur = 18;
  ctx.shadowOffsetY = 8;
  ctx.fillStyle = fill;
  ctx.fill();
  ctx.shadowColor = "transparent";
  ctx.lineWidth = status ? 5 : 3;
  ctx.strokeStyle = stroke;
  ctx.stroke();

  ctx.fillStyle = rawMode ? "#64748b" : "#172033";
  ctx.font = "800 18px Microsoft YaHei UI";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  const label = rawMode && object.type === "user" ? "STA" : object.label;
  ctx.fillText(label, object.x, object.y - (object.type === "ap" ? 5 : 0));
  if (object.type === "ap") {
    ctx.fillStyle = "#667085";
    ctx.font = "700 11px Microsoft YaHei UI";
    ctx.fillText(rawMode ? "5G" : `CH${object.channel}`, object.x, object.y + 15);
  }
  if (bound) {
    ctx.fillStyle = "#0f9f6e";
    ctx.font = "800 12px Microsoft YaHei UI";
    ctx.textAlign = "left";
    ctx.fillText("bound", object.x + radius + 10, object.y - radius + 4);
  }
  ctx.restore();
}

function drawPerceptionOverlays() {
  if (!canvasState.topology || canvasState.perceptionMode === "raw") return;
  const elements = canvasState.topology.ui_perception?.elements || [];
  ctx.save();
  if (canvasState.perceptionMode === "capturing") {
    const world = canvasState.topology.canvas;
    const y = 120 + (canvasState.tick * 8) % Math.max(1, world.height - 240);
    ctx.fillStyle = "rgba(37, 99, 235, 0.12)";
    ctx.fillRect(90, y, world.width - 180, 26);
    ctx.strokeStyle = "#2563eb";
    ctx.lineWidth = 2;
    ctx.strokeRect(90, y, world.width - 180, 26);
  }

  if (canvasState.perceptionMode === "perceived" || canvasState.perceptionMode === "bound") {
    elements.forEach((element) => {
      const [x, y, w, h] = element.bbox;
      const isBound = canvasState.boundObjects.has(element.business_id);
      ctx.strokeStyle = isBound ? "#0f9f6e" : "#2563eb";
      ctx.lineWidth = isBound ? 3 : 2;
      ctx.setLineDash(isBound ? [] : [8, 6]);
      ctx.strokeRect(x, y, w, h);
      ctx.fillStyle = isBound ? "#0f9f6e" : "#2563eb";
      ctx.font = "800 11px Microsoft YaHei UI";
      ctx.fillText(element.element_id, x, y - 8);
    });
  }
  ctx.restore();
}

function drawBadge() {
  if (!canvasState.badge) return;
  const object = canvasState.objectMap.get(canvasState.badge.target);
  if (!object) return;
  ctx.save();
  ctx.translate(object.x + 44, object.y - 58);
  ctx.fillStyle = "#dc2626";
  roundRect(0, 0, 118, 30, 7);
  ctx.fill();
  ctx.fillStyle = "#fff";
  ctx.font = "800 15px Microsoft YaHei UI";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(canvasState.badge.text, 59, 16);
  ctx.restore();
}

function roundRect(x, y, width, height, radius) {
  ctx.beginPath();
  ctx.moveTo(x + radius, y);
  ctx.lineTo(x + width - radius, y);
  ctx.quadraticCurveTo(x + width, y, x + width, y + radius);
  ctx.lineTo(x + width, y + height - radius);
  ctx.quadraticCurveTo(x + width, y + height, x + width - radius, y + height);
  ctx.lineTo(x + radius, y + height);
  ctx.quadraticCurveTo(x, y + height, x, y + height - radius);
  ctx.lineTo(x, y + radius);
  ctx.quadraticCurveTo(x, y, x + radius, y);
}

function renderCanvas() {
  const rect = el.canvas.getBoundingClientRect();
  canvasState.tick += 1;
  canvasState.camera.x += (canvasState.targetCamera.x - canvasState.camera.x) * 0.08;
  canvasState.camera.y += (canvasState.targetCamera.y - canvasState.camera.y) * 0.08;
  canvasState.camera.scale += (canvasState.targetCamera.scale - canvasState.camera.scale) * 0.08;

  ctx.clearRect(0, 0, rect.width, rect.height);
  ctx.save();
  ctx.translate(rect.width / 2, rect.height / 2);
  ctx.scale(canvasState.camera.scale, canvasState.camera.scale);
  ctx.translate(-canvasState.camera.x, -canvasState.camera.y);

  if (canvasState.topology) {
    drawGrid(canvasState.topology.canvas.width, canvasState.topology.canvas.height);
    drawBusinessCanvasFrame(canvasState.topology.canvas.width, canvasState.topology.canvas.height);
    for (const link of canvasState.topology.links || []) {
      const key = `${link.source}->${link.target}`;
      drawLink(link.source, link.target, link.type, canvasState.relationHighlights.has(key));
    }
    if (canvasState.interferenceVisible) {
      for (const relation of canvasState.topology.co_channel_relations || []) drawInterference(relation.source, relation.target);
    }
    for (const object of canvasState.topology.objects || []) drawNode(object);
    drawPerceptionOverlays();
    drawBadge();
  }

  ctx.restore();
  requestAnimationFrame(renderCanvas);
}

el.form.addEventListener("submit", (event) => {
  event.preventDefault();
  const query = el.input.value.trim();
  if (query) runDiagnosis(query);
});

el.input.addEventListener("input", resizeQueryInput);

el.input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    el.form.requestSubmit();
  }
});

el.exampleSelect.addEventListener("change", () => {
  if (state.running || !el.exampleSelect.value) return;
  el.input.value = el.exampleSelect.value;
  el.exampleSelect.value = "";
  resizeQueryInput();
  el.input.focus();
});

el.reset.addEventListener("click", resetDemo);
el.routeToggle.addEventListener("click", () => {
  const expanded = el.routePanel.classList.toggle("expanded");
  el.routeToggle.textContent = expanded ? "收起" : "展开";
});
window.addEventListener("resize", resizeCanvas);

resizeCanvas();
loadInitialTopology().then(resetDemo);
resizeQueryInput();
requestAnimationFrame(renderCanvas);
