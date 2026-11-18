import { errorCodeText } from '../../../lib/errors';
import { tr } from '../../../lib/i18n';

/* ============================================================
   因出错暂停的任务：这次为什么停下、会不会自己再试。
   纯函数，任务详情页与想法页的提醒条共用。

   引擎在任务上给三样东西（见后端 schemas/voyage.py）：
   - last_error：{code, message, detail}——按 code 出当前语言的文案，
     detail 是原始报错，放在第二行给排查用；
   - retry_at：下一次自动重试的时刻（临时性故障才有）；
   - retry_attempt：已经排到第几次（共 3 次）。
   ============================================================ */

export const AUTO_RETRY_MAX = 3;

export interface ParkedError {
  code?: string | null;
  message?: string | null;
  detail?: string | null;
}

/** 原始报错里夹带的更具体的错误码：「ValueError: IDEA_CONTEXT_EMPTY: …」→ IDEA_CONTEXT_EMPTY。
    引擎按异常类型归类，这种前置条件失败只会得到一个笼统的码，具体原因在报错正文里。 */
function embeddedCode(detail: string | null | undefined): string | null {
  if (!detail) return null;
  const re = /(?:^|[\s:：])([A-Z][A-Z0-9]+(?:_[A-Z0-9]+)+)(?=[:：])/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(detail))) {
    if (errorCodeText(m[1]!)) return m[1]!;
  }
  return null;
}

/** 给人看的一句话：先认报错正文里的具体错误码，再认引擎给的 code，都不认得就用引擎的兜底文案。 */
export function parkedErrorText(error: ParkedError | null | undefined): string {
  const fallback = tr('任务出错后暂停了，可以重试', 'The task paused after an error. You can retry it.');
  if (!error) return fallback;
  const specific = embeddedCode(error.detail);
  if (specific) return errorCodeText(specific)!;
  const known = error.code ? errorCodeText(error.code) : null;
  if (known) return known;
  return error.message?.trim() || fallback;
}

/** 第二行的原始报错（没有就不显示这一行）。 */
export function parkedErrorDetail(error: ParkedError | null | undefined): string | null {
  const detail = error?.detail?.trim();
  if (!detail) return null;
  return detail.length > 600 ? `${detail.slice(0, 600)}…` : detail;
}

export type RetryInfo =
  | { kind: 'scheduled'; text: string }
  | { kind: 'exhausted'; text: string }
  | null;

/** 自动重试的安排：排了下一次 → 「将在 N 分钟后自动重试（第 k 次，共 3 次）」；
    三次用完 → 「自动重试已用完」；没有自动重试（不是临时性故障）→ null。 */
export function retryInfo(
  retryAt: string | null | undefined,
  retryAttempt: number | null | undefined,
  now: number = Date.now(),
): RetryInfo {
  const attempt = Math.max(0, Math.floor(retryAttempt ?? 0));
  const due = retryAt ? Date.parse(retryAt) : NaN;
  if (Number.isFinite(due)) {
    const k = Math.min(Math.max(attempt, 1), AUTO_RETRY_MAX);
    const minutes = Math.ceil((due - now) / 60_000);
    const when =
      minutes <= 0
        ? tr('即将自动重试', 'Retrying automatically any moment')
        : minutes === 1
          ? tr('将在 1 分钟内自动重试', 'Retrying automatically within a minute')
          : tr(`将在 ${minutes} 分钟后自动重试`, `Retrying automatically in ${minutes} minutes`);
    return {
      kind: 'scheduled',
      text: `${when}${tr(`（第 ${k} 次，共 ${AUTO_RETRY_MAX} 次）`, ` (attempt ${k} of ${AUTO_RETRY_MAX})`)}`,
    };
  }
  if (attempt >= AUTO_RETRY_MAX) {
    return {
      kind: 'exhausted',
      text: tr('自动重试已用完，需要你来处理', 'Automatic retries are used up. This one needs you.'),
    };
  }
  return null;
}
