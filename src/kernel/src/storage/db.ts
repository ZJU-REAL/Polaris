/* ============================================================
   SQLite 存储底座：打开数据库 + 最小前向迁移器。

   为什么是 node:sqlite 而不是 better-sqlite3/drizzle：kernel 要在
   Electron 主进程与裸 Node 双形态下运行，本机也编不了原生模块——
   Node 内置的 DatabaseSync 零编译、零依赖，Spike-2
   （spikes/p0/2-sqlite-bench）已验证它扛得住文献库量级，配置树这种
   小表更不在话下。

   迁移器刻意做到最小：只前向、不回滚（桌面单机库，坏了靠备份而不是
   down-migration）、按版本号有序执行、事务内落表并记账，重跑天然无操作。
   ============================================================ */

import { mkdirSync } from 'node:fs'
import { dirname } from 'node:path'
import { DatabaseSync } from 'node:sqlite'

export interface Migration {
  /** 单调递增的版本号，与 `_migrations` 表记账对齐。 */
  version: number
  /** 本版本要执行的 DDL/DML 语句，整体包在一个事务里。 */
  statements: string[]
}

/**
 * 全部前向迁移，按 version 升序追加。已发布的条目一律只增不改——
 * 改历史条目会让老库与新库走出不同的 schema。
 */
export const MIGRATIONS: Migration[] = [
  {
    version: 1,
    statements: [
      // 配置树：列与 ConfigEntry 字段一一对应；children 树形结构用
      // parent_id + position（同层内次序）落平，load 时再拼回去。
      // config/disabled 允许 NULL 以区分「键不存在」与显式的值，
      // has_children 记录「children 键是否存在」——这样重开库读回的
      // 对象能与写入时逐字段一致（含空数组这种边角）。
      // parent_id 级联删除让整树替换（save 全量覆盖）不用操心孤儿行。
      `CREATE TABLE config_entries (
        id TEXT PRIMARY KEY,
        parent_id TEXT REFERENCES config_entries(id) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        name TEXT NOT NULL,
        config TEXT,
        disabled INTEGER,
        has_children INTEGER NOT NULL DEFAULT 0
      )`,
      `CREATE INDEX idx_config_entries_parent ON config_entries(parent_id)`,
      // 插件私有元数据的 JSON KV 区：值统一 JSON 编码，避免每个插件
      // 自己发明一张小表。
      `CREATE TABLE plugin_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
      )`,
    ],
  },
]

/**
 * 打开（必要时创建）数据库文件并设置连接级 PRAGMA。
 * 父目录不存在时先建好——桌面端首启时 userData 下的子目录未必存在。
 */
export function openStorage(path: string): DatabaseSync {
  mkdirSync(dirname(path), { recursive: true })
  const db = new DatabaseSync(path)
  // WAL：读写不互斥，桌面端 UI 读配置树时不被后台写阻塞
  db.exec('PRAGMA journal_mode = WAL')
  // foreign_keys 是连接级开关，SQLite 默认关着，每次打开都要显式开
  db.exec('PRAGMA foreign_keys = ON')
  return db
}

/**
 * 把库推进到最新版本。幂等：已应用的版本按 `_migrations` 记账跳过，
 * 全部应用过则整个调用是空操作。每个版本的语句在一个事务里执行并
 * 记账，中途失败回滚，库停在上一个完整版本。
 */
export function migrate(db: DatabaseSync, migrations: Migration[] = MIGRATIONS): void {
  db.exec(`CREATE TABLE IF NOT EXISTS _migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
  )`)
  const { current } = db
    .prepare('SELECT COALESCE(MAX(version), 0) AS current FROM _migrations')
    .get() as { current: number }

  const pending = [...migrations].sort((a, b) => a.version - b.version)
  for (const migration of pending) {
    if (migration.version <= current) continue
    db.exec('BEGIN')
    try {
      for (const statement of migration.statements) db.exec(statement)
      db.prepare('INSERT INTO _migrations (version, applied_at) VALUES (?, ?)')
        .run(migration.version, new Date().toISOString())
      db.exec('COMMIT')
    } catch (error) {
      db.exec('ROLLBACK')
      throw error
    }
  }
}
