/**
 * 实验环境：通用字段在上，Python / 机器学习的收进折叠区；别的领域靠自定义环境变量。
 */
import { beforeEach, describe, expect, it } from 'vitest';
import { setLang } from '../../../lib/i18n';
import { envVarIssue, envVarsPayload, pythonConfiguredCount } from '../ExperimentSettings';

beforeEach(() => setLang('en'));

const base = { model_root: '', dataset_root: '', pip_index_url: '', hf_endpoint: '', proxy_url: '', env_vars: [] };

describe('envVarIssue', () => {
  it('accepts shell identifiers and ignores blank rows', () => {
    const matlab = { name: 'MATLAB_ROOT', value: '/opt/MATLAB R2024b' };
    const blank = { name: '', value: 'x' };
    expect(envVarIssue(matlab, [matlab, blank])).toBeNull();
    expect(envVarIssue(blank, [matlab, blank])).toBeNull();
  });

  it('rejects bad names, names the platform owns, duplicates and line breaks', () => {
    expect(envVarIssue({ name: '1X', value: '' }, [])).toMatch(/digit/);
    expect(envVarIssue({ name: 'path', value: '' }, [])).toMatch(/Polaris uses/);
    const a1 = { name: 'A', value: '1' };
    expect(envVarIssue(a1, [a1, { name: 'A', value: '2' }])).toMatch(/Duplicate/);
    expect(envVarIssue({ name: 'A', value: 'x\ny' }, [])).toMatch(/line breaks/);
  });
});

describe('envVarsPayload', () => {
  it('drops unnamed rows and trims names', () => {
    expect(envVarsPayload([{ name: ' A ', value: ' v ' }, { name: '  ', value: 'x' }])).toEqual([
      { name: 'A', value: ' v ' },
    ]);
  });
});

describe('pythonConfiguredCount', () => {
  it('counts only the Python / ML fields', () => {
    expect(pythonConfiguredCount({ ...base, proxy_url: 'http://p:1', dataset_root: '/data' })).toBe(0);
    expect(pythonConfiguredCount({ ...base, model_root: '/hf/model', hf_endpoint: 'https://hf-mirror.com' })).toBe(2);
  });
});
