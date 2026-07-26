/* ============================================================
   版本比较。单独成文件是因为两处都要用，而它们互相 import 会成环：
   updates/index.ts（判断有没有新版）与 updates/renderer-store.ts（判断热更新
   装的那份界面是否还比外壳自带的新）。这里不 import electron，纯函数好测。
   ============================================================ */

/** 只比较 major.minor.patch；带后缀的（如 0.2.1-win-test）视为该版本的预发布，更低。 */
export function compareVersions(a: string, b: string): number {
  const parse = (v: string) => {
    const [core, pre] = v.replace(/^v/, '').split('-', 2);
    const nums = (core ?? '').split('.').map((n) => parseInt(n, 10) || 0);
    return { nums, pre: pre ?? '' };
  };
  const x = parse(a);
  const y = parse(b);
  for (let i = 0; i < 3; i += 1) {
    const d = (x.nums[i] ?? 0) - (y.nums[i] ?? 0);
    if (d !== 0) return d > 0 ? 1 : -1;
  }
  if (x.pre === y.pre) return 0;
  if (!x.pre) return 1; // 正式版 > 预发布
  if (!y.pre) return -1;
  return x.pre > y.pre ? 1 : -1;
}

/**
 * 热更新装的那份界面是否仍然有效——即它比外壳自带的那份新。
 *
 * 两个方向都要靠这一条判断，漏掉任一个都会坏：
 * - 不新了就必须丢弃。整包更新把外壳升到 0.4.0 后，留在 userData 里的 0.3.2
 *   界面比包内自带的旧，继续提供它等于让用户在新外壳上跑旧界面。
 * - 新的就得算数。热更新只换界面、不换外壳，`app.getVersion()` 仍是旧值；
 *   更新检查若拿它去比，装完新界面后还会认为自己是旧版，同一个更新反复提示。
 */
export function stagedSupersedes(stagedVersion: string, shellVersion: string): boolean {
  return compareVersions(stagedVersion, shellVersion) > 0;
}
