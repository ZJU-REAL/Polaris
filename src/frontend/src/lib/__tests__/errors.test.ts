import { describe, expect, it } from 'vitest';
import { ApiError } from '../api';
import { errorText } from '../errors';

/* 错误码 → 给人看的一句话：认得的换成大白话，不认得的留原码，网络不通指向本机引擎。 */
describe('errorText', () => {
  it('turns a known code into a plain sentence', () => {
    expect(errorText(new ApiError(404, 'PAPER_NOT_FOUND'))).toBe('找不到这篇论文，可能已被删除');
  });

  it('reads the code before a colon', () => {
    expect(errorText(new ApiError(502, 'INDEX_REBUILD_FAILED:timeout'))).toBe('索引没有建立成功，请稍后重试');
  });

  it('keeps an unknown code in brackets so it can be reported', () => {
    expect(errorText(new ApiError(400, 'SOMETHING_NEW'))).toBe('操作没有完成（SOMETHING_NEW）');
  });

  it('points at the local engine when the request never got through', () => {
    expect(errorText(new TypeError('Failed to fetch'))).toContain('本机引擎');
  });

  it('passes ordinary messages through', () => {
    expect(errorText(new Error('Rate limit reached'))).toBe('Rate limit reached');
    expect(errorText('plain text')).toBe('plain text');
  });

  it('never mentions the backend or server', () => {
    const samples = [
      errorText(new ApiError(500, 'Internal Server Error')),
      errorText(new ApiError(503, 'TASK_SERVICE_UNAVAILABLE')),
      errorText(new TypeError('Failed to fetch')),
    ];
    for (const s of samples) expect(s).not.toMatch(/后端|服务端|backend|server/i);
  });
});
