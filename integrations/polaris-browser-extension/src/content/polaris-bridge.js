(() => {
  const PROBE = "polaris:download-extension-probe:v1";
  const READY = "polaris:download-extension-ready:v1";
  const BATCH = "polaris:download-batch:v2";
  const ACK = "polaris:download-batch-ack:v2";

  const clone = (value) => {
    try {
      return value == null ? null : JSON.parse(JSON.stringify(value));
    } catch {
      return null;
    }
  };
  const signalReady = () => document.dispatchEvent(new CustomEvent(READY));

  document.addEventListener(PROBE, signalReady);
  document.addEventListener(BATCH, (event) => {
    const payload = clone(event instanceof CustomEvent ? event.detail : null);
    const requestId = payload?.batch_nonce || null;
    chrome.runtime.sendMessage({
      type: "POLARIS_IMPORT_BATCH",
      payload,
      pageOrigin: location.origin,
    }).then((result) => {
      const safe = result && typeof result === "object" ? result : { ok: false, error: "扩展返回了无效响应" };
      document.dispatchEvent(new CustomEvent(ACK, { detail: { ...safe, requestId } }));
    }).catch(() => {
      document.dispatchEvent(new CustomEvent(ACK, {
        detail: { ok: false, requestId, error: "Polaris 扩展暂时不可用" },
      }));
    });
  });
  signalReady();
})();
