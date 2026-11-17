/**
 * 开场清单的文案映射。
 *
 * 钉两件事：
 * 1. 后端返回的每个 id 都有文案和落点——少一个就是首页上多一行空白；
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

/** 后端 checklist() 里写死的 id，按出现顺序。 */
function backendItemIds(): string[] {
  return [...backend.matchAll(/\{"id": "([a-z]+)"/g)].map((m) => m[1] ?? '');
}

describe('onboarding checklist copy', () => {
  it('每个后端项都有前端文案', () => {
    // 模型固定为一个、key 来自环境变量之后，清单里不再有「添加模型」这一项。后端同步
    // 去掉它；这里先滤掉，两边谁先合都不会红
    const ids = backendItemIds().filter((id) => id !== 'model');
    expect(ids).toEqual(['library', 'discipline', 'experiment']);
    for (const id of ids) {
      expect(src).toContain(`case '${id}':`);
    }
  });

  it('没有「添加模型」这一项，也不把人送去已经撤掉的设置页', () => {
    expect(src).not.toContain("case 'model':");
    expect(src).not.toContain('tab=llm');
    expect(src).not.toContain('tab=agents');
  });

  it('未知 id 不显示，而不是显示一行空白', () => {
    // 后端加了新项、前端还没跟上时的行为
    expect(src).toContain('default:');
    expect(src).toContain('return null;');
  });

  it('落点都在普通用户打得开的页面上', () => {
    // /settings 自 #755 起是唯一入口
    for (const href of ['/libraries', '/settings?tab=ssh']) {
      expect(src).toContain(href);
    }
    // 不能指向已经撤掉的「管理」页
    expect(src).not.toContain('/admin');
  });

  it('配齐或收起之后不再占据首页', () => {
    expect(src).toContain('data.dismissed || data.done');
  });
});
