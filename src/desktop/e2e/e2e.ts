/* ============================================================
   壳级 E2E（issue #611）：用 Playwright 的 _electron 驱动真实的
   dist/main.cjs，把「起壳 → 本地引擎 → 免登录 → 建课题」这条只有
   运行时才暴露的链路整条走一遍。与 smoke.ts 互补：smoke 在 Electron
   进程内做白盒断言，这里从进程外像用户一样操作 UI。

   三个断言组：
   - A「壳与免登录」：需要 docker 与 polaris-api-test:local 镜像，
     门控照抄冒烟——设 POLARIS_E2E_ENGINE=1 且镜像存在才跑，否则
     打明确日志后 skip（CI 无镜像时仍然全绿）。
   - B「无引擎回落」：不依赖 docker，任何机器必跑。不设引擎 env 起壳，
     应停在服务器配置页而不是崩溃。
   - C「插件市场闭环」（#712）：不依赖 docker，任何机器必跑。本进程起
     一个 127.0.0.1 的 http 替身充当索引源与 npm registry，经渲染进程的
     window.polaris（生产 IPC 路径）驱动种子插件走完「换源 → 拉索引 →
     安装 → 启用 → 拒卸 → 禁用 → 卸载」全链路。

   并行安全：容器名与端口都随机化（经 POLARIS_DESKTOP_ENGINE_CONTAINER /
   _PORT 覆盖默认值），同机的其他 docker 套件或手动实例互不干扰；
   userData 指向 mkdtemp 出来的临时目录，单实例锁也随之隔离。

   用法：pnpm run e2e（需要先 build 前端；本包各产物由脚本自动构建）。
   Linux CI 里需要 xvfb-run。退出码非 0 即失败。
   ============================================================ */

import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { createServer } from 'node:http';
import type { AddressInfo } from 'node:net';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { _electron, type ElectronApplication, type Page } from 'playwright-core';

// tar/tgz 生成逻辑复用 kernel 测试的共用 fixture（#710 抽出）：类型仅
// import type，esbuild 打包 e2e 时只会带上纯函数的打包器本体
// eslint-disable-next-line import/no-relative-packages
import { makeTgz } from '../../kernel/tests/helpers/market-fixture';

// Node 里 require('electron') 拿到的是可执行文件路径（字符串），类型声明
// 描述的是渲染/主进程 API，所以这里显式断言。
// eslint-disable-next-line @typescript-eslint/no-require-imports
const electronPath = require('electron') as unknown as string;

const MAIN = join(__dirname, 'main.cjs');
// __dirname = src/desktop/dist → 仓库的 src/backend（与 smoke.ts 同一推导）
const BACKEND_DIR = join(__dirname, '..', '..', 'backend');
const ENGINE_IMAGE = 'polaris-api-test:local';
// 仓库根与种子插件目录（组 C 的 fixture 源头：真实包产物 + 真实索引文件）
const REPO_ROOT = join(__dirname, '..', '..', '..');
const HELLO_DIR = join(REPO_ROOT, 'plugins', 'polaris-plugin-hello');

/** 渲染进程里 preload 暴露的桥的最小形状（类型只在 e2e 侧擦除前有效）。 */
interface PolarisBridge {
  polaris: {
    invoke: (method: string, params?: unknown) => Promise<unknown>;
    subscribe: (handler: (event: unknown) => void) => number;
    unsubscribe: (id: number) => void;
  };
}

const problems: string[] = [];

function check(label: string, ok: boolean, detail = ''): void {
  if (ok) {
    console.log(`  ok   ${label}`);
  } else {
    problems.push(label);
    console.log(`  FAIL ${label}${detail ? ` — ${detail}` : ''}`);
  }
}

/** 起壳用的环境：继承当前进程，但把引擎相关 env 全部清干净——
    组 B 必须证明「没有任何引擎配置」时壳也能正常起。 */
