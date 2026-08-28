const CONNECTION_KEY = "polarisConnection";
const TASKS_KEY = "polarisDownloadTasks";
let writeQueue = Promise.resolve();

async function read(key, fallback) {
  const result = await chrome.storage.local.get(key);
  return result[key] ?? fallback;
}

function serializedWrite(operation) {
  const next = writeQueue.then(operation, operation);
  writeQueue = next.catch(() => undefined);
  return next;
}

export function getConnection() {
  return read(CONNECTION_KEY, null);
}

export async function saveConnection(connection) {
  const value = { ...connection, updatedAt: new Date().toISOString() };
  await chrome.storage.local.set({ [CONNECTION_KEY]: value });
  return value;
}

export function listTasks() {
  return read(TASKS_KEY, []);
}

export async function getTask(taskId) {
  return (await listTasks()).find((task) => task.id === taskId) || null;
}

export function putTask(task) {
  return serializedWrite(async () => {
    const tasks = await listTasks();
    const index = tasks.findIndex((entry) => entry.id === task.id);
    if (index >= 0) tasks[index] = task;
    else tasks.unshift(task);
    await chrome.storage.local.set({ [TASKS_KEY]: tasks.slice(0, 100) });
    return task;
  });
}

export function updateTask(taskId, updater) {
  return serializedWrite(async () => {
    const tasks = await listTasks();
    const index = tasks.findIndex((entry) => entry.id === taskId);
    if (index < 0) throw new Error("下载任务不存在");
    const updated = updater(structuredClone(tasks[index]));
    updated.updatedAt = new Date().toISOString();
    tasks[index] = updated;
    await chrome.storage.local.set({ [TASKS_KEY]: tasks });
    return updated;
  });
}

export function updateItem(taskId, itemId, patch) {
  return updateTask(taskId, (task) => {
    const index = task.items.findIndex((item) => item.id === itemId);
    if (index < 0) throw new Error("下载任务中的论文不存在");
    const value = typeof patch === "function" ? patch(task.items[index]) : { ...task.items[index], ...patch };
    task.items[index] = value;
    return task;
  });
}

export function removeTask(taskId) {
  return serializedWrite(async () => {
    const tasks = await listTasks();
    const removed = tasks.find((task) => task.id === taskId) || null;
    await chrome.storage.local.set({ [TASKS_KEY]: tasks.filter((task) => task.id !== taskId) });
    return removed;
  });
}
