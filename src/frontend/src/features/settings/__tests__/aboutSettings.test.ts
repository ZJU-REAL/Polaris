import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describeCheck, failureReason } from '../AboutSettings';
import { getLang, setLang } from '../../../lib/i18n';
import type { UpdateInfo } from '../../../lib/host';

/**
 * 设置 → 关于的手动检查更新（#812）。
 *
 * 要钉住的是一件事：**没查成不能说成「已是最新」**。自动检查失败时安静是对的，手动
 * 检查失败时还安静，用户就会在限流、断网时被告知自己是最新版。
 */
const info = (extra: Partial<UpdateInfo>): UpdateInfo => ({
  available: false,
  currentVersion: '0.5.0',
  ...extra,
});

describe('describeCheck', () => {
  it('reports a newer version first', () => {
    expect(describeCheck(info({ available: true, latestVersion: '0.6.0', checked: true }))).toEqual({
      kind: 'available',
      latest: '0.6.0',
    });
  });

  it('says latest only when the main process confirmed the check', () => {
    expect(describeCheck(info({ checked: true })).kind).toBe('latest');
  });

  it('does not claim latest when the check failed', () => {
    for (const error of ['rate-limited', 'network', 'http']) {
      expect(describeCheck(info({ error })).kind).toBe('failed');
    }
  });

  it('does not claim latest when a release exists without a package for this system', () => {
    const outcome = describeCheck(info({ checked: true, error: 'no-package', latestVersion: '0.6.0' }));
    expect(outcome.kind).toBe('failed');
  });

  it('falls back to a hedged answer for older shells that send no checked field', () => {
    // 老外壳：查失败与没有新版都只回 available:false，分不出来就不打包票
    expect(describeCheck(info({})).kind).toBe('no-newer');
  });
});

describe('failureReason', () => {
  let lang: ReturnType<typeof getLang>;
  beforeEach(() => {
    lang = getLang();
    setLang('en');
  });
  afterEach(() => setLang(lang));

  it('explains rate limiting as temporary', () => {
    expect(failureReason(info({ error: 'rate-limited', errorDetail: 'HTTP 403' }))).toMatch(
      /rate-limiting/,
    );
  });

  it('carries the detail for network and http errors', () => {
    expect(failureReason(info({ error: 'network', errorDetail: 'ENOTFOUND' }))).toContain('ENOTFOUND');
    expect(failureReason(info({ error: 'http', errorDetail: 'HTTP 502' }))).toContain('HTTP 502');
  });

  it('names the version that has no package yet', () => {
    expect(failureReason(info({ error: 'no-package', latestVersion: '0.6.0' }))).toContain('v0.6.0');
  });

  it('passes an unknown code through instead of inventing a reason', () => {
    expect(failureReason(info({ error: 'weird', errorDetail: 'x' }))).toContain('weird');
  });
});

describe('UpdateInfo stays in step between the shell and the page', () => {
  const SRC = join(__dirname, '..', '..', '..', '..', '..');
  const read = (p: string) => readFileSync(join(SRC, p), 'utf8');
  const fieldsOf = (source: string) => {
    const body = (source.split('export interface UpdateInfo {')[1] ?? '').split('\n}')[0] ?? '';
    return [...body.matchAll(/^ {2}(\w+)\??:/gm)].map((m) => m[1]).sort();
  };

  it('declares the same fields on both sides', () => {
    // 主进程加了字段而界面这份没跟上，界面就读不到它——不报错，只是悄悄不生效
    const shell = fieldsOf(read('desktop/src/shared/contract.ts'));
    const page = fieldsOf(read('frontend/src/lib/host.ts'));
    expect(page).toEqual(shell);
    expect(shell).toEqual(expect.arrayContaining(['checked', 'error', 'errorDetail', 'releaseUrl']));
  });

  it('reports failures from the main process instead of returning a bare result', () => {
    const main = read('desktop/src/main/updates/index.ts');
    expect(main).toContain("error: 'network'");
    expect(main).toContain("'rate-limited'");
    expect(main).not.toMatch(/if \(!res\.ok\) return base;/);
  });
});
