import { afterEach, describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { setLang } from '../../../lib/i18n';
import { summarizeDetail } from '../PaperProgressModal';

const source = readFileSync(join(__dirname, '..', 'PaperProgressModal.tsx'), 'utf8');

afterEach(() => setLang('zh'));

/**
 * 添加论文的进度弹窗多了最后一步「生成解读」。引擎发来的说明是内部短语
 * （"batch too large" 之类），这一步换成用户能看懂的话再显示。
 */
describe('paper progress: summary stage', () => {
  it('lists the summary stage last, after scoring', () => {
    const ids = [...source.matchAll(/\{ id: '(\w+)', zh:/g)].map((m) => m[1]);
    expect(ids).toEqual(['download', 'extract', 'embed', 'score', 'summarize']);
    expect(source).toContain("zh: '生成解读', en: 'Generating summary'");
  });

  it('never shows the engine’s raw wording for this stage', () => {
    setLang('en');
    const raw = ['already summarized', 'batch too large', 'no full text or abstract', 'no target library', 'no membership'];
    for (const detail of raw) {
      expect(summarizeDetail('skipped', detail)).not.toBe(detail);
    }
    expect(summarizeDetail('error', 'RuntimeError: model unavailable')).not.toContain('RuntimeError');
  });

  it('explains the skips a user can act on and stays quiet otherwise', () => {
    setLang('en');
    expect(summarizeDetail('skipped', 'batch too large')).toContain('20 papers');
    expect(summarizeDetail('skipped', 'already summarized')).toBe('This paper already has a summary');
    expect(summarizeDetail('skipped', 'no membership')).toBeNull();
    expect(summarizeDetail('ok', undefined)).toBeNull();
    expect(summarizeDetail('pending', undefined)).toBeNull();
  });

  it('tells the user a running summary takes a while, in the current language', () => {
    setLang('zh');
    expect(summarizeDetail('running', undefined)).toBe('需要一两分钟');
    setLang('en');
    expect(summarizeDetail('running', undefined)).toBe('This takes a minute or two');
  });
});
