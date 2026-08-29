const API_BASE = "http://127.0.0.1:8787";
const DIRECT_CDP_METHODS = new Set([
  "Accessibility.getFullAXTree",
  "Accessibility.queryAXTree",
  "DOM.describeNode",
  "DOM.focus",
  "DOM.getBoxModel",
  "DOM.getNodeForLocation",
  "DOMSnapshot.captureSnapshot",
  "Input.dispatchKeyEvent",
  "Input.dispatchMouseEvent",
  "Input.insertText",
  "Page.captureScreenshot",
  "Page.getFrameTree",
  "Page.getLayoutMetrics",
]);

let relayState = null;
let relayGeneration = 0;

async function api(path, payload) {
  const response = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(String(body.error || `HTTP ${response.status}`));
  return body;
}

function randomIdentifier(prefix) {
  return `${prefix}_${crypto.randomUUID().replaceAll("-", "")}`;
}

function finiteNumber(value, minimum = 0) {
  return typeof value === "number" && Number.isFinite(value) && value >= minimum;
}

function positiveBackendNodeId(value) {
  return Number.isInteger(value) && value > 0;
}

function validateCdpCommand(method, rawParams = {}) {
  if (!DIRECT_CDP_METHODS.has(method)) {
    throw new Error("browser_extension_cdp_method_not_allowed");
  }
  const params = rawParams && typeof rawParams === "object" ? rawParams : {};
  if (method === "Page.getFrameTree" || method === "Page.getLayoutMetrics") {
    if (Object.keys(params).length) throw new Error("browser_extension_cdp_params_invalid");
  } else if (method === "DOMSnapshot.captureSnapshot") {
    if (
      !Array.isArray(params.computedStyles) ||
      params.computedStyles.length !== 0 ||
      params.includePaintOrder !== true ||
      params.includeDOMRects !== true
    ) throw new Error("browser_extension_cdp_params_invalid");
  } else if (method === "Accessibility.getFullAXTree") {
    if (typeof params.frameId !== "string" || !params.frameId || params.frameId.length > 256) {
      throw new Error("browser_extension_cdp_params_invalid");
    }
  } else if (method === "Accessibility.queryAXTree") {
    if (!positiveBackendNodeId(params.backendNodeId)) {
      throw new Error("browser_extension_cdp_params_invalid");
    }
  } else if (method === "DOM.describeNode") {
    if (
      !positiveBackendNodeId(params.backendNodeId) ||
      ![0, 3].includes(params.depth) ||
      params.pierce !== true
    ) throw new Error("browser_extension_cdp_params_invalid");
  } else if (["DOM.focus", "DOM.getBoxModel"].includes(method)) {
    if (!positiveBackendNodeId(params.backendNodeId)) {
      throw new Error("browser_extension_cdp_params_invalid");
    }
  } else if (method === "DOM.getNodeForLocation") {
    if (!Number.isInteger(params.x) || !Number.isInteger(params.y) || params.x < 0 || params.y < 0) {
      throw new Error("browser_extension_cdp_params_invalid");
    }
  } else if (method === "Input.insertText") {
    if (
      typeof params.text !== "string" ||
      !params.text ||
      params.text.length > 1000 ||
      [...params.text].some((value) => value.codePointAt(0) < 32 || value.codePointAt(0) === 127)
    ) throw new Error("browser_extension_cdp_params_invalid");
  } else if (method === "Input.dispatchMouseEvent") {
    if (
      !["mousePressed", "mouseReleased"].includes(params.type) ||
      params.button !== "left" ||
      params.clickCount !== 1 ||
      !finiteNumber(params.x) ||
      !finiteNumber(params.y)
    ) throw new Error("browser_extension_cdp_params_invalid");
  } else if (method === "Input.dispatchKeyEvent") {
    const selectAll =
      ["keyDown", "keyUp"].includes(params.type) &&
      params.modifiers === 2 &&
      params.key === "a" &&
      params.code === "KeyA" &&
      params.windowsVirtualKeyCode === 65;
    const backspace =
      ["rawKeyDown", "keyUp"].includes(params.type) &&
      params.key === "Backspace" &&
      params.code === "Backspace" &&
      params.windowsVirtualKeyCode === 8;
    if (!selectAll && !backspace) throw new Error("browser_extension_cdp_params_invalid");
  } else if (method === "Page.captureScreenshot") {
    if (
      !["jpeg", "png"].includes(params.format) ||
      params.fromSurface !== true ||
      params.captureBeyondViewport !== false ||
      (params.format === "jpeg" && params.quality !== 55)
    ) throw new Error("browser_extension_cdp_params_invalid");
    if (params.clip !== undefined) {
      const clip = params.clip;
      if (
        !clip ||
        !finiteNumber(clip.x) ||
        !finiteNumber(clip.y) ||
        !finiteNumber(clip.width, Number.EPSILON) ||
        !finiteNumber(clip.height, Number.EPSILON) ||
        clip.scale !== 1
      ) throw new Error("browser_extension_cdp_params_invalid");
    }
  }
  return params;
}

async function exactTarget(targetId) {
  const targets = await chrome.debugger.getTargets();
  const matches = targets.filter(
    (target) => target.type === "page" && target.id === targetId && Number.isInteger(target.tabId),
  );
  if (matches.length !== 1) throw new Error("browser_target_binding_mismatch");
  return matches[0];
}

