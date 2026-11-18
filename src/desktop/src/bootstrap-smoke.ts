/* ============================================================
   装包冒烟（node 直跑，不进 Electron）：对着打包产物里的资源执行与
   桌面首启完全相同的引导路径，断言内嵌后端真的能起来。

   为什么不无头拉起整个打包后的 Electron：三平台 runner 上无头跑 GUI 应用
   各有各的坑（mac 的 Gatekeeper/签名、win 无 xvfb 等价物、linux 的沙箱
   helper 权限），而本 PR 要验的其实只有一件事——「包里那份 uv + backend
   资源能在一台没有 Python 的裸机上引导出可服务的引擎」。engine-bootstrap
   刻意写成 electron-free，正是为了让这里能用 node 驱动同一段代码；Electron
   壳本身的加载已由既有的 dist/smoke.cjs 覆盖。

   用法：
     node dist/bootstrap-smoke.cjs --resources <解包产物的 resources 目录> \
       [--data <临时 userData>] [--keep]

   步骤：bootstrap（uv 装 Python → venv → 装后端）→ 引擎起两轮
   （首轮空库不快照；次轮非空库应先落一份迁移前快照，见 #694）→
   用 venv Python 单独驱动迁移守卫片段验证「失败还原 + 成功修剪」→
   删测试数据目录（--keep 保留）。每步耗时打到 stdout，退出码非 0 即失败。
   ============================================================ */

import { spawn, type ChildProcess } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';

import {
  BootstrapError,
  PYPI_MIRROR,
  PYTHON_MIRROR,
  bootstrapEngine,
  buildDataDirMigration,
  buildMigrationGuard,
  buildUvEnv,
  sourceOrder,
  type BootstrapPhase,
  type EngineCommand,
  type MirrorMode,
} from './main/engine-bootstrap';
import { BootstrapProgressTracker, LOG_LINE_MAX } from './main/bootstrap-progress';

const HEALTH_TIMEOUT_MS = 120_000;

interface Args {
  resources: string;
  data: string | null;
  keep: boolean;
  /** 引擎监听端口；默认 18080（安装包用的那个）。本机已有 Polaris 在跑时换一个。 */
  port: number | undefined;
  mirror: MirrorMode;
  /** 把默认下载源指到一个连不上的地址，逼出「换镜像」这条路。 */
  blockDefault: boolean;
  /** 主流程通过后再跑故障场景：磁盘不足、升级失败保留旧环境、取消后续装。 */
  faults: boolean;
}

function parseArgs(): Args {
  const argv = process.argv.slice(2);
  const args: Args = {
    resources: '',
    data: null,
    keep: false,
    port: undefined,
    mirror: 'auto',
    blockDefault: false,
    faults: false,
  };
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--resources') args.resources = argv[++i] ?? '';
    else if (argv[i] === '--data') args.data = argv[++i] ?? null;
    else if (argv[i] === '--keep') args.keep = true;
    else if (argv[i] === '--port') args.port = Number(argv[++i]);
    else if (argv[i] === '--mirror') args.mirror = (argv[++i] ?? 'auto') as MirrorMode;
    else if (argv[i] === '--block-default') args.blockDefault = true;
    else if (argv[i] === '--faults') args.faults = true;
  }
  if (!args.resources) {
    console.error(
      '用法：node bootstrap-smoke.cjs --resources <dir> [--data <dir>] [--keep] [--port <n>] ' +
        '[--mirror auto|on|off] [--block-default] [--faults]',
    );
    process.exit(2);
  }
  args.resources = resolve(args.resources);
  return args;
}

function assert(cond: boolean, message: string): void {
  if (!cond) throw new Error(message);
}

