(() => {
  const TASK = "polaris:download-task:v1";
  const ACK = "polaris:download-task-ack:v1";
  const PROBE = "polaris:download-extension-probe:v1";
  const READY = "polaris:download-extension-ready:v1";
  const BATCH = "polaris:download-batch:v2";
  const BATCH_ACK = "polaris:download-batch-ack:v2";
  const AUTHORIZE = "polaris:extension-authorize:v1";
  const AUTHORIZE_ACK = "polaris:extension-authorize-ack:v1";
  const SCNET_CONTEXT = "polaris:scnet-context:v1";
  const SCNET_REFRESH = "polaris:scnet-refresh:v1";
  const SCNET_GUI_OPEN = "polaris:scnet-gui-open:v1";
  const SCNET_GUI_STATUS = "polaris:scnet-gui-status:v1";
  const SCNET_GUI_COLLECT = "polaris:scnet-gui-collect:v1";
  const signalReady = () => document.dispatchEvent(new CustomEvent(READY));
  const payloadFromEvent = (event) => {
    try {
      const detail = event && typeof event === "object" ? event.detail : null;
      return detail == null ? null : JSON.parse(JSON.stringify(detail));
    } catch {
      return null;
    }
  };
  const send = (type, payload, ackEvent, requestId = null) => {
    Promise.resolve(chrome.runtime.sendMessage({ type, payload, pageOrigin: location.origin }))
      .then((result) => {
        const detail = result && typeof result === "object"
          ? { ...result, ...(requestId ? { requestId } : {}) }
          : { ok: false, ...(requestId ? { requestId } : {}), error: "Polaris 扩展返回了无效响应" };
        document.dispatchEvent(new CustomEvent(ackEvent, { detail }));
      })
      .catch(() => document.dispatchEvent(new CustomEvent(ackEvent, {
        detail: { ok: false, ...(requestId ? { requestId } : {}), error: "Polaris 扩展暂时不可用" },
      })));
  };
  document.addEventListener(PROBE, signalReady);
  document.addEventListener(TASK, (event) => {
    send("POLARIS_IMPORT_TASK", payloadFromEvent(event), ACK);
  });
  document.addEventListener(BATCH, (event) => {
    const payload = payloadFromEvent(event);
    const requestId = payload?.batch_nonce || null;
    send("POLARIS_IMPORT_BATCH", payload, BATCH_ACK, requestId);
  });
  document.addEventListener(AUTHORIZE, (event) => {
    send("AUTHORIZE_POLARIS_CONNECTION", payloadFromEvent(event), AUTHORIZE_ACK);
  });
  document.addEventListener(SCNET_CONTEXT, (event) => {
    const detail = payloadFromEvent(event);
    if (!detail || typeof detail.credentialId !== "string") return;
    send("SCNET_SAVE_CONTEXT", {
      credentialId: detail.credentialId,
      instanceOrigin: detail.instanceOrigin || location.origin,
    }, "polaris:scnet-context-ack:v1");
  });
  document.addEventListener(SCNET_REFRESH, (event) => {
    const detail = payloadFromEvent(event);
    const purpose = detail?.purpose || "planning";
    Promise.resolve(chrome.runtime.sendMessage({
      type: "SCNET_REFRESH",
      purpose,
      payload: { purpose },
      pageOrigin: location.origin,
    }))
      .then((result) => document.dispatchEvent(new CustomEvent("polaris:scnet-refresh-ack:v1", {
        detail: result && typeof result === "object"
          ? result
          : { ok: false, error: "Polaris 扩展返回了无效响应" },
      })))
      .catch(() => document.dispatchEvent(new CustomEvent("polaris:scnet-refresh-ack:v1", {
        detail: { ok: false, error: "Polaris 扩展暂时不可用" },
      })));
  });
  document.addEventListener(SCNET_GUI_OPEN, (event) => {
    send("SCNET_GUI_OPEN", payloadFromEvent(event), "polaris:scnet-gui-open-ack:v1");
  });
  document.addEventListener(SCNET_GUI_STATUS, (event) => {
    send("SCNET_GUI_STATUS", payloadFromEvent(event), "polaris:scnet-gui-status-ack:v1");
  });
  document.addEventListener(SCNET_GUI_COLLECT, (event) => {
    send("SCNET_GUI_COLLECT", payloadFromEvent(event), "polaris:scnet-gui-collect-ack:v1");
  });
  signalReady();
})();
