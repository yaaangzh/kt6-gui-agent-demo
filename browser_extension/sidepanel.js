const API_BASE = "http://127.0.0.1:8787";
const POLL_INTERVAL_MS = 600;

const elements = {
  pageTitle: document.querySelector("#page-title"),
  pageUrl: document.querySelector("#page-url"),
  runtimeStatus: document.querySelector("#runtime-status"),
  workflow: document.querySelector("#workflow"),
  generate: document.querySelector("#generate"),
  refreshContext: document.querySelector("#refresh-context"),
  message: document.querySelector("#message"),
  planCard: document.querySelector("#plan-card"),
  planSteps: document.querySelector("#plan-steps"),
  confirm: document.querySelector("#confirm"),
  execute: document.querySelector("#execute"),
  runCard: document.querySelector("#run-card"),
  runStatus: document.querySelector("#run-status"),
  runSteps: document.querySelector("#run-steps"),
};

let currentContext = null;
let generatedPlan = null;
let generating = false;
let executing = false;

async function resolveCurrentBrowserContext(
  tabsApi = chrome.tabs,
  debuggerApi = chrome.debugger,
) {
  const tabs = await tabsApi.query({ active: true, currentWindow: true });
  if (tabs.length !== 1 || !Number.isInteger(tabs[0].id)) {
    throw new Error("无法唯一识别当前标签页");
  }
  const tab = tabs[0];
  const targets = await debuggerApi.getTargets();
  const matches = targets.filter(
    (target) =>
      target.type === "page" &&
      target.tabId === tab.id &&
      typeof target.id === "string" &&
      target.id.length > 0,
  );
  if (matches.length !== 1) {
    throw new Error("当前标签页与受控浏览器 Target 无法精确绑定");
  }
  const target = matches[0];
  const url = String(target.url || "").trim();
  if (!url.startsWith("http://") && !url.startsWith("https://")) {
    throw new Error("当前标签页不是可执行的 HTTP/HTTPS 页面");
  }
  const tabUrl = String(tab.url || "").trim();
  if (tabUrl && tabUrl !== url) {
    throw new Error("当前标签页与受控浏览器 Target 无法精确绑定");
  }
  return {
    tabId: tab.id,
    title: String(tab.title || target.title || "未命名页面"),
    startUrl: url,
    browserTargetId: target.id,
  };
}

async function api(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  let payload = {};
  try {
    payload = await response.json();
  } catch (_error) {
    throw new Error(`本地服务返回了无效响应（HTTP ${response.status}）`);
  }
  if (!response.ok) {
    throw new Error(String(payload.error || `HTTP ${response.status}`));
  }
  return payload;
}

async function prepareBrowserRuntime(context, runtimeApi = chrome.runtime) {
  const response = await runtimeApi.sendMessage({
    type: "kt6.prepareRuntime",
    context,
  });
  if (!response?.ok || typeof response.runtimeId !== "string") {
    throw new Error(String(response?.error || "无法连接当前 Chrome 标签页"));
  }
  return response;
}

function setStatus(element, text, kind = "neutral") {
  element.textContent = text;
  element.className = `status ${kind}`;
}

function replaceList(element, items) {
  element.replaceChildren();
  for (const text of items) {
    const item = document.createElement("li");
    item.textContent = text;
    element.append(item);
  }
}

async function refreshContext() {
  try {
    currentContext = await resolveCurrentBrowserContext();
    elements.pageTitle.textContent = currentContext.title;
    elements.pageUrl.textContent = currentContext.startUrl;
    return currentContext;
  } catch (error) {
    currentContext = null;
    elements.pageTitle.textContent = "无法绑定当前页面";
    elements.pageUrl.textContent = "";
    throw error;
  }
}

async function checkRuntime() {
  try {
    const health = await api("/api/execution/health", { method: "GET" });
    if (!health.ready) {
      if (health.configured && health.planner_configured) {
        setStatus(elements.runtimeStatus, "本地后端已就绪，生成计划时连接当前标签页", "neutral");
        return true;
      }
      throw new Error("本地执行链尚未就绪");
    }
    setStatus(elements.runtimeStatus, "本地后端与当前 Chrome 标签页已连接", "success");
    return true;
  } catch (error) {
    setStatus(elements.runtimeStatus, `执行链不可用：${error.message}`, "error");
    return false;
  }
}

