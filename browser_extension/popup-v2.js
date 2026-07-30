"use strict";

const CAPTURE_ENDPOINT = "http://127.0.0.1:8787/api/perception/captures";
const MAX_DOM_ELEMENTS = 500;
const MAX_CANVASES = 4;
const PREVIEW_LIMIT = 20;

function compactText(value, maximum = 120) {
  return String(value || "")
    .trim()
    .replace(/\s+/g, " ")
    .slice(0, maximum);
}

function previewPriority(item) {
  let priority = 0;
  if (item.business_id) priority += 100;
  if (item.actionable) priority += 60;
  if (item.aria_label) priority += 25;
  if (item.role === "heading") priority += 20;
  if (item.fallback_text) priority -= 20;
  return priority;
}

function buildPreview(elements) {
  const seen = new Set();
  return elements
    .filter((item) => {
      const label = compactText(
        item.aria_label || item.label || item.placeholder,
      );
      if (!label) return false;
      const key = `${label}\u0000${item.business_id || ""}\u0000${item.role || ""}`;
      if (seen.has(key)) return false;
      seen.add(key);
      item.__preview_label = label;
      item.__preview_priority = previewPriority(item);
      return true;
    })
    .sort(
      (left, right) =>
        right.__preview_priority - left.__preview_priority ||
        left.document_order - right.document_order,
    )
    .slice(0, PREVIEW_LIMIT);
}

async function captureActivePage() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || typeof tab.id !== "number") {
    throw new Error("未找到活动页面");
  }
  if (!/^https?:/i.test(tab.url || "")) {
    throw new Error("只能采集 HTTP(S) 页面");
  }

  const frameResults = await chrome.scripting.executeScript({
    target: { tabId: tab.id, allFrames: true },
    files: ["content-collector.js"],
  });
  const capturedFrames = frameResults
    .filter((item) => item && item.result)
    .sort((left, right) => left.frameId - right.frameId);
  const topFrame = capturedFrames.find((item) => item.frameId === 0);
  if (!topFrame) throw new Error("无法读取页面主文档");

  const elements = [];
  const canvases = [];
  const stats = {
    frame_count: capturedFrames.length,
    scanned_element_count: 0,
    candidate_count: 0,
    captured_element_count: 0,
    actionable_element_count: 0,
    fallback_text_count: 0,
    truncated: false,
  };

  for (const frame of capturedFrames) {
    const frameId = String(frame.frameId);
    const frameUrl = frame.result.page.url;
    const documentId = String(frame.documentId || "");
    const frameStats = frame.result.stats || {};
    stats.scanned_element_count += Number(
      frameStats.scanned_element_count || 0,
    );
    stats.candidate_count += Number(frameStats.candidate_count || 0);
    stats.captured_element_count += Number(
      frameStats.captured_element_count || 0,
    );
    stats.actionable_element_count += Number(
      frameStats.actionable_element_count || 0,
    );
    stats.fallback_text_count += Number(frameStats.fallback_text_count || 0);
    stats.truncated = stats.truncated || Boolean(frameStats.truncated);

    for (const item of frame.result.elements) {
      if (elements.length >= MAX_DOM_ELEMENTS) {
        stats.truncated = true;
        break;
      }
      const localRef = String(item.ref || "");
      const localParent = String(item.parent_ref || "");
      elements.push({
        ...item,
        ref: `frame:${frameId}:${localRef}`,
        parent_ref: localParent ? `frame:${frameId}:${localParent}` : "",
        frame_id: frameId,
        frame_url: frameUrl,
        document_id: documentId,
      });
    }
    for (const canvas of frame.result.canvases) {
      if (canvases.length >= MAX_CANVASES) break;
      canvases.push({
        ...canvas,
        canvas_id: `frame-${frameId}-${canvas.canvas_id}`,
        frame_id: frameId,
        frame_url: frameUrl,
        document_id: documentId,
      });
    }
  }

  const payload = {
    page: {
      ...topFrame.result.page,
      ui_version: "kt6-browser-extension-v0.2",
    },
    dom: { elements },
    canvases,
    adapter_scene: null,
    captured_at: Date.now() / 1000,
  };
  const response = await fetch(CAPTURE_ENDPOINT, {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(result.error || `KT6 返回 HTTP ${response.status}`);
  }
  return {
    result,
    stats,
    submittedElementCount: elements.length,
    canvasCount: canvases.length,
    preview: buildPreview(elements),
  };
}

function setMetric(name, value) {
  const target = document.querySelector(`[data-metric="${name}"]`);
  if (target) target.textContent = String(value);
}

function renderPreview(items) {
  const container = document.querySelector("#element-preview");
  if (!items.length) {
    container.textContent =
      "未发现可访问 DOM；若页面主要由 Canvas 构成，请查看 Canvas 数量或视觉识别结果。";
    container.classList.add("empty");
    return;
  }
  container.classList.remove("empty");
  container.replaceChildren(
    ...items.map((item) => {
      const row = document.createElement("div");
      row.className = "preview-item";
      const label = document.createElement("strong");
      label.textContent = item.__preview_label;
      const detail = document.createElement("span");
      detail.textContent = item.business_id
        ? `业务对象 · ${item.business_id}`
        : item.actionable
          ? `可操作 · ${item.role || item.tag}`
          : item.role || item.tag || "文本";
      const selector = document.createElement("code");
      selector.textContent = item.selector || "无稳定选择器";
      selector.title = selector.textContent;
      row.append(label, detail, selector);
      return row;
    }),
  );
}

const captureButton = document.querySelector("#capture");
const statusOutput = document.querySelector("#status");

captureButton.addEventListener("click", async () => {
  captureButton.disabled = true;
  statusOutput.textContent = "正在采集语义 DOM/ARIA 和 Canvas…";
  statusOutput.dataset.state = "running";
  document.querySelector("#result").hidden = true;
  try {
    const capture = await captureActivePage();
    const summary = capture.result.summary;
    statusOutput.textContent = `采集成功 · ${summary.selected_mode}`;
    statusOutput.dataset.state = "success";
    setMetric("capture-id", capture.result.capture_id);
    setMetric("frames", capture.stats.frame_count);
    setMetric("scanned", capture.stats.scanned_element_count);
    setMetric("candidates", capture.stats.candidate_count);
    setMetric("submitted", capture.submittedElementCount);
    setMetric("actionable", summary.dom_actionable_element_count);
    setMetric("canvas", capture.canvasCount);
    setMetric("truncated", capture.stats.truncated ? "是" : "否");
    renderPreview(capture.preview);
    document.querySelector("#result").hidden = false;
  } catch (error) {
    statusOutput.textContent =
      error instanceof Error ? `采集失败：${error.message}` : "采集失败";
    statusOutput.dataset.state = "error";
  } finally {
    captureButton.disabled = false;
  }
});
