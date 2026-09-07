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
import { existsSync, mkdtempSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';

import {
  bootstrapEngine,
  buildMigrationGuard,
  type BootstrapPhase,
  type EngineCommand,
} from './main/engine-bootstrap';

const HEALTH_TIMEOUT_MS = 120_000;

function parseArgs(): { resources: string; data: string | null; keep: boolean } {
  const argv = process.argv.slice(2);
  let resources = '';
  let data: string | null = null;
  let keep = false;
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--resources') resources = argv[++i] ?? '';
    else if (argv[i] === '--data') data = argv[++i] ?? null;
    else if (argv[i] === '--keep') keep = true;
  }
  if (!resources) {
    console.error('用法：node bootstrap-smoke.cjs --resources <dir> [--data <dir>] [--keep]');
    process.exit(2);
  }
  return { resources: resolve(resources), data, keep };
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

/** 拉起引擎 argv，轮询 /api/health 到 200，然后回收进程。 */
async function runEngineOnce(config: EngineCommand): Promise<void> {
  // 与 legacy-engine 插件 command 模式相同的 spawn 环境：POLARIS_PROFILE
  // 照抄插件注入；POLARIS_LLM_FAKE_FALLBACK 则是本冒烟自己的显式 opt-in——
  // 插件不再代设（#717），测试要确定性 fake 就得在这里明说。
  // （引导器返回的 argv 自带 chdir 与数据库地址，见 engine-bootstrap）
  const child = spawn(config.command[0]!, config.command.slice(1), {
    stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env, POLARIS_PROFILE: 'desktop', POLARIS_LLM_FAKE_FALLBACK: '1' },
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
          console.log(`GET ${baseUrl}/api/health → ${res.status} ${await res.text()}`);
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
}

/** 跑一段 python -c，返回退出码；失败时把 stderr 打出来供排查。 */
function runPython(python: string, code: string): Promise<number> {
  return new Promise((resolveExit, rejectExit) => {
    const child = spawn(python, ['-X', 'utf8', '-c', code], { stdio: ['ignore', 'ignore', 'pipe'] });
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

  let failed = false;
  try {
    const t0 = Date.now();
    const config = await bootstrapEngine({
      resourcesDir: args.resources,
      dataDir,
      onProgress: ({ phase, line }) => {
        if (phase !== curPhase) {
          if (curPhase) timings.push([curPhase, Date.now() - phaseStart]);
          console.log(`\n== phase: ${phase}`);
          curPhase = phase;
          phaseStart = Date.now();
          lineCount = 0;
        }
        if (line && lineCount < 40) {
          console.log(`   ${line}`);
          lineCount++;
        }
      },
    });
    if (curPhase) timings.push([curPhase, Date.now() - phaseStart]);
    console.log(`\nbootstrap done in ${((Date.now() - t0) / 1000).toFixed(1)}s`);
    for (const [phase, ms] of timings) console.log(`  ${phase.padEnd(8)} ${(ms / 1000).toFixed(1)}s`);

    const snapRoot = join(dataDir, 'engine', 'snapshots');

    // 首轮：空库首启，守卫应跳过快照（没有可保护的数据）
    console.log('\n== engine run 1 (fresh database)');
    await runEngineOnce(config);
    if (existsSync(snapRoot) && readdirSync(snapRoot).length > 0) {
      throw new Error('首启空库不该产生迁移前快照');
    }

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

    // 守卫语义：失败还原 + 成功修剪（用引导出来的 venv Python 驱动）
    console.log('\n== migration guard semantics');
    await assertGuardSemantics(config.command[0]!);

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
