import {
  DEFAULT_GLOBAL_CONCURRENCY,
  DEFAULT_MAX_PDF_BYTES,
  DEFAULT_PUBLISHER_CONCURRENCY,
  ITEM_STATE,
  SCHEMA_VERSION,
} from "../shared/constants.js";
import { createTaskCode, dedupePaperRecords, normalizePaperRecord, plannedPdfFilename, stableHash } from "../shared/normalization.js";
import { getAllRecords, getItemsForTask, getLogsForTask, getRecord, putJobWithItems, putRecord } from "../storage/db.js";

export function buildJob(source, records, options = {}, now = new Date()) {
  const normalized = source?.type === "polaris" && records.every((paper) => paper?.polarisTarget)
    ? records.map((paper, index) => normalizePaperRecord(paper, index))
    : dedupePaperRecords(records);
  if (!normalized.length) throw new Error("没有可导入的文献");
  const taskCode = createTaskCode(source, now);
  const id = `job-${taskCode}-${stableHash(`${source.sourceUrl || "manual"}:${now.toISOString()}`)}`;
  const createdAt = now.toISOString();
  const namingTemplate = options.namingTemplate || "{taskCode}_{index}";
  const job = {
    schemaVersion: SCHEMA_VERSION,
    id,
    taskCode,
    origin: { ...source },
    status: "draft",
    createdAt,
    updatedAt: createdAt,
    destination: {
      mode: options.destinationMode || "browser-downloads",
      destinationId: options.destinationId || null,
      displayPath: options.displayPath || null,
      namingTemplate,
    },
    limits: {
      globalConcurrency: DEFAULT_GLOBAL_CONCURRENCY,
      publisherConcurrency: DEFAULT_PUBLISHER_CONCURRENCY,
      maxPdfBytes: options.maxPdfBytes || DEFAULT_MAX_PDF_BYTES,
    },
    itemIds: [],
  };
  const items = normalized.map((paper, index) => {
    const targetIdentity = paper.polarisTarget
      ? `${paper.polarisTarget.libraryId}:${paper.polarisTarget.paperId}`
      : paper.id;
    const itemId = `${id}:${stableHash(targetIdentity)}`;
    job.itemIds.push(itemId);
    return {
      ...paper,
      id: itemId,
      paperKey: paper.polarisTarget ? targetIdentity : paper.yfrPaperId || paper.id,
      taskId: id,
      ordinal: index + 1,
      adapter: "generic",
      publisherKey: "generic",
      state: paper.candidates.length ? ITEM_STATE.CANDIDATE_REGISTERED : ITEM_STATE.PENDING,
      retryCount: 0,
      attempts: [],
      stateHistory: [],
      createdAt,
      updatedAt: createdAt,
      plannedFilename: plannedPdfFilename(namingTemplate, {
        taskCode,
        ordinal: index + 1,
        title: paper.title,
        doi: paper.doi,
      }),
    };
  });
  return { job, items };
}

export async function createJob(source, records, options = {}) {
  const built = buildJob(source, records, options);
  if (Number.isInteger(options.expectedCount) && built.items.length !== options.expectedCount) {
    throw new Error(`YFR 选中 ${options.expectedCount} 篇，但去重后仅生成 ${built.items.length} 篇；任务未创建，请刷新页面后重试`);
  }
  if (Array.isArray(options.expectedPaperIds)) {
    const actualPaperIds = built.items.map((item) => item.yfrPaperId);
    if (actualPaperIds.length !== options.expectedPaperIds.length
      || actualPaperIds.some((id, index) => id !== options.expectedPaperIds[index])) {
      throw new Error("YFR 文献编号与下载任务条目不一致；任务未创建，请刷新页面后重试");
    }
  }
  await putJobWithItems(built.job, built.items);
  return built;
}

export async function updateJob(jobId, patch) {
  const current = await getRecord("jobs", jobId);
  if (!current) throw new Error("下载任务不存在");
  const next = { ...current, ...patch, updatedAt: new Date().toISOString() };
  await putRecord("jobs", next);
  return next;
}

export async function getJobSnapshot(jobId) {
  const job = await getRecord("jobs", jobId);
  if (!job) return null;
  const [items, logs] = await Promise.all([getItemsForTask(jobId), getLogsForTask(jobId)]);
  return { job, items: items.sort((left, right) => left.ordinal - right.ordinal), logs };
}

export async function listJobs() {
  const jobs = await getAllRecords("jobs");
  return jobs.sort((left, right) => String(right.updatedAt).localeCompare(String(left.updatedAt)));
}