/** 下载源与环境的纯函数断言：不联网、不起进程，每次都跑。 */
function assertSourcePlanning(): void {
  const eq = (a: unknown, b: unknown, what: string): void =>
    assert(JSON.stringify(a) === JSON.stringify(b), `${what}: ${JSON.stringify(a)} != ${JSON.stringify(b)}`);
  eq(sourceOrder('auto', false), ['default', 'mirror'], 'auto');
  eq(sourceOrder('on', false), ['mirror', 'default'], 'on');
  eq(sourceOrder('off', false), ['default'], 'off');
  // 用户自己配过 uv：失败时值得再试一次「不读用户配置的官方源」
  eq(sourceOrder('auto', true), ['default', 'mirror', 'official'], 'auto + user config');
  // 上次靠镜像装成的机器，这次先试镜像
  eq(sourceOrder('auto', false, 'mirror'), ['mirror', 'default'], 'auto, last good = mirror');
  eq(sourceOrder('off', false, 'mirror'), ['default'], 'off ignores last good');

  const base = {
    PATH: '/usr/bin',
    VIRTUAL_ENV: '/dev/venv',
    UV_NO_CONFIG: '1',
    UV_DEFAULT_INDEX: 'https://user.example/simple',
    HTTPS_PROXY: 'http://env-proxy:1',
  };
  const def = buildUvEnv(base, '/data/engine', 'default');
  assert(def.UV_NO_CONFIG === undefined, 'default source must let the user’s uv config apply');
  assert(def.UV_DEFAULT_INDEX === 'https://user.example/simple', 'default source keeps the user’s index');
  assert(def.HTTPS_PROXY === 'http://env-proxy:1', 'the environment proxy is inherited');
  assert(def.VIRTUAL_ENV === undefined, 'a developer venv must not leak in');
  assert(def.UV_PYTHON_PREFERENCE === 'only-managed' && def.UV_CACHE_DIR?.includes('uv-cache') === true, 'isolation env');
  const mirror = buildUvEnv(base, '/data/engine', 'mirror', 'socks5://127.0.0.1:1080');
  assert(mirror.UV_NO_CONFIG === '1', 'mirror source ignores user uv config');
  assert(mirror.UV_DEFAULT_INDEX === PYPI_MIRROR && mirror.UV_PYTHON_INSTALL_MIRROR === PYTHON_MIRROR, 'mirror urls');
  assert(
    mirror.HTTPS_PROXY === 'socks5://127.0.0.1:1080' && mirror.https_proxy === 'socks5://127.0.0.1:1080' && mirror.ALL_PROXY === 'socks5://127.0.0.1:1080',
    'an explicit proxy overrides the environment one',
  );
  const official = buildUvEnv(base, '/data/engine', 'official');
  assert(official.UV_NO_CONFIG === '1' && official.UV_DEFAULT_INDEX === undefined, 'official source drops user overrides');
  console.log('source planning: order / env OK');
}

/** 环境里还有没有命令行带着这个目录的 uv 进程（POSIX；Windows 上跳过）。 */
async function uvProcessesUnder(dir: string): Promise<string[]> {
  if (process.platform === 'win32') return [];
  return new Promise((resolvePs) => {
    const child = spawn('ps', ['-axo', 'pid=,command='], { stdio: ['ignore', 'pipe', 'ignore'] });
    let out = '';
    child.stdout!.on('data', (chunk: Buffer) => {
      out += chunk.toString();
    });
    child.on('close', () =>
      resolvePs(out.split('\n').filter((line) => line.includes(dir) && /\buv(\.exe)?\b/.test(line))),
    );
    child.on('error', () => resolvePs([]));
  });
}

function waitExit(child: ChildProcess, ms: number): Promise<boolean> {
  return new Promise((resolveExit) => {
    if (child.exitCode !== null || child.signalCode !== null) return resolveExit(true);
    const timer = setTimeout(() => {
      child.off('exit', onExit);
      resolveExit(false);
    }, ms);
    const onExit = (): void => {
      clearTimeout(timer);
      resolveExit(true);
    };
    child.once('exit', onExit);
  });
}

