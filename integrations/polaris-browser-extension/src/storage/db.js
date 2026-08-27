import { DB_NAME, DB_VERSION } from "../shared/constants.js";
import { redactSensitiveText } from "../shared/url-security.js";
import { sanitizeItemForPersistence } from "./item-security.js";

let openPromise;

function sanitizeLogDetail(value, depth = 0) {
  if (depth > 6 || value == null) return value;
  if (typeof value === "string") return redactSensitiveText(value);
  if (Array.isArray(value)) return value.slice(0, 200).map((entry) => sanitizeLogDetail(entry, depth + 1));
  if (typeof value !== "object") return value;
  return Object.fromEntries(Object.entries(value).slice(0, 200)
    .map(([key, entry]) => [key, sanitizeLogDetail(entry, depth + 1)]));
}

export function openDatabase() {
  if (openPromise) return openPromise;
  openPromise = new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains("jobs")) db.createObjectStore("jobs", { keyPath: "id" });
      if (!db.objectStoreNames.contains("items")) {
        const store = db.createObjectStore("items", { keyPath: "id" });
        store.createIndex("taskId", "taskId", { unique: false });
        store.createIndex("state", "state", { unique: false });
      }
      if (!db.objectStoreNames.contains("logs")) {
        const store = db.createObjectStore("logs", { keyPath: "id", autoIncrement: true });
        store.createIndex("taskId", "taskId", { unique: false });
      }
      if (!db.objectStoreNames.contains("settings")) db.createObjectStore("settings", { keyPath: "key" });
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error("无法打开插件数据库"));
  });
  return openPromise;
}

function requestResult(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error("数据库操作失败"));
  });
}

export async function putRecord(storeName, value) {
  const db = await openDatabase();
  const tx = db.transaction(storeName, "readwrite");
  const storedValue = storeName === "items" ? sanitizeItemForPersistence(value) : value;
  await requestResult(tx.objectStore(storeName).put(storedValue));
  return storedValue;
}

export async function putRecords(storeName, values) {
  const db = await openDatabase();
  const tx = db.transaction(storeName, "readwrite");
  const store = tx.objectStore(storeName);
  const storedValues = storeName === "items" ? values.map(sanitizeItemForPersistence) : values;
  for (const value of storedValues) store.put(value);
  await new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error || new Error("数据库批量写入失败"));
    tx.onabort = () => reject(tx.error || new Error("数据库批量写入已中止"));
  });
  return storedValues;
}

export async function putJobWithItems(job, items) {
  const db = await openDatabase();
  const tx = db.transaction(["jobs", "items"], "readwrite");
  tx.objectStore("jobs").put(job);
  const itemStore = tx.objectStore("items");
  const storedItems = items.map(sanitizeItemForPersistence);
  for (const item of storedItems) itemStore.put(item);
  await new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error || new Error("任务保存失败"));
    tx.onabort = () => reject(tx.error || new Error("任务保存已中止"));
  });
  return { job, items: storedItems };
}

export async function getRecord(storeName, key) {
  const db = await openDatabase();
  const tx = db.transaction(storeName, "readonly");
  return requestResult(tx.objectStore(storeName).get(key));
}

export async function getAllRecords(storeName) {
  const db = await openDatabase();
  const tx = db.transaction(storeName, "readonly");
  return requestResult(tx.objectStore(storeName).getAll());
}

export async function getItemsForTask(taskId) {
  const db = await openDatabase();
  const tx = db.transaction("items", "readonly");
  return requestResult(tx.objectStore("items").index("taskId").getAll(taskId));
}

export async function getLogsForTask(taskId, limit = 200) {
  const db = await openDatabase();
  const tx = db.transaction("logs", "readonly");
  const records = await requestResult(tx.objectStore("logs").index("taskId").getAll(taskId));
  return records.slice(-Math.max(1, Math.min(Number(limit) || 200, 500)));
}

export async function deleteRecord(storeName, key) {
  const db = await openDatabase();
  const tx = db.transaction(storeName, "readwrite");
  await requestResult(tx.objectStore(storeName).delete(key));
}

export async function addLog(taskId, level, message, detail = null) {
  const db = await openDatabase();
  const tx = db.transaction("logs", "readwrite");
  const safeDetail = sanitizeLogDetail(detail);
  await requestResult(tx.objectStore("logs").add({
    taskId,
    level,
    message: redactSensitiveText(message),
    detail: safeDetail,
    createdAt: new Date().toISOString(),
  }));
}

export async function getSetting(key, fallback = null) {
  const record = await getRecord("settings", key);
  return record ? record.value : fallback;
}

export async function setSetting(key, value) {
  return putRecord("settings", { key, value, updatedAt: new Date().toISOString() });
}
