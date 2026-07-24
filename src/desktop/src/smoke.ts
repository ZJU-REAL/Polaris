/* ============================================================
   冒烟测试：真的把 SPA 在 app://polaris 下加载一遍。

   覆盖桌面端最容易一改就坏、又只有运行时才暴露的东西：
   - 自定义协议的 privileges 是否够用（standard/secure/stream）
   - protocol.handle 的路径映射、SPA fallback、路径穿越防护
   - CSP 是否放行了 pdf.js 的 WASM 与 CodeMirror/KaTeX 的行内样式
   - preload 的 sendSync 注入是否早于 renderer 脚本
   - React 应用能否真的挂载起来

   不覆盖：菜单、窗口状态、外链拦截、打包（这些需要人工或真实交互）。

   用法：npm run smoke（需要先 build 前端与本包）。窗口不显示，退出码非 0 即失败。
   Linux CI 里需要 xvfb-run。
   ============================================================ */

import { BrowserWindow, app } from 'electron';
import { join } from 'node:path';

import { pingAgent, stopAgent } from './main/agent/supervisor';
import { capabilityManifest } from './main/capabilities';
import { installIpc } from './main/ipc/router';
import { APP_INDEX, buildCsp, handleAppProtocol, registerAppScheme } from './main/protocol';

const SERVER_URL = 'https://polaris.example.edu';
const problems: string[] = [];

registerAppScheme();

function check(label: string, ok: boolean, detail = ''): void {
  if (ok) {
    console.log(`  ok   ${label}`);
  } else {
    problems.push(label);
    console.log(`  FAIL ${label}${detail ? ` — ${detail}` : ''}`);
  }
}

