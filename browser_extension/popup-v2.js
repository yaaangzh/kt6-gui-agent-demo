"use strict";

const CAPTURE_JOB_ENDPOINT =
  "http://127.0.0.1:8787/api/perception/capture-jobs";
const HEALTH_ENDPOINT = "http://127.0.0.1:8787/api/health";
const PENDING_CAPTURE_STORAGE_KEY = "kt6_pending_capture_job_v1";
const MAX_DOM_ELEMENTS = 500;
const MAX_CANVASES = 4;
const PREVIEW_LIMIT = 20;
const MAX_VISIBLE_SCREENSHOTS = 1;
const MAX_VISUAL_DATA_URL_LENGTH = 6_500_000;
const MAX_VISUAL_PIXELS = 4_000_000;
const DEFAULT_BACKEND_WAIT_MS = 330_000;
const HEALTH_TIMEOUT_MS = 2_500;
const CAPTURE_JOB_REQUEST_TIMEOUT_MS = 15_000;
const CAPTURE_JOB_POLL_INTERVAL_MS = 1_000;
let captureInFlight = false;

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

function clippedRegion(rawBbox, viewport) {
  if (!Array.isArray(rawBbox) || rawBbox.length !== 4) return null;
  const [rawLeft, rawTop, rawWidth, rawHeight] = rawBbox.map(Number);
  if (
    ![rawLeft, rawTop, rawWidth, rawHeight].every(Number.isFinite) ||
    rawWidth <= 0 ||
    rawHeight <= 0
  ) {
    return null;
  }
  const left = Math.max(0, rawLeft);
  const top = Math.max(0, rawTop);
  const right = Math.min(Number(viewport.width || 0), rawLeft + rawWidth);
  const bottom = Math.min(Number(viewport.height || 0), rawTop + rawHeight);
  if (right <= left || bottom <= top) return null;
  return [left, top, right - left, bottom - top].map((value) =>
    Number(value.toFixed(2)),
  );
}

function sourcePixelRegionFor(bbox, viewport, bitmapWidth, bitmapHeight) {
  const viewportWidth = Number(viewport.width || 0);
  const viewportHeight = Number(viewport.height || 0);
  if (viewportWidth <= 0 || viewportHeight <= 0) return null;
  const scaleX = Number(bitmapWidth || 0) / viewportWidth;
  const scaleY = Number(bitmapHeight || 0) / viewportHeight;
  if (scaleX <= 0 || scaleY <= 0) return null;
  const left = Math.max(0, Math.floor(bbox[0] * scaleX));
  const top = Math.max(0, Math.floor(bbox[1] * scaleY));
  const right = Math.min(
    Number(bitmapWidth),
    Math.ceil((bbox[0] + bbox[2]) * scaleX),
  );
  const bottom = Math.min(
    Number(bitmapHeight),
    Math.ceil((bbox[1] + bbox[3]) * scaleY),
  );
  if (right <= left || bottom <= top) return null;
  return [left, top, right - left, bottom - top];
}

function dataUrlWithinLimit(dataUrl) {
  return (
    typeof dataUrl === "string" &&
    dataUrl.startsWith("data:image/") &&
    dataUrl.length <= MAX_VISUAL_DATA_URL_LENGTH
  );
}

function scaledCanvas(source, scale) {
  const target = document.createElement("canvas");
  target.width = Math.max(1, Math.round(source.width * scale));
  target.height = Math.max(1, Math.round(source.height * scale));
  const context = target.getContext("2d");
  if (!context) throw new Error("无法创建截图压缩画布");
  context.drawImage(source, 0, 0, target.width, target.height);
  return target;
}

function encodeVisualCanvas(source) {
  let working = source;
  for (let attempt = 0; attempt < 5; attempt += 1) {
    const mimeType = attempt === 0 ? "image/png" : "image/webp";
    const quality = Math.max(0.62, 0.86 - attempt * 0.06);
    const dataUrl =
      mimeType === "image/png"
        ? working.toDataURL(mimeType)
        : working.toDataURL(mimeType, quality);
    if (dataUrlWithinLimit(dataUrl)) {
      return {
        data_url: dataUrl,
        width: working.width,
        height: working.height,
      };
    }
    working = scaledCanvas(working, 0.72);
  }
  throw new Error("可见区域截图超过扩展大小限制");
}

function cropVisibleRegion(bitmap, rawBbox, viewport) {
  const bbox = clippedRegion(rawBbox, viewport);
  if (!bbox) throw new Error("视觉区域不在当前可见范围内");
  const sourceRegion = sourcePixelRegionFor(
    bbox,
    viewport,
    bitmap.width,
    bitmap.height,
  );
  if (!sourceRegion) throw new Error("截图裁剪区域为空");
  const [sourceLeft, sourceTop, sourceWidth, sourceHeight] = sourceRegion;

  const pixelScale = Math.min(
    1,
    Math.sqrt(MAX_VISUAL_PIXELS / (sourceWidth * sourceHeight)),
  );
  const output = document.createElement("canvas");
  output.width = Math.max(1, Math.round(sourceWidth * pixelScale));
  output.height = Math.max(1, Math.round(sourceHeight * pixelScale));
  const context = output.getContext("2d");
  if (!context) throw new Error("无法创建截图裁剪画布");
  context.drawImage(
    bitmap,
    sourceLeft,
    sourceTop,
    sourceWidth,
    sourceHeight,
    0,
    0,
    output.width,
    output.height,
  );
  return {
    ...encodeVisualCanvas(output),
    bbox,
    source_pixel_region: sourceRegion,
  };
}

