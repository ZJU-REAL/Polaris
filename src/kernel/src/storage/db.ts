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

import { copyFileSync, existsSync, mkdirSync, readdirSync, rmSync, statSync } from 'node:fs'
import { basename, dirname, join } from 'node:path'
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

/* ---------- 迁移前快照 + 失败回滚（#694） ----------
   迁移器只前向不回滚，但「迁移把库改坏」这种事故必须可撤销：每次
   migrate 前把库文件按原样复制到 snapshots/<时间戳>/ 下，失败就整组
   文件复制回来——文件级还原比 SQL 级 down-migration 简单且绝对可靠。
   与桌面引擎侧（engine-bootstrap 启动器里的 alembic 守卫）语义对齐。 */

/** 快照要带上的 SQLite 伴生文件后缀。WAL 模式下 -wal 里可能有未合并
    的写入，只拷主文件会丢数据；-shm 是共享内存索引，跟着拷保持成组。 */
const DB_FILE_SUFFIXES = ['', '-wal', '-shm'] as const

/** 成功迁移后保留的快照份数，更旧的删除。 */
export const SNAPSHOT_KEEP = 3

/** 库文件旁边的快照根目录：<库所在目录>/snapshots。 */
function snapshotRoot(path: string): string {
  return join(dirname(path), 'snapshots')
}

/**
 * 把库文件（含 -wal/-shm 若存在）复制成一份快照，返回快照目录。
 * 空库或不存在（首启）返回 null——没有可保护的数据就不留空快照。
 * 必须在库被打开之前调用：无打开句柄时磁盘上的文件组才保证自洽。
 */
export function snapshotDatabase(path: string): string | null {
  if (!existsSync(path) || statSync(path).size === 0) return null
  // 冒号在 Windows 文件名里非法，换成横线；同毫秒重入时用 -2/-3 后缀
  // 避让（后缀名字典序仍排在原名之后，prune 的排序不乱）。
  const stamp = new Date().toISOString().replace(/:/g, '-')
  const root = snapshotRoot(path)
  let dir = join(root, stamp)
  for (let n = 2; existsSync(dir); n++) dir = join(root, `${stamp}-${n}`)
  mkdirSync(dir, { recursive: true })
  for (const suffix of DB_FILE_SUFFIXES) {
    if (existsSync(path + suffix)) copyFileSync(path + suffix, join(dir, basename(path) + suffix))
  }
  return dir
}

/**
 * 用快照覆盖回库文件。快照里没有的伴生文件要把现场的删掉：失败的
 * 迁移可能留下一个新 -wal，主文件还原后再被它重放就又脏了。
 * 必须在库句柄关闭之后调用。
 */
export function restoreSnapshot(path: string, snapshotDir: string): void {
  for (const suffix of DB_FILE_SUFFIXES) {
    const saved = join(snapshotDir, basename(path) + suffix)
    if (existsSync(saved)) copyFileSync(saved, path + suffix)
    else rmSync(path + suffix, { force: true })
  }
}

/** 按名字（即时间戳）排序，删掉最旧的、只留 keep 份。 */
export function pruneSnapshots(path: string, keep: number = SNAPSHOT_KEEP): void {
  const root = snapshotRoot(path)
  if (!existsSync(root)) return
  const dirs = readdirSync(root, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => entry.name)
    .sort()
  for (const name of dirs.slice(0, Math.max(0, dirs.length - keep))) {
    rmSync(join(root, name), { recursive: true, force: true })
  }
}

/**
 * 打开并迁移，整个过程被快照守护：迁移抛错 → 关句柄 → 文件还原 →
 * 原错误照抛（调用方按「迁移失败」处理，库保证停在迁移前的状态）；
 * 成功才修剪旧快照——失败现场的快照永远留着供人排查。
 */
export function openStorageWithMigrations(
  path: string,
  migrations: Migration[] = MIGRATIONS,
): DatabaseSync {
  const snapshot = snapshotDatabase(path)
  const db = openStorage(path)
  try {
    migrate(db, migrations)
  } catch (error) {
    try {
      db.close()
    } catch {
      // 还原优先：句柄关不上也要把文件抢救回去
    }
    if (snapshot) restoreSnapshot(path, snapshot)
    throw error
  }
  if (snapshot) pruneSnapshots(path)
  return db
}