function launchEnv(overrides: Record<string, string>): Record<string, string> {
  const env: Record<string, string> = {};
  for (const [k, v] of Object.entries(process.env)) {
    if (v !== undefined) env[k] = v;
  }
  delete env.POLARIS_DESKTOP_ENGINE;
  delete env.POLARIS_DESKTOP_ENGINE_CONTAINER;
  delete env.POLARIS_DESKTOP_ENGINE_PORT;
  // fake LLM 回退是严格显式 opt-in（#717）：基线环境必须干净，需要它的组
  // （组 A）在 overrides 里显式声明，其余组证明「不设就没有」。
  delete env.POLARIS_LLM_FAKE_FALLBACK;
  // 内部分发机器可能设了默认服务器：会让「未配置服务器」的断言失真
  delete env.POLARIS_DEFAULT_SERVER_URL;
  return { ...env, ...overrides };
}

async function launch(env: Record<string, string>): Promise<{ app: ElectronApplication; page: Page }> {
  const app = await _electron.launch({
    executablePath: electronPath,
    args: [MAIN],
    env,
    timeout: 60_000,
  });
  // #721 起窗口先于内核创建，几秒内必现；超时保留裕量以防慢 CI。
  // 引擎启动的等待挪到了页面内（首启等待页），由各组的选择器超时盖住。
  const page = app.windows()[0] ?? ((await app.waitForEvent('window', { timeout: 240_000 })) as Page);
  await page.waitForLoadState('domcontentloaded');
  return { app, page };
}

/** 走真实退出路径（app.quit → before-quit → stopKernel → 引擎 disposer），
    而不是让 Playwright 直接掐进程——容器回收正是要测的东西。 */
async function shutdown(app: ElectronApplication | null): Promise<void> {
  if (!app) return;
  try {
    await app.evaluate(({ app: electronApp }) => electronApp.quit());
    await app.waitForEvent('close', { timeout: 30_000 }).catch(() => undefined);
  } catch {
    /* 主进程可能已经退了 */
  }
  await app.close().catch(() => undefined);
}

function dockerNames(): string[] {
  try {
    return execFileSync('docker', ['ps', '--format', '{{.Names}}'], { encoding: 'utf8' })
      .split('\n')
      .filter(Boolean);
  } catch {
    return [];
  }
}

function dockerHasImage(): boolean {
  try {
    execFileSync('docker', ['image', 'inspect', ENGINE_IMAGE], { stdio: 'ignore' });
    return true;
  } catch {
    return false;
  }
}

/* ---------------- 组 A：壳与免登录（需引擎） ---------------- */