function captureErrorItem(region, message) {
  const sourceFrameId = String(region.frame_id || "0");
  return {
    canvas_id: `visible-${sourceFrameId}-${region.region_id || "region"}`,
    width: 0,
    height: 0,
    client_width: Number(region.client_width || region.bbox?.[2] || 0),
    client_height: Number(region.client_height || region.bbox?.[3] || 0),
    bbox: region.bbox || [0, 0, 0, 0],
    source_region: region.source_region || region.bbox || [0, 0, 0, 0],
    source_kind: region.source_kind || "mixed_region",
    source_ref: region.source_ref || "",
    source_frame_id: sourceFrameId,
    source_frame_url: region.frame_url || "",
    frame_id: "0",
    frame_url: region.top_frame_url || region.frame_url || "",
    document_id: region.top_document_id || "",
    capture_kind: "visible_tab",
    capture_method: "capture_visible_tab_crop",
    roi_status: "capture_failed",
    coordinate_space: "top_frame_viewport_css_pixels",
    capture_error: message,
  };
}

async function captureVisibleRegions(tab, topFrame, regions) {
  if (!regions.length) {
    return { items: [], error: "" };
  }
  let screenshotDataUrl;
  try {
    screenshotDataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, {
      format: "png",
    });
    const [currentTab] = await chrome.tabs.query({
      active: true,
      currentWindow: true,
    });
    if (
      !currentTab ||
      currentTab.id !== tab.id ||
      String(currentTab.url || "") !== String(tab.url || "")
    ) {
      throw new Error("截图期间活动页面已切换，已丢弃视觉截图");
    }
    const [documentProbe] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => ({ href: location.href }),
    });
    if (
      !documentProbe ||
      !documentProbe.result ||
      String(documentProbe.result.href || "") !==
        String(topFrame.result.page.url || "")
    ) {
      throw new Error("截图期间页面文档已变化，已丢弃视觉截图");
    }
    if (
      topFrame.documentId &&
      documentProbe.documentId &&
      String(documentProbe.documentId) !== String(topFrame.documentId)
    ) {
      throw new Error("截图期间页面已重新加载，已丢弃视觉截图");
    }
  } catch (error) {
    const message =
      error instanceof Error ? error.message : "活动页面截图失败";
    return {
      items: [captureErrorItem(regions[0], message)],
      error: message,
    };
  }

  let bitmap;
  try {
    const response = await fetch(screenshotDataUrl);
    bitmap = await createImageBitmap(await response.blob());
    const viewport = topFrame.result.page.viewport;
    const topRegions = regions
      .filter((region) => String(region.frame_id) === "0")
      .slice(0, MAX_VISIBLE_SCREENSHOTS);
    const selectedRegions = topRegions.length
      ? topRegions
      : [
          {
            region_id: "viewport_fallback",
            source_kind: "mixed_viewport",
            source_ref: "",
            source_region: [0, 0, viewport.width, viewport.height],
            bbox: [0, 0, viewport.width, viewport.height],
            client_width: viewport.width,
            client_height: viewport.height,
            frame_id: "0",
            frame_url: topFrame.result.page.url,
            document_id: String(topFrame.documentId || ""),
            source_frame_id: String(regions[0].frame_id || ""),
            source_frame_url: regions[0].frame_url || "",
            roi_status: "unverified",
          },
        ];

    const items = [];
    for (const region of selectedRegions) {
      try {
        const crop = cropVisibleRegion(bitmap, region.bbox, viewport);
        const sourceFrameId = String(
          region.source_frame_id || region.frame_id || "0",
        );
        items.push({
          canvas_id: `visible-${sourceFrameId}-${region.region_id}`,
          width: crop.width,
          height: crop.height,
          client_width: crop.bbox[2],
          client_height: crop.bbox[3],
          bbox: crop.bbox,
          data_url: crop.data_url,
          source_kind: region.source_kind || "mixed_region",
          source_ref: region.source_ref || "",
          source_region: region.source_region || region.bbox,
          source_pixel_region: crop.source_pixel_region,
          source_frame_id: sourceFrameId,
          source_frame_url:
            region.source_frame_url || region.frame_url || "",
          frame_id: "0",
          frame_url: topFrame.result.page.url,
          document_id: String(topFrame.documentId || ""),
          device_pixel_ratio: Number(
            topFrame.result.page.viewport.device_pixel_ratio || 1,
          ),
          capture_kind: "visible_tab",
          capture_method: "capture_visible_tab_crop",
          roi_status:
            region.roi_status ||
            (String(region.frame_id) === "0" ? "verified" : "unverified"),
          coordinate_space: "top_frame_viewport_css_pixels",
          primitive_count: Number(region.primitive_count || 0),
          visible_ratio: Number(region.visible_ratio || 0),
        });
      } catch (error) {
        items.push(
          captureErrorItem(
            {
              ...region,
              top_frame_url: topFrame.result.page.url,
              top_document_id: String(topFrame.documentId || ""),
            },
            error instanceof Error ? error.message : "视觉区域裁剪失败",
          ),
        );
      }
    }
    const successfulCount = items.filter((item) => item.data_url).length;
    const firstFailure = items.find((item) => item.capture_error);
    return {
      items,
      error:
        successfulCount > 0
          ? ""
          : firstFailure?.capture_error || "所有视觉区域截图均失败",
    };
  } catch (error) {
    const message =
      error instanceof Error ? error.message : "活动页面截图解码失败";
    return {
      items: [captureErrorItem(regions[0], message)],
      error: message,
    };
  } finally {
    if (bitmap && typeof bitmap.close === "function") bitmap.close();
  }
}

