import { MESSAGE } from "../shared/constants.js";

export function isSupportedYfrPage(value) {
  try {
    const url = new URL(value);
    if (url.protocol === "https:" && url.hostname === "yfr.yangy.cn") return true;
    return url.protocol === "http:"
      && ["127.0.0.1", "localhost"].includes(url.hostname)
      && /\/(?:literature-search|daily-review)(?:\/|$)/.test(url.pathname);
  } catch {
    return false;
  }
}

async function toggleSelection(chromeApi, tabId) {
  try {
    return await chromeApi.tabs.sendMessage(tabId, { type: MESSAGE.TOGGLE_YFR_PAGE_SELECTION });
  } catch (firstError) {
    if (!chromeApi.scripting?.executeScript) throw firstError;
    await chromeApi.scripting.executeScript({
      target: { tabId },
      files: ["src/content/yfr-page-selection.js"],
    });
    return chromeApi.tabs.sendMessage(tabId, { type: MESSAGE.TOGGLE_YFR_PAGE_SELECTION });
  }
}

export async function handleActionClick(chromeApi, tab) {
  if (tab?.id != null && isSupportedYfrPage(tab.url)) {
    return toggleSelection(chromeApi, tab.id);
  }
  if (!chromeApi.sidePanel?.open || tab?.windowId == null) {
    throw new Error("当前浏览器不支持 Polaris 扩展侧栏");
  }
  await chromeApi.sidePanel.open({ windowId: tab.windowId });
  return { ok: true, sidePanel: true };
}