/** 拉起引擎 argv，轮询 /api/health 到 200，然后回收进程。返回引擎自报的版本。 */
async function runEngineOnce(config: EngineCommand): Promise<string> {
  let reported = '';
  // 与 legacy-engine 插件 command 模式相同的 spawn 环境；POLARIS_LLM_FAKE_FALLBACK
  // 是本冒烟自己的显式 opt-in——插件不代设（#717），测试要确定性 fake 就得在这里明说。
  // （引导器返回的 argv 自带 chdir 与数据库地址，见 engine-bootstrap）
  const child = spawn(config.command[0]!, config.command.slice(1), {
    stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env, POLARIS_LLM_FAKE_FALLBACK: '1' },
  });
  const tail: string[] = [];
  const capture = (chunk: Buffer): void => {
    for (const line of chunk.toString().split('\n')) {
      if (!line.trim()) continue;
      tail.push(line);
      if (tail.length > 100) tail.shift();
    }
  };
  child.stdout!.on('data', capture);
  child.stderr!.on('data', capture);

  try {
    const baseUrl = `http://127.0.0.1:${config.port}`;
    const tHealth = Date.now();
    const deadline = tHealth + HEALTH_TIMEOUT_MS;
    for (;;) {
      if (child.exitCode !== null || child.signalCode !== null) {
        throw new Error(`引擎提前退出 (code=${child.exitCode})\n${tail.join('\n')}`);
      }
      try {
        const res = await fetch(`${baseUrl}/api/health`);
        if (res.ok) {
          const text = await res.text();
          try {
            reported = String((JSON.parse(text) as { version?: unknown }).version ?? '');
          } catch {
            /* 版本断言在调用方做 */
          }
          console.log(`GET ${baseUrl}/api/health → ${res.status} ${text}`);
          console.log(`engine healthy in ${((Date.now() - tHealth) / 1000).toFixed(1)}s (含 alembic 迁移)`);
          break;
        }
      } catch {
        /* 还没起来 */
      }
      if (Date.now() > deadline) {
        throw new Error(`引擎 ${HEALTH_TIMEOUT_MS}ms 内未就绪\n${tail.join('\n')}`);
      }
      await delay(500);
    }
  } finally {
    if (child.exitCode === null && child.signalCode === null) {
      child.kill('SIGTERM');
      if (!(await waitExit(child, 5_000))) {
        child.kill('SIGKILL');
        await waitExit(child, 5_000);
      }
    }
  }
  return reported;
}

/** 跑一段 python -c，返回退出码；失败时把 stderr 打出来供排查。 */
function runPython(python: string, code: string, env?: NodeJS.ProcessEnv): Promise<number> {
  return new Promise((resolveExit, rejectExit) => {
    const child = spawn(python, ['-X', 'utf8', '-c', code], {
      stdio: ['ignore', 'ignore', 'pipe'],
      env: env ? { ...process.env, ...env } : process.env,
    });
    let stderr = '';
    child.stderr!.on('data', (chunk: Buffer) => {
      stderr += chunk.toString();
    });
    child.on('error', rejectExit);
    child.on('exit', (exitCode) => {
      if (exitCode !== 0) console.log(`   python 退出码 ${exitCode}：\n${stderr.trim()}`);
      resolveExit(exitCode ?? 1);
    });
  });
}

/**
 * 数据目录搬迁语义（#718）：与守卫同一套路，用 venv Python 直接驱动
 * buildDataDirMigration 生成的片段验证，不必为此再造一个「带旧数据的
 * 安装包」跑整轮引擎。三个断言：旧非空 + 新缺失 → 整体搬走（含子结构）；
 * 新已非空 → 绝不合并、源纹丝不动；片段自身对空环境幂等（旧位置不存在
 * 时是空操作且要把新位置建出来）。
 */
