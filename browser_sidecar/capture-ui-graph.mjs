import { writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import process from "node:process";

const MAX_FRAME_COUNT = 64;
const MAX_NODE_COUNT = 10_000;
const SCHEMA_VERSION = "kt6.cdp-page-snapshot.v1";

function fail(message) {
  process.stderr.write(`${message}\n`);
  process.exitCode = 2;
}

function argumentsFrom(argv) {
  const result = {};
  for (let index = 0; index < argv.length; index += 1) {
    const key = argv[index];
    if (!key.startsWith("--")) throw new Error(`unexpected argument: ${key}`);
    const value = argv[index + 1];
    if (!value || value.startsWith("--")) throw new Error(`${key} requires a value`);
    result[key.slice(2)] = value;
    index += 1;
  }
  return result;
}

function validatedLoopbackURL(value) {
  const parsed = new URL(value);
  const hostname = parsed.hostname.replace(/^\[|\]$/g, "").toLowerCase();
  if (!['http:', 'https:', 'ws:', 'wss:'].includes(parsed.protocol)) {
    throw new Error("--cdp-url must use http(s) or ws(s)");
  }
  if (!['localhost', '127.0.0.1', '::1'].includes(hostname)) {
    throw new Error("--cdp-url must target a loopback browser endpoint");
  }
  if (parsed.username || parsed.password || parsed.hash) {
    throw new Error("--cdp-url must not contain credentials or a fragment");
  }
  return parsed.toString();
}

export function flattenFrames(frameTree) {
  const frames = [];
  const pending = frameTree ? [frameTree] : [];
  while (pending.length) {
    const item = pending.shift();
    if (!item?.frame?.id) continue;
    frames.push({
      frame_id: String(item.frame.id),
      parent_frame_id: String(item.frame.parentId || ""),
      url: String(item.frame.url || "").slice(0, 2048),
      name: String(item.frame.name || "").slice(0, 200),
      security_origin: String(item.frame.securityOrigin || "").slice(0, 500),
    });
    if (frames.length > MAX_FRAME_COUNT) {
      throw new Error(`frame tree exceeds ${MAX_FRAME_COUNT} frames`);
    }
    for (const child of item.childFrames || []) pending.push(child);
  }
  return frames;
}

export function assertDOMSnapshotWithinLimit(domSnapshot) {
  const documents = Array.isArray(domSnapshot?.documents) ? domSnapshot.documents : [];
  let count = 0;
  for (const document of documents) {
    const nodes = document?.nodes || {};
    const parallelLengths = [
      nodes.parentIndex,
      nodes.nodeType,
      nodes.nodeName,
      nodes.nodeValue,
      nodes.backendNodeId,
      nodes.attributes,
    ]
      .filter(Array.isArray)
      .map((values) => values.length);
    count += parallelLengths.length ? Math.max(...parallelLengths) : 0;
    if (count > MAX_NODE_COUNT) {
      throw new Error(`DOM snapshot exceeds ${MAX_NODE_COUNT} nodes`);
    }
  }
  return count;
}

export function appendAXNodesWithinLimit(target, nodes, frameId) {
  const additions = Array.isArray(nodes) ? nodes : [];
  if (target.length + additions.length > MAX_NODE_COUNT) {
    throw new Error(`accessibility snapshot exceeds ${MAX_NODE_COUNT} nodes`);
  }
  for (const node of additions) {
    target.push({ ...node, frameId: node.frameId || frameId });
  }
  return target.length;
}

function decodedString(strings, index) {
  if (!Number.isInteger(index) || index < 0 || index >= strings.length) return "";
  return String(strings[index] ?? "");
}

export function titleFromDOMSnapshot(domSnapshot, mainFrameId) {
  const strings = Array.isArray(domSnapshot?.strings) ? domSnapshot.strings : [];
  const documents = Array.isArray(domSnapshot?.documents) ? domSnapshot.documents : [];
  const mainDocument = documents.find(
    (document) => decodedString(strings, document?.frameId) === String(mainFrameId || ""),
  );
  if (!mainDocument) {
    throw new Error("DOM snapshot does not contain the main-frame document");
  }
  return decodedString(strings, mainDocument.title).slice(0, 300);
}

export function selectUniquePage(pages, pageURLSubstring) {
  const hasSelector = pageURLSubstring !== undefined && pageURLSubstring !== null;
  const selector = hasSelector ? String(pageURLSubstring).trim() : "";
  if (hasSelector && !selector) {
    throw new Error("--page-url must not be empty");
  }
  const candidates = hasSelector
    ? pages.filter((candidate) => candidate.url().includes(selector))
    : pages.filter((candidate) => !candidate.url().startsWith("chrome://"));
  if (candidates.length !== 1) {
    const scope = hasSelector
      ? `--page-url matched ${candidates.length}`
      : `found ${candidates.length} non-chrome`;
    throw new Error(`${scope} pages; expected exactly one`);
  }
  return candidates[0];
}

async function main() {
  const args = argumentsFrom(process.argv.slice(2));
  if (!args['cdp-url'] || !args.output) {
    throw new Error(
      "usage: node capture-ui-graph.mjs --cdp-url http://127.0.0.1:9222 --output snapshot.json [--page-url text]",
    );
  }
  const cdpURL = validatedLoopbackURL(args['cdp-url']);
  const outputPath = resolve(args.output);
  const { chromium } = await import("playwright-core");
  const browser = await chromium.connectOverCDP(cdpURL);
  try {
    const pages = browser.contexts().flatMap((context) => context.pages());
    const page = selectUniquePage(pages, args['page-url']);

    const context = page.context();
    const session = await context.newCDPSession(page);
    const [browserVersion, frameTreeResult, domSnapshot] = await Promise.all([
      session.send("Browser.getVersion"),
      session.send("Page.getFrameTree"),
      session.send("DOMSnapshot.captureSnapshot", {
        computedStyles: [],
        includePaintOrder: true,
        includeDOMRects: true,
      }),
    ]);
    const frames = flattenFrames(frameTreeResult.frameTree);
    assertDOMSnapshotWithinLimit(domSnapshot);
    const axNodes = [];
    const frameErrors = [];
    for (const frame of frames) {
      let result;
      try {
        result = await session.send("Accessibility.getFullAXTree", {
          frameId: frame.frame_id,
        });
      } catch (error) {
        frameErrors.push({
          frame_id: frame.frame_id,
          code: "ax_tree_unavailable",
          message: error instanceof Error ? error.message.slice(0, 300) : "unknown error",
        });
        continue;
      }
      appendAXNodesWithinLimit(axNodes, result.nodes, frame.frame_id);
    }
    const payload = {
      schema_version: SCHEMA_VERSION,
      captured_at: Date.now() / 1000,
      page: {
        url: page.url().slice(0, 2048),
        title: titleFromDOMSnapshot(domSnapshot, frames[0]?.frame_id),
      },
      source_metadata: {
        source_type: "playwright_cdp_sidecar",
        playwright_transport: "connectOverCDP",
        browser_product: String(browserVersion.product || "").slice(0, 200),
        protocol_version: String(browserVersion.protocolVersion || "").slice(0, 100),
        safe_for_execution: false,
        capture_only: true,
      },
      frames,
      frame_errors: frameErrors,
      dom_snapshot: domSnapshot,
      ax_tree: { nodes: axNodes },
      actionable_grounding: false,
      safe_for_execution: false,
    };
    await writeFile(outputPath, `${JSON.stringify(payload)}\n`, {
      encoding: "utf8",
      flag: "wx",
    });
    process.stdout.write(
      `${JSON.stringify({ status: "captured", output: outputPath, frames: frames.length })}\n`,
    );
  } finally {
    await browser.close();
  }
}

const isDirectExecution =
  Boolean(process.argv[1]) && import.meta.url === pathToFileURL(resolve(process.argv[1])).href;
if (isDirectExecution) {
  main().catch((error) => {
    fail(error instanceof Error ? error.message : "CDP capture failed");
  });
}
