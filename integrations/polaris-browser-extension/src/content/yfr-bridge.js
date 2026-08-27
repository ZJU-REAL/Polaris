(() => {
  const SELECTION_EVENT = "yfr:download-selection:v1";
  const READY_EVENT = "yfr:download-extension-ready:v1";
  const PROBE_EVENT = "yfr:download-extension-probe:v1";
  const ACK_EVENT = "yfr:download-selection-ack:v1";

  function allowedOrigin() {
    return location.origin === "https://yfr.yangy.cn"
      || (location.protocol === "http:" && ["localhost", "127.0.0.1"].includes(location.hostname));
  }

  function signalReady() {
    document.dispatchEvent(new CustomEvent(READY_EVENT));
  }

  document.addEventListener(PROBE_EVENT, signalReady);
  document.addEventListener(SELECTION_EVENT, (event) => {
    if (!allowedOrigin()) return;
    const payload = event instanceof CustomEvent ? event.detail : null;
    if (!payload || payload.version !== 1 || !Array.isArray(payload.papers)) return;
    chrome.runtime.sendMessage({ type: "YFR_IMPORT_SELECTION", payload, pageOrigin: location.origin })
      .then((response) => {
        document.dispatchEvent(new CustomEvent(ACK_EVENT, {
        detail: { ok: Boolean(response?.ok), message: response?.message || "任务已发送到 Polaris 扩展" },
        }));
      })
      .catch(() => {
        document.dispatchEvent(new CustomEvent(ACK_EVENT, {
          detail: { ok: false, message: "下载插件暂时不可用，请重新加载插件后再试" },
        }));
      });
  });
  signalReady();
})();
