import { MESSAGE } from "../shared/constants.js";

export async function sendCommand(type, payload = {}) {
  const response = await chrome.runtime.sendMessage({ type, ...payload });
  if (!response?.ok) throw new Error(response?.error || "插件后台未完成操作");
  return response;
}

export const getState = (taskId) => sendCommand(MESSAGE.GET_STATE, taskId ? { taskId } : {});
export const importRecords = (source, records) => sendCommand(MESSAGE.IMPORT_RECORDS, { source, records });
export const testPolarisConnection = (connection) => sendCommand(MESSAGE.TEST_POLARIS_CONNECTION, { connection });
export const savePolarisConnection = (connection) => sendCommand(MESSAGE.SAVE_POLARIS_CONNECTION, { connection });
export const startRegistration = (taskId, itemIds) => sendCommand(MESSAGE.START_REGISTRATION, { taskId, itemIds });
export const startCaching = (taskId, itemIds) => sendCommand(MESSAGE.START_CACHING, { taskId, itemIds });
export const startDownloads = (taskId, itemIds) => sendCommand(MESSAGE.START_DOWNLOADS, { taskId, itemIds });
export const stopTask = (taskId) => sendCommand(MESSAGE.STOP_TASK, { taskId });
export const resumeTask = (taskId) => sendCommand(MESSAGE.RESUME_TASK, { taskId });
export const retryItem = (taskId, itemId) => sendCommand(MESSAGE.RETRY_ITEM, { taskId, itemId });
export const reparseItem = (taskId, itemId) => sendCommand(MESSAGE.REPARSE_ITEM, { taskId, itemId });
export const abandonItem = (taskId, itemId) => sendCommand(MESSAGE.ABANDON_ITEM, { taskId, itemId });
export const recheckPublisher = (taskId, itemId) => sendCommand(MESSAGE.RECHECK_PUBLISHER, { taskId, itemId });
export const openManualPage = (taskId, itemId) => sendCommand(MESSAGE.OPEN_MANUAL_PAGE, { taskId, itemId });
export const openCachedPdf = (taskId, itemId) => sendCommand(MESSAGE.OPEN_CACHED_PDF, { taskId, itemId });
export const startAssistedPdfCapture = (taskId, itemId) => sendCommand(MESSAGE.START_ASSISTED_PDF_CAPTURE, { taskId, itemId });
export const stopAssistedPdfCapture = (taskId, itemId) => sendCommand(MESSAGE.STOP_ASSISTED_PDF_CAPTURE, { taskId, itemId });
export const chooseDestination = (taskId) => sendCommand(MESSAGE.CHOOSE_DESTINATION, { taskId });
export const updateSettings = (taskId, destination, limits) => sendCommand(MESSAGE.UPDATE_SETTINGS, { taskId, destination, limits });
export const downloadBridgeInstaller = () => sendCommand(MESSAGE.DOWNLOAD_BRIDGE_INSTALLER);
export const openBridgeInstaller = () => sendCommand(MESSAGE.OPEN_BRIDGE_INSTALLER);
export const showBridgeInstaller = () => sendCommand(MESSAGE.SHOW_BRIDGE_INSTALLER);
export const refreshBridgeStatus = () => sendCommand(MESSAGE.REFRESH_BRIDGE_STATUS);
export const refreshZoteroStatus = () => sendCommand(MESSAGE.REFRESH_ZOTERO_STATUS);
export const pairZotero = (pairingCode) => sendCommand(MESSAGE.PAIR_ZOTERO, { pairingCode });
export const disconnectZotero = () => sendCommand(MESSAGE.DISCONNECT_ZOTERO);
export const updateZoteroSettings = (autoSync) => sendCommand(MESSAGE.UPDATE_ZOTERO_SETTINGS, { autoSync });
export const retryZoteroItem = (taskId, itemId) => sendCommand(MESSAGE.RETRY_ZOTERO_ITEM, { taskId, itemId });
export const retryZoteroPending = (taskId, itemIds = null) => sendCommand(MESSAGE.RETRY_ZOTERO_PENDING, {
  ...(taskId ? { taskId } : {}),
  ...(Array.isArray(itemIds) ? { itemIds } : {}),
});
