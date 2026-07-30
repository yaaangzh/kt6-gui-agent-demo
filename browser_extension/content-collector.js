(() => {
  "use strict";

  const MAX_SCANNED_ELEMENTS = 12000;
  const MAX_CAPTURED_ELEMENTS = 220;
  const MAX_CANVASES = 4;
  const MAX_CANVAS_DATA_URL_LENGTH = 6_500_000;
  const SEMANTIC_SELECTOR = [
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

  function compactText(value, maximum = 300) {
    return String(value || "")
      .trim()
      .replace(/\s+/g, " ")
      .slice(0, maximum);
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

  function priorityFor(candidate) {
    let priority = 0;
    if (candidate.businessId) priority += 120;
    if (candidate.testId) priority += 35;
    if (candidate.actionable) priority += 70;
    if (candidate.ariaLabel) priority += 30;
    if (candidate.element.hasAttribute("data-kt6-action")) priority += 45;
    if (candidate.role === "heading") priority += 25;
    if (
      ["table", "tree", "grid", "navigation", "main"].includes(candidate.role)
    ) {
      priority += 20;
    }
    if (candidate.fallbackText) priority -= 20;
    return priority;
  }

  const scanned = Array.from(document.querySelectorAll("*")).slice(
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
    const testId = compactText(element.getAttribute("data-testid"), 200);
    const label = labelFor(element);
    if (!label && !businessId && !testId && !ariaLabel && !placeholder) return;

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

  if (candidates.length < 5) {
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
    let parent = item.element.parentElement;
    while (parent && !refByElement.has(parent)) parent = parent.parentElement;
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
      depth,
      document_order: item.documentOrder,
      tag: item.element.tagName.toLowerCase(),
      role: item.role,
      label: item.label,
      aria_label: item.ariaLabel,
      placeholder: item.placeholder,
      business_id: item.businessId,
      business_type: item.businessType,
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
    stats: {
      scanned_element_count: scanned.length,
      candidate_count: candidates.length,
      captured_element_count: elements.length,
      actionable_element_count: elements.filter((item) => item.actionable)
        .length,
      fallback_text_count: elements.filter((item) => item.fallback_text).length,
      truncated: candidates.length > elements.length,
    },
  };
  globalThis.__KT6_EXTENSION_CAPTURE__ = captureResult;
  return captureResult;
})();