async function readBackendHealth() {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), HEALTH_TIMEOUT_MS);
  try {
    const response = await fetch(HEALTH_ENDPOINT, {
      method: "GET",
      headers: { Accept: "application/json" },
      signal: controller.signal,
    });
    if (!response.ok) return null;
    return await response.json();
  } catch (_error) {
    return null;
  } finally {
    clearTimeout(timeout);
  }
}

async function readExplicitPageAdapter() {
  const MAX_BYTES = 1_000_000;
  const MAX_DEPTH = 8;
  const SENSITIVE_KEY =
    /(?:authorization|cookie|credential|password|secret|session|token|api[_-]?key|csrf|xsrf)/i;
  const EXECUTION_CLAIMS = new Set([
    "actionable",
    "clickable",
    "interaction",
    "interaction_eligible",
    "origin",
    "safe_for_execution",
  ]);
  const ALLOWED_SCENE_KEYS = new Set([
    "ui_version",
    "topology_revision",
    "site",
    "floor",
    "scene",
    "canvas",
    "objects",
    "links",
    "co_channel_relations",
    "visual_grounding",
    "view_transform",
  ]);

  function sanitize(value, depth = 0) {
    if (depth > MAX_DEPTH || value === null) {
      return value === null ? null : undefined;
    }
    if (typeof value === "string") return value.slice(0, 10_000);
    if (typeof value === "number") return Number.isFinite(value) ? value : null;
    if (typeof value === "boolean") return value;
    if (Array.isArray(value)) {
      return value.slice(0, 4_000).map((item) => sanitize(item, depth + 1));
    }
    if (typeof value !== "object") return undefined;
    const clean = {};
    for (const [rawKey, item] of Object.entries(value).slice(0, 200)) {
      const key = String(rawKey).slice(0, 200);
      if (SENSITIVE_KEY.test(key) || EXECUTION_CLAIMS.has(key)) continue;
      const normalized = sanitize(item, depth + 1);
      if (normalized !== undefined) clean[key] = normalized;
    }
    return clean;
  }
  let safePageUrl = "";
  try {
    const parsed = new URL(location.href);
    parsed.search = "";
    parsed.hash = "";
    safePageUrl = parsed.toString();
  } catch (_error) {
    safePageUrl = String(location.origin || "").slice(0, 2048);
  }


  const adapter = globalThis.__KT6_PAGE_ADAPTER__;
  if (!adapter || typeof adapter !== "object") {
    return { status: "unavailable", page_url: safePageUrl };
  }
  const methodName = ["snapshot", "getSnapshot", "exportScene", "captureScene"].find(
    (name) => typeof adapter[name] === "function",
  );
  let rawSnapshot;
  try {
    if (methodName) {
      rawSnapshot = await Promise.race([
        Promise.resolve(
          adapter[methodName].call(adapter, {
            mode: "read_only",
            schema_version: "1",
          }),
        ),
        new Promise((_, reject) =>
          setTimeout(() => reject(new Error("page adapter timeout")), 5_000),
        ),
      ]);
    } else {
      rawSnapshot = adapter.scene;
    }
  } catch (error) {
    return {
      status: "error",
      page_url: safePageUrl,
      error: error instanceof Error ? error.message.slice(0, 300) : "adapter failed",
    };
  }
  const rawScene =
    rawSnapshot && typeof rawSnapshot === "object" && rawSnapshot.scene
      ? rawSnapshot.scene
      : rawSnapshot;
  if (!rawScene || typeof rawScene !== "object" || !Array.isArray(rawScene.objects)) {
    return {
      status: "invalid",
      page_url: safePageUrl,
      error: "page adapter did not return a scene with objects",
    };
  }
  const scene = {};
  for (const [key, value] of Object.entries(rawScene)) {
    if (!ALLOWED_SCENE_KEYS.has(key)) continue;
    const normalized = sanitize(value);
    if (normalized !== undefined) scene[key] = normalized;
  }
  const objectIds = Array.isArray(scene.objects)
    ? scene.objects.map((item) => String(item?.business_id || "").trim())
    : [];
  const objectIdSet = new Set(objectIds);
  const validObjects =
    objectIds.length > 0 &&
    objectIdSet.size === objectIds.length &&
    scene.objects.every((item, index) => {
      if (!item || typeof item !== "object" || Array.isArray(item)) return false;
      if (!objectIds[index]) return false;
      return ["x", "y", "width", "height"].every((field) => {
        if (item[field] === undefined) return true;
        const numeric = Number(item[field]);
        if (!Number.isFinite(numeric)) return false;
        return !["width", "height"].includes(field) || numeric > 0;
      });
    });
  const validCanvas =
    scene.canvas === undefined ||
    (scene.canvas && typeof scene.canvas === "object" && !Array.isArray(scene.canvas));
  const validRelations = ["links", "co_channel_relations"].every((field) => {
    if (scene[field] === undefined) return true;
    if (!Array.isArray(scene[field])) return false;
    return scene[field].every((item) => {
      if (!item || typeof item !== "object" || Array.isArray(item)) return false;
      const source = String(item.source || "").trim();
      const target = String(item.target || "").trim();
      return objectIdSet.has(source) && objectIdSet.has(target);
    });
  });
  if (!validObjects || !validCanvas || !validRelations) {
    return {
      status: "invalid",
      page_url: safePageUrl,
      error: "page adapter snapshot does not match the read-only scene contract",
    };
  }
  const adapterId = String(
    adapter.adapter_id || adapter.id || rawSnapshot?.adapter_id || "page-adapter",
  ).slice(0, 200);
  const adapterVersion = String(
    adapter.adapter_version || adapter.version || rawSnapshot?.adapter_version || "unknown",
  ).slice(0, 100);
  scene.source_metadata = {
    source_type: "explicit_page_adapter",
    adapter_id: adapterId,
    adapter_version: adapterVersion,
    schema_version: "1",
    captured_at: Date.now() / 1000,
    page_url: safePageUrl,
    snapshot_complete:
      adapter.snapshot_complete === true || rawSnapshot?.snapshot_complete === true,
    safe_for_execution: false,
  };
  const serialized = JSON.stringify(scene);
  if (serialized.length > MAX_BYTES) {
    return {
      status: "too_large",
      page_url: safePageUrl,
      error: "page adapter snapshot exceeds 1 MB",
    };
  }
  return {
    status: "available",
    page_url: safePageUrl,
    adapter_scene: scene,
  };
}

