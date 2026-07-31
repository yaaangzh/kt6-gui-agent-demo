"use strict";

const CAPTURE_ENDPOINT = "http://127.0.0.1:8787/api/perception/captures";
const MAX_DOM_ELEMENTS = 500;
const MAX_CANVASES = 4;
const PREVIEW_LIMIT = 20;
const MAX_VISIBLE_SCREENSHOTS = 1;
const MAX_VISUAL_DATA_URL_LENGTH = 6_500_000;
const MAX_VISUAL_PIXELS = 4_000_000;

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

async function captureActivePage() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || typeof tab.id !== "number") {
    throw new Error("未找到活动页面");
  }
  if (!/^https?:/i.test(tab.url || "")) {
    throw new Error("只能采集 HTTP(S) 页面");
  }

  const frameResults = await collectFrameResults(tab.id);
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
    scanned_element_count: 0,
    candidate_count: 0,
    captured_element_count: 0,
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
  };

  for (const frame of capturedFrames) {
    const frameId = String(frame.frameId);
    const frameUrl = frame.result.page.url;
    const documentId = String(frame.documentId || "");
    const frameStats = frame.result.stats || {};
    const frameViewport = frame.result.page.viewport || {};
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
    stats.native_canvas_count += Number(
      frameStats.native_canvas_count || frame.result.canvases?.length || 0,
    );
    stats.visual_region_count += Number(
      frameStats.visual_region_count || frame.result.visual_regions?.length || 0,
    );
    stats.svg_region_count += Number(frameStats.svg_region_count || 0);
    stats.truncated = stats.truncated || Boolean(frameStats.truncated);

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
      ui_version: "kt6-browser-extension-v0.4",
    },
    dom: { elements },
    canvases,
    svg_element_texts: svgElementTexts,
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
    nativeCanvasCount: stats.native_canvas_count,
    visibleScreenshotCount: stats.visible_screenshot_count,
    visualEvidenceCount: canvases.filter((item) => item.data_url).length,
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
});

const captureButton = document.querySelector("#capture");
const statusOutput = document.querySelector("#status");

captureButton.addEventListener("click", async () => {
  captureButton.disabled = true;
  statusOutput.textContent = "正在并行采集 DOM/ARIA 与 Canvas/SVG 视觉区域…";
  statusOutput.dataset.state = "running";
  document.querySelector("#result").hidden = true;
  try {
    const capture = await captureActivePage();
    const summary = capture.result.summary;
    statusOutput.textContent = capture.stats.visible_capture_error
      ? `DOM 采集成功 · 视觉截图失败：${capture.stats.visible_capture_error}`
      : capture.stats.page_screenshot_fallback
        ? `采集成功 · 全页视觉兜底（仅分析） · ${summary.selected_mode}`
        : `采集成功 · ${summary.selected_mode}`;
    statusOutput.dataset.state = "success";
    setMetric("capture-id", capture.result.capture_id);
    setMetric("frames", capture.stats.frame_count);
    setMetric("scanned", capture.stats.scanned_element_count);
    setMetric("candidates", capture.stats.candidate_count);
    setMetric("submitted", capture.submittedElementCount);
    setMetric("actionable", summary.dom_actionable_element_count);
    setMetric("native-canvas", capture.nativeCanvasCount);
    setMetric("visual-regions", capture.stats.visual_region_count);
    setMetric("visible-screenshot", capture.visibleScreenshotCount);
    setMetric("visual-evidence", capture.visualEvidenceCount);
    setMetric(
      "perception-decision",
      summary.perception_decision || "等待后端判断",
    );
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
