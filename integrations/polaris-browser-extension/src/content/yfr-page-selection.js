(() => {
  if (globalThis.__yfrStandaloneSelectionLoaded) return;
  globalThis.__yfrStandaloneSelectionLoaded = true;

  const MESSAGE_TYPE = "TOGGLE_YFR_PAGE_SELECTION";
  const IMPORT_TYPE = "YFR_IMPORT_SELECTION";
  const CARD_SELECTOR = ".literature-paper-card, .daily-paper[id^='paper-']";
  const CONTROL_ATTRIBUTE = "data-yfr-extension-control";
  const PAPER_ATTRIBUTE = "data-yfr-extension-paper-id";
  const YFR_PAPER_ID_ATTRIBUTE = "data-yfr-paper-id";
  const YFR_SOURCE_AREA_ATTRIBUTE = "data-yfr-source-area";
  const YFR_CONTEXT_ID_ATTRIBUTE = "data-yfr-context-id";
  const MAX_PAPERS = 1000;

  const state = {
    active: false,
    papers: new Map(),
    duplicateIds: new Set(),
    selected: new Set(),
    controls: new Map(),
    toolbarHost: null,
    toolbarSpacer: null,
    toolbar: null,
    observer: null,
    routeTimer: null,
    scanTimer: null,
    routeKey: "",
    notice: "",
    syncingNativeControls: false,
  };

  function cleanText(value, maxLength = 1000) {
    return String(value ?? "").replace(/\s+/g, " ").trim().slice(0, maxLength);
  }

  function stableHash(value) {
    let hash = 0x811c9dc5;
    for (const char of String(value || "")) {
      hash ^= char.codePointAt(0);
      hash = Math.imul(hash, 0x01000193);
    }
    return (hash >>> 0).toString(16).padStart(8, "0");
  }

  function canonicalTitle(value) {
    return cleanText(value, 2000).toLocaleLowerCase().replace(/[^a-z0-9\u4e00-\u9fff]+/g, "");
  }

  function canonicalDoi(value) {
    return cleanText(value, 512)
      .replace(/^doi\s*:\s*/i, "")
      .replace(/^https?:\/\/(?:dx\.)?doi\.org\//i, "")
      .replace(/[\s.,;]+$/g, "")
      .toLowerCase();
  }

  function trustedYfrPdfAccess(url) {
    const keys = url.searchParams.getAll("accessKey");
    return url.origin === location.origin
      && /^\/api\/daily-review\/assets\/pdfs\/[^/]+\.pdf$/i.test(url.pathname)
      && keys.length === 1
      && keys[0].length > 0
      && keys[0].length <= 512;
  }

  function absoluteHttpUrl(value, { preserveYfrPdfAccessKey = false } = {}) {
    try {
      const url = new URL(value, location.href);
      if (!["http:", "https:"].includes(url.protocol)) return null;
      const sensitive = /^(?:access_?key|api_?key|token|key|secret|signature|sig|auth|authorization|cdk)$/i;
      const preserveAccessKey = preserveYfrPdfAccessKey && trustedYfrPdfAccess(url);
      for (const key of Array.from(url.searchParams.keys())) {
        if (sensitive.test(key) && !(preserveAccessKey && key === "accessKey")) url.searchParams.delete(key);
      }
      return url.toString();
    } catch {
      return null;
    }
  }

  function doiFromCard(card, links) {
    for (const link of links) {
      const href = absoluteHttpUrl(link.getAttribute("href"));
      if (!href) continue;
      try {
        const url = new URL(href);
        if (["doi.org", "dx.doi.org"].includes(url.hostname)) return canonicalDoi(decodeURIComponent(url.pathname.slice(1)));
      } catch {
        // Continue with visible text detection.
      }
    }
    const match = cleanText(card.textContent, 20_000).match(/10\.\d{4,9}\/[-._;()/:a-z0-9]+/i);
    return match ? canonicalDoi(match[0]) : "";
  }

  function firstLink(links, predicate) {
    for (const link of links) {
      const href = absoluteHttpUrl(link.getAttribute("href"));
      if (href && predicate(link, href)) return href;
    }
    return null;
  }

  function parseMeta(card) {
    const searchMeta = card.querySelector(".literature-paper-top p")?.textContent;
    const reviewMeta = card.querySelector(".daily-paper-title-row + p")?.textContent;
    const raw = cleanText(searchMeta || reviewMeta, 2000);
    const separator = searchMeta ? /\s*·\s*/ : /\s*\/\s*/;
    const parts = raw.split(separator).map((part) => cleanText(part, 500)).filter(Boolean);
    const yearMatch = raw.match(/(?:19|20)\d{2}/);
    const authorsText = parts[0] && !/^(unknown authors|作者未知)$/i.test(parts[0]) ? parts[0] : "";
    return {
      authors: authorsText.split(/[,;，；]+/).map((name) => cleanText(name, 240)).filter(Boolean).slice(0, 50),
      year: yearMatch ? Number(yearMatch[0]) : null,
      venue: parts.length >= 3 ? parts.at(-1) : null,
    };
  }

  function titleFromCard(card) {
    return cleanText(
      card.querySelector(".daily-paper-title-markdown")?.textContent
        || card.querySelector("h3")?.textContent
        || card.querySelector("h2, h4")?.textContent,
      1000,
    );
  }

  function attributeText(element, name, maxLength = 160) {
    return cleanText(element?.getAttribute?.(name), maxLength);
  }

  function explicitPaperId(card) {
    return attributeText(card, YFR_PAPER_ID_ATTRIBUTE)
      || attributeText(card.querySelector?.(`[${YFR_PAPER_ID_ATTRIBUTE}]`), YFR_PAPER_ID_ATTRIBUTE);
  }

  function extractPaper(card, index = 0) {
    const title = titleFromCard(card);
    if (!title) return null;
    const links = Array.from(card.querySelectorAll("a[href]"));
    const doi = doiFromCard(card, links);
    const meta = parseMeta(card);
    const pdfLinks = links
      .map((link) => ({
        href: absoluteHttpUrl(link.getAttribute("href"), { preserveYfrPdfAccessKey: true }),
        text: cleanText(link.textContent, 120),
      }))
      .filter(({ href, text }) => href && !/doi\.org\//i.test(href) && (/pdf|下载本地|打开开放|全文/i.test(text) || /\.pdf(?:$|[?#])/i.test(href)));
    const articleUrl = firstLink(links, (link, href) => /原始链接|article|publisher|DOI/i.test(cleanText(link.textContent, 120)) || /doi\.org\//i.test(href));
    const cardText = cleanText(card.textContent, 30_000);
    const identity = explicitPaperId(card)
      || (doi ? `doi-${stableHash(doi)}` : `title-${stableHash(canonicalTitle(title))}`);
    return {
      id: identity || `paper-${index + 1}`,
      title,
      authors: meta.authors,
      year: meta.year,
      venue: meta.venue,
      publisher: null,
      doi: doi || null,
      url: articleUrl,
      pdfUrl: pdfLinks[0]?.href || null,
      pdfRemoteUrl: pdfLinks[1]?.href || null,
      pdfSource: pdfLinks.length ? "YFR page" : null,
      pdfAvailable: Boolean(pdfLinks.length || /有开放\s*PDF|下载本地\s*PDF|原文：\s*(?:可用|已缓存)|已下载到本地/i.test(cardText)),
      pdfCached: /下载本地\s*PDF|已缓存|已下载到本地/i.test(cardText),
      sources: [],
    };
  }

  function sourceFromPage() {
    const parts = location.pathname.split("/").filter(Boolean);
    const marker = document.querySelector?.(`[${YFR_SOURCE_AREA_ATTRIBUTE}]`);
    const markedArea = attributeText(marker, YFR_SOURCE_AREA_ATTRIBUTE, 40);
    const markedContextId = attributeText(marker, YFR_CONTEXT_ID_ATTRIBUTE);
    const exclusivePath = parts[0] === "admin" && parts[1] === "exclusive-review";
    const review = parts[0] === "daily-review" || exclusivePath;
    const area = ["literature-search", "daily-review", "exclusive-review"].includes(markedArea)
      ? markedArea
      : exclusivePath ? "exclusive-review" : review ? "daily-review" : "literature-search";
    const topic = cleanText(
      document.querySelector(review ? ".daily-output-head h2, .daily-review-export-area h2" : ".literature-result-head h2")?.textContent
        || document.title.replace(/\s*[|｜-].*$/, ""),
      500,
    );
    if (area === "literature-search") {
      return {
        type: "yfr-search",
        area,
        searchId: markedContextId || (parts[0] === "literature-search" && parts[1] ? parts[1] : null),
        topic,
        exclusivePage: false,
        sourceUrl: absoluteHttpUrl(location.href),
      };
    }
    const routeRunId = area === "exclusive-review"
      ? (parts[0] === "admin" && parts[1] === "exclusive-review" ? parts[3] : null)
      : (parts[0] === "daily-review" ? parts[2] : null);
    return {
      type: "yfr-review",
      area,
      runId: markedContextId || routeRunId || null,
      topic,
      exclusivePage: area === "exclusive-review",
      sourceUrl: absoluteHttpUrl(location.href),
    };
  }

  function currentRouteKey() {
    return `${location.origin}${location.pathname}${location.search}`;
  }

  function setSelected(id, selected) {
    if (selected) state.selected.add(id);
    else state.selected.delete(id);
    syncControls();
    renderToolbar();
  }

  function createInjectedControl(card, paper) {
    const host = document.createElement("span");
    host.setAttribute(CONTROL_ATTRIBUTE, "injected");
    host.style.cssText = "position:absolute;top:10px;left:10px;z-index:20;width:28px;height:28px;";
    if (getComputedStyle(card).position === "static") card.style.position = "relative";
    const root = host.attachShadow({ mode: "closed" });
    const style = document.createElement("style");
    style.textContent = `*{box-sizing:border-box}label{display:grid;place-items:center;width:28px;height:28px;border:2px solid #24211d;border-radius:4px;background:#fffdf8;box-shadow:2px 2px 0 #24211d;cursor:pointer}input{position:absolute;opacity:0;width:1px;height:1px}span{width:15px;height:15px;border:2px solid #75633e;border-radius:2px;background:#fff}input:checked+span{border-color:#b33b28;background:#b33b28;box-shadow:inset 0 0 0 3px #fff}input:focus-visible+span{outline:2px solid #245d65;outline-offset:2px}`;
    const label = document.createElement("label");
    label.title = `选择 ${paper.title}`;
    const input = document.createElement("input");
    input.type = "checkbox";
    input.setAttribute("aria-label", `选择 ${paper.title}`);
    const mark = document.createElement("span");
    input.addEventListener("change", () => setSelected(paper.id, input.checked));
    label.append(input, mark);
    root.append(style, label);
    card.prepend(host);
    return { input, cleanup: () => host.remove() };
  }

  function ensureControl(card, paper) {
    const existing = state.controls.get(card);
    if (existing?.paperId === paper.id && existing.input.isConnected) return;
    existing?.cleanup();
    const nativeInput = card.querySelector(".paper-download-checkbox input[type='checkbox']");
    if (nativeInput) {
      if (nativeInput.checked) state.selected.add(paper.id);
      const onChange = () => {
        if (!state.syncingNativeControls) setSelected(paper.id, nativeInput.checked);
      };
      nativeInput.addEventListener("change", onChange);
      nativeInput.setAttribute(CONTROL_ATTRIBUTE, "native");
      state.controls.set(card, {
        paperId: paper.id,
        input: nativeInput,
        native: true,
        cleanup: () => {
          nativeInput.removeEventListener("change", onChange);
          nativeInput.removeAttribute(CONTROL_ATTRIBUTE);
        },
      });
      return;
    }
    state.controls.set(card, { paperId: paper.id, native: false, ...createInjectedControl(card, paper) });
  }

  function cleanupStaleControls(cards) {
    for (const [card, control] of state.controls) {
      if (cards.has(card) && card.isConnected) continue;
      control.cleanup();
      state.controls.delete(card);
    }
  }

  function syncControls() {
    for (const control of state.controls.values()) {
      const selected = state.selected.has(control.paperId);
      if (!control.native || control.input.checked === selected) {
        control.input.checked = selected;
        continue;
      }
      state.syncingNativeControls = true;
      try {
        control.input.click();
      } finally {
        state.syncingNativeControls = false;
      }
    }
  }

  function scan() {
    if (!state.active) return;
    const routeKey = currentRouteKey();
    if (state.routeKey && routeKey !== state.routeKey) state.selected.clear();
    state.routeKey = routeKey;
    const cards = new Set(Array.from(document.querySelectorAll(CARD_SELECTOR)).slice(0, MAX_PAPERS));
    const papers = new Map();
    const duplicateIds = new Set();
    let index = 0;
    for (const card of cards) {
      const paper = extractPaper(card, index++);
      if (!paper) continue;
      if (papers.has(paper.id)) {
        duplicateIds.add(paper.id);
        continue;
      }
      papers.set(paper.id, paper);
      card.setAttribute(PAPER_ATTRIBUTE, paper.id);
      ensureControl(card, paper);
    }
    cleanupStaleControls(cards);
    state.papers = papers;
    state.duplicateIds = duplicateIds;
    syncControls();
    renderToolbar();
  }

  function scheduleScan() {
    clearTimeout(state.scanTimer);
    state.scanTimer = setTimeout(scan, 120);
  }

  function selectWhere(predicate) {
    state.selected = new Set(Array.from(state.papers.values()).filter(predicate).map((paper) => paper.id));
    syncControls();
    renderToolbar();
  }

  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function createToolbar() {
    const host = document.createElement("div");
    host.setAttribute("data-yfr-extension-toolbar", "true");
    host.style.cssText = "position:fixed;left:50%;bottom:14px;z-index:2147483647;width:min(920px,calc(100vw - 24px));transform:translateX(-50%);";
    const root = host.attachShadow({ mode: "closed" });
    const style = element("style");
    style.textContent = `:host{all:initial}*{box-sizing:border-box;letter-spacing:0}.bar{display:grid;grid-template-columns:auto minmax(130px,1fr) auto;gap:12px;align-items:center;padding:10px 12px;border:2px solid #24211d;border-radius:6px;background:rgba(255,253,248,.98);box-shadow:5px 5px 0 #24211d;color:#24211d;font:13px/1.35 "Microsoft YaHei","PingFang SC",sans-serif;backdrop-filter:blur(12px)}.brand{display:flex;align-items:center;gap:8px}.mark{font:900 20px/1 Georgia,serif}.mark b{color:#b33b28}.counts{min-width:0}.counts strong{font-size:14px}.counts span,.notice{display:block;color:#756f65;font-size:11px;overflow-wrap:anywhere}.actions{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:6px}button{min-height:34px;border:1px solid #24211d;border-radius:4px;padding:6px 9px;background:#fffdf8;color:#24211d;font:800 11px/1.2 "Microsoft YaHei","PingFang SC",sans-serif;cursor:pointer}button:hover{background:#f4ecd9}button.primary{background:#d7f36a;box-shadow:2px 2px 0 #24211d}button.exit{color:#8c2f22}button:disabled{cursor:not-allowed;opacity:.45}@media(max-width:680px){.bar{grid-template-columns:1fr;gap:8px}.brand{display:none}.actions{justify-content:flex-start}.actions button{flex:1 1 calc(33.333% - 6px)}.actions .primary{flex-basis:100%}}`;
    const bar = element("section", null, "bar");
    bar.setAttribute("aria-label", "YFR 文献下载选择工具");
    const brand = element("div", null, "brand");
    const mark = element("span", "YFR.", "mark");
    brand.append(mark);
    const counts = element("div", null, "counts");
    const count = element("strong", "0 篇已选");
    const detail = element("span", "正在识别当前页面文献...");
    const notice = element("span", "", "notice");
    counts.append(count, detail, notice);
    const actions = element("div", null, "actions");
    const addButton = (label, action, className = "") => {
      const button = element("button", label, className);
      button.type = "button";
      button.addEventListener("click", action);
      actions.append(button);
      return button;
    };
    addButton("全选", () => selectWhere(() => true));
    addButton("反选", () => selectWhere((paper) => !state.selected.has(paper.id)));
    addButton("仅选有 PDF", () => selectWhere((paper) => paper.pdfAvailable || paper.pdfCached || paper.pdfUrl || paper.pdfRemoteUrl));
    addButton("清空", () => selectWhere(() => false));
    addButton("退出", disable, "exit");
    const send = addButton("推送到扩展", sendSelection, "primary");
    bar.append(brand, counts, actions);
    root.append(style, bar);
    document.documentElement.append(host);
    const spacer = document.createElement("div");
    spacer.setAttribute("data-yfr-extension-toolbar-spacer", "true");
    spacer.setAttribute("aria-hidden", "true");
    spacer.style.cssText = "display:block;width:1px;height:120px;pointer-events:none;";
    document.body?.append(spacer);
    state.toolbarHost = host;
    state.toolbarSpacer = spacer;
    state.toolbar = { bar, actions, count, detail, notice, send };
  }

  function renderToolbar() {
    if (!state.toolbar) return;
    const selectedCount = Array.from(state.papers.keys()).filter((id) => state.selected.has(id)).length;
    const pdfCount = Array.from(state.papers.values()).filter((paper) => paper.pdfAvailable || paper.pdfCached || paper.pdfUrl || paper.pdfRemoteUrl).length;
    state.toolbar.count.textContent = `${selectedCount} 篇已选`;
    state.toolbar.detail.textContent = state.duplicateIds.size
      ? `当前页面存在 ${state.duplicateIds.size} 个重复 YFR 编号，请刷新页面`
      : `当前页面 ${state.papers.size} 篇 · ${pdfCount} 篇已有 PDF 候选`;
    state.toolbar.notice.textContent = state.notice;
    state.toolbar.send.disabled = selectedCount < 1 || state.duplicateIds.size > 0;
    requestAnimationFrame(() => {
      if (!state.toolbarHost || !state.toolbarSpacer) return;
      state.toolbarSpacer.style.height = `${Math.ceil(state.toolbarHost.getBoundingClientRect().height + 32)}px`;
    });
  }

  async function sendSelection() {
    const papers = Array.from(state.papers.values()).filter((paper) => state.selected.has(paper.id));
    if (!papers.length) return;
    if (state.duplicateIds.size) {
      state.notice = "页面存在重复的 YFR 文献编号，已停止发送；请刷新页面后重试";
      renderToolbar();
      return;
    }
    const paperIds = papers.map((paper) => paper.id);
    state.notice = "正在推送到 Polaris 扩展...";
    renderToolbar();
    try {
      const response = await chrome.runtime.sendMessage({
        type: IMPORT_TYPE,
        pageOrigin: location.origin,
        payload: {
          version: 1,
          source: sourceFromPage(),
          selectedCount: papers.length,
          paperIds,
          papers,
          createdAt: new Date().toISOString(),
        },
      });
      state.notice = response?.ok ? (response.message || `已推送 ${papers.length} 篇文献`) : (response?.error || "Polaris 扩展未接收任务");
    } catch (error) {
      state.notice = error instanceof Error ? error.message : "Polaris 扩展暂时不可用";
    }
    renderToolbar();
  }

  function openReviewEvidenceTab() {
    if (document.querySelector(CARD_SELECTOR)) return;
    const button = Array.from(document.querySelectorAll(".daily-tabs button, [role='tablist'] button"))
      .find((item) => cleanText(item.textContent, 80) === "文献证据");
    button?.click();
  }

  function enable() {
    if (state.active) return;
    state.active = true;
    state.routeKey = currentRouteKey();
    state.notice = "";
    openReviewEvidenceTab();
    if (!state.toolbarHost) createToolbar();
    state.observer = new MutationObserver(scheduleScan);
    state.observer.observe(document.documentElement, { childList: true, subtree: true });
    state.routeTimer = setInterval(() => {
      if (currentRouteKey() !== state.routeKey) scheduleScan();
    }, 750);
    scan();
  }

  function disable() {
    state.active = false;
    state.observer?.disconnect();
    state.observer = null;
    clearInterval(state.routeTimer);
    clearTimeout(state.scanTimer);
    for (const [card, control] of state.controls) {
      control.cleanup();
      card.removeAttribute(PAPER_ATTRIBUTE);
    }
    state.controls.clear();
    state.papers.clear();
    state.duplicateIds.clear();
    state.selected.clear();
    state.toolbarHost?.remove();
    state.toolbarSpacer?.remove();
    state.toolbarHost = null;
    state.toolbarSpacer = null;
    state.toolbar = null;
  }

  function toggle() {
    if (state.active) disable();
    else enable();
    return { ok: true, active: state.active, count: state.papers.size };
  }

  globalThis.YfrPageSelectionCore = Object.freeze({
    canonicalDoi,
    canonicalTitle,
    extractPaper,
    isActive: () => state.active,
    layoutMetrics: () => {
      const hostRect = state.toolbarHost?.getBoundingClientRect();
      return {
        viewportWidth: document.documentElement.clientWidth,
        documentScrollWidth: document.documentElement.scrollWidth,
        hostLeft: hostRect?.left ?? null,
        hostRight: hostRect?.right ?? null,
        barClientWidth: state.toolbar?.bar.clientWidth ?? null,
        barScrollWidth: state.toolbar?.bar.scrollWidth ?? null,
        actionsClientWidth: state.toolbar?.actions.clientWidth ?? null,
        actionsScrollWidth: state.toolbar?.actions.scrollWidth ?? null,
      };
    },
    paperCount: () => state.papers.size,
    duplicateCount: () => state.duplicateIds.size,
    paperIds: () => Array.from(state.papers.keys()),
    scan,
    selectAll: () => selectWhere(() => true),
    selectAvailable: () => selectWhere((paper) => paper.pdfAvailable || paper.pdfCached || paper.pdfUrl || paper.pdfRemoteUrl),
    selectedCount: () => Array.from(state.papers.keys()).filter((id) => state.selected.has(id)).length,
    sendSelection,
    sourceFromPage,
    stableHash,
    toggle,
  });

  if (typeof chrome !== "undefined" && chrome.runtime?.onMessage) {
    chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
      if (message?.type !== MESSAGE_TYPE) return false;
      sendResponse(toggle());
      return false;
    });
  }

  if (typeof document !== "undefined" && typeof location !== "undefined") {
    const startFromQuery = () => {
      if (new URLSearchParams(location.search).get("yfr-download") === "1") enable();
    };
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", startFromQuery, { once: true });
    else startFromQuery();
  }
})();
