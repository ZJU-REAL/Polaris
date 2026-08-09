import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

/* ============================================================
   切换语言时，哪些东西该重挂载、哪些该原地留着。

   这几条都是类型检查看不见的回归：把 key={lang} 挪回根部、或者给新的独立页面漏掉
   useLang()，编译一样过，只有真去点那个语言开关才会发现——要么文案不跟着切，要么
   用户填了一半的表单被清空（issue #377 就是后者）。所以在源码层面钉住。
   ============================================================ */

const read = (rel: string) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf-8');

/** 只看代码：注释里正好在解释「以前这里是 key={lang}」，不该被当成还留着那行代码。 */
const code = (source: string) =>
  source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/.*$/gm, '$1');

const app = read('../../App.tsx');
const shell = read('../AppShell.tsx');
const login = read('../../features/auth/LoginPage.tsx');
const setup = read('../../features/desktop/ServerSetupPage.tsx');

describe('语言切换的重挂载边界', () => {
  it('根部不再整树重挂载', () => {
    // 这是 issue #377 的病灶：RouterProvider 被 key={lang} 一裹，换语言等于把
    // 登录页连同用户刚输入的内容一起重建。
    expect(code(app)).not.toContain('key={lang}');
    expect(code(app)).not.toContain('useLang');
  });

  it('壳层把重挂载收在路由出口这一层', () => {
    expect(shell).toContain('useLang()');
    // key 必须落在 Outlet 外面：业务页的元素来自路由表的稳定引用，
    // 只靠父组件重渲染带不动它们。
    const shellCode = code(shell);
    const outletAt = shellCode.indexOf('<Outlet context={ctx} />');
    const keyAt = shellCode.lastIndexOf('<Fragment key={lang}>', outletAt);
    expect(outletAt).toBeGreaterThan(0);
    expect(keyAt).toBeGreaterThan(0);
    expect(outletAt - keyAt).toBeLessThan(400); // 紧挨着，而不是包住了整个壳层
  });
});

describe('带表单的独立页面就地重渲染', () => {
  it.each([
    ['登录页', login],
    ['桌面端服务器配置页', setup],
  ])('%s 订阅了语言', (_name, source) => {
    expect(source).toContain('useLang');
    // 反面：自己再套一层 key={lang} 就等于把刚修好的状态又丢一次
    expect(code(source)).not.toContain('key={lang}');
  });

  it('登录页的输入仍然是组件内状态（没有为了绕开重挂载改存全局）', () => {
    // 修法应该是「不重建这一页」，而不是「把密码搬到模块级变量里躲开重建」。
    expect(login).toContain('const [mode, setMode] = useState<Mode>');
    expect(login).toContain('const [password, setPassword] = useState');
  });
});
