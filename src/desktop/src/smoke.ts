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
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync } from 'node:fs';
import { writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { gzipSync } from 'node:zlib';
import { join } from 'node:path';

import type { FetchImpl, SqliteTree, StorageService } from '@polaris/kernel';

import {
  engineBootstrapStatus,
  kernelPluginMeta,
  localBackend,
  marketPluginsDir,
  onEngineState,
  startKernel,
  stopKernel,
} from './main/kernel';
import { capabilityManifest } from './main/capabilities';
import { diagnostics } from './main/diagnostics';
import { chooseUpdate, parseRendererAsset, sameBackend } from './main/updates/engine-compat';
import { normalizeProxy, parseProxyRule } from './main/network';
import { installIpc, registeredMethods } from './main/ipc/router';
import {
  MARKET_ENDPOINT_META_KEY,
  awaitInstallForTesting,
  marketFetchIndex,
  marketGetEndpoint,
  marketInstall,
  marketSetEndpoint,
  marketUninstall,
  setMarketFetchForTesting,
} from './main/ipc/methods.market';
import { writeConfig } from './main/store';
import { pluginsDisable, pluginsEnable, pluginsExportTree, pluginsList } from './main/ipc/methods.plugins';
import { MARKET_ENDPOINT_DEFAULT } from './shared/contract';
import { extractTarGz } from './main/updates/tar';
import { compareVersions, stagedSupersedes } from './main/updates/version';
import { APP_INDEX, buildCsp, handleAppProtocol, registerAppScheme } from './main/protocol';

const LOCAL_ENGINE_URL = 'http://127.0.0.1:18080';
const problems: string[] = [];

// 全程用一次性 userData：storage 持久层（#609）起来后内核会真的写库，
// 不能把冒烟数据落进开发者真实的 userData。目录在退出前删除。
const smokeUserData = mkdtempSync(join(tmpdir(), 'polaris-smoke-'));
app.setPath('userData', smokeUserData);

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
  handleAppProtocol();
  installIpc(); // preload 的 sendSync 依赖它，不装就测不到真实注入链路
  // 白盒断言需要内核已就绪：这里显式 await（生产 main/index.ts 自 #721 起
  // 窗口先起、内核后台启动，渲染层靠首启等待页消化启动窗口期）
  const kernelInstance = await startKernel();

  console.log('CSP');
  const csp = buildCsp(LOCAL_ENGINE_URL);
  check("script-src 放行 wasm-unsafe-eval（pdf.js）", csp.includes("'wasm-unsafe-eval'"));
  check("style-src 放行 unsafe-inline（CodeMirror/KaTeX）", csp.includes("style-src 'self' 'unsafe-inline'"));
  check('connect-src 含本机引擎 http 源', csp.includes(LOCAL_ENGINE_URL));
  check('connect-src 含本机引擎 ws 源', csp.includes('ws://127.0.0.1:18080'));
  check('无本机引擎时 connect-src 只留自身与 blob:', !/connect-src [^;]*(https?|wss?):/.test(buildCsp(null)));
  // 结构化阅读把论文里的图改写成本机引擎地址再用 <img> 渲染：img-src / media-src
  // 不放行引擎源的话，这些图只在桌面端被拦（浏览器开发态同源，看不出来）
  check('img-src 含本机引擎源（结构化阅读的图）', new RegExp(`img-src [^;]*${LOCAL_ENGINE_URL}`).test(csp));
  check('media-src 含本机引擎源', new RegExp(`media-src [^;]*${LOCAL_ENGINE_URL}`).test(csp));
  check(
    '无本机引擎时 img-src / media-src 不放行任何网络源',
    !/(img|media)-src [^;]*(https?|wss?):/.test(buildCsp(null)),
  );
  check('img-src 不放行任意 http/https 源', !/img-src [^;]*\s(https?:|\*)(\s|;|$)/.test(csp));
  check("connect-src 放行 blob:（pdf.js 取正文）", /connect-src [^;]*\bblob:/.test(csp));
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
  win.webContents.on('console-message', ({ level, message }) => {
    if (level === 'error' || level === 'warning') consoleErrors.push(message);
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
    info: { platform?: string } | null;
    bridge: string;
  };
  check('window.__POLARIS__ 已注入', state.info != null && typeof state.info.platform === 'string');
  check('window.polaris 桥已暴露', state.bridge === 'object');

  const mounted = (await win.webContents.executeJavaScript(
    'document.querySelector("#root")?.childElementCount ?? 0',
  )) as number;
  check('React 应用已挂载（#root 有子节点）', mounted > 0);

  // 冒烟没有拉起本机引擎：必须落在「本机引擎没有运行」兜底页，而不是白屏或登录页
  const title = (await win.webContents.executeJavaScript(
    'document.querySelector(".auth-card-title")?.textContent ?? ""',
  )) as string;
  check('无本机引擎时进入兜底页', /本机引擎没有运行|The local engine isn’t running/.test(title), `title=${title}`);

  // macOS 的交通灯占据窗口左上角约 y=14..26（见 window.ts 的 trafficLightPosition），
  // 页面左上角的品牌标必须落在这条带子下面，否则会被压住。
  const geom = JSON.parse(
    (await win.webContents.executeJavaScript(`(() => {
      const el = document.querySelector('.auth-brand');
      const r = el ? el.getBoundingClientRect() : null;
      return JSON.stringify({
        platformAttr: document.documentElement.dataset.desktopPlatform ?? null,
        band: getComputedStyle(document.documentElement).getPropertyValue('--titlebar-h').trim(),
        brandTop: r ? Math.round(r.top) : null,
        brandLeft: r ? Math.round(r.left) : null,
      });
    })()`)) as string,
  ) as { platformAttr: string | null; band: string; brandTop: number | null; brandLeft: number | null };

  // 标题栏留白只在 macOS 需要：Windows/Linux 保留系统原生标题栏，页面不该预留、
  // 也不该自己做拖拽区。两边都正向断言，免得哪天在非 macOS 上误开。
  const isMac = process.platform === 'darwin';

  check('<html> 已标记桌面平台', geom.platformAttr === process.platform, `attr=${geom.platformAttr}`);
  if (isMac) {
    check('标题栏留白变量已生效', geom.band !== '' && geom.band !== '0px', `--titlebar-h=${geom.band}`);
    check(
      '品牌标避开交通灯（top ≥ 34）',
      geom.brandTop !== null && geom.brandTop >= 34,
      `top=${geom.brandTop} left=${geom.brandLeft}`,
    );
  } else {
    check('非 macOS 不预留标题栏', geom.band === '0px', `--titlebar-h=${geom.band}`);
  }

  // 顶部留白必须是拖拽区：内容盖住了系统标题栏，不声明就拖不动窗口。
  // （主内容区顶栏的 .crumb/.spacer 同理，但那要登录后才存在，smoke 覆盖不到。）
  const dragRegion = (await win.webContents.executeJavaScript(
    `getComputedStyle(document.querySelector('.auth-page'), '::before')
       .getPropertyValue('-webkit-app-region')`,
  )) as string;
  if (isMac) {
    check('顶部留白是拖拽区', dragRegion.trim() === 'drag', `app-region=${dragRegion}`);
  } else {
    check('非 macOS 顶部无拖拽区', dragRegion.trim() !== 'drag', `app-region=${dragRegion}`);
  }

  // 主内容顶栏要登录后才存在，这里注入一份同构 DOM 来验规则本身。
  // 重点是**高度**：.topbar 是 align-items:center，空的 .spacer 默认高度为 0，
  // 光有 -webkit-app-region:drag 也抓不住——这正是第一版漏掉的地方。
  const topbar = JSON.parse(
    (await win.webContents.executeJavaScript(`(() => {
      const bar = document.createElement('div');
      bar.className = 'topbar';
      bar.innerHTML = '<button class="icon-btn"></button><div class="crumb"><span>a</span></div><div class="spacer"></div>';
      document.body.appendChild(bar);
      const spacer = bar.querySelector('.spacer');
      const crumb = bar.querySelector('.crumb');
      const out = {
        spacerH: Math.round(spacer.getBoundingClientRect().height),
        crumbH: Math.round(crumb.getBoundingClientRect().height),
        spacerRegion: getComputedStyle(spacer).getPropertyValue('-webkit-app-region').trim(),
        btnRegion: getComputedStyle(bar.querySelector('.icon-btn')).getPropertyValue('-webkit-app-region').trim(),
      };
      bar.remove();
      return JSON.stringify(out);
    })()`)) as string,
  ) as { spacerH: number; crumbH: number; spacerRegion: string; btnRegion: string };

  if (isMac) {
    check('顶栏空白是拖拽区', topbar.spacerRegion === 'drag', `region=${topbar.spacerRegion}`);
    check(
      '顶栏空白有可抓取的高度',
      topbar.spacerH >= 40,
      `spacer=${topbar.spacerH}px crumb=${topbar.crumbH}px`,
    );
  } else {
    check('非 macOS 顶栏无拖拽区', topbar.spacerRegion !== 'drag', `region=${topbar.spacerRegion}`);
  }
  check('顶栏按钮不被拖拽区吞掉', topbar.btnRegion !== 'drag', `btn=${topbar.btnRegion}`);

  // 收起态侧栏必须容得下交通灯：灯组在窗口坐标里占 x=18..70pt（见 window.ts 的
  // trafficLightPosition），窄了灯就会压到主内容区上。侧栏要登录后才渲染，
  // 这里注入同构 DOM 验规则本身。
  const railPt = (await win.webContents.executeJavaScript(`(() => {
    const host = document.createElement('div');
    host.className = 'app nav-collapsed';
    host.style.cssText = 'position:fixed;left:-9999px;top:0;display:flex';
    host.innerHTML = '<div class="sidebar"></div>';
    document.body.appendChild(host);
    const w = host.querySelector('.sidebar').getBoundingClientRect().width;
    host.remove();
    return Math.round(w);
  })()`)) as number;

  // 收放时 logo 与 item 图标的包围盒必须逐像素不动。注意收起态 React 不渲染字标
  // （见 AppShell），所以这里也要把它从布局里去掉，否则测的不是真实 DOM。
  const anchors = JSON.parse(
    (await win.webContents.executeJavaScript(`(() => {
      const mk = (collapsed) => {
        const host = document.createElement('div');
        host.className = 'app' + (collapsed ? ' nav-collapsed' : '');
        host.style.cssText = 'position:fixed;left:0;top:0;display:flex';
        host.innerHTML =
          '<div class="sidebar"><div class="sb-brand">' +
          '<svg width="41" height="41" viewBox="0 0 41 41"></svg>' +
          (collapsed ? '' : '<svg width="110" height="30" viewBox="0 0 110 30"></svg>') +
          '</div><div class="sb-scroll"><a class="nav-item">' +
          '<span class="nav-ic"><svg viewBox="0 0 18 18"></svg></span>' +
          '<span class="nav-label">x</span></a></div></div>';
        document.body.appendChild(host);
        const r = (sel) => { const b = host.querySelector(sel).getBoundingClientRect();
          return [Math.round(b.left), Math.round(b.top), Math.round(b.width), Math.round(b.height)]; };
        const out = { rail: Math.round(host.querySelector('.sidebar').getBoundingClientRect().width),
                      logo: r('.sb-brand svg'), icon: r('.nav-ic') };
        host.remove();
        return out;
      };
      return JSON.stringify({ open: mk(false), collapsed: mk(true) });
    })()`)) as string,
  ) as { open: { rail: number; logo: number[]; icon: number[] }; collapsed: { rail: number; logo: number[]; icon: number[] } };

  const boxEq = (a: number[], b: number[]) => a.every((v, i) => Math.abs(v - (b[i] ?? 0)) <= 1);

  if (isMac) {
    check('收起态侧栏容得下交通灯（≥84pt）', railPt >= 84, `rail=${railPt}pt`);
    check(
      'logo 收放不位移不变形',
      boxEq(anchors.open.logo, anchors.collapsed.logo),
      `open=${anchors.open.logo} collapsed=${anchors.collapsed.logo}`,
    );
    check(
      'item 图标收放不位移',
      boxEq(anchors.open.icon, anchors.collapsed.icon),
      `open=${anchors.open.icon} collapsed=${anchors.collapsed.icon}`,
    );
    check(
      '收起态 logo 居中于轨道',
      Math.abs(anchors.collapsed.logo[0]! + anchors.collapsed.logo[2]! / 2 - anchors.collapsed.rail / 2) <= 2,
      `logo=${anchors.collapsed.logo} rail=${anchors.collapsed.rail}`,
    );
  } else {
    check('非 macOS 收起态侧栏保持原宽', railPt < 84, `rail=${railPt}pt`);
  }

  // Windows 的窗口控件排在顶栏右端（与铃铛同一行），页面靠 --winctl-w 留出等宽
  // 空位。smoke 跑在 macOS/Linux 上，这里临时把平台属性改成 win32 来验规则本身
  // ——CSS 完全由属性驱动，与进程真实平台无关。
  const winReserve = JSON.parse(
    (await win.webContents.executeJavaScript(`(() => {
      const root = document.documentElement;
      const prev = { p: root.dataset.desktopPlatform, t: root.dataset.desktopTitlebar };
      const host = document.createElement('div');
      host.style.cssText = 'position:fixed;left:0;top:0;width:800px';
      host.innerHTML = '<div class="topbar"><div class="crumb">c</div><div class="spacer"></div>' +
        '<button class="icon-btn" id="probe-bell">b</button></div>';
      document.body.appendChild(host);
      const bar = host.querySelector('.topbar');
      const bell = host.querySelector('#probe-bell');
      const read = () => ({
        top: Math.round(bar.getBoundingClientRect().top),
        padRight: Math.round(parseFloat(getComputedStyle(bar).paddingRight)),
        bellGap: Math.round(bar.getBoundingClientRect().right - bell.getBoundingClientRect().right),
        drag: getComputedStyle(host.querySelector('.spacer')).getPropertyValue('-webkit-app-region').trim(),
      });
      const web = read();
      root.dataset.desktopPlatform = 'win32';
      root.dataset.desktopTitlebar = 'overlay';
      const win32 = read();
      if (prev.p) root.dataset.desktopPlatform = prev.p; else delete root.dataset.desktopPlatform;
      if (prev.t) root.dataset.desktopTitlebar = prev.t; else delete root.dataset.desktopTitlebar;
      host.remove();
      return JSON.stringify({ web, win32 });
    })()`)) as string,
  ) as { web: { padRight: number }; win32: { top: number; padRight: number; bellGap: number; drag: string } };

  check(
    'Windows 顶栏不下移（控件与铃铛同一行）',
    winReserve.win32.top === 0,
    `top=${winReserve.win32.top}px`,
  );
  check(
    'Windows 顶栏右端为控件留位',
    winReserve.win32.bellGap > 100,
    `bellGap=${winReserve.win32.bellGap}px padRight=${winReserve.win32.padRight}px`,
  );
  check('Windows 顶栏可拖窗口', winReserve.win32.drag === 'drag', `region=${winReserve.win32.drag}`);
  check(
    'web 端顶栏留位不变',
    winReserve.web.padRight === 22,
    `padRight=${winReserve.web.padRight}px`,
  );

  const secure = (await win.webContents.executeJavaScript('window.isSecureContext')) as boolean;
  check('secure context（clipboard / Notification 可用）', secure === true);

  console.log('\n能力清单与 local.*');
  // 「设置 → 插件」目前整体关闭：默认能力位必须是不可用，界面才不会出现这一页
  delete process.env.POLARIS_PLUGINS_UI;
  check(
    'plugins.manage 默认不可用（插件页与市场暂不对用户开放）',
    capabilityManifest().capabilities['plugins.manage']?.available === false,
  );
  // 开发/测试开关打开后，下面照旧验证这套代码仍然是通的
  process.env.POLARIS_PLUGINS_UI = '1';
  const manifest = capabilityManifest();
  check('契约版本已声明', manifest.contract >= 1);

  // 方法表与契约版本是一对：#705 把 local.* 换成 kernel.*/plugins.* 却没动
  // CONTRACT_VERSION，老外壳因此会照收新界面——插件页按能力表自动隐藏，不报错，
  // 只是永远少一块。这条断言让「改表不改版本号」当场失败。
  const EXPECTED_METHODS: string[] = [
    'host.capabilities',
    'host.copyText',
    'host.diagnostics',
    'host.network.get',
    'host.network.set',
    'host.openExternal',
    'host.openLogsFolder',
    'host.relaunch',
    'host.setBadgeCount',
    'host.update.apply',
    'host.update.check',
    'kernel.engineBootstrapStatus',
    'kernel.localBackend',
    'kernel.status',
    'local.job.cancel',
    'plugins.disable',
    'plugins.enable',
    'plugins.exportTree',
    'plugins.importTree',
    'plugins.list',
    'plugins.market.fetchIndex',
    'plugins.market.getEndpoint',
    'plugins.market.install',
    'plugins.market.setEndpoint',
    'plugins.market.uninstall',
    'plugins.updateConfig',
    'plugins.validateConfig',
  ];
  const methods = registeredMethods();
  check(
    '方法表未变（变了就回头确认 CONTRACT_VERSION 是否要涨）',
    JSON.stringify(methods) === JSON.stringify(EXPECTED_METHODS),
    `actual=${JSON.stringify(methods)}`,
  );
  check(
    '能力清单里没有永远为 false 的占位能力（latex.compile 已删）',
    !('latex.compile' in manifest.capabilities),
  );
  // plugins.manage（#705）：开关打开、kernel 已起、树已就绪
  check(
    'plugins.manage 能力位可用（开关打开且配置树已就绪）',
    manifest.capabilities['plugins.manage']?.available === true,
    JSON.stringify(manifest.capabilities['plugins.manage']),
  );

  // 旧 stdio agent 及其三个占位方法已整体拆除（#731）：这些方法名如今必须是
  // 「未知方法」——若这条断言变红，说明有人把半截管道又接了回来。
  const removedLocal = (await win.webContents.executeJavaScript(
    `window.polaris.invoke('local.latex.compile', { manuscriptId: 'x', engine: 'tectonic' })
       .then(() => 'UNEXPECTED_SUCCESS', e => String(e && e.message || e))`,
  )) as string;
  check(
    '已拆除的 local.* 方法返回 ERR_UNKNOWN_METHOD',
    removedLocal.includes('ERR_UNKNOWN_METHOD'),
    removedLocal.slice(0, 120),
  );
  // local.job.cancel 是唯一留下的 local.* 方法（main 内的 job 簿记，与 agent
  // 无关）：取消不存在的 job 是 no-op，renderer 全链路必须能正常往返。
  const cancelRoundtrip = (await win.webContents.executeJavaScript(
    `window.polaris.invoke('local.job.cancel', { jobId: 'no-such-job' })
       .then(() => 'ok', e => String(e && e.message || e))`,
  )) as string;
  check('local.job.cancel 经 IPC 往返仍可用', cancelRoundtrip === 'ok', cancelRoundtrip);

  // 内核：证明 @polaris/kernel 真的挂在主进程里，且 renderer 能经唯一
  // IPC 通道读到它的状态（renderer → preload → router → kernel 单例）。
  console.log('\n内核');
  const kernelState = (await win.webContents.executeJavaScript(
    `window.polaris.invoke('kernel.status').catch(e => ({ error: String(e && e.message || e) }))`,
  )) as { started?: boolean; name?: string; plugins?: number; storage?: boolean; error?: string };
  check('kernel.status 经 IPC 往返返回', kernelState.error === undefined, kernelState.error ?? '');
  check('内核已启动（started=true）', kernelState.started === true);
  check('实例名为 polaris-desktop', kernelState.name === 'polaris-desktop', `name=${kernelState.name}`);
  // registry 至少有：直挂的 storage、Loader（连带其内部 isolate）、SqliteTree。
  // 树里不再种任何内置条目；计数只会随用户装的插件增多而涨，用 ≥ 兜底。
  check('插件宿主已建立（plugins ≥ 3）', (kernelState.plugins ?? 0) >= 3, `plugins=${kernelState.plugins}`);

  // 配置树只装用户自己装的插件：全新安装是一棵空树。引擎不在树里（它由宿主
  // 直挂，见 main/kernel.ts），所以用户在任何界面都关不掉它，树写入也带不走它的
  // 启动参数。
  console.log('\n配置树');
  const tree = kernelInstance.ctx.get('configTree') as SqliteTree | undefined;
  check('configTree 服务可从 ctx 读到', tree != null);
  check('全新安装的树是空的（没有内置条目）', pluginsList().length === 0, `ids=${pluginsList().map((i) => i.id).join(',')}`);

  // plugins.* IPC（#705）：与 kernel.status 同款调用路径——直接调 router
  // 背后的具名实现（router 只做参数形状分发）。语义细节在 kernel 的
  // plugins-manage.test 里测全，这里只验「桌面接线真的驱动 loader」。
  // 树里没有现成条目可用，冒烟自己登记一个空插件当样本（只活在这次会话的
  // 内置表里），用完即删。
  console.log('\nplugins.* IPC');
  kernelInstance.ctx.loader.builtins['smoke-probe'] = { name: 'smoke-probe', apply(): void {} };
  await tree?.create({ id: 'smoke-probe', name: 'cordis:smoke-probe' } as Parameters<SqliteTree['create']>[0]);
  const pluginList = pluginsList();
  check(
    'plugins.list 返回新建的条目且 state=active',
    pluginList.find((info) => info.id === 'smoke-probe')?.state === 'active',
    `list=${JSON.stringify(pluginList)}`,
  );
  const treeExport = pluginsExportTree();
  check(
    'plugins.exportTree 带版本号且含该条目',
    treeExport.version === 1 && treeExport.entries.some((entry) => entry.id === 'smoke-probe'),
  );
  const probeDisabled = await pluginsDisable('smoke-probe');
  check(
    'plugins.disable 停掉 fiber',
    probeDisabled.state === 'disabled' && tree?.store['smoke-probe']?.fiber == null,
    `state=${probeDisabled.state}`,
  );
  const probeEnabled = await pluginsEnable('smoke-probe');
  check(
    'plugins.enable 重建 fiber',
    probeEnabled.state === 'active' && tree?.store['smoke-probe']?.fiber != null,
    `state=${probeEnabled.state}`,
  );
  await tree?.remove('smoke-probe');
  await tree?.flush();

  // 插件市场（#708）：源配置持久化 + 全离线的「安装→启用→拒卸→卸载」
  // 闭环。fetch 换成进程内替身（registry packument + tarball 都是现造的），
  // 但其余全是真的：真解压落盘到 userData/plugins、真写 PluginMetaStore、
  // 真在配置树上挂 file:// 条目、启用时真的动态 import——这条断言顺带
  // 守住 esbuild CJS 打包必须保留原生 import() 的前提（R1 家族）。
  console.log('\n插件市场（plugins.market.*）');
  check(
    '默认索引源为官方 raw URL',
    marketGetEndpoint().endpoint === MARKET_ENDPOINT_DEFAULT && marketGetEndpoint().isDefault,
  );
  marketSetEndpoint('https://mirror.example.edu/market/index.json');
  check(
    'setEndpoint 持久化并回读（isDefault=false）',
    marketGetEndpoint().endpoint === 'https://mirror.example.edu/market/index.json'
      && !marketGetEndpoint().isDefault,
  );
  marketSetEndpoint('');
  check('空串复位官方默认源', marketGetEndpoint().isDefault);

  // #737 配置分层：索引源的真相在 kernel KV（PluginMetaStore），旧 electron
  // store 只是读穿回退 + 一次性迁移源。
  const endpointMeta = kernelPluginMeta();
  check(
    'setEndpoint 落 kernel KV（market:endpoint）',
    endpointMeta?.get(MARKET_ENDPOINT_META_KEY) === MARKET_ENDPOINT_DEFAULT,
  );
  endpointMeta?.delete(MARKET_ENDPOINT_META_KEY);
  writeConfig({ marketEndpoint: 'https://legacy.example.edu/market/index.json' });
  check(
    '读穿 shim：KV 无值时回读旧 store 并迁移写入 KV',
    marketGetEndpoint().endpoint === 'https://legacy.example.edu/market/index.json'
      && endpointMeta?.get(MARKET_ENDPOINT_META_KEY)
        === 'https://legacy.example.edu/market/index.json',
  );
  // 复位两层，别让后面的安装链路拿错索引源
  marketSetEndpoint('');
  writeConfig({ marketEndpoint: MARKET_ENDPOINT_DEFAULT });
  check('shim 迁移后空串仍复位官方默认源', marketGetEndpoint().isDefault);

  // 现造一个合法的 npm 发布包（ustar 头带校验和：市场解包器会验，
  // 上面更新包用的 tarEntry 不带校验和，不能复用）
  const marketTar = (name: string, body: string): Buffer => {
    const data = Buffer.from(body, 'utf8');
    const header = Buffer.alloc(512);
    header.write(name, 0, 100, 'utf8');
    header.write('0000644\0', 100);
    header.write('0000000\0', 108);
    header.write('0000000\0', 116);
    header.write(data.length.toString(8).padStart(11, '0') + '\0', 124);
    header.write('00000000000\0', 136);
    header.write('        ', 148);
    header.write('0', 156);
    header.write('ustar\0', 257);
    header.write('00', 263);
    let sum = 0;
    for (const byte of header) sum += byte;
    header.write(sum.toString(8).padStart(6, '0') + '\0 ', 148);
    const padded = Buffer.alloc(Math.ceil(data.length / 512) * 512);
    data.copy(padded);
    return Buffer.concat([header, padded]);
  };
  const HELLO = 'polaris-plugin-hello';
  const helloPkgJson = JSON.stringify({
    name: HELLO,
    version: '1.0.0',
    description: 'A tiny smoke plugin',
    polaris: { kind: 'panel', entry: 'index.js' },
  });
  const helloEntry = "module.exports = { name: 'hello-smoke', apply() {} };\n";
  const helloTgz = gzipSync(
    Buffer.concat([
      marketTar('package/package.json', helloPkgJson),
      marketTar('package/index.js', helloEntry),
      Buffer.alloc(1024),
    ]),
  );
  const tarballUrl = `https://registry.npmjs.org/${HELLO}/-/${HELLO}-1.0.0.tgz`;
  const indexEntry = {
    name: HELLO,
    version: '1.0.0',
    kind: 'panel',
    description: 'A tiny smoke plugin',
    publisher: 'polaris',
    permissions: {},
    tier: 'bronze',
    badges: ['official'],
  };
  setMarketFetchForTesting((async (input: unknown) => {
    const url = String(input);
    if (url === MARKET_ENDPOINT_DEFAULT) {
      return Response.json({ schemaVersion: 1, plugins: [indexEntry] });
    }
    if (url === `https://registry.npmjs.org/${HELLO}`) {
      return Response.json({
        name: HELLO,
        versions: {
          '1.0.0': {
            dist: {
              tarball: tarballUrl,
              integrity: `sha512-${createHash('sha512').update(helloTgz).digest('base64')}`,
            },
          },
        },
      });
    }
    if (url === tarballUrl) return new Response(new Uint8Array(helloTgz));
    return new Response('not found', { status: 404 });
  }) as FetchImpl);

  try {
    const indexEntries = await marketFetchIndex().catch((err) => `threw: ${String(err)}`);
    check(
      'fetchIndex 返回校验后的条目',
      Array.isArray(indexEntries) && indexEntries.length === 1 && indexEntries[0].name === HELLO,
      typeof indexEntries === 'string' ? indexEntries : `count=${indexEntries.length}`,
    );

    const handle = marketInstall(HELLO, '1.0.0');
    await awaitInstallForTesting(handle.jobId);
    const meta = kernelPluginMeta();
    check('安装记录已落 PluginMetaStore', meta?.get(`market:install:${HELLO}`) != null);
    const installed = tree?.store[HELLO];
    check(
      '安装后树条目 = file:// 入口 + disabled（装/启分离）',
      installed != null
        && installed.options.name.startsWith('file://')
        && installed.options.disabled === true
        && installed.fiber == null,
      `name=${installed?.options.name} disabled=${installed?.options.disabled}`,
    );
    check(
      'plugins.list 可见安装物且 state=disabled',
      pluginsList().find((info) => info.id === HELLO)?.state === 'disabled',
    );

    const helloEnabled = await pluginsEnable(HELLO).catch((err) => `threw: ${String(err)}`);
    check(
      '安装物可启用（file:// 动态 import 在打包主进程真实走通）',
      typeof helloEnabled !== 'string' && helloEnabled.state === 'active',
      typeof helloEnabled === 'string' ? helloEnabled : `state=${helloEnabled.state}`,
    );

    const refused = await marketUninstall(HELLO);
    check(
      'enabled 状态拒卸（错误作为数据返回）',
      refused.ok === false && refused.code === 'plugin-enabled',
      JSON.stringify(refused),
    );

    await pluginsDisable(HELLO);
    const removed = await marketUninstall(HELLO);
    check('禁用后卸载成功', removed.ok === true, JSON.stringify(removed));
    check(
      '卸载后树条目与盘面均已清理',
      tree?.store[HELLO] == null && !existsSync(join(marketPluginsDir(), HELLO)),
    );
    check('卸载后安装记录已删', meta?.get(`market:install:${HELLO}`) === undefined);
  } finally {
    setMarketFetchForTesting(undefined);
  }

  // storage 持久层（#609）：就绪性经 IPC 可见，数据要真的穿过一次「停机 →
  // 重启」仍然在——这正是配置树持久化存在的意义，光断言服务挂着不够。
  console.log('\n持久层');
  check('kernel.status 报告 storage 就绪', kernelState.storage === true);
  const storageSvc = kernelInstance.ctx.get('storage') as StorageService | undefined;
  check('storage 服务可从 ctx 读到', storageSvc != null);
  check(
    'storage 落在 userData/kernel/ 下',
    storageSvc?.path.startsWith(join(app.getPath('userData'), 'kernel')) === true,
    `path=${storageSvc?.path}`,
  );
  if (storageSvc) {
    // 一行用户条目 + 旧版本种在树里的三条内置条目（其中引擎那条还带着泄进树的
    // 启动命令——正是旧逻辑在任何一次树写入后留下的样子）。重启后前者必须原样
    // 还在，后三条必须被清掉，而且绝不能有引擎被那条旧命令拉起来。
    // disabled: true 让假插件名（smoke-plugin 并不存在）在重启装载时不会被 import。
    const current = await storageSvc.configTree.load();
    await storageSvc.configTree.save([
      ...current,
      { id: 'smoke-entry', name: 'smoke-plugin', disabled: true, config: { touched: true } },
      { id: 'desktop-probe', name: 'cordis:desktop-probe', config: { smokeTouched: true } },
      { id: 'sources', name: 'cordis:sources' },
      {
        id: 'legacy-engine',
        name: 'cordis:legacy-engine',
        config: { mode: 'command', command: ['/nonexistent/old-install/venv/bin/python', '-c', 'pass'], port: 18080 },
      },
    ]);
  }

  // smoke 用 app.exit 直接退出、不经过 before-quit，这里手动停机以覆盖
  // stop 路径（fiber.dispose 级联）不抛错。
  await stopKernel();

  // 重启内核（同一 userData → 同一 db 文件），配置树必须还能读回来
  const reopened = await startKernel();
  const reopenedSvc = reopened.ctx.get('storage') as StorageService | undefined;
  const entries = reopenedSvc ? await reopenedSvc.configTree.load() : [];
  check(
    '重启内核后配置树数据仍在',
    entries.some(
      (e) => e.id === 'smoke-entry' && (e.config as { touched?: boolean } | undefined)?.touched === true,
    ),
    `entries=${JSON.stringify(entries)}`,
  );

  // 旧版本留下的内置条目：装载前清掉（否则一条未知内置条目就让整棵树装不起来），
  // 用户自己的条目不受影响。
  const reopenedTree = reopened.ctx.get('configTree') as SqliteTree | undefined;
  check('重启后配置树照常装载（没被旧条目拖垮）', reopenedTree != null);
  check(
    '旧版本的内置条目已从库里清掉',
    !entries.some((e) => ['desktop-probe', 'sources', 'legacy-engine'].includes(e.id)),
    `ids=${entries.map((e) => e.id).join(',')}`,
  );
  check('库里不再有引擎的启动命令', !JSON.stringify(entries).includes('old-install'));
  if (reopenedTree) {
    check(
      '树里只剩用户条目',
      [...reopenedTree.entries()].map((entry) => entry.id).join(',') === 'smoke-entry',
      `ids=${[...reopenedTree.entries()].map((entry) => entry.id).join(',')}`,
    );
  }
  check('没有引擎被旧条目拉起来', localBackend().baseUrl === null && localBackend().state === 'none');
  const backups = existsSync(join(app.getPath('userData'), 'kernel', 'backups'))
    ? readdirSync(join(app.getPath('userData'), 'kernel', 'backups'))
    : [];
  check('清理前的树留了备份文件', backups.some((name) => name.startsWith('config-tree-dropped-builtins-')), backups.join(','));
  await stopKernel();

  // 本机引擎（command 模式，就是安装包用的那种）：一个 node 写的假引擎，任何
  // 机器都能跑。验的是外壳这一层的接线——引擎不进配置树、日志落盘、中途被杀
  // 会自动重启并通知界面、诊断信息拿得到、停机不留进程。
  console.log('\n本机引擎（command 模式）');
  {
    const enginePort = 20000 + Math.floor(Math.random() * 20000);
    const engineScript = [
      "const http = require('node:http');",
      "console.log('fake engine up, key=' + (process.env.POLARIS_SMOKE_API_KEY || ''));",
      'http.createServer((req, res) => {',
      "  res.setHeader('content-type', 'application/json');",
      "  res.end(JSON.stringify({ status: 'ok', version: '9.9.9-smoke', pid: process.pid, instance_id: process.env.POLARIS_INSTANCE_ID || '' }));",
      `}).listen(${enginePort}, '127.0.0.1');`,
    ].join('\n');
    process.env.POLARIS_DESKTOP_ENGINE = `command:${JSON.stringify(['node', '-e', engineScript])}`;
    process.env.POLARIS_DESKTOP_ENGINE_PORT = String(enginePort);
    process.env.POLARIS_SMOKE_API_KEY = 'smoke-secret-value-do-not-log';
    const states: string[] = [];
    const unsubscribe = onEngineState((event) => states.push(event.state));
    const waitFor = async (cond: () => boolean, ms: number): Promise<boolean> => {
      const deadline = Date.now() + ms;
      while (Date.now() < deadline) {
        if (cond()) return true;
        await new Promise((r) => setTimeout(r, 50));
      }
      return cond();
    };
    const enginePid = async (baseUrl: string): Promise<number> =>
      ((await (await fetch(`${baseUrl}/api/health`)).json()) as { pid: number }).pid;
    const alive = (pid: number): boolean => {
      try {
        process.kill(pid, 0);
        return true;
      } catch {
        return false;
      }
    };
    try {
      const engineKernel = await startKernel();
      const { baseUrl, state } = localBackend();
      check('引擎已起来（baseUrl 非空、state=running）', baseUrl === `http://127.0.0.1:${enginePort}` && state === 'running', `baseUrl=${baseUrl} state=${state}`);
      check('显式引擎路径的引导状态收尾为 idle', engineBootstrapStatus().phase === 'idle' && engineBootstrapStatus().done);

      // 引擎参数不进配置树：做几次树写入，库里和导出里都不能出现它
      const engineTree = engineKernel.ctx.get('configTree') as SqliteTree | undefined;
      await engineTree?.update('smoke-entry', { config: { touched: 'again' } });
      await engineTree?.flush();
      const stored = JSON.stringify(await (engineKernel.ctx.get('storage') as StorageService).configTree.load());
      check(
        '树写入后库里没有引擎条目与启动命令',
        !stored.includes('legacy-engine') && !stored.includes('fake engine up') && !stored.includes(String(enginePort)),
        stored.slice(0, 200),
      );
      check('plugins.list 里没有引擎', !pluginsList().some((info) => /engine/.test(info.id)));

      // 日志落盘 + 抹密钥
      const engineLog = join(app.getPath('userData'), 'logs', 'engine.log');
      await waitFor(() => existsSync(engineLog) && readFileSync(engineLog, 'utf8').includes('fake engine up'), 5_000);
      const logText = existsSync(engineLog) ? readFileSync(engineLog, 'utf8') : '';
      check('引擎输出写进 logs/engine.log', logText.includes('fake engine up'));
      check('日志里没有密钥原文', !logText.includes('smoke-secret-value-do-not-log') && logText.includes('[redacted:POLARIS_SMOKE_API_KEY]'));

      // 诊断信息
      const diag = await diagnostics();
      check('诊断信息带引擎自报的版本', diag.engineVersion === '9.9.9-smoke', `engineVersion=${diag.engineVersion}`);
      check(
        '诊断文本含版本、系统、引擎日志',
        diag.text.includes(`Polaris ${app.getVersion()}`)
          && diag.text.includes('Engine: 9.9.9-smoke')
          && diag.text.includes('System: ')
          && diag.text.includes('fake engine up'),
      );
      check('诊断文本里没有密钥原文', !diag.text.includes('smoke-secret-value-do-not-log'));
      check('诊断信息给出日志文件夹', diag.logsDir === join(app.getPath('userData'), 'logs'));

      // 中途杀掉引擎：自动重启一次，状态事件依次是 restarting → running
      const firstPid = baseUrl ? await enginePid(baseUrl) : 0;
      process.kill(firstPid, 'SIGKILL');
      const sawRestarting = await waitFor(() => states.includes('restarting'), 5_000);
      check('引擎被杀后进入 restarting 并通知订阅方', sawRestarting, `states=${states.join(',')}`);
      const during = engineBootstrapStatus();
      check(
        '重启期间：本地地址不可用，引导状态回到 engine 且带 restarting 标记',
        states.includes('running') || (localBackend().baseUrl === null && during.phase === 'engine' && during.restarting === true),
        `phase=${during.phase} restarting=${during.restarting}`,
      );
      const recovered = await waitFor(() => states.includes('running'), 15_000);
      check('引擎自动重启成功（running）', recovered, `states=${states.join(',')}`);
      const after = localBackend();
      const secondPid = after.baseUrl ? await enginePid(after.baseUrl) : 0;
      check('重启后是新进程、地址恢复可用', after.baseUrl === baseUrl && secondPid !== 0 && secondPid !== firstPid);
      check('引导状态回到 ready', engineBootstrapStatus().phase === 'ready' && engineBootstrapStatus().done);

      // 再杀一次：十分钟内只自动重启一次，这次应当停在 failed 并给出原因
      process.kill(secondPid, 'SIGKILL');
      const gaveUp = await waitFor(() => states.includes('failed'), 10_000);
      check('第二次崩溃不再重启（failed）', gaveUp, `states=${states.join(',')}`);
      const failed = engineBootstrapStatus();
      check(
        '失败状态带原因与最后几行日志',
        failed.phase === 'failed' && failed.failure?.code === 'engine-crashed' && (failed.failure?.lines.length ?? 0) > 0,
        JSON.stringify(failed.failure ?? null).slice(0, 200),
      );
      check('failed 时本地地址为空', localBackend().baseUrl === null && localBackend().state === 'failed');

      await stopKernel();
      check('停机后没有残留引擎进程', !alive(firstPid) && !alive(secondPid));
      check('停机后孤儿记录已清', !existsSync(join(app.getPath('userData'), 'engine.pid.json')));
    } finally {
      unsubscribe();
      await stopKernel();
      delete process.env.POLARIS_DESKTOP_ENGINE;
      delete process.env.POLARIS_DESKTOP_ENGINE_PORT;
      delete process.env.POLARIS_SMOKE_API_KEY;
    }

    // 启动途中退出：引擎进程活着但迟迟不就绪（首启跑迁移时就是这样），
    // 停机必须立刻打断等待并回收它，而不是等满两分钟的健康超时。
    process.env.POLARIS_DESKTOP_ENGINE = `command:${JSON.stringify(['node', '-e', "console.log('slow pid=' + process.pid); setInterval(() => {}, 1000)"])}`;
    process.env.POLARIS_DESKTOP_ENGINE_PORT = String(enginePort + 1);
    try {
      const starting = startKernel().then(() => 'started', () => 'rejected');
      await new Promise((r) => setTimeout(r, 1_200));
      const t0 = Date.now();
      await stopKernel();
      const outcome = await starting;
      const took = Date.now() - t0;
      check('启动途中停机在几秒内完成', took < 8_000, `took=${took}ms`);
      check('被打断的启动没有留下可用的内核', outcome === 'rejected' && localBackend().baseUrl === null, `outcome=${outcome}`);
      const slowLog = readFileSync(join(app.getPath('userData'), 'logs', 'engine.log'), 'utf8');
      const slowPid = Number(/slow pid=(\d+)/.exec(slowLog)?.[1] ?? 0);
      check('被打断的引擎进程已回收', slowPid > 0 && (await waitFor(() => !alive(slowPid), 6_000)), `pid=${slowPid}`);
    } finally {
      await stopKernel();
      delete process.env.POLARIS_DESKTOP_ENGINE;
      delete process.env.POLARIS_DESKTOP_ENGINE_PORT;
    }
  }

  if (consoleErrors.length) {
    console.log('\n渲染进程 console 错误：');
    for (const m of consoleErrors.slice(0, 20)) console.log('  -', m);
  }
  check('渲染进程无 console 错误', consoleErrors.length === 0);

  // 更新包是从网络下载的，解包器按不可信输入处理。这段跑在主进程里，可以直接
  // 调到解包函数，所以把安全边界直接测了，而不是只测「功能能用」。
  console.log('\n更新包解包（安全边界）');
  const tarEntry = (name: string, body: string, typeflag = '0'): Buffer => {
    const header = Buffer.alloc(512);
    header.write(name, 0, 100, 'utf8');
    header.write('000644 \0', 100, 8, 'utf8');
    header.write(body.length.toString(8).padStart(11, '0') + ' ', 124, 12, 'utf8');
    header.write(typeflag, 156, 1, 'utf8');
    const data = Buffer.alloc(Math.ceil(body.length / 512) * 512);
    data.write(body, 0, 'utf8');
    return Buffer.concat([header, data]);
  };
  const gzip = (b: Buffer) => gzipSync(b);
  const scratch = mkdtempSync(join(tmpdir(), 'polaris-tar-'));

  const okArchive = gzip(Buffer.concat([tarEntry('meta.json', '{}'), Buffer.alloc(1024)]));
  let extracted: string[] = [];
  try {
    extracted = extractTarGz(okArchive, scratch);
  } catch (err) {
    extracted = [`threw: ${String(err)}`];
  }
  check('正常包可解出文件', extracted.includes('meta.json'), `files=${extracted.join(',')}`);

  // CI 用 `tar -C dist .` 打包，条目名带 ./ 前缀。第一版漏了这个形态，导致
  // index.html 的存在性检查永远失败、热更新必然被拒——只有对着真实产物才查得出来。
  let dotted: string[] = [];
  try {
    dotted = extractTarGz(
      gzip(Buffer.concat([tarEntry('./index.html', '<html></html>'), Buffer.alloc(1024)])),
      scratch,
    );
  } catch (err) {
    dotted = [`threw: ${String(err)}`];
  }
  check('./ 前缀的条目名被归一化', dotted.includes('index.html'), `files=${dotted.join(',')}`);

  // 同一条命令的第一个条目是 "./" 目录本身，归一化后为空——必须跳过而不是报错。
  let withRoot: string[] = [];
  try {
    withRoot = extractTarGz(
      gzip(Buffer.concat([tarEntry('./', '', '5'), tarEntry('./index.html', 'x'), Buffer.alloc(1024)])),
      scratch,
    );
  } catch (err) {
    withRoot = [`threw: ${String(err)}`];
  }
  check('根目录条目 "./" 被跳过而非报错', withRoot.includes('index.html'), `files=${withRoot.join(',')}`);

  const rejects = (label: string, archive: Buffer) => {
    let threw = false;
    try {
      extractTarGz(archive, scratch);
    } catch {
      threw = true;
    }
    check(label, threw);
  };
  rejects(
    '拒绝路径穿越条目（../evil）',
    gzip(Buffer.concat([tarEntry('../evil.txt', 'x'), Buffer.alloc(1024)])),
  );
  rejects(
    '拒绝软链条目（可绕过路径检查）',
    gzip(Buffer.concat([tarEntry('link', '', '2'), Buffer.alloc(1024)])),
  );
  rmSync(scratch, { recursive: true, force: true });

  console.log('\n[版本比较]');
  check('版本序：patch/minor/major', compareVersions('0.3.2', '0.3.1') > 0 && compareVersions('0.3.10', '0.3.9') > 0 && compareVersions('0.4.0', '0.3.99') > 0);
  check('正式版高于同号预发布', compareVersions('0.3.1', '0.3.1-win-test') > 0 && compareVersions('0.3.1', '0.3.1') === 0);
  // 热更新只换界面，外壳版本不动。装完 0.3.2 后若仍拿外壳的 0.3.1 去比，
  // 同一个更新会被无限提示——这两条就是防这个的。
  check('热更装上的新界面参与版本比较', stagedSupersedes('0.3.2', '0.3.1'));
  check('整包更新反超后旧界面失效', !stagedSupersedes('0.3.2', '0.4.0') && !stagedSupersedes('0.3.2', '0.3.2'));

  // 「只换界面」的判据：契约号管 preload，后端哈希管 Python 引擎。只看前者的旧逻辑
  // 会让老安装装上新界面、引擎却停在旧版，还自认为已是最新。
  console.log('\n[更新方式的选择]');
  const HASH_A = 'a'.repeat(64);
  const HASH_B = 'b'.repeat(64);
  const installerAsset = { name: 'Polaris-0.6.0-universal.dmg' };
  const rendererFor = (hash: string, contract = 4) => ({ name: `renderer-0.6.0-c${contract}-b${hash.slice(0, 16)}.tar.gz` });
  const facts = { latestVersion: '0.6.0', contract: 4, backendHash: HASH_A };
  const same = chooseUpdate([rendererFor(HASH_A), installerAsset], installerAsset, facts);
  check('引擎相同 → 只换界面', same?.kind === 'hot' && same.renderer?.backendHash === HASH_A.slice(0, 16));
  const engineChanged = chooseUpdate([rendererFor(HASH_B), installerAsset], installerAsset, facts);
  check(
    '引擎不同 → 走安装器，并说明是引擎变了',
    engineChanged?.kind === 'full' && engineChanged.asset === installerAsset && engineChanged.fullReason === 'engine',
  );
  const shellChanged = chooseUpdate([rendererFor(HASH_A, 5), installerAsset], installerAsset, facts);
  check('契约更高 → 走安装器（外壳要换）', shellChanged?.kind === 'full' && shellChanged.fullReason === 'shell');
  const legacyName = chooseUpdate([{ name: 'renderer-0.6.0-c3.tar.gz' }, installerAsset], installerAsset, facts);
  check('不带后端哈希的旧格式界面包不算数', legacyName?.kind === 'full' && legacyName.fullReason === 'no-renderer');
  const fromSource = chooseUpdate([rendererFor(HASH_A), installerAsset], installerAsset, { ...facts, backendHash: null });
  check('本机引擎哈希未知（从源码跑）→ 不做只换界面的更新', fromSource?.kind === 'full');
  check('需要安装器却没有适合本机的 → 无可用更新包', chooseUpdate([rendererFor(HASH_B)], undefined, facts) === null);
  check('别的版本的界面包被忽略', chooseUpdate([{ name: `renderer-0.5.9-c4-b${HASH_A.slice(0, 16)}.tar.gz` }], installerAsset, facts)?.kind === 'full');
  check(
    '文件名解析与哈希前缀比较',
    parseRendererAsset(rendererFor(HASH_A).name)?.contract === 4
      && sameBackend(HASH_A.slice(0, 16), HASH_A)
      && !sameBackend(HASH_A.slice(0, 16), HASH_B)
      && !sameBackend('', HASH_A)
      && !sameBackend('aaaa', HASH_A),
  );
  // 旧外壳的文件名正则（只认 renderer-<版本>-c<契约>.tar.gz）必须认不出新格式：
  // 这样已经装了旧版的机器会回落到安装器，而不是再被只换一次界面。
  check('旧外壳认不出新格式的界面包', !/^renderer-(.+)-c(\d+)\.tar\.gz$/.test(rendererFor(HASH_A).name));

  console.log('\n[联网设置]');
  check('代理地址：host:port 补成 http://', normalizeProxy(' 127.0.0.1:7890 ') === 'http://127.0.0.1:7890');
  check('代理地址：socks5 原样保留、去掉尾部斜杠', normalizeProxy('socks5://127.0.0.1:1080/') === 'socks5://127.0.0.1:1080');
  check('代理地址：空串 = 清除', normalizeProxy('   ') === '');
  check('代理地址：不认识的协议拒绝', (() => { try { normalizeProxy('ftp://x:1'); return false; } catch { return true; } })());
  check(
    '系统代理规则解析',
    parseProxyRule('PROXY 127.0.0.1:7890; DIRECT') === 'http://127.0.0.1:7890'
      && parseProxyRule('SOCKS5 127.0.0.1:1080') === 'socks5://127.0.0.1:1080'
      && parseProxyRule('DIRECT') === null,
  );

  // 本地引擎（P1-A4）：真的用 docker 拉起 Python 后端并走一遍
  // startKernel → kernel.localBackend → /api/health → stopKernel 回收。
  // 依赖本机 docker 与测试镜像，默认关闭：设 POLARIS_SMOKE_ENGINE=1 才跑，
  // 前置不满足输出 skip 而不是失败（CI 无 docker 时冒烟仍然全绿）。
  console.log('\n本地引擎');
  const ENGINE_IMAGE = 'polaris-api-test:local';
  const ENGINE_CONTAINER = 'polaris-desktop-engine';
  const dockerHasImage = (() => {
    try {
      execFileSync('docker', ['image', 'inspect', ENGINE_IMAGE], { stdio: 'ignore' });
      return true;
    } catch {
      return false;
    }
  })();
  if (process.env.POLARIS_SMOKE_ENGINE !== '1') {
    console.log('  skip 本地引擎组（未设 POLARIS_SMOKE_ENGINE=1）');
  } else if (!dockerHasImage) {
    console.log(`  skip 本地引擎组（docker 不可用或缺镜像 ${ENGINE_IMAGE}）`);
  } else {
    // 清掉上一轮可能的残留容器，保证本组幂等
    try {
      execFileSync('docker', ['rm', '-f', ENGINE_CONTAINER], { stdio: 'ignore' });
    } catch {
      /* 不存在 */
    }
    // __dirname = src/desktop/dist，后端源码在仓库的 src/backend
    const backendDir = join(__dirname, '..', '..', 'backend');
    process.env.POLARIS_DESKTOP_ENGINE = `docker:${ENGINE_IMAGE}:${backendDir}`;
    // fake LLM 回退是严格显式 opt-in（#717）：插件不再代设，测试确定性由
    // 这里显式声明——legacy-engine 的 docker 无值 -e 透传会把它带进容器。
    process.env.POLARIS_LLM_FAKE_FALLBACK = '1';
    try {
      // 上面主流程已 stopKernel（单例清空），这里起的是全新实例；
      // startKernel 会等到引擎健康或失败才返回（首启跑全部迁移，最长 120s）。
      await startKernel();
      const { baseUrl, sessionSecret } = localBackend();
      check('kernel.localBackend 返回本地地址', baseUrl != null, `baseUrl=${baseUrl}`);
      if (baseUrl) {
        const health = await fetch(`${baseUrl}/api/health`).catch(() => null);
        check('本地引擎 /api/health 可达', health?.ok === true, `status=${health?.status}`);
        // #850：本地会话只发给带着本次启动口令的请求
        check('kernel.localBackend 带本次会话口令', typeof sessionSecret === 'string' && sessionSecret.length > 0);
        const denied = await fetch(`${baseUrl}/api/auth/local-session`, { method: 'POST' }).catch(() => null);
        check('不带口令拿不到本地会话', denied?.status === 403, `status=${denied?.status}`);
        const granted = await fetch(`${baseUrl}/api/auth/local-session`, {
          method: 'POST',
          headers: { 'X-Polaris-Session-Secret': sessionSecret ?? '' },
        }).catch(() => null);
        check('带口令拿到本地会话', granted?.ok === true, `status=${granted?.status}`);
        check(
          '每份安装的引擎密钥落在 userData（0600）',
          (() => {
            try {
              const st = statSync(join(app.getPath('userData'), 'engine-secrets.json'));
              return process.platform === 'win32' || (st.mode & 0o777) === 0o600;
            } catch {
              return false;
            }
          })(),
        );
      }
    } finally {
      await stopKernel();
      delete process.env.POLARIS_DESKTOP_ENGINE;
      delete process.env.POLARIS_LLM_FAKE_FALLBACK;
    }
    const running = (() => {
      try {
        return execFileSync('docker', ['ps', '--format', '{{.Names}}'], { encoding: 'utf8' });
      } catch {
        return '';
      }
    })();
    check('停机后引擎容器已回收', !running.split('\n').includes(ENGINE_CONTAINER), running.trim());
  }

  if (process.env.POLARIS_SMOKE_SHOT) {
    const image = await win.webContents.capturePage();
    await writeFile(process.env.POLARIS_SMOKE_SHOT, image.toPNG());
    console.log(`\n已截图 → ${process.env.POLARIS_SMOKE_SHOT}`);
  }

  rmSync(smokeUserData, { recursive: true, force: true });
  console.log(problems.length ? `\n${problems.length} 项失败` : '\n全部通过');
  app.exit(problems.length ? 1 : 0);
});