void app.whenReady().then(async () => {
  handleAppProtocol(() => SERVER_URL);
  installIpc(); // preload 的 sendSync 依赖它，不装就测不到真实注入链路

  console.log('CSP');
  const csp = buildCsp(SERVER_URL);
  check("script-src 放行 wasm-unsafe-eval（pdf.js）", csp.includes("'wasm-unsafe-eval'"));
  check("style-src 放行 unsafe-inline（CodeMirror/KaTeX）", csp.includes("style-src 'self' 'unsafe-inline'"));
  check('connect-src 含服务器 https 源', csp.includes(SERVER_URL));
  check('connect-src 含服务器 wss 源', csp.includes('wss://polaris.example.edu'));
  check("worker-src 放行 blob:（pdf.js worker）", csp.includes("worker-src 'self' blob:"));
  check("frame-src 放行 blob:（写作页 PDF 预览）", csp.includes('frame-src blob:'));

  const win = new BrowserWindow({
    show: false,
    width: 1280,
    height: 800,
    webPreferences: {
      preload: join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      plugins: true,
    },
  });

  const consoleErrors: string[] = [];
  win.webContents.on('console-message', (_e, level, message) => {
    // level 3 = error
    if (level >= 2) consoleErrors.push(message);
  });

  console.log('\n协议与资源');
  const root = APP_INDEX;
  const fetchStatus = async (path: string): Promise<number> => {
    const res = await win.webContents.executeJavaScript(
      `fetch(${JSON.stringify(root + path)}).then(r => r.status).catch(() => 0)`,
    );
    return res as number;
  };

  try {
    await win.loadURL(APP_INDEX);
  } catch (err) {
    check('index.html 加载', false, String(err));
  }

  check('index.html 加载', win.webContents.getURL().startsWith('app://polaris'));

  // SPA fallback：无扩展名的深链接要回 index.html 而不是 404
  check('SPA fallback（/t/some-id）', (await fetchStatus('t/some-id')) === 200);
  // 带扩展名的缺失资源应当是 404，不能被 fallback 吞掉
  check('缺失资源仍是 404（/nope.js）', (await fetchStatus('nope.js')) === 404);
  // 路径穿越：Chromium 在请求到达 protocol.handle 之前就把 ../ 与 %2e%2e 一并
  // 归一化掉了，所以这些载荷实际都落到 SPA fallback（返回首页 200）。
  // 因此断言的是真正要保证的性质——任何载荷都拿不到系统文件的内容。
  // protocol.ts 里的 startsWith(root) 守卫仍然保留：不把「Chromium 会替我归一化」
  // 当作契约，那是纵深防御。
  const traversalBody = async (path: string): Promise<string> =>
    (await win.webContents.executeJavaScript(
      `fetch(${'`'}${'$'}{${JSON.stringify(root)}}${'$'}{${JSON.stringify(path)}}${'`'}).then(r => r.text()).catch(() => '')`,
    )) as string;
  for (const payload of ['../../../../etc/passwd', '%2e%2e/%2e%2e/%2e%2e/%2e%2e/etc/passwd']) {
    const body = await traversalBody(payload);
    check(`路径穿越读不到系统文件（${payload}）`, !body.includes('root:') && body.includes('<div id="root">'));
  }
  // pdf.js 的 cmaps 走绝对路径，必须能取到
  check('pdfjs cmaps 可取（/pdfjs/cmaps/Adobe-Japan1-UCS2.bcmap）',
    (await fetchStatus('pdfjs/cmaps/Adobe-Japan1-UCS2.bcmap')) === 200);

  console.log('\nPreload 与应用挂载');
  const injected = (await win.webContents.executeJavaScript(
    'JSON.stringify({ info: window.__POLARIS__ ?? null, bridge: typeof window.polaris })',
  )) as string;
  const state = JSON.parse(injected) as {
    info: { serverUrl?: string; platform?: string } | null;
    bridge: string;
  };
  check('window.__POLARIS__ 已注入', state.info != null && typeof state.info.platform === 'string');
  check('window.polaris 桥已暴露', state.bridge === 'object');

  const mounted = (await win.webContents.executeJavaScript(
    'document.querySelector("#root")?.childElementCount ?? 0',
  )) as number;
  check('React 应用已挂载（#root 有子节点）', mounted > 0);

  // 首启流程：桌面端未配置服务器时必须落在配置页，而不是登录页
  const title = (await win.webContents.executeJavaScript(
    'document.querySelector(".auth-card-title")?.textContent ?? ""',
  )) as string;
  check('未配置服务器时进入配置页', /连接到服务器|Connect to a server/.test(title), `title=${title}`);

  const secure = (await win.webContents.executeJavaScript('window.isSecureContext')) as boolean;
  check('secure context（clipboard / Notification 可用）', secure === true);

  console.log('\n第二期骨架（一期应当全部「不可用」但管道是通的）');
  const manifest = await capabilityManifest();
  check('契约版本已声明', manifest.contract >= 1);
  check(
    '所有本地能力一期均为不可用',
    Object.values(manifest.capabilities).every((c) => !c.available),
  );
  check(
    'tectonic 探测已真的执行（第二期直接用）',
    typeof (manifest.capabilities['latex.compile'].detail as { found?: boolean })?.found === 'boolean',
  );

  // 这是最关键的一条：证明 renderer → preload → router → supervisor → agent
  // 这条 stdio 管道现在就是通的，第二期只需要换掉 agent 侧的 handler。
  check('本地 agent 进程可探活（stdio JSON-RPC 往返）', await pingAgent());

  const localError = (await win.webContents.executeJavaScript(
    `window.polaris.invoke('local.latex.compile', { manuscriptId: 'x', engine: 'tectonic' })
       .then(() => 'UNEXPECTED_SUCCESS', e => String(e && e.message || e))`,
  )) as string;
  check(
    'local.* 以结构化的能力不可用错误结束',
    localError.includes('ERR_CAPABILITY_UNAVAILABLE'),
    localError.slice(0, 120),
  );
  check(
    '错误确实来自 agent 而不是 main 就地抛出',
    localError.includes('method not implemented in phase 1'),
    localError.slice(0, 120),
  );

  stopAgent();

  if (consoleErrors.length) {
    console.log('\n渲染进程 console 错误：');
    for (const m of consoleErrors.slice(0, 20)) console.log('  -', m);
  }
  check('渲染进程无 console 错误', consoleErrors.length === 0);

  console.log(problems.length ? `\n${problems.length} 项失败` : '\n全部通过');
  app.exit(problems.length ? 1 : 0);
});
