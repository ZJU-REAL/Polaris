/* ============================================================
   服务器形态的内核宿主入口（#754）：裸 Node 跑插件内核。

   桌面把内核挂在 Electron 主进程里；服务器形态没有 Electron，所以这里是
   一个独立进程。内核本来就不认识 electron（tests/electron-free.test.ts
   机械把关），所以这个进程要做的只有三件事：装配宿主、挂上 HTTP 传输、
   把 job 事件广播出去。

   ## 与桌面的两处有意不同

   1. **种子树里没有 legacy-engine**。桌面要靠内核把 Python 引擎拉起来；
      服务器形态的 api/worker 是各自的容器，由 compose 管生命周期，内核
      不该再去拉一份。树里只种 sources。
   2. **安装物落在数据目录下，一份，不按用户分**。服务器上的插件跑在服务端
      进程里，不是某个用户的机器上——它对所有账号生效，所以也只有一份。
      谁能装由后端的 owner 守卫决定（见 rpc-http.ts 的信任边界说明）。

   环境变量：
     POLARIS_KERNEL_DATA_DIR   数据根（storage.db 与 plugins/ 都在其下）
     POLARIS_KERNEL_TOKEN      与后端之间的共享密钥，缺失即拒绝启动
     POLARIS_KERNEL_PORT       监听端口，默认 8770
     POLARIS_KERNEL_HOST       绑定地址，默认 0.0.0.0（容器内部网络）
   ============================================================ */

import { createPluginHost, marketPluginsDir } from '../host.ts'
import { JobBus } from '../rpc/jobs.ts'
import { createMarketMethods } from '../rpc/market-methods.ts'
import { createPluginMethods } from '../rpc/plugin-methods.ts'
import { createRpcHttpServer } from './rpc-http.ts'

/** 服务器形态的首启种子：只有 sources，理由见文件头。 */
export const SERVER_SEED_ENTRIES = [{ id: 'sources', name: 'cordis:sources' }]

export interface ServerKernelOptions {
  dataDir: string
  token: string
  port: number
  host?: string
}

export async function startServerKernel(options: ServerKernelOptions) {
  const host = await createPluginHost({
    name: 'polaris-server',
    dataRoot: options.dataDir,
    seedEntries: SERVER_SEED_ENTRIES,
    // 服务器一律严格：安装物对不上哈希就禁用，没有「开发态放宽」这回事
    strict: true,
  })

  // 总线与 HTTP 服务互相需要（事件要经 SSE 发出去，服务要先有方法表才能建）。
  // 用一个可变引用打结：事件总是在服务建好之后才产生，早到的事件没有订阅者，
  // 丢掉即可——这也是 SSE 的常态。
  let sink: { broadcast(event: unknown): void } | null = null
  const jobs = new JobBus((event) => sink?.broadcast(event))

  const deps = {
    configTree: () => host.configTree ?? null,
    pluginMeta: () => host.pluginMeta ?? null,
  }

  const market = createMarketMethods({
    ...deps,
    pluginsDir: () => marketPluginsDir(options.dataDir),
    jobs,
  })

  const methods = {
    ...createPluginMethods(deps),
    ...market.methods,
  }

  methods['kernel.status'] = () => ({
    started: true,
    name: 'polaris-server',
    plugins: host.kernel.ctx.registry.size,
    storage: host.pluginMeta != null,
  })

  const rpc = await createRpcHttpServer({
    methods,
    token: options.token,
    port: options.port,
    host: options.host,
  })
  sink = rpc

  await host.kernel.start()
  return { host, rpc, jobs, market }
}

/** 直接跑本文件时的入口（容器 CMD）。 */
export async function main(): Promise<void> {
  const dataDir = process.env.POLARIS_KERNEL_DATA_DIR
  const token = process.env.POLARIS_KERNEL_TOKEN
  if (!dataDir) throw new Error('POLARIS_KERNEL_DATA_DIR is required')
  if (!token) throw new Error('POLARIS_KERNEL_TOKEN is required')

  const port = Number(process.env.POLARIS_KERNEL_PORT ?? 8770)
  const started = await startServerKernel({
    dataDir,
    token,
    port,
    host: process.env.POLARIS_KERNEL_HOST ?? '0.0.0.0',
  })
  console.log(`[kernel] server host listening on ${started.rpc.port}`)

  const stop = async () => {
    await started.rpc.close()
    await started.host.kernel.stop()
    process.exit(0)
  }
  process.on('SIGTERM', () => void stop())
  process.on('SIGINT', () => void stop())
}
