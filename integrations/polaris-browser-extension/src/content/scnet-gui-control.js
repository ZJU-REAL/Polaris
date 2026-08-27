(() => {
  const SCNET_ORIGIN = "https://www.scnet.cn";
  const JOB_LIST_PATH = "/acx/jobgather/jobs/monitor/page-list";
  const USER_INFO_PATH = "/acx/user/users/current-user-info?includeToken=true&refresh=true";
  const CLUSTER_INFO_PATH = "/acx/user/users/cluster-user-detail";
  const GUI_TEMPLATE_PATH = "/acx/appcenter/apptemplates/BASIC/GUI/template";
  const GUI_SUBMIT_PATH = "/acx/jobmgt/job/submit";
  const AES_KEY = "SugonGridview123";
  const MAX_RUNNING_JOBS = 100;
  const GUI_START_ATTEMPTS = 60;
  const GUI_START_INTERVAL_MS = 2000;
  const pendingLaunches = new Map();

  function text(value, max = 255) {
    return String(value == null ? "" : value).trim().slice(0, max);
  }

  function digits(value) {
    const normalized = text(value, 64);
    return /^\d{1,32}$/.test(normalized) ? normalized : "";
  }

  function requestJobId(payload) {
    const direct = digits(payload?.job_id || payload?.jobId);
    if (direct) return direct;
    const sessionId = text(payload?.scnet_session_id || payload?.scnetSessionId, 255);
    const match = sessionId.match(/--(\d{1,32})--/);
    return match?.[1] || "";
  }

  function workspaceName(value) {
    const parts = text(value, 1024).replace(/\\/g, "/").split("/").filter(Boolean);
    return parts.at(-1) || "";
  }

  function safeWorkspace(value) {
    const normalized = text(value, 1024).replace(/\\/g, "/").replace(/\/+$/, "");
    if (!/^\/public\/home\/[A-Za-z0-9._-]+(?:\/[A-Za-z0-9._-]+)*$/.test(normalized)) {
      throw new Error("SCNET_GUI_WORKSPACE_INVALID");
    }
    return normalized;
  }

  function portalDescriptor(payload) {
    const templateId = digits(payload?.template_id || payload?.templateId);
    const portalUrl = text(payload?.portal_url || payload?.portalUrl, 2048);
    if (!templateId || !portalUrl) throw new Error("SCNET_GUI_TEMPLATE_CONTEXT_MISSING");
    let parsed;
    try { parsed = new URL(portalUrl); } catch { throw new Error("SCNET_GUI_TEMPLATE_CONTEXT_INVALID"); }
    if (parsed.origin !== SCNET_ORIGIN) throw new Error("SCNET_GUI_TEMPLATE_CONTEXT_INVALID");
    const hashQuery = parsed.hash.includes("?") ? parsed.hash.slice(parsed.hash.indexOf("?") + 1) : "";
    const portalTemplate = new URLSearchParams(hashQuery).get("template") || "";
    const parts = portalTemplate.split("/");
    if (parts.length !== 2 || parts.some((part) => !/^[A-Za-z0-9_-]{1,64}$/.test(part))) {
      throw new Error("SCNET_GUI_TEMPLATE_CONTEXT_INVALID");
    }
    return {
      templateId,
      portalTemplate: parts.join("/"),
      appName: parts[1].toUpperCase(),
    };
  }

  function safeControlOrigin(value) {
    let parsed;
    try { parsed = new URL(String(value || "")); } catch { return null; }
    if (parsed.protocol !== "https:") return null;
    if (parsed.hostname === "scnet.cn" || parsed.hostname.endsWith(".scnet.cn")
      || parsed.hostname === "hpccube.com" || parsed.hostname.endsWith(".hpccube.com")) {
      return parsed.origin;
    }
    return null;
  }

  async function request(path, options = {}) {
    const response = await fetch(path, {
      credentials: "include",
      cache: "no-store",
      headers: {
        Accept: "application/json",
        ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...(options.headers || {}),
      },
      ...options,
    });
    if (!response.ok) throw new Error(`SCNET_GUI_HTTP_${response.status}`);
    const json = await response.json();
    if (String(json?.code ?? "0") !== "0") {
      throw new Error(text(json?.msg || "SCNET_GUI_API_FAILED", 300));
    }
    return json;
  }

  function jobQuery(jobId) {
    return {
      strJobId: jobId,
      strJobOwner: "",
      strJobName: "",
      strJobStat: "",
      appType: "",
      strClusterIDList: "",
      strClusterNameList: "",
      clusterId: "",
      showGroupJobs: false,
      userName: "",
      taskType: "HPC",
      appTypes: [],
      statuses: ["statR"],
      clusterIds: [],
      tagIds: [],
      strQueueName: "",
      orderBy: "jobSubmitTime",
      page: 1,
      size: MAX_RUNNING_JOBS,
      order: "DESC",
    };
  }

  function publicStatus(record) {
    const vnc = record?.jobVncSessionInfo || null;
    const running = record?.jobStatus === "statR";
    const sessionReady = running && Boolean(vnc?.strSessionID && vnc?.strServerName);
    const jobId = digits(record?.jobId || vnc?.strRelateJobID) || null;
    const appKey = text(record?.appType, 64)
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "") || "scnet";
    const sessionId = text(vnc?.sessionId, 255) || (jobId ? `${appKey}--${jobId}--gui` : null);
    return {
      ok: true,
      state: sessionReady ? "online" : running ? "starting" : "not_found",
      job_id: jobId,
      scnet_session_id: sessionId,
      template_version: text(record?.appVersionName, 255) || null,
      app_type: text(record?.appType, 100) || null,
      started_at: text(record?.jobStartTime, 64) || null,
    };
  }

  async function resolveRunningJob(payload) {
    const requestedJobId = requestJobId(payload);
    const response = await request(JOB_LIST_PATH, {
      method: "POST",
      body: JSON.stringify(jobQuery(requestedJobId)),
    });
    const records = Array.isArray(response?.data?.records) ? response.data.records : [];
    const requestedSession = text(payload?.scnet_session_id || payload?.scnetSessionId, 255);
    const requestedWorkspace = text(payload?.workspace, 1024).replace(/\\/g, "/").replace(/\/$/, "");
    const requestedName = workspaceName(requestedWorkspace);
    const matches = records.filter((record) => {
      const vnc = record?.jobVncSessionInfo || {};
      if (requestedJobId) return digits(record?.jobId || vnc?.strRelateJobID) === requestedJobId;
      if (requestedSession && text(vnc?.sessionId, 255) === requestedSession) return true;
      const workdir = text(record?.workDir, 1024).replace(/\\/g, "/").replace(/\/$/, "");
      if (requestedWorkspace && workdir) return workdir === requestedWorkspace;
      return Boolean(requestedWorkspace && requestedName
        && !workdir && text(record?.jobName, 255) === requestedName);
    });
    if (matches.length > 1 && !requestedJobId && !requestedSession && !requestedWorkspace) {
      throw new Error("SCNET_GUI_SESSION_AMBIGUOUS");
    }
    return matches[0] || null;
  }

  async function status(payload = {}) {
    const record = await resolveRunningJob(payload);
    return record ? publicStatus(record) : {
      ok: true,
      state: "not_found",
      job_id: requestJobId(payload) || null,
      scnet_session_id: text(payload?.scnet_session_id, 255) || null,
      template_version: null,
      app_type: null,
      started_at: null,
    };
  }

  function parseServiceList(value) {
    if (Array.isArray(value)) return value;
    if (typeof value !== "string" || !value.trim()) return [];
    try {
      const parsed = JSON.parse(value);
      return Array.isArray(parsed) ? parsed : [];
    } catch {
      return [];
    }
  }

  async function clusterContext(clusterId) {
    const [userInfo, clusterInfo] = await Promise.all([
      request(USER_INFO_PATH),
      request(CLUSTER_INFO_PATH),
    ]);
    const tokenEntry = (Array.isArray(userInfo?.data?.tokenList) ? userInfo.data.tokenList : [])
      .find((entry) => digits(entry?.clusterId) === clusterId);
    const cluster = (Array.isArray(clusterInfo?.data) ? clusterInfo.data : [])
      .find((entry) => digits(entry?.clusterId) === clusterId);
    const services = [
      ...parseServiceList(cluster?.params?.sothisai_service),
      ...parseServiceList(cluster?.params?.gridview_service),
    ];
    const service = services.find((entry) => entry?.enable === true || String(entry?.enable).toLowerCase() === "true");
    const serviceOrigin = safeControlOrigin(service?.url);
    const token = text(tokenEntry?.token, 4096);
    if (!cluster || !serviceOrigin || !token) throw new Error("SCNET_GUI_CONTROL_CONTEXT_UNAVAILABLE");
    return {
      serviceOrigin,
      token,
      clusterUser: text(cluster?.clusterUserName || cluster?.userName, 255),
      jobManagerId: digits(cluster?.params?.jobManagerID || cluster?.params?.jobManagerId),
    };
  }

  async function createGuiJob(payload) {
    const clusterId = digits(payload?.region_key || payload?.regionKey);
    if (!clusterId) throw new Error("SCNET_GUI_CLUSTER_MISSING");
    const workspace = safeWorkspace(payload?.workspace);
    const descriptor = portalDescriptor(payload);
    const templateResponse = await request(
      `${GUI_TEMPLATE_PATH}?${new URLSearchParams({
        templateName: "template_last",
        ossName: descriptor.appName,
      })}`,
    );
    const template = templateResponse?.data?.template;
    if (!template || typeof template !== "object") throw new Error("SCNET_GUI_TEMPLATE_SETUP_REQUIRED");
    if (text(template.GAP_APPNAME, 64).toUpperCase() !== descriptor.appName) {
      throw new Error("SCNET_GUI_TEMPLATE_APP_MISMATCH");
    }
    if (digits(template.GAP_CLUSTER_ID || template.clusterId) !== clusterId) {
      throw new Error("SCNET_GUI_TEMPLATE_CLUSTER_MISMATCH");
    }
    const expectedVersion = text(payload?.template_version || payload?.templateVersion, 255);
    const displayVersion = text(template.GAP_APP_VERSION_LABEL, 255);
    if (expectedVersion && displayVersion && expectedVersion !== displayVersion) {
      throw new Error("SCNET_GUI_TEMPLATE_VERSION_MISMATCH");
    }
    const context = await clusterContext(clusterId);
    if (!context.jobManagerId) throw new Error("SCNET_GUI_JOB_MANAGER_MISSING");
    const sessionKey = text(payload?.session_id || payload?.sessionId, 128).replace(/[^A-Za-z0-9]/g, "");
    const jobName = `POLARIS_GUI_${sessionKey.slice(0, 16) || Date.now().toString(36)}`.slice(0, 32);
    const mapAppJobInfo = {
      ...template,
      GAP_JOB_NAME: jobName,
      GAP_WORK_DIR: workspace,
      GAP_NNODE: "1",
      GAP_PPN: "1",
      GAP_WALL_TIME: "01:00:00",
      locale: "zh",
      appId: descriptor.templateId,
      submitWay: "gui",
      template: descriptor.portalTemplate,
    };
    const submitted = await request(GUI_SUBMIT_PATH, {
      method: "POST",
      body: JSON.stringify({
        strJobManagerId: context.jobManagerId,
        mapAppJobInfo,
        clusterId,
        appType: "BASIC",
        appName: "GUI",
        jobType: "",
        jobTypeId: "",
        appVersion: text(template.GAP_APP_VERSION, 255),
        displayVersion: displayVersion || expectedVersion,
        testAreaId: "",
      }),
    });
    const jobId = digits(submitted?.data);
    if (!jobId) throw new Error("SCNET_GUI_SUBMIT_JOB_ID_MISSING");
    return jobId;
  }

  async function waitForGuiJob(jobId) {
    for (let attempt = 0; attempt < GUI_START_ATTEMPTS; attempt += 1) {
      const record = await resolveRunningJob({ job_id: jobId });
      if (record && publicStatus(record).state === "online") return record;
      if (typeof setTimeout === "function") {
        await new Promise((resolve) => setTimeout(resolve, GUI_START_INTERVAL_MS));
      }
    }
    throw new Error("SCNET_GUI_SESSION_START_TIMEOUT");
  }

  async function ensureRunningJob(payload) {
    const existing = await resolveRunningJob(payload);
    if (existing) return existing;
    const launchKey = text(payload?.session_id || payload?.workspace, 1024);
    if (!launchKey) throw new Error("SCNET_GUI_SESSION_CONTEXT_MISSING");
    if (!pendingLaunches.has(launchKey)) {
      pendingLaunches.set(launchKey, (async () => {
        const jobId = await createGuiJob(payload);
        return waitForGuiJob(jobId);
      })().finally(() => pendingLaunches.delete(launchKey)));
    }
    return pendingLaunches.get(launchKey);
  }

  async function encryptVncPassword(password) {
    const encoder = new TextEncoder();
    const raw = encoder.encode(text(password, 64));
    if (!raw.length || raw.length >= 16) throw new Error("SCNET_GUI_PASSWORD_FORMAT_UNSUPPORTED");
    const key = await crypto.subtle.importKey(
      "raw",
      encoder.encode(AES_KEY),
      { name: "AES-CBC" },
      false,
      ["encrypt"],
    );
    const encrypted = await crypto.subtle.encrypt(
      { name: "AES-CBC", iv: new Uint8Array(16) },
      key,
      raw,
    );
    return btoa(String.fromCharCode(...new Uint8Array(encrypted)));
  }

  async function launch(payload = {}) {
    const record = await ensureRunningJob(payload);
    const session = record.jobVncSessionInfo || {};
    const state = publicStatus(record);
    if (state.state !== "online") throw new Error("SCNET_GUI_SESSION_STARTING");
    const clusterId = digits(record.clusterId);
    if (!clusterId) throw new Error("SCNET_GUI_CLUSTER_MISSING");
    const context = await clusterContext(clusterId);
    const hostName = text(session.strServerName, 255);
    const sid = digits(session.strSessionID);
    const jobId = state.job_id;
    const vncUser = context.clusterUser || text(session.strSessionOwner, 255);
    if (!hostName || !sid || !jobId || !vncUser) throw new Error("SCNET_GUI_SESSION_INCOMPLETE");
    const params = new URLSearchParams({
      hostName,
      sid,
      vncpasswd: await encryptVncPassword(session.loginPasswd),
      vncUser,
      clusterId,
      jobId,
    });
    const response = await request(
      `${context.serviceOrigin}/acx/desktopagent/vncs/launchNoVnc?${params}`,
      {
        credentials: "omit",
        headers: { token: context.token, version: "2.3.3" },
      },
    );
    const relativeUrl = text(response?.data?.novncUrl, 8192);
    if (!relativeUrl) throw new Error("SCNET_GUI_SHORT_LINK_MISSING");
    const launchUrl = new URL(relativeUrl, context.serviceOrigin);
    if (launchUrl.origin !== context.serviceOrigin) throw new Error("SCNET_GUI_SHORT_LINK_ORIGIN_MISMATCH");
    launchUrl.searchParams.set("clusterToken", context.token);
    launchUrl.searchParams.set("clusterId", clusterId);
    launchUrl.searchParams.set("user", vncUser);
    launchUrl.searchParams.set("language", "zh");
    launchUrl.searchParams.set("name", text(record.jobName || record.appType || "SCNet GUI", 255));
    launchUrl.searchParams.set("jobId", jobId);
    launchUrl.searchParams.set("from", SCNET_ORIGIN);
    return { ...state, launch_url: launchUrl.toString() };
  }

  globalThis.PolarisScnetGuiControl = Object.freeze({ status, launch });
})();
