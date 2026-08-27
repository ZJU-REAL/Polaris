import { MESSAGE } from "../shared/constants.js";
import { redactSensitiveText } from "../shared/url-security.js";

const KNOWN_TYPES = new Set(Object.values(MESSAGE));

export function validateMessage(message) {
  if (!message || typeof message !== "object" || !KNOWN_TYPES.has(message.type)) {
    return { ok: false, error: "未知的插件消息" };
  }
  if (message.taskId != null && (typeof message.taskId !== "string" || message.taskId.length > 200)) {
    return { ok: false, error: "任务编号无效" };
  }
  if (message.itemId != null && (typeof message.itemId !== "string" || message.itemId.length > 400)) {
    return { ok: false, error: "文献编号无效" };
  }
  if (message.itemIds != null && (!Array.isArray(message.itemIds)
    || message.itemIds.length > 1000
    || message.itemIds.some((itemId) => typeof itemId !== "string" || itemId.length > 400))) {
    return { ok: false, error: "文献编号列表无效" };
  }
  if (message.pairingCode != null && !/^\d{6}$/.test(String(message.pairingCode))) {
    return { ok: false, error: "Zotero 配对码无效" };
  }
  if (message.autoSync != null && typeof message.autoSync !== "boolean") {
    return { ok: false, error: "Zotero 自动同步设置无效" };
  }
  if (["SCNET_GUI_OPEN", "SCNET_GUI_STATUS", "SCNET_GUI_COLLECT"].includes(message.type)
    && message.payload != null && (typeof message.payload !== "object" || Array.isArray(message.payload))) {
    return { ok: false, error: "SCNet GUI 参数无效" };
  }
  return { ok: true, value: message };
}

export function publicError(error, fallback = "插件操作失败") {
  const message = error instanceof Error ? error.message : String(error || fallback);
  return redactSensitiveText(message).slice(0, 1000) || fallback;
}
