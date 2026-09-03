/* ============================================================
   壳级 E2E（issue #611）：用 Playwright 的 _electron 驱动真实的
   dist/main.cjs，把「起壳 → 本地引擎 → 免登录 → 建课题」这条只有
   运行时才暴露的链路整条走一遍。与 smoke.ts 互补：smoke 在 Electron
   进程内做白盒断言，这里从进程外像用户一样操作 UI。

   两个断言组：
   - A「壳与免登录」：需要 docker 与 polaris-api-test:local 镜像，
     门控照抄冒烟——设 POLARIS_E2E_ENGINE=1 且镜像存在才跑，否则
     打明确日志后 skip（CI 无镜像时仍然全绿）。
   - B「无引擎回落」：不依赖 docker，任何机器必跑。不设引擎 env 起壳，
     应停在服务器配置页而不是崩溃。

   并行安全：容器名与端口都随机化（经 POLARIS_DESKTOP_ENGINE_CONTAINER /
   _PORT 覆盖默认值），同机的其他 docker 套件或手动实例互不干扰；
   userData 指向 mkdtemp 出来的临时目录，单实例锁也随之隔离。

   用法：pnpm run e2e（需要先 build 前端；本包各产物由脚本自动构建）。
   Linux CI 里需要 xvfb-run。退出码非 0 即失败。
   ============================================================ */

import { execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { _electron, type ElectronApplication, type Page } from 'playwright-core';

// Node 里 require('electron') 拿到的是可执行文件路径（字符串），类型声明
// 描述的是渲染/主进程 API，所以这里显式断言。
// eslint-disable-next-line @typescript-eslint/no-require-imports
const electronPath = require('electron') as unknown as string;

const MAIN = join(__dirname, 'main.cjs');
// __dirname = src/desktop/dist → 仓库的 src/backend（与 smoke.ts 同一推导）
const BACKEND_DIR = join(__dirname, '..', '..', 'backend');
const ENGINE_IMAGE = 'polaris-api-test:local';

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
  // 窗口在 startKernel 之后才创建：docker 引擎首启要跑全部迁移（最长 120s），
  // 等窗口的超时必须盖过它。
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
      }),
    );
    app = r.app;
    const { page } = r;

    // 免登录（#601）：desktop 档后端 local_session=true，RequireAuth 应静默
    // 换会话直接进工作台。等 AppShell 的侧栏出现即视为「进了应用」；
    // 全新数据库没有课题，会被 RequireTopic 送到 /start。
    await page.waitForSelector('.sidebar', { timeout: 60_000 });
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

    // 回落地页确认列表可见——整页重载，顺带验证会话在刷新后依然有效
    await page.goto('app://polaris/start');
    await page.waitForSelector('.sidebar', { timeout: 60_000 });
    const visible = await page
      .getByText(topicName)
      .first()
      .isVisible()
      .catch(() => false);
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
  } catch (err) {
    check('无引擎回落组执行完成', false, String(err).slice(0, 400));
  } finally {
    await shutdown(app);
    rmSync(userData, { recursive: true, force: true });
  }
}

void (async () => {
  await groupEngine();
  await groupNoEngine();
  console.log(problems.length ? `\n${problems.length} 项失败` : '\n全部通过');
  process.exit(problems.length ? 1 : 0);
})();