async function groupEngine(): Promise<void> {
  console.log('壳与免登录（本地引擎）');
  if (process.env.POLARIS_E2E_ENGINE !== '1') {
    console.log('  skip 壳与免登录组（未设 POLARIS_E2E_ENGINE=1）');
    return;
  }
  if (!dockerHasImage()) {
    console.log(`  skip 壳与免登录组（docker 不可用或缺镜像 ${ENGINE_IMAGE}）`);
    return;
  }

  // 容器名/端口随机化：默认名 polaris-desktop-engine 若被并行任务占着，
  // docker run 会直接撞名失败，回收时还可能误删别人的容器。
  const container = `polaris-desktop-engine-e2e-${Math.random().toString(36).slice(2, 8)}`;
  const port = 18100 + Math.floor(Math.random() * 1800);
  const userData = mkdtempSync(join(tmpdir(), 'polaris-e2e-a-'));
  let app: ElectronApplication | null = null;

  try {
    const r = await launch(
      launchEnv({
        POLARIS_DESKTOP_ENGINE: `docker:${ENGINE_IMAGE}:${BACKEND_DIR}`,
        POLARIS_DESKTOP_ENGINE_CONTAINER: container,
        POLARIS_DESKTOP_ENGINE_PORT: String(port),
        POLARIS_USER_DATA_DIR: userData,
        // 显式 opt-in：插件不再代设 fake 回退（#717），引擎容器经 docker 的
        // 无值 -e 透传拿到它——测试确定性由这里声明，而不是产品替我们开
        POLARIS_LLM_FAKE_FALLBACK: '1',
      }),
    );
    app = r.app;
    const { page } = r;

    // 免登录（#601）：desktop 档后端 local_session=true，RequireAuth 应静默
    // 换会话直接进工作台。等 AppShell 的侧栏出现即视为「进了应用」；
    // 全新数据库没有课题，会被 RequireTopic 送到 /start。
    // #721 起窗口先起：docker 引擎首启的迁移（最长 120s）发生在首启等待页
    // 期间，等待页就绪后整页 reload 再进应用，这条超时必须盖过全程。
    await page.waitForSelector('.sidebar', { timeout: 240_000 });
    await page.waitForSelector('text=/选择或创建课题|Pick or create a topic/', { timeout: 30_000 });
    check('免登录直达工作台（侧栏 + /start 落地页）', true);

    const authCards = await page.locator('.auth-card-title').count();
    const path = await page.evaluate('window.location.pathname');
    check('不是登录页/配置页', authCards === 0 && path !== '/login', `authCards=${authCards} path=${String(path)}`);

    // 渲染进程内直接打本地引擎：证明 CSP 放行了 127.0.0.1、端点解析走了本地
    const health = (await page.evaluate(
      (u: string) => fetch(u).then((res) => res.status).catch(() => 0),
      `http://127.0.0.1:${port}/api/health`,
    )) as number;
    check('渲染进程可达本地引擎 /api/health', health === 200, `status=${health}`);

    // 深一步：从 UI 建一个课题（常规模式只需名称），断言出现在列表里
    const topicName = `E2E 课题 ${Math.random().toString(36).slice(2, 8)}`;
    await page.getByRole('button', { name: /新建课题|New topic/ }).click();
    await page.waitForSelector('.project-wizard-card', { timeout: 30_000 });
    await page.locator('.project-wizard-card input.input').first().fill(topicName);
    await page.getByRole('button', { name: /创建课题|Create topic/ }).click();
    await page.waitForURL(/app:\/\/polaris\/t\//, { timeout: 30_000 });
    check('创建课题后进入课题工作台（/t/<id>）', true);

    // 回落地页确认列表可见——整页重载，顺带验证会话在刷新后依然有效。
    // .sidebar 先于课题列表出现（列表是异步查询），isVisible() 的立即快照
    // 必然竞态：用有界等待，断言的语义仍是「重载后新课题可见」。
    await page.goto('app://polaris/start');
    await page.waitForSelector('.sidebar', { timeout: 60_000 });
    const visible = await page
      .getByText(topicName)
      .first()
      .waitFor({ state: 'visible', timeout: 30_000 })
      .then(
        () => true,
        () => false,
      );
    check('新建课题出现在课题列表', visible, `name=${topicName}`);
  } catch (err) {
    check('壳与免登录组执行完成', false, String(err).slice(0, 400));
  } finally {
    await shutdown(app);
    // 真实退出路径应已回收容器；轮询确认（docker rm 是异步的）
    let gone = false;
    for (let i = 0; i < 30; i++) {
      if (!dockerNames().includes(container)) {
        gone = true;
        break;
      }
      await delay(500);
    }
    check('退出后引擎容器已回收', gone, `container=${container}`);
    if (!gone) {
      // 兜底清理，别把失败现场留给下一轮（上面的 FAIL 已经记账）
      try {
        execFileSync('docker', ['rm', '-f', container], { stdio: 'ignore' });
      } catch {
        /* 已不在 */
      }
    }
    rmSync(userData, { recursive: true, force: true });
  }
}

/* ---------------- 组 B：无引擎回落（必跑） ---------------- */

async function groupNoEngine(): Promise<void> {
  console.log('\n无引擎回落');
  const userData = mkdtempSync(join(tmpdir(), 'polaris-e2e-b-'));
  let app: ElectronApplication | null = null;

  try {
    const r = await launch(launchEnv({ POLARIS_USER_DATA_DIR: userData }));
    app = r.app;
    const { page } = r;

    // 未配置服务器、也没有本地引擎：应停在首启的服务器配置页，而不是白屏/崩溃
    await page.waitForSelector('.auth-card-title', { timeout: 30_000 });
    const title = (await page.locator('.auth-card-title').textContent()) ?? '';
    check('停在服务器配置页', /连接到服务器|Connect to a server/.test(title), `title=${title}`);
    check('窗口仍然存活（未崩溃）', !page.isClosed());

    const mounted = (await page.evaluate('document.querySelector("#root")?.childElementCount ?? 0')) as number;
    check('React 应用已挂载', mounted > 0, `children=${mounted}`);

    // storage 持久层（#609）与引擎无关，无引擎时也必须随壳就绪
    const ks = (await page.evaluate(
      `window.polaris.invoke('kernel.status').catch(() => null)`,
    )) as { storage?: boolean } | null;
    check('kernel storage 持久层就绪', ks?.storage === true, `status=${JSON.stringify(ks)}`);
  } catch (err) {
    check('无引擎回落组执行完成', false, String(err).slice(0, 400));
  } finally {
    await shutdown(app);
    rmSync(userData, { recursive: true, force: true });
  }
}


/* ---------------- 组 C：插件市场闭环（必跑） ---------------- */

async function groupMarket(): Promise<void> {
  console.log('\n插件市场闭环');

  // fixture 全部取自「将来真上架的那份东西」：种子插件的真实 dist（已
  // 入库）打成 npm 规范形状的 tarball，索引就是仓内 market/index.json
  // 原文——断言对象是真实产物链，而不是另一套测试专用副本。
  const pkgRaw = readFileSync(join(HELLO_DIR, 'package.json'), 'utf8');
  const pkg = JSON.parse(pkgRaw) as { name: string; version: string };
  const entryJs = readFileSync(join(HELLO_DIR, 'dist', 'index.js'), 'utf8');
  const readme = readFileSync(join(HELLO_DIR, 'README.md'), 'utf8');
  const indexRaw = readFileSync(join(REPO_ROOT, 'market', 'index.json'), 'utf8');

  const tgz = makeTgz([
    { name: 'package/package.json', data: pkgRaw },
    { name: 'package/dist/index.js', data: entryJs },
    { name: 'package/README.md', data: readme },
  ]);
  const integrity = `sha512-${createHash('sha512').update(tgz).digest('base64')}`;

  // 本地 http 替身，一个进程同演索引源与 npm registry。为什么不走 #710
  // 的 setMarketFetchForTesting：那是主进程 bundle 内的模块级变量，从
  // e2e 进程够不到。索引源经生产路径换源（setEndpoint 指到 127.0.0.1）；
  // registry 域名是安装引擎里写死的，靠下面 app.evaluate 在主进程包一层
  // fetch 重定向。
  let port = 0;
  const server = createServer((req, res) => {
    const url = req.url ?? '';
    if (url === '/index.json') {
      res.setHeader('content-type', 'application/json');
      res.end(indexRaw);
      return;
    }
    if (url === `/registry/${pkg.name}`) {
      res.setHeader('content-type', 'application/json');
      res.end(
        JSON.stringify({
          name: pkg.name,
          versions: {
            [pkg.version]: {
              dist: {
                tarball: `http://127.0.0.1:${port}/registry/${pkg.name}/-/${pkg.name}-${pkg.version}.tgz`,
                integrity,
              },
            },
          },
        }),
      );
      return;
    }
    if (url === `/registry/${pkg.name}/-/${pkg.name}-${pkg.version}.tgz`) {
      res.end(tgz);
      return;
    }
    res.statusCode = 404;
    res.end('not found');
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  port = (server.address() as AddressInfo).port;

  const userData = mkdtempSync(join(tmpdir(), 'polaris-e2e-c-'));
  let app: ElectronApplication | null = null;

  try {
    const indexEntry = (JSON.parse(indexRaw) as { plugins: { name: string; version: string }[] }).plugins.find(
      (entry) => entry.name === pkg.name,
    );
    check(
      'market/index.json 收录 hello 且版本与仓内包一致',
      indexEntry != null && indexEntry.version === pkg.version,
      `index=${JSON.stringify(indexEntry)} pkg=${pkg.version}`,
    );

    const r = await launch(launchEnv({ POLARIS_USER_DATA_DIR: userData }));
    app = r.app;
    const { page } = r;
    await page.waitForFunction('typeof window.polaris?.invoke === "function"', undefined, { timeout: 30_000 });
    // #721 起内核在窗口之后后台启动：plugins.* 依赖配置树就绪，先等内核
    // 真正 started 再开始驱动，否则会撞上 ERR_CAPABILITY_UNAVAILABLE。
    await page.waitForFunction(
      `window.polaris.invoke('kernel.status').then((s) => s.started === true).catch(() => false)`,
      undefined,
      { timeout: 60_000 },
    );

    // 安装引擎固定打 https://registry.npmjs.org：在主进程的全局 fetch 外
    // 包一层，把该域名重定向到本地替身。只改本次 e2e 会话的运行时全局，
    // 生产代码零改动；索引拉取等其余 URL 原样放行。
    await app.evaluate((_electron, arg) => {
      const original = globalThis.fetch;
      const prefix = 'https://registry.npmjs.org/';
      globalThis.fetch = ((input: unknown, init?: unknown) => {
        const url = typeof input === 'string' ? input : String((input as { url?: unknown }).url ?? input);
        if (url.startsWith(prefix)) {
          return original(`http://127.0.0.1:${arg.port}/registry/${url.slice(prefix.length)}`, init as RequestInit);
        }
        return original(input as RequestInfo, init as RequestInit);
      }) as typeof fetch;
    }, { port });

    const endpoint = `http://127.0.0.1:${port}/index.json`;
    const ep = (await page.evaluate(
      (u) => (window as unknown as PolarisBridge).polaris.invoke('plugins.market.setEndpoint', { endpoint: u }),
      endpoint,
    )) as { endpoint: string; isDefault: boolean };
    check('setEndpoint 指向本地索引源', ep.endpoint === endpoint && !ep.isDefault, JSON.stringify(ep));

    const entries = (await page.evaluate(() =>
      (window as unknown as PolarisBridge).polaris.invoke('plugins.market.fetchIndex'),
    )) as { name: string; version: string; tier: string; badges: string[] }[];
    const listed = Array.isArray(entries) ? entries.find((entry) => entry.name === pkg.name) : undefined;
    check(
      'fetchIndex 经校验路径见到 hello（bronze/official）',
      listed != null && listed.version === pkg.version && listed.tier === 'bronze' && listed.badges.includes('official'),
      JSON.stringify(entries).slice(0, 300),
    );

    // 安装是长任务：先订阅事件再 invoke（进度可能赶在 invoke 返回之前），
    // 等到本 job 的 job.done / job.error 或 60s 超时
    const install = (await page.evaluate(async (arg) => {
      const w = window as unknown as PolarisBridge;
      const events: { type: string; jobId?: string; phase?: string; result?: unknown; code?: string; message?: string }[] = [];
      const sub = w.polaris.subscribe((event) => {
        const e = event as { type?: unknown };
        if (typeof e.type === 'string' && e.type.startsWith('job.')) events.push(event as (typeof events)[number]);
      });
      try {
        const { jobId } = (await w.polaris.invoke('plugins.market.install', arg)) as { jobId: string };
        const deadline = Date.now() + 60_000;
        for (;;) {
          const finished = events.find((e) => e.jobId === jobId && (e.type === 'job.done' || e.type === 'job.error'));
          if (finished || Date.now() > deadline) {
            return {
              finished: finished ?? null,
              phases: events.filter((e) => e.jobId === jobId && e.type === 'job.progress').map((e) => e.phase),
            };
          }
          await new Promise((resolve) => setTimeout(resolve, 100));
        }
      } finally {
        w.polaris.unsubscribe(sub);
      }
    }, { name: pkg.name, version: pkg.version })) as {
      finished: { type: string; result?: { entryId?: string }; code?: string; message?: string } | null;
      phases: (string | undefined)[];
    };
    check('install job 以 job.done 收尾', install.finished?.type === 'job.done', JSON.stringify(install.finished));
    check('job.done 带回条目 id', install.finished?.result?.entryId === pkg.name, JSON.stringify(install.finished?.result));
    check(
      '四个安装阶段全部上报进度',
      ['download', 'verify', 'extract', 'register'].every((phase) => install.phases.includes(phase)),
      `phases=${install.phases.join(',')}`,
    );
    check(
      '安装物落盘 userData/plugins/<name>/<version>/',
      existsSync(join(userData, 'plugins', pkg.name, pkg.version, 'dist', 'index.js')),
    );

    const listPlugins = async (): Promise<{ id: string; name: string; state: string }[]> =>
      (await page.evaluate(() =>
        (window as unknown as PolarisBridge).polaris.invoke('plugins.list'),
      )) as { id: string; name: string; state: string }[];
    const installedInfo = (await listPlugins()).find((info) => info.id === pkg.name);
    check(
      'plugins.list 出现 disabled 条目（装/启分离）',
      installedInfo != null && installedInfo.state === 'disabled' && installedInfo.name.startsWith('file://'),
      JSON.stringify(installedInfo),
    );

    const pluginCount = async (): Promise<number> =>
      (((await page.evaluate(() =>
        (window as unknown as PolarisBridge).polaris.invoke('kernel.status'),
      )) as { plugins?: number }).plugins ?? 0);
    const countBefore = await pluginCount();

    const enabled = (await page.evaluate(
      (id) => (window as unknown as PolarisBridge).polaris.invoke('plugins.enable', { id }),
      pkg.name,
    )) as { state: string };
    check('enable 后条目 active（file:// 动态 import 真实走通）', enabled.state === 'active', JSON.stringify(enabled));
    const countAfter = await pluginCount();
    check('hello 占据 registry 名额（kernel.status 可观测）', countAfter > countBefore, `plugins ${countBefore} -> ${countAfter}`);

    const refused = (await page.evaluate(
      (name) => (window as unknown as PolarisBridge).polaris.invoke('plugins.market.uninstall', { name }),
      pkg.name,
    )) as { ok: boolean; code?: string };
    check('enabled 状态拒卸（错误作为数据返回）', refused.ok === false && refused.code === 'plugin-enabled', JSON.stringify(refused));

    const disabledInfo = (await page.evaluate(
      (id) => (window as unknown as PolarisBridge).polaris.invoke('plugins.disable', { id }),
      pkg.name,
    )) as { state: string };
    check('disable 停回 disabled', disabledInfo.state === 'disabled', JSON.stringify(disabledInfo));

    const removed = (await page.evaluate(
      (name) => (window as unknown as PolarisBridge).polaris.invoke('plugins.market.uninstall', { name }),
      pkg.name,
    )) as { ok: boolean };
    check('禁用后卸载成功', removed.ok === true, JSON.stringify(removed));
    check('卸载后树条目消失', !(await listPlugins()).some((info) => info.id === pkg.name));
    check('卸载后盘面清空', !existsSync(join(userData, 'plugins', pkg.name)));
    // 再卸一次报 not-installed：该分支要求树条目与安装记录**都**不在，
    // 顺带证明 PluginMetaStore 里的记录也拆干净了
    const again = (await page.evaluate(
      (name) => (window as unknown as PolarisBridge).polaris.invoke('plugins.market.uninstall', { name }),
      pkg.name,
    )) as { ok: boolean; code?: string };
    check('重复卸载报 not-installed（安装记录已清）', again.ok === false && again.code === 'not-installed', JSON.stringify(again));
  } catch (err) {
    check('插件市场组执行完成', false, String(err).slice(0, 400));
  } finally {
    await shutdown(app);
    server.close();
    rmSync(userData, { recursive: true, force: true });
  }
}

void (async () => {
  await groupEngine();
  await groupNoEngine();
  await groupMarket();
  console.log(problems.length ? `\n${problems.length} 项失败` : '\n全部通过');
  process.exit(problems.length ? 1 : 0);
})();
