/* 迁移前快照 + 失败回滚（#694）的单元测试。用「追加一条坏 SQL 的
   迁移」模拟升级事故：断言库文件被逐字节还原、原错误照抛、之后仍能
   用正常迁移打开且数据无损；成功路径断言快照存在并修剪到只留 3 份。 */
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterAll, describe, expect, it } from 'vitest'
import {
  MIGRATIONS,
  PluginMetaStore,
  SNAPSHOT_KEEP,
  openStorageWithMigrations,
  type Migration,
} from '../src/index.ts'

const dir = mkdtempSync(join(tmpdir(), 'kernel-snapshot-'))
afterAll(() => rmSync(dir, { recursive: true, force: true }))

let seq = 0
/** 每个用例独立子目录：snapshots/ 落在库旁边，互相不串。 */
const freshPath = (): string => join(dir, `case-${++seq}`, 'storage.db')

/** 在 MIGRATIONS 之后追加一条必然失败的迁移，模拟「升级把库搞坏」。 */
const badMigrations = (): Migration[] => [
  ...MIGRATIONS,
  { version: 9999, statements: ['THIS IS NOT SQL'] },
]

const snapshotsRoot = (path: string): string => join(path, '..', 'snapshots')

describe('database snapshot before migrations', () => {
  it('does not snapshot a fresh (empty) database', () => {
    const path = freshPath()
    const db = openStorageWithMigrations(path)
    db.close()
    expect(existsSync(snapshotsRoot(path))).toBe(false)
  })

  it('restores the files and rethrows when a migration fails', () => {
    const path = freshPath()
    // 先造一个有数据的正常库
    const db = openStorageWithMigrations(path)
    new PluginMetaStore(db).set('探针', { 值: '迁移前' })
    db.close()
    const before = readFileSync(path)

    // 坏迁移：错误原样抛出，不能被吞成静默降级
    expect(() => openStorageWithMigrations(path, badMigrations())).toThrow(/syntax|SQL/i)

    // 库文件逐字节回到迁移前；失败留下的 -wal/-shm 也被清场
    expect(readFileSync(path)).toEqual(before)
    expect(existsSync(`${path}-wal`)).toBe(false)
    expect(existsSync(`${path}-shm`)).toBe(false)

    // 失败现场的快照保留（不修剪），且就是迁移前的那份
    const kept = readdirSync(snapshotsRoot(path))
    expect(kept.length).toBe(1)
    expect(readFileSync(join(snapshotsRoot(path), kept[0]!, 'storage.db'))).toEqual(before)

    // 还原后的库仍可正常打开，数据无损
    const reopened = openStorageWithMigrations(path)
    expect(new PluginMetaStore(reopened).get('探针')).toEqual({ 值: '迁移前' })
    reopened.close()
  })

  it('keeps a snapshot on success and prunes to the retention limit', () => {
    const path = freshPath()
    // 首启（空库）不产生快照；之后每次打开都会先快照再迁移
    for (let round = 0; round < SNAPSHOT_KEEP + 3; round++) {
      openStorageWithMigrations(path).close()
    }
    const names = readdirSync(snapshotsRoot(path)).sort()
    expect(names.length).toBe(SNAPSHOT_KEEP)
    // 每份快照都完整带着主库文件
    for (const name of names) {
      expect(existsSync(join(snapshotsRoot(path), name, 'storage.db'))).toBe(true)
    }
  })
})
