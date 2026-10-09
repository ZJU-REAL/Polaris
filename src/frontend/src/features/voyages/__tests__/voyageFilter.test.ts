import { describe, expect, it } from 'vitest';
import type { VoyageRead } from '../../../lib/api';
import { matchFilter } from '../VoyagesPage';

const voyage = (status: string) => ({ status }) as unknown as VoyageRead;

describe('任务列表筛选', () => {
  it('等你回答的任务算在「已暂停」里', () => {
    expect(matchFilter(voyage('paused_ask'), 'paused')).toBe(true);
    expect(matchFilter(voyage('paused_gate'), 'paused')).toBe(true);
    expect(matchFilter(voyage('paused_error'), 'paused')).toBe(true);
  });

  it('已暂停不混进完成或失败', () => {
    expect(matchFilter(voyage('paused_ask'), 'done')).toBe(false);
    expect(matchFilter(voyage('paused_ask'), 'failed')).toBe(false);
  });
});