async function collectPageAdapterResults(tabId) {
  try {
    return await chrome.scripting.executeScript({
      target: { tabId, allFrames: true },
      world: "MAIN",
      func: readExplicitPageAdapter,
    });
  } catch (_allFramesError) {
    try {
      return await chrome.scripting.executeScript({
        target: { tabId },
        world: "MAIN",
        func: readExplicitPageAdapter,
      });
    } catch (error) {
      return [{
        frameId: 0,
        result: {
          status: "error",
          error: error instanceof Error ? error.message : "page adapter probe failed",
        },
      }];
    }
  }
}

function backendWaitMilliseconds(health) {
  const configured = Number(health?.vision?.timeout_seconds);
  if (!Number.isFinite(configured) || configured <= 0) {
    return DEFAULT_BACKEND_WAIT_MS;
  }
  return Math.min(360_000, Math.max(30_000, (configured + 30) * 1000));
}

function visionConfigurationState(health) {
  return typeof health?.vision?.configured === "boolean"
    ? health.vision.configured
    : null;
}

function createClientRequestId() {
  let randomPart = "";
  try {
    randomPart = globalThis.crypto.randomUUID().replace(/-/g, "");
  } catch (_error) {
    randomPart = `${Math.random().toString(16).slice(2)}${Math.random()
      .toString(16)
      .slice(2)}`;
  }
  return `ext_${Date.now().toString(36)}_${randomPart.slice(0, 32)}`;
}

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function fetchJsonWithTimeout(url, options = {}, timeoutMs) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, {
      ...options,
      signal: controller.signal,
    });
    return {
      response,
      result: await response.json().catch(() => ({})),
    };
  } catch (error) {
    if (error?.name === "AbortError") {
      const timeoutError = new Error("连接本地 KT6 服务超时");
      timeoutError.transient = true;
      throw timeoutError;
    }
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

function responseError(result, status) {
  const rawError = result?.error;
  const message =
    typeof rawError === "string"
      ? rawError
      : rawError?.message || `KT6 返回 HTTP ${status}`;
  const error = new Error(compactText(message, 500));
  error.terminal = status >= 400 && status < 500;
  error.httpStatus = Number(status);
  error.status = Number(status);
  return error;
}

async function createCaptureJob(payload, clientRequestId) {
  let lastError = null;
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      const { response, result } = await fetchJsonWithTimeout(
        CAPTURE_JOB_ENDPOINT,
        {
          method: "POST",
          headers: {
            Accept: "application/json",
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            client_request_id: clientRequestId,
            payload,
          }),
        },
        CAPTURE_JOB_REQUEST_TIMEOUT_MS,
      );
      if (!response.ok) throw responseError(result, response.status);
      const jobId = String(result.job_id || "").trim();
      if (!jobId) throw new Error("KT6 未返回异步任务 ID");
      return {
        job_id: jobId,
        status: String(result.status || "running"),
      };
    } catch (error) {
      lastError = error;
      if (error?.terminal || attempt === 1) throw error;
    }
  }
  throw lastError || new Error("无法提交 KT6 异步识别任务");
}

