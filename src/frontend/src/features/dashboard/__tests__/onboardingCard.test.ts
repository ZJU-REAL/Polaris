/**
 * 开场清单的文案映射。
 *
 * 钉两件事：
 * 1. 后端返回的四个 id 每个都有文案和落点——少一个就是首页上多一行空白；
 * 2. 落点是这个用户真打得开的页面。指着一个他一点就 403 的地方，等于把
 *    「你还没做」说成「你去做」，而他根本做不了（#801 那条判据）。
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

const src = readFileSync(join(__dirname, '..', 'OnboardingCard.tsx'), 'utf8');
const backend = readFileSync(
  join(__dirname, '..', '..', '..', '..', '..', 'backend', 'app', 'services', 'onboarding.py'),
  'utf8',
);

/** 后端 checklist() 里写死的四个 id，按出现顺序。 */
function backendItemIds(): string[] {
  return [...backend.matchAll(/\{"id": "([a-z]+)"/g)].map((m) => m[1] ?? '');
}

describe('onboarding checklist copy', () => {
  it('每个后端项都有前端文案', () => {
    const ids = backendItemIds();
    expect(ids).toEqual(['model', 'library', 'discipline', 'experiment']);
    for (const id of ids) {
      expect(src).toContain(`case '${id}':`);
    }
  });

  it('未知 id 不显示，而不是显示一行空白', () => {
    // 后端加了新项、前端还没跟上时的行为
    expect(src).toContain('default:');
    expect(src).toContain('return null;');
  });

  it('落点都在普通用户打得开的页面上', () => {
    // /settings 自 #755 起是唯一入口；模型那一块在 #803 之后对非主人也可用
    for (const href of ['/settings?tab=llm', '/libraries', '/settings?tab=ssh']) {
      expect(src).toContain(href);
    }
    // 不能指向已经撤掉的「管理」页
    expect(src).not.toContain('/admin');
  });

  it('配齐或收起之后不再占据首页', () => {
    expect(src).toContain('data.dismissed || data.done');
  });
});
