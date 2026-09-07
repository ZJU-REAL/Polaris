/* 首启等待页状态机（#721）：纯函数直测。

   契约要点：
   - null（web / 旧宿主异常）与 idle（没走内嵌路径）都放行——尤其旧宿主的
     初始值是 idle+done:false，不放行会把旧宿主上的用户永远卡在等待页；
   - failed 走失败分支（给「使用远程服务器」出路），不算放行也不算等待；
   - 引导中的所有具名阶段都映射到四步进度里，未知阶段稳妥地不高亮。 */

import { describe, expect, it } from 'vitest';
import { BOOTSTRAP_STEPS, bootstrapGate, bootstrapStepIndex } from './engineBootstrap';

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
