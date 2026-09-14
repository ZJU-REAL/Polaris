import { describe, expect, it } from 'vitest';
import { disciplinePatchValue } from '../GovernanceTab';

describe('library discipline picker', () => {
  it('sends null rather than an empty string when clearing the discipline', () => {
    // 后端拿这个值去比对已装的包名，"" 匹配不到就按未知学科 400 掉——表现是
    // 「想清空学科，保存却失败」，而错误信息说的是用户从没输入过的值
    expect(disciplinePatchValue('')).toBeNull();
  });

  it('sends the pack name as-is when one is chosen', () => {
    expect(disciplinePatchValue('structural')).toBe('structural');
  });
});
