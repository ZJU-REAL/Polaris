/* ============================================================
   storage 插件：为 kernel 挂载 SQLite 持久层。

   打开句柄放进 ctx.effect：fiber 被 dispose（kernel.stop / 插件卸载 /
   启动失败）时 disposer 必然执行，数据库句柄不会泄漏；重新装载插件
   等价于重开一次库，migrate 的幂等性保证重开无副作用。

   close 失败只记日志不抛：disposer 跑在整条 dispose 链上，这里一炸
   会连累其他插件的清理（子进程回收等），句柄泄漏远比清理链断裂便宜。
   本文件必须保持 electron-free（tests/electron-free.test.ts 强制）。
   ============================================================ */

import type { DatabaseSync } from 'node:sqlite'
import Schema from '@deepseek-ai/schemastery'
import type { Context } from '@deepseek-ai/cordis'
import { migrate, openStorage } from '../storage/db.ts'
import { PluginMetaStore, SqliteConfigTreeStore } from '../storage/store.ts'

export interface StorageConfig {
  /** 数据库文件的绝对路径。父目录不存在会自动创建。 */
  path: string
}

export const StorageConfig = Schema.object({
  path: Schema.string().required(),
})

export interface StorageService {
  /** 底层句柄。留给需要自建表的插件（配表迁移仍走 MIGRATIONS）。 */
  db: DatabaseSync
  /** 配置树的持久化实现（ConfigTreeStore）。 */
  configTree: SqliteConfigTreeStore
  /** 插件私有 JSON KV。 */
  pluginMeta: PluginMetaStore
  /** 实际打开的数据库文件路径，诊断用。 */
  path: string
}

export const storage = {
  name: 'storage',

  Config: StorageConfig,

  apply(ctx: Context, config: StorageConfig): void {
    let db!: DatabaseSync
    ctx.effect(() => {
      db = openStorage(config.path)
      migrate(db)
      return () => {
        try {
          db.close()
        } catch (error) {
          // 见文件头：吞掉并记日志，绝不让 dispose 链爆炸
          console.warn(`storage: failed to close database at ${config.path}:`, error)
        }
      }
    }, 'storage database handle')

    const service: StorageService = {
      db,
      configTree: new SqliteConfigTreeStore(db),
      pluginMeta: new PluginMetaStore(db),
      path: config.path,
    }
    ctx.provide('storage', service)
  },
}
