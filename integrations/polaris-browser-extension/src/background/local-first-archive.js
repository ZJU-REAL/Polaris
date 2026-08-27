const ACCEPTED_LOCAL_STATUSES = new Set(["verified", "verified-manual"]);

export function isReusableLocalArchive(localArchive) {
  return localArchive?.status === "saved"
    && Boolean(localArchive.file)
    && ACCEPTED_LOCAL_STATUSES.has(localArchive.bridgeStatus);
}

export function createLocalArchiveRecord(bridgeResult, now = new Date()) {
  if (!ACCEPTED_LOCAL_STATUSES.has(bridgeResult?.status) || !bridgeResult?.file) {
    throw new Error("本地下载桥未返回可复用的归档文件");
  }
  return {
    status: "saved",
    bridgeStatus: bridgeResult.status,
    message: bridgeResult.message || null,
    file: bridgeResult.file,
    savedAt: now.toISOString(),
  };
}

export async function runLocalFirstPolarisArchive({
  localArchive,
  saveLocal,
  persistLocal,
  uploadCloud,
}) {
  let confirmedLocal = localArchive;
  let bridgeResult = null;
  if (!isReusableLocalArchive(confirmedLocal)) {
    bridgeResult = await saveLocal();
    if (!ACCEPTED_LOCAL_STATUSES.has(bridgeResult?.status)) {
      return { completed: false, bridgeResult, localArchive: null };
    }
    confirmedLocal = createLocalArchiveRecord(bridgeResult);
    await persistLocal(confirmedLocal, bridgeResult);
  }
  const cloudArchive = await uploadCloud(confirmedLocal);
  return {
    completed: true,
    bridgeResult,
    localArchive: confirmedLocal,
    cloudArchive,
  };
}

