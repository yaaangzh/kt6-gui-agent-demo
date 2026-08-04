(() => {
  "use strict";

  const MAX_SCANNED_ELEMENTS = 12000;
  const MAX_CAPTURED_ELEMENTS = 220;
  const MAX_CANVASES = 4;
  const MAX_VISUAL_REGIONS = 6;
  const MAX_CANVAS_DATA_URL_LENGTH = 6_500_000;
  const MIN_VISUAL_REGION_WIDTH = 220;
  const MIN_VISUAL_REGION_HEIGHT = 120;
  const MIN_VISUAL_REGION_AREA = 48_000;
  const VISUAL_REGION_HINT =
    /(topology|topo|network|graph|map|canvas|diagram|拓扑|网络|地图)/i;
  const SEMANTIC_SELECTOR = [
    "[data-asset-id]",
    "[data-management-ip]",
    "[data-serial-number]",
    "[data-site-id]",
    "[data-asset-version]",
    "[data-business-id]",
    "[data-business-type]",
    "[data-testid]",
    "[data-kt6-action]",
    "[aria-label]",
    "[aria-live]",
    "[role]",
    "[tabindex]",
    "[contenteditable='true']",
    "[onclick]",
    "a[href]",
    "button",
    "input",
    "select",
    "summary",
    "textarea",
    "h1",
    "h2",
    "h3",
    "table",
    "th",
    "nav",
    "main",
    "aside",
    "svg text",
  ].join(",");
  const ACTIONABLE_ROLES = new Set([
    "button",
    "checkbox",
    "combobox",
    "gridcell",
    "link",
    "menuitem",
    "menuitemcheckbox",
    "menuitemradio",
    "option",
    "radio",
    "searchbox",
    "slider",
    "spinbutton",
    "switch",
    "tab",
    "textbox",
    "treeitem",
  ]);
  const ACTIONABLE_TAGS = new Set([
    "a",
    "button",
    "input",
    "select",
    "summary",
    "textarea",
  ]);
  const FALLBACK_TEXT_TAGS = new Set([
    "article",
    "dd",
    "dt",
    "figcaption",
    "li",
    "p",
    "small",
    "strong",
  ]);
  const STATISTIC_TEXT_TAGS = new Set(["div", "span", "p", "strong"]);

  function compactText(value, maximum = 300) {
    return String(value || "")
      .trim()
      .replace(/\s+/g, " ")
      .slice(0, maximum);
  }

  function looksLikeStatistic(value) {
    const text = compactText(value, 120);
    if (!text) return false;
    return Boolean(
      /\d+(?:[.,]\d+)?\s*(?:%|ms|s|kbps|mbps|gbps|kb|mb|gb)(?![A-Za-z])/i.test(
        text,
      ) ||
        /\d+(?:[.,]\d+)?\s*(?:台|个|条|处|站|次|户)/.test(text) ||
        /\d+\s*\/\s*\d+/.test(text),
    );
  }

  function escapeIdentifier(value) {
    if (globalThis.CSS && typeof globalThis.CSS.escape === "function") {
      return globalThis.CSS.escape(value);
    }
    return String(value).replace(
      /[^A-Za-z0-9_-]/g,
      (character) => `\\${character}`,
    );
  }

  function quoteAttribute(value) {
    return String(value).replace(/\\/g, "\\\\").replace(/"/g, '\\"');
  }

  function uniqueSelector(candidate) {
    try {
      return document.querySelectorAll(candidate).length === 1 ? candidate : "";
    } catch (_error) {
      return "";
    }
  }

  function selectorFor(element) {
    if (element.id) {
      const byId = uniqueSelector(`#${escapeIdentifier(element.id)}`);
      if (byId) return byId;
    }
    for (const name of [
      "data-asset-id",
      "data-business-id",
      "data-testid",
      "data-kt6-action",
      "aria-label",
      "name",
    ]) {
      const value = element.getAttribute(name);
      if (!value || value.length > 200) continue;
      const candidate = `${element.tagName.toLowerCase()}[${name}="${quoteAttribute(value)}"]`;
      const unique = uniqueSelector(candidate);
      if (unique) return unique;
    }

    const segments = [];
    let current = element;
    while (
      current &&
      current.nodeType === Node.ELEMENT_NODE &&
      segments.length < 8
    ) {
      const tag = current.tagName.toLowerCase();
      if (current.id) {
        segments.unshift(`#${escapeIdentifier(current.id)}`);
        break;
      }
      let position = 1;
      let sibling = current.previousElementSibling;
      while (sibling) {
        if (sibling.tagName === current.tagName) position += 1;
        sibling = sibling.previousElementSibling;
      }
      segments.unshift(`${tag}:nth-of-type(${position})`);
      const candidate = segments.join(" > ");
      if (uniqueSelector(candidate)) return candidate;
      current = current.parentElement;
    }
    return segments.join(" > ").slice(0, 500);
  }

  function visibleBox(element) {
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    if (
      rect.width <= 0 ||
      rect.height <= 0 ||
      style.display === "none" ||
      style.visibility === "hidden" ||
      Number.parseFloat(style.opacity || "1") === 0
    ) {
      return null;
    }
    return {
      bbox: [
        Number(rect.left.toFixed(2)),
        Number(rect.top.toFixed(2)),
        Number(rect.width.toFixed(2)),
        Number(rect.height.toFixed(2)),
      ],
      style,
    };
  }

  function clippedBoxToViewport(bbox) {
    const [left, top, width, height] = bbox;
    const right = Math.min(innerWidth, left + width);
    const bottom = Math.min(innerHeight, top + height);
    const clippedLeft = Math.max(0, left);
    const clippedTop = Math.max(0, top);
    const clippedWidth = right - clippedLeft;
    const clippedHeight = bottom - clippedTop;
    if (clippedWidth <= 0 || clippedHeight <= 0) return null;
    return [
      Number(clippedLeft.toFixed(2)),
      Number(clippedTop.toFixed(2)),
      Number(clippedWidth.toFixed(2)),
      Number(clippedHeight.toFixed(2)),
    ];
  }

  function regionHintFor(element) {
    return compactText(
      [
        element.id,
        typeof element.className === "string"
          ? element.className
          : element.getAttribute("class"),
        element.getAttribute("aria-label"),
        element.getAttribute("data-testid"),
        element.getAttribute("role"),
        element.getAttribute("title"),
      ].join(" "),
      500,
    );
  }

  function regionOverlapRatio(left, right) {
    const intersectionWidth = Math.max(
      0,
      Math.min(left[0] + left[2], right[0] + right[2]) -
        Math.max(left[0], right[0]),
    );
    const intersectionHeight = Math.max(
      0,
      Math.min(left[1] + left[3], right[1] + right[3]) -
        Math.max(left[1], right[1]),
    );
    const intersection = intersectionWidth * intersectionHeight;
    const smallerArea = Math.min(left[2] * left[3], right[2] * right[3]);
    return smallerArea > 0 ? intersection / smallerArea : 0;
  }

  function inferredRole(element) {
    const explicit = compactText(element.getAttribute("role"), 100);
    if (explicit) return explicit.toLowerCase();
    const tag = element.tagName.toLowerCase();
    if (tag === "a" && element.hasAttribute("href")) return "link";
    if (tag === "button" || tag === "summary") return "button";
    if (tag === "select") return "combobox";
    if (tag === "textarea") return "textbox";
    if (tag === "h1" || tag === "h2" || tag === "h3") return "heading";
    if (tag === "table") return "table";
    if (tag === "th") return "columnheader";
    if (tag === "nav") return "navigation";
    if (tag === "main") return "main";
    if (tag === "aside") return "complementary";
    if (tag === "text" && element.closest("svg")) return "svg_text";
    if (tag === "input") {
      const inputType = compactText(element.getAttribute("type"), 30).toLowerCase();
      if (["button", "reset", "submit"].includes(inputType)) return "button";
      if (inputType === "checkbox") return "checkbox";
      if (inputType === "radio") return "radio";
      if (inputType === "range") return "slider";
      if (inputType === "search") return "searchbox";
      return "textbox";
    }
    return "";
  }

  function isDisabled(element) {
    return Boolean(
      element.disabled ||
        element.hasAttribute("disabled") ||
        element.getAttribute("aria-disabled") === "true",
    );
  }

  function isActionable(element, role, style) {
    if (isDisabled(element)) return false;
    const tag = element.tagName.toLowerCase();
    const rawTabIndex = element.getAttribute("tabindex");
    const tabIndex = rawTabIndex === null ? Number.NaN : Number(rawTabIndex);
    return Boolean(
      ACTIONABLE_TAGS.has(tag) ||
        ACTIONABLE_ROLES.has(role) ||
        element.hasAttribute("onclick") ||
        element.hasAttribute("data-kt6-action") ||
        element.isContentEditable ||
        (!Number.isNaN(tabIndex) && tabIndex >= 0) ||
        style.cursor === "pointer"
    );
  }

  function labelFor(element) {
    const ariaLabel = compactText(element.getAttribute("aria-label"));
    if (ariaLabel) return ariaLabel;
    if (element.labels && element.labels.length) {
      const explicitLabel = compactText(
        Array.from(element.labels)
          .map((item) => item.innerText || item.textContent)
          .join(" "),
      );
      if (explicitLabel) return explicitLabel;
    }
    const placeholder = compactText(element.getAttribute("placeholder"));
    if (placeholder) return placeholder;
    if (["INPUT", "TEXTAREA", "SELECT"].includes(element.tagName)) {
      return compactText(
        element.getAttribute("name") || element.getAttribute("type"),
      );
    }
    return compactText(element.innerText || element.textContent);
  }

  function ownerBusinessIdFor(element) {
    const owner = element.closest("[data-asset-id],[data-business-id]");
    if (!owner) return "";
    return compactText(
      owner.getAttribute("data-asset-id") ||
        owner.getAttribute("data-business-id"),
      200,
    );
  }

  function priorityFor(candidate) {
    let priority = 0;
    if (candidate.businessId) priority += 120;
    if (candidate.assetId) priority += 120;
    if (candidate.testId) priority += 35;
    if (candidate.actionable) priority += 70;
    if (candidate.ariaLabel) priority += 30;
    if (candidate.actionId) priority += 45;
    if (candidate.role === "heading") priority += 25;
    if (candidate.role === "svg_text") priority += 15;
    if (looksLikeStatistic(candidate.label)) priority += 18;
    if (
      ["table", "tree", "grid", "navigation", "main"].includes(candidate.role)
    ) {
      priority += 20;
    }
    if (candidate.fallbackText) priority -= 20;
    return priority;
  }

  const pageElements = document.querySelectorAll("*");
  const totalElementCount = pageElements.length;
  const scanned = Array.from(pageElements).slice(
    0,
    MAX_SCANNED_ELEMENTS,
  );
  const candidates = [];
  const candidateElements = new Set();

  function addCandidate(element, documentOrder, fallbackText = false) {
    if (candidateElements.has(element)) return;
    const visible = visibleBox(element);
    if (!visible) return;
    const semantic = element.matches(SEMANTIC_SELECTOR);
    let pointerCandidate = visible.style.cursor === "pointer";
    if (pointerCandidate && !semantic && element.parentElement) {
      const parentStyle = getComputedStyle(element.parentElement);
      if (parentStyle.cursor === "pointer") pointerCandidate = false;
    }
    if (!semantic && !pointerCandidate && !fallbackText) return;

    const role = inferredRole(element);
    const actionable = isActionable(element, role, visible.style);
    const ariaLabel = compactText(element.getAttribute("aria-label"));
    const placeholder = compactText(element.getAttribute("placeholder"));
    const businessId = compactText(
      element.getAttribute("data-business-id"),
      200,
    );
    const businessType = compactText(
      element.getAttribute("data-business-type"),
      100,
    );
    const assetId = compactText(
      element.getAttribute("data-asset-id") || businessId,
      200,
    );
    const managementIp = compactText(
      element.getAttribute("data-management-ip"),
      100,
    );
    const serialNumber = compactText(
      element.getAttribute("data-serial-number"),
      200,
    );
    const siteId = compactText(element.getAttribute("data-site-id"), 200);
    const assetVersion = compactText(
      element.getAttribute("data-asset-version"),
      50,
    );
    const actionId = compactText(
      element.getAttribute("data-kt6-action"),
      200,
    );
    const ownerBusinessId = ownerBusinessIdFor(element);
    const testId = compactText(element.getAttribute("data-testid"), 200);
    const label = labelFor(element);
    if (
      !label &&
      !businessId &&
      !assetId &&
      !actionId &&
      !testId &&
      !ariaLabel &&
      !placeholder
    ) return;

    const candidate = {
      element,
      documentOrder,
      bbox: visible.bbox,
      role: role || (fallbackText ? "text" : ""),
      actionable,
      ariaLabel,
      placeholder,
      businessId,
      businessType,
      assetId,
      managementIp,
      serialNumber,
      siteId,
      assetVersion,
      actionId,
      ownerBusinessId,
      testId,
      label,
      fallbackText,
    };
    candidate.priority = priorityFor(candidate);
    candidateElements.add(element);
    candidates.push(candidate);
  }

  scanned.forEach((element, documentOrder) => {
    addCandidate(element, documentOrder, false);
  });

  scanned.forEach((element, documentOrder) => {
    if (!STATISTIC_TEXT_TAGS.has(element.tagName.toLowerCase())) return;
    const text = compactText(element.innerText || element.textContent, 100);
    if (!looksLikeStatistic(text)) return;
    const childHasSameStatistic = Array.from(element.children).some((child) =>
      looksLikeStatistic(child.innerText || child.textContent),
    );
    if (childHasSameStatistic) return;
    addCandidate(element, documentOrder, true);
  });

  if (candidates.length < 20) {
    scanned.forEach((element, documentOrder) => {
      if (!FALLBACK_TEXT_TAGS.has(element.tagName.toLowerCase())) return;
      const text = compactText(element.innerText || element.textContent, 180);
      if (!text || text.length > 180) return;
      addCandidate(element, documentOrder, true);
    });
  }

  const selected = candidates
    .slice()
    .sort(
      (left, right) =>
        right.priority - left.priority ||
        left.documentOrder - right.documentOrder,
    )
    .slice(0, MAX_CAPTURED_ELEMENTS)
    .sort((left, right) => left.documentOrder - right.documentOrder);
  const refByElement = new Map();
  selected.forEach((item, index) => {
    refByElement.set(
      item.element,
      selectorFor(item.element) || `@capture:${index + 1}`,
    );
  });

  const elements = selected.map((item) => {
    const immediateParent = item.element.parentElement;
    let parent = immediateParent;
    let omittedAncestorCount = 0;
    while (parent && !refByElement.has(parent)) {
      omittedAncestorCount += 1;
      parent = parent.parentElement;
    }
    let depth = 0;
    let ancestor = item.element;
    while (ancestor && ancestor !== document.documentElement) {
      depth += 1;
      ancestor = ancestor.parentElement;
    }
    const selector = refByElement.get(item.element);
    return {
      ref: selector,
      selector: selector.startsWith("@capture:") ? "" : selector,
      parent_ref: parent ? refByElement.get(parent) || "" : "",
      parent_relation: parent
        ? parent === immediateParent
          ? "direct"
          : "nearest_captured_ancestor"
        : "root_or_unobserved",
      omitted_ancestor_count: omittedAncestorCount,
      depth,
      document_order: item.documentOrder,
      tag: item.element.tagName.toLowerCase(),
      role: item.role,
      label: item.label,
      aria_label: item.ariaLabel,
      placeholder: item.placeholder,
      business_id: item.businessId,
      business_type: item.businessType,
      asset_id: item.assetId,
      management_ip: item.managementIp,
      serial_number: item.serialNumber,
      site_id: item.siteId,
      asset_version: item.assetVersion,
      action_id: item.actionId,
      owner_business_id: item.ownerBusinessId,
      test_id: item.testId,
      bbox: item.bbox,
      disabled: isDisabled(item.element),
      checked: Boolean(item.element.checked),
      actionable: item.actionable,
      fallback_text: item.fallbackText,
    };
  });

  const canvases = [];
  for (const [index, canvas] of Array.from(
    document.querySelectorAll("canvas"),
  ).entries()) {
    if (canvases.length >= MAX_CANVASES) break;
    const visible = visibleBox(canvas);
    if (!visible) continue;
    const result = {
      canvas_id: canvas.id || `canvas_${index}`,
      width: canvas.width,
      height: canvas.height,
      client_width: visible.bbox[2],
      client_height: visible.bbox[3],
      bbox: visible.bbox,
    };
    try {
      const dataUrl = canvas.toDataURL("image/png");
      if (dataUrl.length <= MAX_CANVAS_DATA_URL_LENGTH) {
        result.data_url = dataUrl;
      } else {
        result.capture_error = "canvas data URL exceeds extension limit";
      }
    } catch (error) {
      result.capture_error =
        error instanceof Error ? error.message : "canvas capture failed";
    }
    canvases.push(result);
  }

  const visualRegions = [];

  function addVisualRegion(
    element,
    sourceKind,
    sourceIndex,
    primitiveCount = 0,
  ) {
    const visible = visibleBox(element);
    if (!visible) return;
    const clipped = clippedBoxToViewport(visible.bbox);
    if (!clipped) return;
    const area = clipped[2] * clipped[3];
    const hint = regionHintFor(element);
    const hasHint = VISUAL_REGION_HINT.test(hint);
    const largeEnough =
      clipped[2] >= MIN_VISUAL_REGION_WIDTH &&
      clipped[3] >= MIN_VISUAL_REGION_HEIGHT &&
      area >= MIN_VISUAL_REGION_AREA;
    if (!largeEnough) return;
    if (
      sourceKind === "svg" &&
      !hasHint &&
      Number(primitiveCount || 0) < 6
    ) {
      return;
    }
    if (
      sourceKind === "graphic_container" &&
      !hasHint &&
      Number(primitiveCount || 0) < 6
    ) {
      return;
    }
    if (
      ["embedded_region", "role_img"].includes(sourceKind) &&
      !hasHint &&
      area < 160_000
    ) {
      return;
    }

    const originalArea = visible.bbox[2] * visible.bbox[3];
    const selector = selectorFor(element);
    const sourcePriority = {
      graphic_container: 40,
      svg: 110,
      canvas: 120,
      embedded_region: 80,
      role_img: 10,
    };
    const score =
      (sourcePriority[sourceKind] || 0) +
      (hasHint ? 60 : 0) +
      Math.min(Number(primitiveCount || 0), 20);
    const candidate = {
      region_id: `${sourceKind}_${element.id || sourceIndex}`,
      source_kind: sourceKind,
      source_ref: selector,
      bbox: clipped,
      source_region: visible.bbox,
      client_width: clipped[2],
      client_height: clipped[3],
      visible_ratio:
        originalArea > 0
          ? Number((area / originalArea).toFixed(4))
          : 0,
      primitive_count: Number(primitiveCount || 0),
      explicit_hint: hasHint,
      _score: score,
    };

    const duplicateIndex = visualRegions.findIndex(
      (item) => regionOverlapRatio(item.bbox, clipped) >= 0.9,
    );
    if (duplicateIndex >= 0) {
      if (score > visualRegions[duplicateIndex]._score) {
        visualRegions[duplicateIndex] = candidate;
      }
      return;
    }
    visualRegions.push(candidate);
  }

  scanned.forEach((element, index) => {
    const tag = element.tagName.toLowerCase();
    if (
      ["html", "body", "canvas", "svg", "iframe", "object", "embed"].includes(
        tag,
      ) ||
      !VISUAL_REGION_HINT.test(regionHintFor(element))
    ) {
      return;
    }
    const primitiveCount = element.querySelectorAll(
      "canvas,svg,circle,ellipse,line,path,polygon,polyline",
    ).length;
    addVisualRegion(element, "graphic_container", index, primitiveCount);
  });

  const graphicAncestorCounts = new Map();
  Array.from(document.querySelectorAll("canvas,svg")).forEach((graphic) => {
    let ancestor = graphic.parentElement;
    let depth = 0;
    while (
      ancestor &&
      ancestor !== document.body &&
      ancestor !== document.documentElement &&
      depth < 8
    ) {
      graphicAncestorCounts.set(
        ancestor,
        (graphicAncestorCounts.get(ancestor) || 0) + 1,
      );
      ancestor = ancestor.parentElement;
      depth += 1;
    }
  });
  Array.from(graphicAncestorCounts.entries())
    .filter(([, count]) => count >= 8)
    .map(([element, count]) => {
      const visible = visibleBox(element);
      return {
        element,
        count,
        area: visible ? visible.bbox[2] * visible.bbox[3] : 0,
      };
    })
    .sort(
      (left, right) =>
        right.count - left.count || left.area - right.area,
    )
    .slice(0, 12)
    .forEach((item, index) => {
      addVisualRegion(
        item.element,
        "graphic_container",
        `cluster_${index}`,
        item.count,
      );
    });

  Array.from(document.querySelectorAll("canvas")).forEach((canvas, index) => {
    addVisualRegion(canvas, "canvas", index, 1);
  });
  Array.from(document.querySelectorAll("svg")).forEach((svg, index) => {
    if (svg.ownerSVGElement) return;
    const primitiveCount = svg.querySelectorAll(
      "circle,ellipse,line,path,polygon,polyline,rect,text,use,g",
    ).length;
    addVisualRegion(svg, "svg", index, primitiveCount);
  });
  Array.from(document.querySelectorAll("iframe,object,embed")).forEach(
    (element, index) => {
      addVisualRegion(element, "embedded_region", index, 0);
    },
  );
  Array.from(document.querySelectorAll("[role='img']")).forEach(
    (element, index) => {
      addVisualRegion(element, "role_img", index, 0);
    },
  );

  visualRegions.sort(
    (left, right) =>
      right._score - left._score ||
      right.bbox[2] * right.bbox[3] - left.bbox[2] * left.bbox[3],
  );
  visualRegions.splice(MAX_VISUAL_REGIONS);
  visualRegions.forEach((item) => delete item._score);

  const svgElementTexts = [];
  const svgTextKeys = new Set();
  for (const textElement of Array.from(
    document.querySelectorAll("svg text"),
  )) {
    if (svgElementTexts.length >= 1000) break;
    const visible = visibleBox(textElement);
    if (!visible) continue;
    const text = compactText(textElement.textContent, 500);
    if (!text) continue;
    const selector = selectorFor(textElement);
    const key = `${text}\u0000${visible.bbox.join(",")}`;
    if (svgTextKeys.has(key)) continue;
    svgTextKeys.add(key);
    svgElementTexts.push({
      text,
      bbox: visible.bbox,
      selector,
    });
  }

  const captureResult = {
    page: {
      url: location.href,
      title: document.title,
      language: document.documentElement.lang || "",
      viewport: {
        width: innerWidth,
        height: innerHeight,
        device_pixel_ratio: devicePixelRatio || 1,
      },
    },
    elements,
    canvases,
    visual_regions: visualRegions,
    svg_element_texts: svgElementTexts,
    stats: {
      total_element_count: totalElementCount,
      scanned_element_count: scanned.length,
      scan_truncated: totalElementCount > scanned.length,
      candidate_count: candidates.length,
      captured_element_count: elements.length,
      actionable_element_count: elements.filter((item) => item.actionable)
        .length,
      fallback_text_count: elements.filter((item) => item.fallback_text).length,
      native_canvas_count: canvases.length,
      visual_region_count: visualRegions.length,
      svg_region_count: visualRegions.filter((item) => item.source_kind === "svg")
        .length,
      svg_element_text_count: svgElementTexts.length,
      projected_parent_count: elements.filter((item) => item.parent_ref).length,
      omitted_ancestor_count: elements.reduce(
        (total, item) => total + Number(item.omitted_ancestor_count || 0),
        0,
      ),
      open_shadow_root_count: scanned.filter((item) => Boolean(item.shadowRoot))
        .length,
      truncated:
        totalElementCount > scanned.length ||
        candidates.length > elements.length,
    },
  };
  globalThis.__KT6_EXTENSION_CAPTURE__ = captureResult;
  return captureResult;
})();
