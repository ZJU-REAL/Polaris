import { describe, expect, it } from 'vitest';
import { greetingFor } from '../greeting';

/* 分界点（4/5、11/12、17/18）是这种函数唯一会出错的地方，而错了没人会发现——
   谁会在早上十一点半特地去看一眼 Buddy 说的是不是「早上好」。所以整点逐个钉住。 */

describe('按本地时间问候', () => {
  it('三段的边界各在该在的地方', () => {
    const at = (h: number) => greetingFor(h);
    expect(at(4)).toBe('晚上好'); // 凌晨仍算晚上：不为深夜单开一档
    expect(at(5)).toBe('早上好');
    expect(at(11)).toBe('早上好');
    expect(at(12)).toBe('下午好');
    expect(at(17)).toBe('下午好');
    expect(at(18)).toBe('晚上好');
    expect(at(23)).toBe('晚上好');
    expect(at(0)).toBe('晚上好');
  });

  it('一天二十四小时都有话说', () => {
    for (let h = 0; h < 24; h += 1) {
      expect(greetingFor(h)).not.toBe('');
    }
  });

  it('有名字就带上名字', () => {
    expect(greetingFor(9, '小明')).toBe('早上好，小明');
  });

  it('没名字就只问好——不留一个孤零零的逗号', () => {
    expect(greetingFor(9, '')).toBe('早上好');
    expect(greetingFor(9, null)).toBe('早上好');
    expect(greetingFor(9, undefined)).toBe('早上好');
    // 只有空白的名字等于没名字
    expect(greetingFor(9, '   ')).toBe('早上好');
  });
});
