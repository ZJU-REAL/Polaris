import { afterEach, describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { setLang } from '../i18n';
import { selectableIdeaOptions } from '../ideaOptions';
import { batchProgressTitle } from '../../features/library/PaperBatchProgressModal';
import { progressTitle } from '../../features/library/PaperProgressModal';
import { realCommand } from '../../features/voyages/shared/StepCard';

afterEach(() => setLang('zh'));

const src = (rel: string) => readFileSync(join(__dirname, '..', '..', rel), 'utf8');

/**
 * 「选中想法」不再需要审批，实验可以从任意想法开始。新建实验 / 新建稿件的想法下拉
 * 以前只列 status=promoted，文案还写着「已晋级」。
 */
describe('idea picker in the new-experiment / new-manuscript dialogs', () => {
  const ideas = [
    { id: 'c1', title: '候选一', status: 'candidate' as const },
    { id: 'r1', title: '淘汰的', status: 'rejected' as const },
    { id: 'p1', title: '选中的', status: 'promoted' as const },
    { id: 'u1', title: '评审中', status: 'under_review' as const },
  ];

  it('lists every idea that is not rejected, picked ones first', () => {
    expect(selectableIdeaOptions(ideas)).toEqual([
      { value: 'p1', label: '已选中 · 选中的' },
      { value: 'c1', label: '候选一' },
      { value: 'u1', label: '评审中' },
    ]);
    setLang('en');
    expect(selectableIdeaOptions(ideas)[0]!.label).toBe('Picked · 选中的');
  });

  it('both dialogs ask for all ideas instead of only promoted ones', () => {
    for (const file of ['features/experiment/NewExperimentModal.tsx', 'features/writer/NewManuscriptModal.tsx']) {
      const text = src(file);
      expect(text, file).toContain('selectableIdeaOptions(');
      expect(text, file).not.toContain("status: 'promoted'");
    }
  });

  it('no user-facing copy says 晋级 / promote any more', () => {
    const files = [
      'features/experiment/NewExperimentModal.tsx',
      'features/experiment/ExperimentPage.tsx',
      'features/writer/NewManuscriptModal.tsx',
      'components/ui/GateCard.tsx',
    ];
    for (const file of files) {
      const text = src(file);
      expect(text, file).not.toContain('晋级');
      expect(text, file).not.toMatch(/'[^'\n]*\b[Pp]romot(e|ed|ion)\b[^'\n]*'\s*\)/);
    }
  });
});

describe('progress dialog titles follow the state', () => {
  it('single paper', () => {
    expect(progressTitle('running')).toBe('正在处理论文');
    expect(progressTitle('done')).toBe('论文已处理完成');
    expect(progressTitle('failed')).toBe('论文处理失败');
    expect(progressTitle('partial')).toBe('论文已添加，部分步骤没有成功');
    setLang('en');
    expect(progressTitle('done')).toBe('Paper processed');
  });

  it('batch import', () => {
    expect(batchProgressTitle('running')).toBe('正在添加论文');
    expect(batchProgressTitle('done')).toBe('论文已添加完成');
    expect(batchProgressTitle('failed')).toBe('批量添加失败');
  });
});

describe('failed step: command line', () => {
  it('is shown only for a real command', () => {
    expect(realCommand('python train.py --epochs 3')).toBe('python train.py --epochs 3');
    expect(realCommand('unknown command')).toBeNull();
    expect(realCommand('  Unknown Command ')).toBeNull();
    expect(realCommand('')).toBeNull();
    expect(realCommand(null)).toBeNull();
    expect(realCommand(undefined)).toBeNull();
  });
});