async function readPendingCapture() {
  const stored = await chrome.storage.local.get(PENDING_CAPTURE_STORAGE_KEY);
  const pending = stored?.[PENDING_CAPTURE_STORAGE_KEY];
  if (!pending || typeof pending !== "object") return null;
  if (!String(pending.job_id || "").trim()) {
    await chrome.storage.local.remove(PENDING_CAPTURE_STORAGE_KEY);
    return null;
  }
  return pending;
}

async function savePendingCapture(pending) {
  await chrome.storage.local.set({
    [PENDING_CAPTURE_STORAGE_KEY]: pending,
  });
}

async function clearPendingCapture(jobId) {
  try {
    const pending = await readPendingCapture();
    if (!pending || String(pending.job_id) === String(jobId)) {
      await chrome.storage.local.remove(PENDING_CAPTURE_STORAGE_KEY);
    }
  } catch (_error) {
    // A completed backend job must still be shown even if local cleanup fails.
  }
}

async function fetchCaptureJob(jobId) {
  const { response, result } = await fetchJsonWithTimeout(
    `${CAPTURE_JOB_ENDPOINT}/${encodeURIComponent(jobId)}`,
    {
      method: "GET",
      headers: { Accept: "application/json" },
    },
    CAPTURE_JOB_REQUEST_TIMEOUT_MS,
  );
  if (!response.ok) throw responseError(result, response.status);
  return result;
}

function captureJobErrorMessage(job) {
  const rawError = job?.error;
  if (typeof rawError === "string") return compactText(rawError, 500);
  if (rawError && typeof rawError === "object") {
    return compactText(rawError.message || rawError.type, 500);
  }
  return "后端异步识别任务失败";
}

async function waitForCaptureJob(
  pending,
  health,
  onStage,
  {
    pollIntervalMs = CAPTURE_JOB_POLL_INTERVAL_MS,
    waitMs = backendWaitMilliseconds(health),
  } = {},
) {
  const sessionStartedAt = Date.now();
  const submittedAt = Number(pending.submitted_at_ms || sessionStartedAt);
  let lastTransientError = null;
  while (true) {
    const elapsedSeconds = Math.max(
      0,
      Math.round((Date.now() - submittedAt) / 1000),
    );
    onStage("backend", {
      elapsedSeconds,
      visionConfigured: visionConfigurationState(health),
      jobId: pending.job_id,
      resumed: Boolean(pending.resumed),
      connectionIssue: lastTransientError?.message || "",
    });

    try {
      const job = await fetchCaptureJob(pending.job_id);
      lastTransientError = null;
      const status = String(job.status || "").toLowerCase();
      if (status === "completed") {
        if (!job.capture || typeof job.capture !== "object") {
          const error = new Error("异步任务完成但未返回 Capture 结果");
          error.terminal = true;
          throw error;
        }
        await clearPendingCapture(pending.job_id);
        return job.capture;
      }
      if (status === "error" || status === "failed") {
        const error = new Error(captureJobErrorMessage(job));
        error.terminal = true;
        throw error;
      }
      if (!["accepted", "queued", "pending", "running"].includes(status)) {
        const error = new Error(`KT6 返回未知任务状态：${status || "空"}`);
        error.terminal = true;
        throw error;
      }
    } catch (error) {
      if (error?.httpStatus === 404) {
        await clearPendingCapture(pending.job_id);
        const expiredError = new Error(
          "后端已重启或任务记录已过期，请重新采集",
        );
        expiredError.terminal = true;
        expiredError.httpStatus = 404;
        expiredError.status = 404;
        throw expiredError;
      }
      if (error?.terminal) {
        await clearPendingCapture(pending.job_id);
        throw error;
      }
      lastTransientError = error;
    }

    if (Date.now() - sessionStartedAt >= waitMs) {
      const error = new Error(
        `等待任务 ${pending.job_id} 超过 ${Math.round(
          waitMs / 1000,
        )} 秒；任务仍保留在后台，关闭并重新打开扩展可继续查询`,
      );
      error.keepPending = true;
      throw error;
    }
    await delay(Math.max(0, pollIntervalMs));
  }
}

async function submitCapture(payload, health, onStage, context) {
  const clientRequestId = createClientRequestId();
  const job = await createCaptureJob(payload, clientRequestId);
  const pending = {
    schema_version: 1,
    job_id: job.job_id,
    client_request_id: clientRequestId,
    submitted_at_ms: Date.now(),
    context,
  };
  await savePendingCapture(pending);
  return await waitForCaptureJob(pending, health, onStage);
}

async function collectFrameResults(tabId) {
  try {
    return await chrome.scripting.executeScript({
      target: { tabId, allFrames: true },
      files: ["content-collector.js"],
    });
  } catch (allFramesError) {
    const topFrameOnly = await chrome.scripting.executeScript({
      target: { tabId },
      files: ["content-collector.js"],
    });
    topFrameOnly.collectionError =
      allFramesError instanceof Error
        ? allFramesError.message
        : "部分 iframe 无法采集";
    return topFrameOnly;
  }
}