async function prepareRuntime(context) {
  if (
    !context ||
    !Number.isInteger(context.tabId) ||
    typeof context.browserTargetId !== "string" ||
    typeof context.startUrl !== "string"
  ) throw new Error("browser_extension_context_invalid");
  const target = await exactTarget(context.browserTargetId);
  if (target.tabId !== context.tabId || target.url !== context.startUrl) {
    throw new Error("browser_target_binding_mismatch");
  }
  if (!relayState || relayState.tabId !== context.tabId || relayState.targetId !== context.browserTargetId) {
    const previous = relayState;
    relayGeneration += 1;
    if (previous) {
      await chrome.debugger.detach({ tabId: previous.tabId }).catch(() => {});
      await api("/api/execution/extension-runtimes/unregister", {
        runtime_id: previous.runtimeId,
        token: previous.token,
      }).catch(() => {});
    }
    await chrome.debugger.attach({ tabId: context.tabId }, "1.3");
    relayState = {
      runtimeId: randomIdentifier("runtime"),
      token: `${randomIdentifier("token")}${randomIdentifier("token")}`,
      tabId: context.tabId,
      targetId: context.browserTargetId,
      pageUrl: context.startUrl,
      title: String(context.title || "").slice(0, 300),
      switching: false,
    };
  } else {
    relayState.pageUrl = context.startUrl;
    relayState.title = String(context.title || "").slice(0, 300);
  }
  const state = relayState;
  await api("/api/execution/extension-runtimes/register", {
    runtime_id: state.runtimeId,
    token: state.token,
    target_id: state.targetId,
    page_url: state.pageUrl,
    title: state.title,
  });
  const generation = relayGeneration;
  if (!state.polling) {
    state.polling = true;
    pollCommands(state, generation).catch((error) => console.error(error));
  }
  return { runtimeId: state.runtimeId, targetId: state.targetId, pageUrl: state.pageUrl };
}

async function executeCommand(state, command) {
  const kind = String(command.kind || "");
  const params = command.params && typeof command.params === "object" ? command.params : {};
  if (kind === "ping") return { ready: true };
  if (kind === "get_version") {
    return { product: navigator.userAgent.slice(0, 200), protocolVersion: "1.3" };
  }
  if (kind === "get_targets") {
    const targets = await chrome.debugger.getTargets();
    return {
      targetInfos: targets.map((target) => ({
        targetId: String(target.id || ""),
        type: String(target.type || ""),
        title: String(target.title || ""),
        url: String(target.url || ""),
      })),
    };
  }
  if (kind === "switch_target") {
    const targetId = String(params.target_id || "");
    const target = await exactTarget(targetId);
    state.switching = true;
    try {
      await chrome.debugger.detach({ tabId: state.tabId }).catch(() => {});
      await chrome.debugger.attach({ tabId: target.tabId }, "1.3");
      state.tabId = target.tabId;
      state.targetId = target.id;
      state.pageUrl = target.url;
      await chrome.tabs.update(target.tabId, { active: true });
    } finally {
      state.switching = false;
    }
    return { target_id: state.targetId, page_url: state.pageUrl };
  }
  if (kind !== "cdp") throw new Error("browser_extension_command_not_allowed");
  const method = String(command.method || "");
  return (await chrome.debugger.sendCommand(
    { tabId: state.tabId },
    method,
    validateCdpCommand(method, params),
  )) || {};
}

async function pollCommands(state, generation) {
  while (relayState === state && relayGeneration === generation) {
    let response;
    try {
      response = await api("/api/execution/extension-runtimes/poll", {
        runtime_id: state.runtimeId,
        token: state.token,
        wait_milliseconds: 20000,
      });
    } catch (_error) {
      if (relayState !== state || relayGeneration !== generation) return;
      await new Promise((resolve) => setTimeout(resolve, 500));
      continue;
    }
    const command = response.command;
    if (!command) continue;
    let result = {};
    let errorCode = "";
    try {
      result = await executeCommand(state, command);
    } catch (error) {
      errorCode = String(error?.message || "browser_extension_command_failed").slice(0, 200);
    }
    await api("/api/execution/extension-runtimes/complete", {
      runtime_id: state.runtimeId,
      token: state.token,
      command_id: String(command.command_id || ""),
      result,
      error_code: errorCode,
    }).catch(() => {});
  }
}

async function enableActionSidePanel() {
  await chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
}

chrome.runtime.onInstalled.addListener(() => {
  enableActionSidePanel().catch((error) => console.error(error));
});

chrome.runtime.onStartup.addListener(() => {
  enableActionSidePanel().catch((error) => console.error(error));
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type !== "kt6.prepareRuntime") return false;
  prepareRuntime(message.context).then(
    (result) => sendResponse({ ok: true, ...result }),
    (error) => sendResponse({ ok: false, error: String(error?.message || error) }),
  );
  return true;
});

chrome.debugger.onDetach.addListener((source) => {
  if (!relayState || relayState.switching || source.tabId !== relayState.tabId) return;
  const previous = relayState;
  relayState = null;
  relayGeneration += 1;
  api("/api/execution/extension-runtimes/unregister", {
    runtime_id: previous.runtimeId,
    token: previous.token,
  }).catch(() => {});
});

globalThis.__KT6_EXTENSION_RUNTIME_INTERNALS__ = {
  validateCdpCommand,
  executeCommand,
};

enableActionSidePanel().catch((error) => console.error(error));
