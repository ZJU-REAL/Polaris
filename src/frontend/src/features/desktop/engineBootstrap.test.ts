/* 首启等待页状态机（#721）：纯函数直测。

   契约要点：
   - null（web / 旧宿主异常）与 idle（没走内嵌路径）都放行——尤其旧宿主的
     初始值是 idle+done:false，不放行会把旧宿主上的用户永远卡在等待页；
   - failed 走失败分支（给「重新打开」出路），不算放行也不算等待；
   - 引导中的所有具名阶段都映射到四步进度里，未知阶段稳妥地不高亮。 */

import { describe, expect, it } from 'vitest';
import {
  BOOTSTRAP_STEPS,
  STALL_THRESHOLD_MS,
  bootstrapGate,
  bootstrapStepIndex,
  formatBytes,
  formatElapsed,
  isStalled,
} from './engineBootstrap';

describe('bootstrapGate', () => {
  it('web 端 / 桥失败（null）放行', () => {
    expect(bootstrapGate(null)).toBe('proceed');
  });

  it('idle 放行——不论 done（旧宿主初始值是 idle+done:false）', () => {
    expect(bootstrapGate({ phase: 'idle', done: true })).toBe('proceed');
    expect(bootstrapGate({ phase: 'idle', done: false })).toBe('proceed');
  });

  it('failed 走失败分支，即使 done=true 也不放行', () => {
    expect(bootstrapGate({ phase: 'failed', done: true })).toBe('failed');
  });

  it('引导中的各阶段都等待', () => {
    for (const phase of ['starting', 'check', 'python', 'venv', 'install', 'engine', 'ready']) {
      expect(bootstrapGate({ phase, done: false })).toBe('wait');
    }
  });

  it('done 后放行（ready 收尾）', () => {
    expect(bootstrapGate({ phase: 'ready', done: true })).toBe('proceed');
  });
});

describe('bootstrapStepIndex', () => {
  it('具名阶段映射到四步进度', () => {
    expect(bootstrapStepIndex('python')).toBe(0);
    expect(bootstrapStepIndex('venv')).toBe(1);
    expect(bootstrapStepIndex('install')).toBe(2);
    expect(bootstrapStepIndex('engine')).toBe(3);
    expect(bootstrapStepIndex('ready')).toBe(3);
  });

  it('starting/check 与未知阶段不高亮任何一步', () => {
    expect(bootstrapStepIndex('starting')).toBe(-1);
    expect(bootstrapStepIndex('check')).toBe(-1);
    expect(bootstrapStepIndex('whatever-new-phase')).toBe(-1);
  });

  it('步骤序号都落在展示列表范围内', () => {
    for (const phase of ['python', 'venv', 'install', 'engine', 'ready']) {
      const i = bootstrapStepIndex(phase);
      expect(i).toBeGreaterThanOrEqual(0);
      expect(i).toBeLessThan(BOOTSTRAP_STEPS.length);
    }
  });
});

describe('formatBytes', () => {
  it('KB / MB / GB 分档，小于 10 MB 留一位小数', () => {
    expect(formatBytes(0)).toBe('0 KB');
    expect(formatBytes(100)).toBe('1 KB');
    expect(formatBytes(512 * 1024)).toBe('512 KB');
    expect(formatBytes(8.44 * 1024 * 1024)).toBe('8.4 MB');
    expect(formatBytes(236 * 1024 * 1024 + 300_000)).toBe('236 MB');
    expect(formatBytes(1.24 * 1024 ** 3)).toBe('1.2 GB');
  });

  it('负数按 0 处理', () => {
    expect(formatBytes(-5)).toBe('0 KB');
  });
});

describe('formatElapsed', () => {
  it('中文：秒 / 分秒 / 小时分', () => {
    expect(formatElapsed(45_400, 'zh')).toBe('45 秒');
    expect(formatElapsed(80_000, 'zh')).toBe('1 分 20 秒');
    expect(formatElapsed(3_720_000, 'zh')).toBe('1 小时 2 分');
  });

  it('英文：m:ss / h:mm:ss', () => {
    expect(formatElapsed(45_000, 'en')).toBe('0:45');
    expect(formatElapsed(80_000, 'en')).toBe('1:20');
    expect(formatElapsed(3_723_000, 'en')).toBe('1:02:03');
  });

  it('时钟回拨（负值）显示 0', () => {
    expect(formatElapsed(-3000, 'zh')).toBe('0 秒');
    expect(formatElapsed(-3000, 'en')).toBe('0:00');
  });
});

describe('isStalled', () => {
  const t0 = 1_000_000;

  it('老宿主（没有 phaseStartedAt）永不提示', () => {
    expect(isStalled({}, t0 + 10 * STALL_THRESHOLD_MS)).toBe(false);
  });

  it('阶段刚开始不提示，超过阈值且无任何动静才提示', () => {
    expect(isStalled({ phaseStartedAt: t0 }, t0 + STALL_THRESHOLD_MS)).toBe(false);
    expect(isStalled({ phaseStartedAt: t0 }, t0 + STALL_THRESHOLD_MS + 1)).toBe(true);
  });

  it('近期有输出或字节增长都算有动静', () => {
    const now = t0 + 5 * STALL_THRESHOLD_MS;
    expect(isStalled({ phaseStartedAt: t0, lastOutputAt: now - 1000 }, now)).toBe(false);
    expect(isStalled({ phaseStartedAt: t0, lastGrowthAt: now - 1000 }, now)).toBe(false);
    expect(isStalled({ phaseStartedAt: t0, lastOutputAt: t0 + 1, lastGrowthAt: t0 + 2 }, now)).toBe(true);
  });

  it('上一阶段的旧输出不影响新阶段的起点', () => {
    // lastOutputAt 早于本阶段开始：以阶段开始为准
    expect(isStalled({ phaseStartedAt: t0, lastOutputAt: t0 - 500_000 }, t0 + 1000)).toBe(false);
  });
});