async function assertDataDirMigration(python: string): Promise<void> {
  const dir = mkdtempSync(join(tmpdir(), 'polaris-datadir-'));
  try {
    const oldDir = join(dir, 'backend', 'data');
    const newDir = join(dir, 'userData', 'engine', 'data');
    const code = buildDataDirMigration(oldDir.split('\\').join('/')).join('\n');
    const env = { POLARIS_DATA_DIR: newDir.split('\\').join('/') };

    // 场景 1：旧位置有数据、新位置不存在 → 搬走，旧位置消失
    mkdirSync(join(oldDir, 'papers'), { recursive: true });
    writeFileSync(join(oldDir, 'papers', 'a.pdf'), 'pdf-bytes');
    if ((await runPython(python, code, env)) !== 0) {
      throw new Error('datadir migration: 正常搬迁不该失败');
    }
    if (readFileSync(join(newDir, 'papers', 'a.pdf'), 'utf8') !== 'pdf-bytes') {
      throw new Error('datadir migration: 旧数据没有搬到新位置');
    }
    if (existsSync(oldDir)) {
      throw new Error('datadir migration: 搬迁后旧目录应当消失');
    }

    // 场景 2：新位置已非空 → 不合并；旧位置原样保留
    mkdirSync(oldDir, { recursive: true });
    writeFileSync(join(oldDir, 'stale.txt'), 'stale');
    if ((await runPython(python, code, env)) !== 0) {
      throw new Error('datadir migration: 新位置非空时应是无害空操作');
    }
    if (!existsSync(join(oldDir, 'stale.txt'))) {
      throw new Error('datadir migration: 新位置非空时不得动旧目录');
    }
    if (existsSync(join(newDir, 'stale.txt')) || existsSync(join(newDir, 'data'))) {
      throw new Error('datadir migration: 新位置非空时不得合并/嵌套旧数据');
    }

    // 场景 3：旧位置不存在 → 空操作，但要把新位置目录建出来
    await rm(oldDir, { recursive: true, force: true });
    await rm(newDir, { recursive: true, force: true });
    if ((await runPython(python, code, env)) !== 0 || !existsSync(newDir)) {
      throw new Error('datadir migration: 无旧数据时应只创建新目录');
    }
    console.log('datadir migration: 搬迁/不合并/幂等 OK');
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
}

/**
 * 引擎跑两轮太贵，失败注入更没法对真 alembic 做——所以「失败还原 +
 * 成功修剪」直接用 venv Python 驱动 buildMigrationGuard 生成的同一段
 * 片段验证：伪迁移命令先把库改坏再失败，守卫必须把文件组还原并透传
 * 非零退出；随后的空操作成功迁移把快照修剪到 3 份。
 */
async function assertGuardSemantics(python: string): Promise<void> {
  const dir = mkdtempSync(join(tmpdir(), 'polaris-guard-'));
  try {
    const db = join(dir, 'polaris.db').split('\\').join('/');
    const snapRoot = join(dir, 'snapshots').split('\\').join('/');
    writeFileSync(db, 'original-db');
    writeFileSync(`${db}-wal`, 'original-wal');

    // 伪迁移先覆写主库再失败：逼出「真还原」，而不是「碰巧没动过」
    const sabotage = `open(${JSON.stringify(db)}, 'wb').write(b'corrupted'); raise SystemExit(3)`;
    const failCode = buildMigrationGuard(db, snapRoot, `[sys.executable, '-c', ${JSON.stringify(sabotage)}]`).join('\n');
    if ((await runPython(python, failCode)) === 0) {
      throw new Error('migration guard: 迁移失败必须透传为非零退出');
    }
    if (readFileSync(db, 'utf8') !== 'original-db') {
      throw new Error('migration guard: 迁移失败后主库文件没有还原');
    }
    if (readFileSync(`${db}-wal`, 'utf8') !== 'original-wal') {
      throw new Error('migration guard: 迁移失败后 -wal 没有还原');
    }
    if (readdirSync(snapRoot).length !== 1) {
      throw new Error('migration guard: 失败现场应保留恰好 1 份快照');
    }

    // 成功路径：每轮留一份快照，修剪只保最近 3 份
    const okCode = buildMigrationGuard(db, snapRoot, "[sys.executable, '-c', 'pass']").join('\n');
    for (let i = 0; i < 4; i++) {
      if ((await runPython(python, okCode)) !== 0) {
        throw new Error('migration guard: 空操作迁移不该失败');
      }
    }
    const kept = readdirSync(snapRoot);
    if (kept.length !== 3) {
      throw new Error(`migration guard: 快照应修剪到 3 份，实际 ${kept.length} 份`);
    }
    console.log('migration guard: 失败还原 + 成功修剪 OK');
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
}

/**
 * 故障场景（--faults）。都在主流程装好的那套环境上做，不重新下载：
 * 1. 磁盘不足：开工前就说清楚，而不是装到一半报错；
 * 2. 升级失败：新环境装不成时旧环境原样可用，下次启动会再试；
 * 3. 取消后续装：引导途中取消（用户退出应用）立刻停下、不留 uv 进程，
 *    再来一次能干净地装完。
 */
async function runFaultScenarios(args: Args, dataDir: string, config: EngineCommand): Promise<void> {
  const engineDir = join(dataDir, 'engine');
  const sentinelPath = join(engineDir, 'bootstrap.json');
  const python = config.command[0]!;
  const importCheck = 'import fastapi, uvicorn, alembic, sqlalchemy';
  const base = { resourcesDir: args.resources, dataDir, port: args.port } as const;

  console.log('\n== fault: not enough disk space');
  {
    const fresh = mkdtempSync(join(tmpdir(), 'polaris-nodisk-'));
    try {
      let code = '';
      try {
        await bootstrapEngine({ ...base, dataDir: fresh, minFreeBytes: Number.MAX_SAFE_INTEGER });
      } catch (err) {
        code = err instanceof BootstrapError ? err.code : `unexpected: ${String(err)}`;
        if (err instanceof BootstrapError) console.log(`   ${err.message}`);
      }
      assert(code === 'disk-space', `磁盘不足应报 disk-space，实际 ${code || '（没有报错）'}`);
      assert(!existsSync(join(fresh, 'engine', 'venv.next')), '磁盘检查不过时不该开始装');
    } finally {
      await rm(fresh, { recursive: true, force: true });
    }
    console.log('disk space: refused up front OK');
  }

  // 把哨兵改成「上个版本装的」：下面两个场景都会因此进入升级流程
  const original = readFileSync(sentinelPath, 'utf8');
  const staleSentinel = JSON.stringify({ ...(JSON.parse(original) as object), backendHash: 'stale-previous-release' });

  console.log('\n== fault: upgrade fails → previous environment stays usable');
  {
    writeFileSync(sentinelPath, staleSentinel);
    const notes: string[] = [];
    const result = await bootstrapEngine({
      ...base,
      // 1 毫秒的安装时限：安装这一步必然失败（不依赖断网）
      timeouts: { install: 1 },
      rounds: 1,
      retryDelayMs: 10,
      onProgress: ({ line }) => {
        if (line?.startsWith('[polaris]')) notes.push(line);
      },
    });
    for (const note of notes) console.log(`   ${note}`);
    assert(result.degraded !== undefined, '升级失败应返回降级结果（沿用旧环境），而不是抛错');
    assert(result.command[0] === python, '降级后仍应指向原来的 venv');
    assert(!existsSync(join(engineDir, 'venv.next')), '失败后应清掉 venv.next');
    assert(existsSync(python), '旧 venv 必须原样还在');
    assert((await runPython(python, importCheck)) === 0, '旧环境必须仍然可用');
    assert(readFileSync(sentinelPath, 'utf8') === staleSentinel, '哨兵不该更新：下次启动要再试一次升级');
    console.log(`upgrade failure: kept the previous environment (${result.degraded?.reason.slice(0, 80)}) OK`);
  }

  console.log('\n== fault: cancel during setup → clean resume');
  {
    writeFileSync(sentinelPath, staleSentinel);
    const controller = new AbortController();
    const started = Date.now();
    const pending = bootstrapEngine({ ...base, signal: controller.signal }).then(
      () => 'completed',
      (err: unknown) => (err instanceof BootstrapError ? err.code : `unexpected: ${String(err)}`),
    );
    await delay(250);
    controller.abort();
    const outcome = await pending;
    const took = Date.now() - started;
    // 机器很快、250 毫秒内就装完了也是可能的；那就没有可断言的取消路径
    if (outcome === 'completed') {
      console.log(`cancel: setup finished in ${took}ms before the cancel landed (nothing to assert)`);
    } else {
      assert(outcome === 'cancelled', `取消应报 cancelled，实际 ${outcome}`);
      assert(took < 10_000, `取消应在几秒内生效，实际 ${took}ms`);
      await delay(500);
      const leftovers = await uvProcessesUnder(dataDir);
      assert(leftovers.length === 0, `取消后不该留下 uv 进程：\n${leftovers.join('\n')}`);
      assert(existsSync(python), '取消不该动旧 venv');
      console.log(`cancel: stopped in ${took}ms, no uv process left, previous environment intact OK`);

      const t1 = Date.now();
      const resumed = await bootstrapEngine({ ...base });
      assert(resumed.degraded === undefined && resumed.steps.length === 3, '取消后再来一次应当正常装完');
      assert(!existsSync(join(engineDir, 'venv.next')) && !existsSync(join(engineDir, 'venv.prev')), '续装后不该留下临时环境');
      assert((await runPython(python, importCheck)) === 0, '续装后的环境必须可用');
      assert(
        (JSON.parse(readFileSync(sentinelPath, 'utf8')) as { backendHash: string }).backendHash !== 'stale-previous-release',
        '续装成功后哨兵应更新',
      );
      console.log(`resume: reinstalled from cache in ${((Date.now() - t1) / 1000).toFixed(1)}s OK`);
    }
  }
}

async function main(): Promise<void> {
  const args = parseArgs();
  const dataDir = args.data ? resolve(args.data) : mkdtempSync(join(tmpdir(), 'polaris-engine-smoke-'));
  console.log(`bootstrap-smoke: resources = ${args.resources}`);
  console.log(`bootstrap-smoke: data      = ${dataDir}`);

  // 逐阶段计时：phase 变化时结算上一段（uv 的下载/编译输出行太密，只打点不刷屏）
  const timings: [string, number][] = [];
  let curPhase: BootstrapPhase | '' = '';
  let phaseStart = Date.now();
  let lineCount = 0;
  // 与桌面主进程同一套进度观测：顺带核对首启等待页拿到的字段形态
  const tracker = new BootstrapProgressTracker({ engineDir: join(dataDir, 'engine') });
  const phaseBytes: [string, number | undefined][] = [];

  // --block-default：把「用户环境」里的下载源指到本机一个没人监听的端口，模拟官方源
  // 连不上。default 源必然失败，引导应当自己换到国内镜像并装成功。
  const env: NodeJS.ProcessEnv = { ...process.env };
  if (args.blockDefault) {
    env.UV_DEFAULT_INDEX = 'http://127.0.0.1:9/simple';
    env.UV_PYTHON_INSTALL_MIRROR = 'http://127.0.0.1:9/python-build-standalone';
    console.log('bootstrap-smoke: default sources blocked (pointing at 127.0.0.1:9)');
  }

  let failed = false;
  try {
    assertSourcePlanning();
    const t0 = Date.now();
    const config = await bootstrapEngine({
      resourcesDir: args.resources,
      dataDir,
      port: args.port,
      env,
      network: { mirror: args.mirror },
      onProgress: ({ phase, line }) => {
        if (phase !== curPhase) {
          if (curPhase) phaseBytes.push([curPhase, tracker.snapshot().downloadedBytes]);
          tracker.phase(phase);
          if (curPhase) timings.push([curPhase, Date.now() - phaseStart]);
          console.log(`\n== phase: ${phase}`);
          curPhase = phase;
          phaseStart = Date.now();
          lineCount = 0;
        }
        if (line) tracker.line(line);
        if (line && lineCount < 40) {
          console.log(`   ${line}`);
          lineCount++;
        }
      },
    });
    if (curPhase) timings.push([curPhase, Date.now() - phaseStart]);
    console.log(`\nbootstrap done in ${((Date.now() - t0) / 1000).toFixed(1)}s`);
    for (const [phase, ms] of timings) console.log(`  ${phase.padEnd(8)} ${(ms / 1000).toFixed(1)}s`);
    // 每一步是靠哪个下载源、试了几次成的
    for (const step of config.steps) {
      console.log(`  ${step.phase.padEnd(8)} source=${step.source} attempts=${step.attempts} ${(step.ms / 1000).toFixed(1)}s`);
    }
    assert(config.degraded === undefined, `全新安装不该处于降级状态：${config.degraded?.reason}`);
    assert(config.steps.length === 3, `全新安装应记录三步，实际 ${config.steps.length}`);
    if (args.blockDefault) {
      const install = config.steps.find((s) => s.phase === 'install');
      const python = config.steps.find((s) => s.phase === 'python');
      assert(python?.source === 'mirror' && (python?.attempts ?? 0) >= 2, `Python 应在默认源失败后经镜像装成：${JSON.stringify(python)}`);
      assert(install?.source === 'mirror', `依赖应经镜像装成：${JSON.stringify(install)}`);
      console.log('mirror fallback: default source failed, mirrors succeeded OK');
    }
    assert(!existsSync(join(dataDir, 'engine', 'venv.next')), '装完后不该留下 venv.next');
    assert(!existsSync(join(dataDir, 'engine', 'venv.prev')), '装完后不该留下 venv.prev');
    tracker.stop();
    for (const [phase, bytes] of phaseBytes) {
      if (bytes !== undefined) console.log(`  ${phase.padEnd(8)} ${(bytes / 1024 / 1024).toFixed(1)} MB on disk`);
    }
    const snap = tracker.snapshot();
    if (!Array.isArray(snap.log) || snap.log.some((l) => typeof l !== 'string' || l.length > LOG_LINE_MAX || l.includes('\u001b'))) {
      throw new Error(`进度快照的 log 形态不对：${JSON.stringify(snap.log)}`);
    }

    const snapRoot = join(dataDir, 'engine', 'snapshots');

    // 首轮：空库首启，守卫应跳过快照（没有可保护的数据）
    console.log('\n== engine run 1 (fresh database)');
    const reportedVersion = await runEngineOnce(config);
    if (existsSync(snapRoot) && readdirSync(snapRoot).length > 0) {
      throw new Error('首启空库不该产生迁移前快照');
    }
    // 引擎自报的版本必须是打包时盖上去的那个，而不是写死在源码里的 0.1.0
    const releaseFile = join(args.resources, 'backend', 'app', '_release.py');
    const stamped = existsSync(releaseFile) ? /VERSION = "([^"]+)"/.exec(readFileSync(releaseFile, 'utf8'))?.[1] : undefined;
    assert(stamped !== undefined, `安装包里缺少 ${releaseFile}（stage-backend 没有写版本号）`);
    assert(reportedVersion === stamped, `引擎自报版本 ${reportedVersion}，应为打包时写入的 ${stamped}`);
    console.log(`engine version: ${reportedVersion} (stamped at packaging) OK`);

    // 次轮：库已非空，alembic（即便空操作）之前必须先落一份快照
    console.log('\n== engine run 2 (snapshot before alembic)');
    await runEngineOnce(config);
    const snaps = existsSync(snapRoot) ? readdirSync(snapRoot) : [];
    if (snaps.length === 0) {
      throw new Error('第二次启动应在迁移前留下快照目录');
    }
    for (const name of snaps) {
      if (!existsSync(join(snapRoot, name, 'polaris.db'))) {
        throw new Error(`快照 ${name} 里缺少 polaris.db`);
      }
    }
    console.log(`snapshots after run 2: ${snaps.sort().join(', ')}`);

    // #718：用户数据目录必须落在（临时）userData 下，安装目录（resources/
    // backend）里绝不能长出 data/——那正是应用更新会整包清掉的地方
    if (!existsSync(join(dataDir, 'engine', 'data'))) {
      throw new Error('引擎启动后 <userData>/engine/data 应已创建');
    }
    if (existsSync(join(args.resources, 'backend', 'data'))) {
      throw new Error('安装目录 resources/backend/data 不该被创建（#718）');
    }
    console.log(`data dir under userData: ${join(dataDir, 'engine', 'data')}`);

    // 数据目录搬迁语义（#718，用引导出来的 venv Python 驱动）
    console.log('\n== data dir migration semantics');
    await assertDataDirMigration(config.command[0]!);

    // 守卫语义：失败还原 + 成功修剪（用引导出来的 venv Python 驱动）
    console.log('\n== migration guard semantics');
    await assertGuardSemantics(config.command[0]!);

    if (args.faults) await runFaultScenarios(args, dataDir, config);

    console.log('\nbootstrap-smoke: PASS');
  } catch (err) {
    failed = true;
    console.error(`\nbootstrap-smoke: FAIL — ${err instanceof Error ? err.message : String(err)}`);
  } finally {
    if (args.keep) {
      console.log(`bootstrap-smoke: --keep，保留 ${dataDir}`);
    } else {
      // Windows 上刚被 kill 的引擎会短暂占着 python.exe 的文件锁，立刻删目录
      // 会 EPERM——重试几轮；清理失败只警告不改判（冒烟结论以引擎断言为准）
      for (let i = 0; i < 5; i++) {
        try {
          await rm(dataDir, { recursive: true, force: true });
          break;
        } catch (err) {
          if (i === 4) {
            console.warn(`bootstrap-smoke: 清理 ${dataDir} 失败（${err instanceof Error ? err.message : String(err)}），忽略`);
          } else {
            await delay(1_000);
          }
        }
      }
    }
  }
  process.exit(failed ? 1 : 0);
}

void main();
