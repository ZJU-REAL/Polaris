(() => {
  const SNAPSHOT = "polaris:scnet-snapshot:v1";
  const PROBE = "polaris:scnet-probe:v1";
  const REFRESH = "polaris:scnet-refresh:v1";
  const DISCOVERY_REQUEST = "POLARIS_SCNET_DISCOVERY_REQUEST:v1";
  const DISCOVERY_ACK = "POLARIS_SCNET_DISCOVERY_ACK:v1";
  const DISCOVERY_STATUS = "POLARIS_SCNET_DISCOVERY_STATUS:v1";
  const DISCOVERY_REFRESH = "POLARIS_SCNET_DISCOVERY_REFRESH:v1";
  const DISCOVERY_REFRESH_ACK = "POLARIS_SCNET_DISCOVERY_REFRESH_ACK:v1";
  const READY = "polaris:scnet-ready:v1";
  const MAX_ITEMS = 500;
  const VERSION_CONCURRENCY = 4;
  const NONCE = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;

  const text = (value, max = 255) => String(value == null ? "" : value).replace(/\s+/g, " ").trim().slice(0, max);
  const bool = (value) => value === true || value === 1 || String(value).toLowerCase() === "true";
  const first = (...values) => values.find((value) => value != null && String(value).trim() !== "");

  // SCNet has returned several equivalent entitlement fields across portal
  // versions.  Version discovery must not depend on one response spelling.
  function isSubscribed(item) {
    if (bool(item.subscribed)) return true;
    const value = text(first(
      item.subscriptionStatus,
      item.entitlementStatus,
      item.goodsStatus,
      item.licenseStatus,
      item.status,
    ), 48).toLowerCase();
    return ["subscribed", "enabled", "enable", "in_use", "in-use", "active", "opened", "已订阅", "已启用"].includes(value);
  }

  async function request(path, options = {}) {
    const response = await fetch(path, {
      credentials: "include",
      cache: "no-store",
      headers: { Accept: "application/json", ...(options.body ? { "Content-Type": "application/json" } : {}) },
      ...options,
    });
    if (!response.ok) throw new Error(`SCNet 模板接口 HTTP ${response.status}`);
    return response.json();
  }

  function objectChildren(value, depth = 0) {
    if (depth > 5 || value == null || typeof value !== "object") return [];
    const output = [value];
    if (Array.isArray(value)) {
      for (const item of value) output.push(...objectChildren(item, depth + 1));
    } else {
      for (const item of Object.values(value)) output.push(...objectChildren(item, depth + 1));
    }
    return output;
  }

  function submitModes(item) {
    const raw = first(item.submitModes, item.submitWay, item.submitType, item.submit_mode);
    const values = Array.isArray(raw) ? raw : String(raw || "").split(/[,_/| ]+/);
    const normalized = values.map((value) => String(value).toLowerCase())
      .filter((value) => ["gui", "template", "cmd", "url"].includes(value));
    if (normalized.length) return [...new Set(normalized)].slice(0, 4);
    return item.template || item.templateName || item.templatePath ? ["gui", "template"] : ["url"];
  }

  function normalizeItem(item) {
    const templateId = first(item.templateId, item.softId, item.appId, item.id);
    if (!templateId || !/^\d{1,20}$/.test(String(templateId))) return null;
    const template = text(first(item.template, item.templateName, item.templatePath, item.path), 160);
    const modes = submitModes(item);
    const subscribed = isSubscribed(item);
    const entitlement = text(first(item.entitlementStatus, item.goodsStatus, item.licenseStatus, item.status), 48).toLowerCase() || "unknown";
    return {
      template_id: text(templateId, 100),
      region_key: text(first(item.regionKey, item.region, item.clusterId), 255) || null,
      submit_mode: modes[0] || "url",
      source: "browser_bridge",
      status: subscribed ? "subscribed" : "catalogued",
      evidence: {
        subscription_status: subscribed ? "subscribed" : "not_subscribed",
        entitlement_status: entitlement,
        subscription_source: subscribed
          ? (item.subscribed ? "subscribed" : "status_field")
          : "status_field",
        display_name: text(first(item.displayName, item.appName, item.name, item.title), 255),
        template,
        submit_modes: modes,
        version: text(first(item.version, item.appVersion, item.currentVersion), 120) || null,
        gui_ready: Boolean(item.usability === "in_use" || item.guiReady === true),
        source_nonce: NONCE,
      },
    };
  }

  function collectSnapshots(responses) {
    const byKey = new Map();
    for (const response of responses) {
      for (const candidate of objectChildren(response)) {
        const normalized = normalizeItem(candidate);
        if (!normalized) continue;
        const key = `${normalized.template_id}:${normalized.region_key || ""}:${normalized.submit_mode}`;
        if (!byKey.has(key)) byKey.set(key, normalized);
      }
    }
    return [...byKey.values()].slice(0, MAX_ITEMS);
  }

  function templateApps(response) {
    const rows = response?.data?.apps;
    return Array.isArray(rows) ? rows.filter((item) => item && typeof item === "object") : [];
  }

  function normalizeVersionResponse(response) {
    const roots = Array.isArray(response?.data) ? response.data : [];
    // Cluster-level rows contain the executable and environment evidence. Keep
    // them ahead of the aggregate row so readiness never loses that evidence.
    const candidates = roots.flatMap((row) => [
      ...(Array.isArray(row?.versionList) ? row.versionList : []),
      row,
    ]);
    const records = [];
    const seen = new Set();
    for (const row of candidates) {
      if (!row || typeof row !== "object") continue;
      const versionId = text(first(row.version, row.versionId, row.id), 160);
      const versionLabel = text(first(
        row.disPlayVersion,
        row.displayVersion,
        row.versionName,
        versionId,
      ), 120);
      if (!versionId && !versionLabel) continue;
      const clusterId = text(first(row.clusterId, row.clusterKey), 100) || null;
      const key = `${versionId}:${versionLabel}:${clusterId || ""}`;
      if (seen.has(key)) continue;
      seen.add(key);
      records.push({
        version_id: versionId || versionLabel,
        version_label: versionLabel || versionId,
        cluster_id: clusterId,
        resource_id: text(row.resourceId, 160) || null,
        usability: text(row.usability, 48).toLowerCase() || "unknown",
        package_status: text(row.packageStatus, 48).toLowerCase() || "unknown",
        default_version: bool(row.defaultVersion),
        expired: bool(row.expireVersion),
        program_available: Boolean(row.program),
        environment_available: Boolean(row.env),
      });
    }
    const versions = [...new Set(records.map((row) => row.version_label).filter(Boolean))]
      .slice(0, 50);
    const allClusters = new Set();
    const blockedClusters = new Set();
    for (const row of roots) {
      for (const clusterId of Object.keys(row?.resourceMap || {})) allClusters.add(text(clusterId, 100));
      for (const clusterId of row?.notOpenClusterIds || []) blockedClusters.add(text(clusterId, 100));
    }
    const availableClusters = [...allClusters].filter((value) => value && !blockedClusters.has(value));
    const defaultRecord = records.find((row) => row.default_version && !row.expired)
      || records.find((row) => row.usability === "in_use" && !row.expired)
      || records.find((row) => !row.expired)
      || records[0];
    return {
      versions,
      version_records: records.slice(0, 100),
      default_version: defaultRecord?.version_label || versions[0] || null,
      selected_version: defaultRecord?.version_label || versions[0] || null,
      selected_version_id: defaultRecord?.version_id || null,
      available_cluster_ids: availableClusters.slice(0, 100),
      blocked_cluster_ids: [...blockedClusters].filter(Boolean).slice(0, 100),
    };
  }

  async function collectSubscribedVersions(apps) {
    const subscribed = apps.filter(isSubscribed).slice(0, MAX_ITEMS);
    const versions = new Map();
    let cursor = 0;
    async function worker() {
      while (cursor < subscribed.length) {
        const item = subscribed[cursor++];
        const templateId = text(first(item.templateId, item.softId, item.id), 100);
        if (!templateId || !/^\d{1,20}$/.test(templateId)) continue;
        const mode = submitModes(item)[0] || "gui";
        try {
          const response = await request("/acx/appcenter/userapp/version", {
            method: "POST",
            body: JSON.stringify({ softId: templateId, submitType: mode }),
          });
          const evidence = normalizeVersionResponse(response);
          if (evidence.versions.length) versions.set(templateId, evidence);
        } catch {
          // 单个模板版本接口失败不影响其余能力快照；后端会保持版本门禁未通过。
        }
      }
    }
    await Promise.all(Array.from(
      { length: Math.min(VERSION_CONCURRENCY, Math.max(1, subscribed.length)) },
      () => worker(),
    ));
    return versions;
  }

  function mergeVersionEvidence(snapshots, versions) {
    return snapshots.map((snapshot) => {
      const versionEvidence = versions.get(snapshot.template_id);
      if (!versionEvidence?.versions?.length) return snapshot;
      return {
        ...snapshot,
        evidence: {
          ...snapshot.evidence,
          version: versionEvidence.selected_version,
          versions: versionEvidence.versions,
          version_records: versionEvidence.version_records,
          default_version: versionEvidence.default_version,
          selected_version: versionEvidence.selected_version,
          selected_version_id: versionEvidence.selected_version_id,
          available_cluster_ids: versionEvidence.available_cluster_ids,
          blocked_cluster_ids: versionEvidence.blocked_cluster_ids,
          version_source: "browser_userapp_version",
        },
      };
    });
  }

  async function collect() {
    const responses = [];
    const calls = [
      request("/acx/appcenter/userapp/template-apps", { method: "POST", body: "{}" }),
      request("/acx/appcenter/clusterapp/installed-cmd-versions"),
    ];
    const settled = await Promise.allSettled(calls);
    for (const result of settled) if (result.status === "fulfilled") responses.push(result.value);
    const apps = responses.flatMap((response) => templateApps(response));
    const snapshots = mergeVersionEvidence(
      collectSnapshots(responses),
      await collectSubscribedVersions(apps),
    );
    if (!snapshots.length) throw new Error("当前 SCNet 页面没有可读取的模板状态");
    return snapshots;
  }

  function collectGuiManifest() {
    let value = window.__POLARIS_SCNET_GUI_MANIFEST__;
    if (!Array.isArray(value)) {
      const node = document.querySelector('meta[name="polaris-scnet-manifest"], script[data-polaris-scnet-manifest]');
      if (node) {
        try { value = JSON.parse(node.content || node.textContent || ""); } catch { value = null; }
      }
    }
    if (!Array.isArray(value)) throw new Error("SCNet 当前页面未公开可核验的工作区清单");
    return value.slice(0, 10000).map((file) => ({
      path: String(file?.path || "").replace(/\\/g, "/").replace(/^\/+/, ""),
      size: Number.isFinite(Number(file?.size)) ? Math.max(0, Number(file.size)) : 0,
      mtime_ns: Number.isFinite(Number(file?.mtime_ns)) ? Math.max(0, Number(file.mtime_ns)) : 0,
      sha256: String(file?.sha256 || "").toLowerCase(),
      media_type: file?.media_type == null ? null : String(file.media_type).slice(0, 128),
    }));
  }

  async function sync(reason = "page-ready", options = {}) {
    try {
      const snapshots = await collect();
      const result = await chrome.runtime.sendMessage({
        type: "SCNET_SYNC_SNAPSHOT",
        pageOrigin: location.origin,
        reason,
        nonce: NONCE,
        accountFingerprint: options.accountFingerprint || null,
        purpose: options.purpose || "planning",
        snapshots,
      });
      const detail = { ...result, count: snapshots.length, reason };
      document.dispatchEvent(new CustomEvent(SNAPSHOT, { detail }));
      document.dispatchEvent(new CustomEvent(DISCOVERY_STATUS, {
        detail: { ok: true, phase: "snapshot_saved", count: snapshots.length, reason },
      }));
      document.dispatchEvent(new CustomEvent(options.ackEvent || DISCOVERY_ACK, {
        detail,
      }));
      return detail;
    } catch (error) {
      const detail = { ok: false, reason, error: error instanceof Error ? error.message : "SCNet 模板同步失败" };
      document.dispatchEvent(new CustomEvent(SNAPSHOT, {
        detail,
      }));
      document.dispatchEvent(new CustomEvent(DISCOVERY_STATUS, { detail: { ...detail, phase: "failed" } }));
      document.dispatchEvent(new CustomEvent(options.ackEvent || DISCOVERY_ACK, { detail }));
      return detail;
    }
  }

  document.addEventListener(PROBE, () => document.dispatchEvent(new CustomEvent(READY)));
  document.addEventListener(REFRESH, () => void sync("manual-refresh"));
  document.addEventListener(DISCOVERY_REQUEST, (event) => {
    const detail = event?.detail && typeof event.detail === "object" ? event.detail : {};
    void sync("discovery-request", {
      accountFingerprint: detail.account_fingerprint || detail.accountFingerprint || null,
      purpose: detail.purpose || "planning",
      ackEvent: DISCOVERY_ACK,
    });
  });
  document.addEventListener(DISCOVERY_REFRESH, () => {
    void sync("discovery-refresh", { ackEvent: DISCOVERY_REFRESH_ACK });
  });
  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type !== "SCNET_REFRESH") return undefined;
    void sync("manual-refresh", { purpose: message.purpose || "planning" }).then(sendResponse);
    return true;
  });
  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type !== "SCNET_GUI_COLLECT") return undefined;
    try {
      sendResponse({ ok: true, files: collectGuiManifest(), scnet_session_id: NONCE });
    } catch (error) {
      sendResponse({ ok: false, error: error instanceof Error ? error.message : "工作区清单读取失败" });
    }
    return true;
  });
  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (!["SCNET_GUI_STATUS", "SCNET_GUI_LAUNCH"].includes(message?.type)) return undefined;
    const control = globalThis.PolarisScnetGuiControl;
    if (!control) {
      sendResponse({ ok: false, error: "SCNET_GUI_CONTROL_UNAVAILABLE" });
      return true;
    }
    const operation = message.type === "SCNET_GUI_LAUNCH" ? control.launch : control.status;
    void operation(message.payload || {})
      .then(sendResponse)
      .catch((error) => sendResponse({
        ok: false,
        error: error instanceof Error ? error.message : "SCNET_GUI_CONTROL_FAILED",
      }));
    return true;
  });
  document.dispatchEvent(new CustomEvent(READY));
  void sync();
})();
