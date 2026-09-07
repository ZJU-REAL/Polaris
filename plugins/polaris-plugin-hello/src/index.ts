/* ============================================================
   polaris-plugin-hello（#712）：官方种子插件，兼第三方开发者模板。

   刻意保持「最小但完整」：一个 schemastery Config、一次 ctx.effect、
   一次 ctx.provide——这三样分别示范配置校验、可回收副作用、服务挂出，
   是 Polaris 插件的三种基本动作。没有任何网络/文件系统访问，与
   manifest 里 permissions 全 false 的声明一致。

   为什么依赖全是 devDependencies：入口必须是单文件 bundle（安装引擎
   的硬规则，kernel 零依赖解析、装完即可 import），schemastery 会被
   esbuild 打进 dist/index.js，运行时不存在 node_modules。
   ============================================================ */

import type { Context } from '@deepseek-ai/cordis'
import Schema from '@deepseek-ai/schemastery'

export interface HelloConfig {
  /** 问候语前缀，默认 Hello。 */
  greeting?: string
}

/** schemastery Config：设置页的配置表单与 plugins.updateConfig 的
    校验都读它。字段全部可选——模板插件必须能零配置启用。 */
export const Config: Schema<HelloConfig> = Schema.object({
  greeting: Schema.string().description('问候语前缀 / greeting prefix'),
})

/** hello-panel 服务的形状：panel kind 在 v1 只有语义（清单/市场里的
    归类），实际可观测面就是这个服务与 registry 里的插件名额。 */
export interface HelloPanelService {
  message(): string
}

const hello = {
  name: 'polaris-plugin-hello',

  Config,

  apply(ctx: Context, config: HelloConfig): void {
    const greeting = config.greeting ?? 'Hello'

    // effect：副作用要可回收。禁用/卸载时 fiber dispose 会执行返回的
    // 清理函数——日志本身没有句柄要收，这里示范的是形状
    ctx.effect(() => {
      console.log(`[polaris-plugin-hello] panel ready (greeting=${greeting})`)
      return () => {
        console.log('[polaris-plugin-hello] panel disposed')
      }
    }, 'hello panel lifecycle')

    // 服务挂出：其他插件可 ctx.get('hello-panel') 消费；随 fiber 生命
    // 周期自动挂上/摘下
    const service: HelloPanelService = {
      message: () => `${greeting} from Polaris!`,
    }
    ctx.provide('hello-panel', service)
  },
}

export default hello
