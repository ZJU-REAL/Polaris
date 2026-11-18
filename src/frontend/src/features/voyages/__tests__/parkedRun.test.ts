import { afterEach, describe, expect, it } from 'vitest';
import { setLang } from '../../../lib/i18n';
import { parkedErrorDetail, parkedErrorText, retryInfo } from '../shared/parkedRun';

afterEach(() => setLang('zh'));

const NOW = Date.parse('2026-11-18T10:00:00Z');

describe('parked run: why it stopped', () => {
  it('localises by the engine code', () => {
    expect(parkedErrorText({ code: 'RATE_LIMITED', message: '执行步骤时出错：请求太频繁' })).toContain('限流');
    setLang('en');
    expect(parkedErrorText({ code: 'RATE_LIMITED', message: '执行步骤时出错：请求太频繁' })).toMatch(/rate.limit/i);
  });

  it('every code the engine can emit has its own text in both languages', () => {
    const codes = [
      'NETWORK', 'TIMEOUT', 'RATE_LIMITED', 'UPSTREAM_UNAVAILABLE', 'DB_BUSY', 'AUTH', 'BAD_REQUEST',
      'NOT_CONFIGURED', 'REFUSAL', 'INVALID_OUTPUT', 'MISSING_PREREQUISITE', 'INTERNAL', 'STEP_FAILED',
      'CHECK_FAILED', 'NEEDS_USER', 'NO_PROGRESS', 'RETRIEVAL_UNAVAILABLE', 'IDEA_CONTEXT_EMPTY',
      'LIBRARY_TOO_SMALL', 'LLM_UNREACHABLE', 'LLM_RATE_LIMITED', 'LLM_OVERLOADED', 'LLM_AUTH',
      'LLM_BAD_REQUEST', 'LLM_TRUNCATED', 'LLM_REFUSED', 'LLM_SCHEMA', 'LLM_ERROR',
    ];
    for (const lang of ['zh', 'en'] as const) {
      setLang(lang);
      for (const code of codes) {
        const text = parkedErrorText({ code, message: 'ENGINE FALLBACK' });
        expect(text, code).not.toBe('ENGINE FALLBACK');
        expect(text, code).not.toContain(code);
      }
    }
  });

  it('prefers a specific code carried inside the raw error', () => {
    const error = {
      code: 'INVALID_OUTPUT',
      message: '执行步骤时出错：拿到的结果不符合要求',
      detail: 'ValueError: IDEA_CONTEXT_EMPTY: 关联的文献库里没有可用的论文，请先添加论文',
    };
    expect(parkedErrorText(error)).toContain('文献库');
    expect(parkedErrorText(error)).not.toContain('不符合要求');
    // 正文里随便一个全大写的词不算错误码
    expect(parkedErrorText({ code: 'INVALID_OUTPUT', detail: 'ValueError: HTTP_STATUS: 400' })).toContain('不符合要求');
  });

  it('reads a precondition code straight from last_error.code', () => {
    // 引擎现在把前置条件的码直接放在 code 里：正文里另有别的码也不改主意
    const error = { code: 'IDEA_CONTEXT_EMPTY', message: '执行步骤时出错', detail: 'RuntimeError: LIBRARY_TOO_SMALL: 3' };
    expect(parkedErrorText(error)).toContain('没有可用的论文');
    expect(parkedErrorText({ code: 'LIBRARY_TOO_SMALL', detail: null })).toContain('论文太少');
  });

  it('falls back to the engine sentence for an unknown code', () => {
    expect(parkedErrorText({ code: 'SOMETHING_NEW', message: '执行步骤时出错：某个新问题' })).toBe('执行步骤时出错：某个新问题');
    expect(parkedErrorText(null)).toBe('任务出错后暂停了，可以重试');
  });

  it('shows the raw detail on its own line, capped', () => {
    expect(parkedErrorDetail({ detail: '  ConnectError: boom  ' })).toBe('ConnectError: boom');
    expect(parkedErrorDetail({ detail: '' })).toBeNull();
    expect(parkedErrorDetail({ detail: 'x'.repeat(2000) })!.length).toBe(601);
  });
});

describe('parked run: automatic retry line', () => {
  it('says when the next retry happens and which attempt it is', () => {
    expect(retryInfo('2026-11-18T10:04:10Z', 2, NOW)).toEqual({
      kind: 'scheduled',
      text: '将在 5 分钟后自动重试（第 2 次，共 3 次）',
    });
    expect(retryInfo('2026-11-18T10:00:40Z', 1, NOW)?.text).toBe('将在 1 分钟内自动重试（第 1 次，共 3 次）');
    expect(retryInfo('2026-11-18T09:59:00Z', 3, NOW)?.text).toBe('即将自动重试（第 3 次，共 3 次）');
    setLang('en');
    expect(retryInfo('2026-11-18T10:20:00Z', 3, NOW)?.text).toBe('Retrying automatically in 20 minutes (attempt 3 of 3)');
  });

  it('says retries are used up after the third one', () => {
    expect(retryInfo(null, 3, NOW)).toEqual({ kind: 'exhausted', text: '自动重试已用完，需要你来处理' });
  });

  it('says nothing when the failure was never retried automatically', () => {
    expect(retryInfo(null, 0, NOW)).toBeNull();
    expect(retryInfo(undefined, undefined, NOW)).toBeNull();
    // 上一次是临时故障（试过 1 次），这一次不是：不再自己试，也没有用完
    expect(retryInfo(null, 1, NOW)).toBeNull();
    expect(retryInfo('not a date', 0, NOW)).toBeNull();
  });
});