async function generatePlan() {
  if (generating || executing) return;
  const userRequest = elements.workflow.value.trim();
  if (!userRequest) {
    setStatus(elements.message, "请先输入要完成的流程", "error");
    return;
  }
  generating = true;
  elements.generate.disabled = true;
  elements.planCard.hidden = true;
  elements.runCard.hidden = true;
  generatedPlan = null;
  setStatus(elements.message, "正在感知当前页面并生成计划…");
  try {
    const context = await refreshContext();
    const runtime = await prepareBrowserRuntime(context);
    await checkRuntime();
    const generated = await api("/api/execution/plans", {
      method: "POST",
      body: JSON.stringify({
        start_url: context.startUrl,
        user_request: userRequest,
        browser_target_id: context.browserTargetId,
        browser_runtime_id: runtime.runtimeId,
      }),
    });
    generatedPlan = {
      plan: generated.plan,
      browserTargetId: generated.browser_target_id,
      browserRuntimeId: generated.browser_runtime_id,
      startUrl: context.startUrl,
      tabId: context.tabId,
    };
    replaceList(elements.planSteps, generated.readable_steps || []);
    elements.confirm.checked = false;
    elements.execute.disabled = true;
    elements.planCard.hidden = false;
    setStatus(elements.message, "计划已生成，请检查后确认", "success");
  } catch (error) {
    setStatus(elements.message, `计划生成失败：${error.message}`, "error");
  } finally {
    generating = false;
    elements.generate.disabled = false;
  }
}

async function executePlan() {
  if (executing || !generatedPlan || !elements.confirm.checked) return;
  executing = true;
  elements.execute.disabled = true;
  elements.generate.disabled = true;
  try {
    const context = await refreshContext();
    const runtime = await prepareBrowserRuntime(context);
    if (
      context.tabId !== generatedPlan.tabId ||
      context.startUrl !== generatedPlan.startUrl ||
      context.browserTargetId !== generatedPlan.browserTargetId ||
      runtime.runtimeId !== generatedPlan.browserRuntimeId
    ) {
      throw new Error("当前标签页已变化，请重新生成计划");
    }
    const run = await api("/api/execution/runs", {
      method: "POST",
      body: JSON.stringify({
        plan: generatedPlan.plan,
        confirmed: true,
        browser_target_id: generatedPlan.browserTargetId,
        browser_runtime_id: generatedPlan.browserRuntimeId,
      }),
    });
    elements.runCard.hidden = false;
    await pollRun(run.run_id);
  } catch (error) {
    elements.runCard.hidden = false;
    setStatus(elements.runStatus, `执行失败：${error.message}`, "error");
  } finally {
    executing = false;
    elements.generate.disabled = false;
  }
}

async function pollRun(runId) {
  for (;;) {
    const run = await api(`/api/execution/runs/${encodeURIComponent(runId)}`, {
      method: "GET",
    });
    const steps = (run.steps || []).map(
      (step) => `${step.id} · ${step.op} · ${step.status}`,
    );
    replaceList(elements.runSteps, steps);
    if (run.status === "success") {
      setStatus(elements.runStatus, "流程执行并验证成功", "success");
      return;
    }
    if (run.status === "failed") {
      throw new Error(String(run.error_code || "execution_failed"));
    }
    const current = run.current_step ? `，当前 ${run.current_step}` : "";
    setStatus(elements.runStatus, `执行中${current}`);
    await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
  }
}

elements.generate.addEventListener("click", generatePlan);
elements.refreshContext.addEventListener("click", async () => {
  try {
    await refreshContext();
    setStatus(elements.message, "已刷新当前标签页", "success");
  } catch (error) {
    setStatus(elements.message, error.message, "error");
  }
});
elements.confirm.addEventListener("change", () => {
  elements.execute.disabled = !elements.confirm.checked || executing;
});
elements.execute.addEventListener("click", executePlan);

globalThis.__KT6_AGENT_PANEL_INTERNALS__ = {
  prepareBrowserRuntime,
  resolveCurrentBrowserContext,
};

Promise.all([refreshContext(), checkRuntime()]).catch((error) => {
  setStatus(elements.message, error.message, "error");
});
