import { describe, expect, it } from 'vitest';
import { backendNeedsSsh } from '../NewExperimentModal';
import type { RunnerBackendSummary } from '../../../lib/api';

const pythonMl: RunnerBackendSummary = {
  backend: 'python-ml',
  interaction: 'batch',
  side_effects: 'filesystem',
  credential_kinds: ['ssh'],
  licenses: [],
  is_default: true,
};
const ngspice: RunnerBackendSummary = {
  backend: 'ngspice',
  interaction: 'batch',
  side_effects: 'filesystem',
  credential_kinds: [],
  licenses: [],
  is_default: false,
};

describe('experiment backend picker', () => {
  it('requires a credential for the default backend', () => {
    expect(backendNeedsSsh([pythonMl, ngspice], '')).toBe(true);
  });

  it('does not require one for a container backend', () => {
    // 后端 #716 已按 manifest 放宽；前端这道硬门是把容器后端挡在外面的最后一道
    expect(backendNeedsSsh([pythonMl, ngspice], 'ngspice')).toBe(false);
  });

  it('requires one while the backend list has not loaded', () => {
    // 默认「不需要」的话，列表慢一拍就会放行一个没选凭据的 python-ml 实验，
    // 提交之后才发现跑不起来——而那时想法、开题问答都已经填完了
    expect(backendNeedsSsh([], '')).toBe(true);
    expect(backendNeedsSsh([], 'ngspice')).toBe(true);
  });

  it('falls back to the default when the stored choice is no longer registered', () => {
    expect(backendNeedsSsh([pythonMl, ngspice], 'removed-backend')).toBe(true);
  });
});
