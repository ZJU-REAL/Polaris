/* ============================================================
   sources 插件：把文献源适配器注册成 kernel 服务（#646，P2 E4）。

   配置树按源开关 + 参数（schemastery 校验），启用的源构造成
   SourceAdapter 实例，经 ctx.provide('sources') 挂出统一入口。
   桌面端接线（配置树装载）归市场工作，这里只管注册面。

   注册表的填充放进 ctx.effect：适配器本身无句柄可回收，但插件
   卸载/重载时 disposer 清空注册表，可以保证还攥着旧 service 引用
   的消费方立刻查不到已下线的源——而不是继续拿着一套僵尸适配器。
   本文件必须保持 electron-free（tests/electron-free.test.ts 强制）。
   ============================================================ */

import Schema from '@deepseek-ai/schemastery'
import type { Context } from '@deepseek-ai/cordis'
import type { SourceAdapter } from '../sources/contract.ts'
import { OpenAlexAdapter } from '../sources/openalex.ts'
import { CordisProjectsAdapter } from '../sources/cordis-projects.ts'

/* 字段全部可选：schemastery 校验时会补全默认值（enabled 默认 true），
   TS 形状对齐「调用方可以只写想覆盖的部分」这一使用姿势。 */
export interface SourcesConfig {
  adapters?: {
    openalex?: { enabled?: boolean; mailto?: string; baseUrl?: string }
    'cordis-projects'?: { enabled?: boolean; baseUrl?: string }
  }
}

export const SourcesConfig: Schema<SourcesConfig> = Schema.object({
  adapters: Schema.object({
    openalex: Schema.object({
      enabled: Schema.boolean().default(true),
      // polite pool 联系邮箱；不填走匿名池，功能不受影响只是限速更紧
      mailto: Schema.string(),
      baseUrl: Schema.string(),
    }),
    'cordis-projects': Schema.object({
      enabled: Schema.boolean().default(true),
      baseUrl: Schema.string(),
    }),
  }),
})

export interface SourcesService {
  /** 当前启用的适配器，注册顺序稳定。 */
  list(): SourceAdapter[]
  get(id: string): SourceAdapter | undefined
}

export const sources = {
  name: 'sources',

  Config: SourcesConfig,

  apply(ctx: Context, config: SourcesConfig): void {
    const registry = new Map<string, SourceAdapter>()
    ctx.effect(() => {
      // 防御性 ?? {}：cordis 正常会用 Config 校验补全默认值，但直接
      // 调 apply 的测试/嵌入方不一定走那条路
      const openalex = config.adapters?.openalex ?? {}
      const cordisProjects = config.adapters?.['cordis-projects'] ?? {}
      if (openalex.enabled !== false) {
        registry.set(
          'openalex',
          new OpenAlexAdapter({ baseUrl: openalex.baseUrl, mailto: openalex.mailto }),
        )
      }
      if (cordisProjects.enabled !== false) {
        registry.set('cordis-projects', new CordisProjectsAdapter({ baseUrl: cordisProjects.baseUrl }))
      }
      return () => registry.clear()
    }, 'sources adapter registry')

    const service: SourcesService = {
      list: () => [...registry.values()],
      get: (id) => registry.get(id),
    }
    ctx.provide('sources', service)
  },
}
