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

   步骤：bootstrap（uv 装 Python → venv → 装后端）→ spawn 引擎 argv →
   轮询 /api/health 到 200 → 回收进程 → 删测试数据目录（--keep 保留）。
   每步耗时打到 stdout，退出码非 0 即失败。
   ============================================================ */

import { spawn, type ChildProcess } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';

import { bootstrapEngine, type BootstrapPhase } from './main/engine-bootstrap';

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

  let engineChild: ChildProcess | null = null;
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

    // 与 legacy-engine 插件 command 模式相同的 spawn 环境（它注入的两个变量
    // 这里照抄；引导器返回的 argv 自带 chdir 与数据库地址，见 engine-bootstrap）
    console.log('\n== engine start');
    const child = spawn(config.command[0]!, config.command.slice(1), {
      stdio: ['ignore', 'pipe', 'pipe'],
      env: { ...process.env, POLARIS_PROFILE: 'desktop', POLARIS_LLM_FAKE_FALLBACK: '1' },
    });
    engineChild = child;
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
    console.log('\nbootstrap-smoke: PASS');
  } catch (err) {
    failed = true;
    console.error(`\nbootstrap-smoke: FAIL — ${err instanceof Error ? err.message : String(err)}`);
  } finally {
    if (engineChild && engineChild.exitCode === null && engineChild.signalCode === null) {
      engineChild.kill('SIGTERM');
      if (!(await waitExit(engineChild, 5_000))) {
        engineChild.kill('SIGKILL');
        await waitExit(engineChild, 5_000);
      }
    }
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
