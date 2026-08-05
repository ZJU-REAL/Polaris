/* ============================================================
   PolarisBuddy 的主动提示：什么时候值得主动说一句话。

   主动服务和骚扰之间只隔着一条线：**有没有新的、他还没看过的东西**。所以这里
   只有两条规矩：

   1. 一天最多提一次（按本地日期记在 localStorage）。同一件事反复冒泡，第二次就是
      噪音，第三次用户就学会无视这个球了——那比不提示更糟。
   2. **没有真事就不提**。徽标必须对应一句能说出口的话；为了让球看起来"活着"
      而常亮的红点，是在教用户忽略它。

   **说什么由后端决定**（`buddy.compose_nudge`，与问候语同一处措辞、有测试覆盖）；
   这里只管「今天提过没」——那部分只有浏览器知道。
   ============================================================ */

const SEEN_KEY = 'polaris.buddy.nudge-seen';

/** 本地日期（不是 UTC）：用户感知的"今天"是他自己时区的今天。 */
export function todayKey(now: Date): string {
  return `${now.getFullYear()}-${now.getMonth() + 1}-${now.getDate()}`;
}

/** 今天已经提过了吗。读写失败（隐私模式、配额满）一律当作"没提过"——
 *  提示重复一次的代价，远小于因为存不下就永远不提示。 */
export function alreadyNudgedToday(now: Date, read: () => string | null = defaultRead): boolean {
  try {
    return read() === todayKey(now);
  } catch {
    return false;
  }
}

export function markNudged(now: Date, write: (v: string) => void = defaultWrite): void {
  try {
    write(todayKey(now));
  } catch {
    /* 存不下就下次再提一次，不是错误 */
  }
}

function defaultRead(): string | null {
  return localStorage.getItem(SEEN_KEY);
}

function defaultWrite(value: string): void {
  localStorage.setItem(SEEN_KEY, value);
}