async function captureActivePage({ onStage = () => {} } = {}) {
  onStage("locating");
  const healthPromise = readBackendHealth();
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || typeof tab.id !== "number") {
    throw new Error("未找到活动页面");
  }
  if (!/^https?:/i.test(tab.url || "")) {
    throw new Error("只能采集 HTTP(S) 页面");
  }

  onStage("dom");
  const [frameResults, pageAdapterResults] = await Promise.all([
    collectFrameResults(tab.id),
    collectPageAdapterResults(tab.id),
  ]);
  const capturedFrames = frameResults
    .filter((item) => item && item.result)
    .sort((left, right) => left.frameId - right.frameId);
  const topFrame = capturedFrames.find((item) => item.frameId === 0);
  if (!topFrame) throw new Error("无法读取页面主文档");

  const elements = [];
  const nativeCanvases = [];
  const visualRegions = [];
  const svgElementTexts = [];
  const svgTextKeys = new Set();
  const stats = {
    frame_count: capturedFrames.length,
    total_element_count: 0,
    scanned_element_count: 0,
    scan_truncated: false,
    candidate_count: 0,
    captured_element_count: 0,
    projected_parent_count: 0,
    omitted_ancestor_count: 0,
    open_shadow_root_count: 0,
    actionable_element_count: 0,
    fallback_text_count: 0,
    native_canvas_count: 0,
    visual_region_count: 0,
    svg_region_count: 0,
    visible_screenshot_count: 0,
    visible_capture_error: "",
    frame_collection_error: frameResults.collectionError || "",
    page_screenshot_fallback: false,
    truncated: false,
    page_adapter_status: "unavailable",
    page_adapter_frame_id: "",
  };
  const pageAdapterCandidates = pageAdapterResults
    .filter((item) => item?.result)
    .sort((left, right) => Number(left.frameId || 0) - Number(right.frameId || 0));
  const pageAdapterEntry =
    pageAdapterCandidates.find((item) => item.result.status === "available") ||
    pageAdapterCandidates[0] ||
    null;
  const pageAdapterScene = pageAdapterEntry?.result?.adapter_scene || null;
  stats.page_adapter_status = pageAdapterEntry?.result?.status || "unavailable";
  if (pageAdapterScene?.source_metadata) {
    stats.page_adapter_frame_id = String(pageAdapterEntry.frameId ?? "0");
    pageAdapterScene.source_metadata.frame_id = stats.page_adapter_frame_id;
    pageAdapterScene.source_metadata.frame_url = String(
      pageAdapterEntry.result.page_url || "",
    ).slice(0, 2048);
    pageAdapterScene.source_metadata.document_id = String(
      pageAdapterEntry.documentId || "",
    ).slice(0, 200);
  }


  for (const frame of capturedFrames) {
    const frameId = String(frame.frameId);
    const frameUrl = frame.result.page.url;
    const documentId = String(frame.documentId || "");
    const frameStats = frame.result.stats || {};
    const frameViewport = frame.result.page.viewport || {};
    stats.total_element_count += Number(frameStats.total_element_count || 0);
    stats.scanned_element_count += Number(
      frameStats.scanned_element_count || 0,
    );
    stats.scan_truncated =
      stats.scan_truncated || Boolean(frameStats.scan_truncated);
    stats.candidate_count += Number(frameStats.candidate_count || 0);
    stats.captured_element_count += Number(
      frameStats.captured_element_count || 0,
    );
    stats.projected_parent_count += Number(
      frameStats.projected_parent_count || 0,
    );
    stats.omitted_ancestor_count += Number(frameStats.omitted_ancestor_count || 0);
    stats.open_shadow_root_count += Number(frameStats.open_shadow_root_count || 0);
    stats.actionable_element_count += Number(
      frameStats.actionable_element_count || 0,
    );
    stats.fallback_text_count += Number(frameStats.fallback_text_count || 0);
    stats.native_canvas_count += Number(
      frameStats.native_canvas_count || frame.result.canvases?.length || 0,
    );
    stats.visual_region_count += Number(
      frameStats.visual_region_count || frame.result.visual_regions?.length || 0,
    );
    stats.svg_region_count += Number(frameStats.svg_region_count || 0);
    stats.truncated =
      stats.truncated ||
      Boolean(frameStats.truncated || frameStats.scan_truncated);

    for (const item of frame.result.svg_element_texts || []) {
      if (svgElementTexts.length >= 1000) break;
      const text = compactText(item.text, 500);
      if (!text) continue;
      const bbox = Array.isArray(item.bbox) ? item.bbox : [];
      const key = `${frameId}\u0000${text}\u0000${bbox.join(",")}`;
      if (svgTextKeys.has(key)) continue;
      svgTextKeys.add(key);
      svgElementTexts.push({
        text,
        bbox,
        selector: item.selector || "",
        frame_id: frameId,
        frame_url: frameUrl,
        document_id: documentId,
      });
    }

    for (const item of frame.result.elements || []) {
      if (elements.length >= MAX_DOM_ELEMENTS) {
        stats.truncated = true;
        break;
      }
      const localRef = String(item.ref || "");
      const localParent = String(item.parent_ref || "");
      const normalized = {
        ...item,
        ref: `frame:${frameId}:${localRef}`,
        parent_ref: localParent ? `frame:${frameId}:${localParent}` : "",
        frame_id: frameId,
        frame_url: frameUrl,
        document_id: documentId,
      };
      elements.push(normalized);
      if (item.role === "svg_text") {
        const text = compactText(item.label, 500);
        const bbox = Array.isArray(item.bbox) ? item.bbox : [];
        const key = `${frameId}\u0000${text}\u0000${bbox.join(",")}`;
        if (text && !svgTextKeys.has(key) && svgElementTexts.length < 1000) {
          svgTextKeys.add(key);
          svgElementTexts.push({
            text,
            bbox,
            selector: item.selector || "",
            frame_id: frameId,
            frame_url: frameUrl,
            document_id: documentId,
          });
        }
      }
    }
    for (const canvas of frame.result.canvases || []) {
      if (nativeCanvases.length >= MAX_CANVASES * 2) break;
      nativeCanvases.push({
        ...canvas,
        canvas_id: `frame-${frameId}-${canvas.canvas_id}`,
        frame_id: frameId,
        frame_url: frameUrl,
        document_id: documentId,
        source_frame_id: frameId,
        source_frame_url: frameUrl,
        source_kind: canvas.source_kind || "canvas",
        capture_kind: "native_canvas",
        capture_method: "canvas_to_data_url",
        roi_status: canvas.data_url ? "verified" : "capture_failed",
        coordinate_space: "frame_viewport_css_pixels",
        device_pixel_ratio: Number(frameViewport.device_pixel_ratio || 1),
      });
    }
    for (const region of frame.result.visual_regions || []) {
      if (visualRegions.length >= MAX_VISIBLE_SCREENSHOTS * 8) {
        stats.truncated = true;
        break;
      }
      visualRegions.push({
        ...region,
        region_id: `frame-${frameId}-${region.region_id}`,
        frame_id: frameId,
        frame_url: frameUrl,
        document_id: documentId,
      });
    }
  }

  const hasNativePixels = nativeCanvases.some((item) => item.data_url);
  const topViewport = topFrame.result.page.viewport || {};
  const shouldUsePageFallback =
    visualRegions.length === 0 &&
    !hasNativePixels &&
    elements.length < 5 &&
    Number(topViewport.width || 0) > 0 &&
    Number(topViewport.height || 0) > 0;
  stats.page_screenshot_fallback = shouldUsePageFallback;
  const regionsForCapture = shouldUsePageFallback
    ? [
        {
          region_id: "page_fallback",
          source_kind: "page_fallback",
          source_ref: "",
          source_region: [0, 0, topViewport.width, topViewport.height],
          bbox: [0, 0, topViewport.width, topViewport.height],
          client_width: topViewport.width,
          client_height: topViewport.height,
          frame_id: "0",
          frame_url: topFrame.result.page.url,
          document_id: String(topFrame.documentId || ""),
          roi_status: "unverified",
        },
      ]
    : visualRegions;

  onStage("screenshot");
  const visibleCapture = await captureVisibleRegions(
    tab,
    topFrame,
    regionsForCapture,
  );
  stats.visible_capture_error = visibleCapture.error;
  stats.visible_screenshot_count = visibleCapture.items.filter(
    (item) => item.data_url,
  ).length;

  const successfulVisible = visibleCapture.items.filter(
    (item) => item.data_url,
  );
  const successfulNative = nativeCanvases.filter((item) => item.data_url);
  const failedEvidence = [
    ...visibleCapture.items.filter((item) => !item.data_url),
    ...nativeCanvases.filter((item) => !item.data_url),
  ];
  const primaryEvidence =
    successfulVisible[0] || successfulNative[0] || failedEvidence[0] || null;
  const canvases = primaryEvidence ? [primaryEvidence] : [];

  const payload = {
    page: {
      ...topFrame.result.page,
      ui_version: "kt6-browser-extension-v0.5.1",
    },
    dom: { elements, stats },
    canvases,
    svg_element_texts: svgElementTexts,
    adapter_scene: pageAdapterScene,
    captured_at: Date.now() / 1000,
  };
  const health = await healthPromise;
  const context = {
    stats,
    submittedElementCount: elements.length,
    nativeCanvasCount: stats.native_canvas_count,
    visibleScreenshotCount: stats.visible_screenshot_count,
    visualEvidenceCount: canvases.filter((item) => item.data_url).length,
    preview: buildPreview(elements),
  };
  onStage("backend", {
    elapsedSeconds: 0,
    visionConfigured: visionConfigurationState(health),
  });
  const result = await submitCapture(payload, health, onStage, context);
  return {
    result,
    health,
    ...context,
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
      "未发现可访问 DOM；若页面主要由 Canvas/SVG 构成，请查看可见截图和视觉识别结果。";
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

globalThis.__KT6_VISUAL_CAPTURE_INTERNALS__ = Object.freeze({
  clippedRegion,
  sourcePixelRegionFor,
  backendWaitMilliseconds,
  visionConfigurationState,
});
globalThis.__KT6_CAPTURE_JOB_INTERNALS__ = Object.freeze({
  visionConfigurationState,
  createClientRequestId,
  waitForCaptureJob,
  readPendingCapture,
});

const captureButton = document.querySelector("#capture");
const statusOutput = document.querySelector("#status");

function backendStageMessage(detail = {}) {
  const visionNote =
    detail.visionConfigured === true
      ? ""
      : detail.visionConfigured === false
        ? "（视觉引擎未配置时仅保留证据）"
        : "（视觉引擎配置状态未知）";
  const connectionNote = detail.connectionIssue
    ? `；连接重试中：${compactText(detail.connectionIssue, 100)}`
    : "";
  const action = detail.resumed ? "继续查询" : "正在识别";
  const jobNote = detail.jobId ? ` · ${detail.jobId}` : "";
  return `已提交后端${jobNote}，${action} ${Number(
    detail.elapsedSeconds || 0,
  )} 秒${visionNote}${connectionNote}；请勿重复点击`;
}

function updateCaptureStage(stage, detail = {}) {
  const messages = {
    locating: "正在确认活动页面…",
    dom: "正在采集 DOM/ARIA、父子结构与页面适配器…",
    screenshot: "正在截取 Canvas/SVG 视觉区域…",
  };
  statusOutput.textContent =
    stage === "backend"
      ? backendStageMessage(detail)
      : messages[stage] || "正在采集…";
}

function renderCapture(capture, { resumed = false } = {}) {
  const result = capture?.result || {};
  const summary = result.summary || {};
  const stats = capture?.stats || {};
  const selectedMode = summary.selected_mode || "识别完成";
  const resumedNote = resumed ? " · 已恢复后台任务" : "";
  statusOutput.textContent = stats.visible_capture_error
    ? `DOM 采集成功 · 视觉截图失败：${stats.visible_capture_error}${resumedNote}`
    : stats.page_screenshot_fallback
      ? `采集成功 · 全页视觉兜底（仅分析） · ${selectedMode}${resumedNote}`
      : `采集成功 · ${selectedMode}${resumedNote}`;
  statusOutput.dataset.state = "success";
  setMetric("capture-id", result.capture_id || "-");
  setMetric("frames", stats.frame_count ?? "-");
  setMetric("scanned", stats.scanned_element_count ?? "-");
  setMetric("candidates", stats.candidate_count ?? "-");
  setMetric("submitted", capture?.submittedElementCount ?? "-");
  setMetric("actionable", summary.dom_actionable_element_count ?? "-");
  setMetric("native-canvas", capture?.nativeCanvasCount ?? "-");
  setMetric("visual-regions", stats.visual_region_count ?? "-");
  setMetric("visible-screenshot", capture?.visibleScreenshotCount ?? "-");
  setMetric("visual-evidence", capture?.visualEvidenceCount ?? "-");
  setMetric(
    "perception-decision",
    summary.perception_decision || "等待后端判断",
  );
  setMetric("page-adapter", stats.page_adapter_status || "-");
  setMetric("truncated", stats.truncated === undefined ? "-" : stats.truncated ? "是" : "否");
  renderPreview(Array.isArray(capture?.preview) ? capture.preview : []);
  document.querySelector("#result").hidden = false;
}

async function resumePendingCapture(pending) {
  const health = await readBackendHealth();
  const resumedPending = { ...pending, resumed: true };
  const result = await waitForCaptureJob(
    resumedPending,
    health,
    updateCaptureStage,
  );
  return {
    result,
    health,
    ...(pending.context && typeof pending.context === "object"
      ? pending.context
      : {}),
  };
}

async function runCaptureOperation(operation, { resumed = false } = {}) {
  if (captureInFlight) {
    statusOutput.textContent = "已有采集请求正在处理，请勿重复提交";
    return false;
  }
  captureInFlight = true;
  captureButton.disabled = true;
  statusOutput.dataset.state = "running";
  document.querySelector("#result").hidden = true;
  try {
    const capture = await operation();
    renderCapture(capture, { resumed });
    return true;
  } catch (error) {
    const message = error instanceof Error ? error.message : "采集失败";
    statusOutput.textContent = error?.keepPending
      ? `后台任务仍在运行：${message}`
      : `采集失败：${message}`;
    statusOutput.dataset.state = error?.keepPending ? "running" : "error";
    return false;
  } finally {
    captureInFlight = false;
    captureButton.disabled = false;
  }
}

captureButton.addEventListener("click", async () => {
  let pending = null;
  try {
    pending = await readPendingCapture();
  } catch (error) {
    statusOutput.textContent = `无法读取后台任务状态：${error.message}`;
    statusOutput.dataset.state = "error";
    return;
  }
  if (pending) {
    await runCaptureOperation(() => resumePendingCapture(pending), {
      resumed: true,
    });
    return;
  }
  await runCaptureOperation(
    () => captureActivePage({ onStage: updateCaptureStage }),
    { resumed: false },
  );
});

async function resumePendingCaptureOnOpen() {
  let pending = null;
  try {
    pending = await readPendingCapture();
  } catch (error) {
    statusOutput.textContent = `无法恢复后台任务：${error.message}`;
    statusOutput.dataset.state = "error";
    return;
  }
  if (!pending) return;
  await runCaptureOperation(() => resumePendingCapture(pending), {
    resumed: true,
  });
}

if (globalThis.chrome?.storage?.local) {
  void resumePendingCaptureOnOpen();
}
