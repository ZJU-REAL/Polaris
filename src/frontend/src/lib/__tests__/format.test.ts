import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { fmtDuration, fmtFullTime, fmtRelative, fmtTime, parseApiDate } from '../format';

/* 引擎的时间都是 UTC。不带偏移的 ISO 时间如果交给 new Date() 会被当成本地时间，
   东八区的用户看到的时间就整整差 8 小时（#872：刚建的任务显示「8 小时前」）。
   这里把时区固定在东八区，钉住「不带偏移 = UTC」。 */
const ORIGINAL_TZ = process.env.TZ;

beforeAll(() => {
  process.env.TZ = 'Asia/Shanghai';
});

afterAll(() => {
  if (ORIGINAL_TZ === undefined) delete process.env.TZ;
  else process.env.TZ = ORIGINAL_TZ;
});

afterEach(() => {
  vi.useRealTimers();
});

describe('parseApiDate', () => {
  it('runs under a non-UTC time zone', () => {
    expect(new Date(2026, 9, 10, 12).getTimezoneOffset()).toBe(-480);
  });

  it('reads an offset-less date-time as UTC', () => {
    const expected = Date.UTC(2026, 9, 10, 2, 53, 51, 123);
    expect(parseApiDate('2026-10-10T02:53:51.123456').getTime()).toBe(expected);
    expect(parseApiDate('2026-10-10T02:53:51.123').getTime()).toBe(expected);
    expect(parseApiDate('2026-10-10 02:53:51.123456').getTime()).toBe(expected);
    expect(parseApiDate('2026-10-10T02:53:51').getTime()).toBe(Date.UTC(2026, 9, 10, 2, 53, 51));
    expect(parseApiDate('2026-10-10T02:53').getTime()).toBe(Date.UTC(2026, 9, 10, 2, 53));
  });

  it('keeps an explicit offset as written', () => {
    const expected = Date.UTC(2026, 9, 10, 2, 53, 51);
    expect(parseApiDate('2026-10-10T02:53:51Z').getTime()).toBe(expected);
    expect(parseApiDate('2026-10-10T02:53:51+00:00').getTime()).toBe(expected);
    expect(parseApiDate('2026-10-10T10:53:51+08:00').getTime()).toBe(expected);
    expect(parseApiDate('2026-10-09T21:53:51-0500').getTime()).toBe(expected);
  });

  it('leaves a date-only string a date', () => {
    expect(parseApiDate('2026-10-10').getTime()).toBe(Date.UTC(2026, 9, 10));
  });

  it('returns an invalid date for junk and empty input', () => {
    expect(Number.isNaN(parseApiDate('not a date').getTime())).toBe(true);
    expect(Number.isNaN(parseApiDate('').getTime())).toBe(true);
    expect(Number.isNaN(parseApiDate(null).getTime())).toBe(true);
    expect(Number.isNaN(parseApiDate(undefined).getTime())).toBe(true);
  });
});

describe('time formatting for a UTC+8 user', () => {
  it('shows the local clock time of a UTC timestamp', () => {
    // 02:53 UTC = 10:53 北京时间
    expect(fmtTime('2026-10-10T02:53:51.123456')).toBe('10-10 10:53');
    expect(fmtTime('2026-10-10T02:53:51.123456+00:00')).toBe('10-10 10:53');
    expect(fmtFullTime('2026-10-10T02:53:51')).toBe('2026-10-10 10:53:51');
    // 跨日：20:30 UTC = 次日 04:30
    expect(fmtTime('2026-10-10T20:30:00')).toBe('10-11 04:30');
  });

  it('says "just now" for something created just now', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-10-10T02:54:00Z'));
    expect(fmtRelative('2026-10-10T02:53:51.123456')).toBe('刚刚');
    expect(fmtRelative('2026-10-10T02:53:51.123456+00:00')).toBe('刚刚');
  });

  it('counts minutes and hours from the real instant', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-10-10T06:00:00Z')); // 14:00 北京时间
    expect(fmtRelative('2026-10-10T05:52:00')).toBe('8 分钟前');
    expect(fmtRelative('2026-10-10T03:00:00')).toBe('3 小时前');
    // 昨天 21:28 北京时间 = 13:28 UTC
    expect(fmtRelative('2026-10-09T13:28:00')).toBe('昨天 21:28');
  });

  it('measures durations between mixed offset styles', () => {
    expect(fmtDuration('2026-10-10T02:00:00', '2026-10-10T02:03:12Z')).toBe('3m 12s');
    expect(fmtDuration('2026-10-10T02:00:00+00:00', '2026-10-10T03:04:00')).toBe('1h 04m');
  });
});
