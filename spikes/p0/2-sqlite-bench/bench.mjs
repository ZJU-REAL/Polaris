// P0 Spike 2: can SQLite (FTS5 + sqlite-vec, int8) carry the local literature store?
// Runs on the Node built-in sqlite driver (node:sqlite) + the prebuilt sqlite-vec
// loadable extension — zero native compilation. (better-sqlite3 measures the same
// SQLite engine through a different binding; the ABI/packaging chain for it is
// exercised in CI where prebuilds exist.)
// Usage: node bench.mjs [--count 100000] [--dim 1024] [--db /tmp/spike2.db] [--json out.json]
import { DatabaseSync } from 'node:sqlite'
import { getLoadablePath } from 'sqlite-vec'
import { existsSync, rmSync, statSync, writeFileSync } from 'node:fs'

const arg = (name, dflt) => {
  const i = process.argv.indexOf(`--${name}`)
  return i >= 0 ? process.argv[i + 1] : dflt
}
const COUNT = Number(arg('count', 100_000))
const DIM = Number(arg('dim', 1024))
const DB_PATH = arg('db', `/tmp/spike2-${COUNT}.db`)
const JSON_OUT = arg('json', '')
const LIBRARIES = 10
const QUERIES = Number(arg('queries', 500))
const WARMUP = Number(arg('warmup', 100))

// Deterministic PRNG (mulberry32) so runs are reproducible.
function mulberry32(seed) {
  return function () {
    seed |= 0
    seed = (seed + 0x6d2b79f5) | 0
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}
const rand = mulberry32(42)

const VOCAB = Array.from({ length: 5000 }, (_, i) => `term${i}`)
function chunkText() {
  const words = []
  for (let i = 0; i < 60; i++) words.push(VOCAB[Math.floor(rand() * VOCAB.length)])
  return words.join(' ')
}

// Random unit vector, then int8 quantization (x*127 clamp).
function randomVec() {
  const v = new Float32Array(DIM)
  let norm = 0
  for (let i = 0; i < DIM; i++) {
    v[i] = rand() * 2 - 1
    norm += v[i] * v[i]
  }
  norm = Math.sqrt(norm)
  const q = new Int8Array(DIM)
  for (let i = 0; i < DIM; i++) {
    v[i] /= norm
    q[i] = Math.max(-127, Math.min(127, Math.round(v[i] * 127)))
  }
  return { f32: v, i8: new Uint8Array(q.buffer) }
}

function percentile(samples, p) {
  const s = [...samples].sort((a, b) => a - b)
  return s[Math.min(s.length - 1, Math.floor((p / 100) * s.length))]
}
const stats = (samples) => ({
  p50: +percentile(samples, 50).toFixed(2),
  p95: +percentile(samples, 95).toFixed(2),
  p99: +percentile(samples, 99).toFixed(2),
})

const report = {
  count: COUNT,
  dim: DIM,
  runtime: process.versions.electron ? `electron ${process.versions.electron}` : `node ${process.versions.node}`,
  driver: 'node:sqlite',
}
const log = (...a) => console.log('[bench]', ...a)

if (existsSync(DB_PATH)) rmSync(DB_PATH)
const db = new DatabaseSync(DB_PATH, { allowExtension: true })
db.loadExtension(getLoadablePath())
db.exec('PRAGMA journal_mode = WAL')
db.exec('PRAGMA synchronous = NORMAL')

db.exec(`
  CREATE TABLE chunks (id INTEGER PRIMARY KEY, library_id INTEGER NOT NULL, text TEXT NOT NULL);
  CREATE VIRTUAL TABLE chunks_fts USING fts5(text, content='chunks', content_rowid='id');
  CREATE VIRTUAL TABLE chunks_vec USING vec0(
    library_id INTEGER partition key,
    embedding int8[${DIM}] distance_metric=cosine
  );
`)

// ---- ingest ----
log(`ingesting ${COUNT} chunks (dim=${DIM}, int8) on ${report.runtime}...`)
const t0 = performance.now()
const insChunk = db.prepare('INSERT INTO chunks (id, library_id, text) VALUES (?, ?, ?)')
const insFts = db.prepare('INSERT INTO chunks_fts (rowid, text) VALUES (?, ?)')
const insVec = db.prepare('INSERT INTO chunks_vec (rowid, library_id, embedding) VALUES (?, ?, vec_int8(?))')
const keepF32 = new Map() // sample of float vectors for the recall check
let inBatch = 0
db.exec('BEGIN')
for (let id = 1; id <= COUNT; id++) {
  const lib = 1 + Math.floor(rand() * LIBRARIES)
  const vec = randomVec()
  if (id <= 2000) keepF32.set(id, vec.f32)
  const text = chunkText()
  insChunk.run(BigInt(id), BigInt(lib), text)
  insFts.run(BigInt(id), text)
  insVec.run(BigInt(id), BigInt(lib), vec.i8)
  if (++inBatch === 2000) {
    db.exec('COMMIT')
    db.exec('BEGIN')
    inBatch = 0
  }
}
db.exec('COMMIT')
const ingestSec = (performance.now() - t0) / 1000
report.ingest = { seconds: +ingestSec.toFixed(1), rowsPerSec: Math.round(COUNT / ingestSec) }
log(`ingest: ${report.ingest.seconds}s (${report.ingest.rowsPerSec} rows/s)`)
db.exec("INSERT INTO chunks_fts(chunks_fts) VALUES('optimize')")
report.dbBytes = statSync(DB_PATH).size
log(`db size: ${(report.dbBytes / 1e9).toFixed(2)} GB`)

// ---- query helpers ----
const qVec = db.prepare(
  `SELECT rowid, distance FROM chunks_vec WHERE embedding MATCH vec_int8(?) AND k = 50 ORDER BY distance`,
)
const qVecLib = db.prepare(
  `SELECT rowid, distance FROM chunks_vec WHERE embedding MATCH vec_int8(?) AND library_id = ? AND k = 50 ORDER BY distance`,
)
const qFts = db.prepare(
  `SELECT rowid, rank FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY rank LIMIT 50`,
)
const queryVecs = Array.from({ length: QUERIES + WARMUP }, () => randomVec())
const queryTerms = Array.from(
  { length: QUERIES + WARMUP },
  () => `${VOCAB[Math.floor(rand() * VOCAB.length)]} OR ${VOCAB[Math.floor(rand() * VOCAB.length)]}`,
)

function bench(name, fn) {
  for (let i = 0; i < WARMUP; i++) fn(i)
  const samples = []
  for (let i = 0; i < QUERIES; i++) {
    const t = performance.now()
    fn(WARMUP + i)
    samples.push(performance.now() - t)
  }
  report[name] = stats(samples)
  log(`${name}: p50=${report[name].p50}ms p95=${report[name].p95}ms p99=${report[name].p99}ms`)
}

bench('vectorKnn', (i) => qVec.all(queryVecs[i].i8))
bench('vectorKnnPartitioned', (i) => qVecLib.all(queryVecs[i].i8, BigInt(1 + (i % LIBRARIES))))
bench('fts', (i) => qFts.all(queryTerms[i]))
bench('hybridRrf', (i) => {
  const v = qVec.all(queryVecs[i].i8)
  const f = qFts.all(queryTerms[i])
  const score = new Map()
  v.forEach((r, rank) => score.set(r.rowid, (score.get(r.rowid) ?? 0) + 1 / (60 + rank)))
  f.forEach((r, rank) => score.set(r.rowid, (score.get(r.rowid) ?? 0) + 1 / (60 + rank)))
  ;[...score.entries()].sort((a, b) => b[1] - a[1]).slice(0, 20)
})

// ---- int8 vs float32 recall (top-20 overlap, brute force over the f32 sample) ----
{
  const ids = [...keepF32.keys()]
  let overlapSum = 0
  const trials = 20
  for (let t = 0; t < trials; t++) {
    const q = queryVecs[t].f32
    const dots = ids.map((id) => {
      const v = keepF32.get(id)
      let d = 0
      for (let i = 0; i < DIM; i++) d += q[i] * v[i]
      return [id, d]
    })
    const truth = new Set(dots.sort((a, b) => b[1] - a[1]).slice(0, 20).map(([id]) => id))
    const qi = new Int8Array(queryVecs[t].i8.buffer)
    const dotsQ = ids.map((id) => {
      const v = keepF32.get(id)
      let d = 0
      for (let i = 0; i < DIM; i++) d += qi[i] * Math.max(-127, Math.min(127, Math.round(v[i] * 127)))
      return [id, d]
    })
    const got = dotsQ.sort((a, b) => b[1] - a[1]).slice(0, 20).map(([id]) => id)
    overlapSum += got.filter((id) => truth.has(id)).length / 20
  }
  report.int8Top20Overlap = +(overlapSum / trials).toFixed(3)
  log(`int8 vs f32 top-20 overlap: ${report.int8Top20Overlap}`)
}

// ---- cold open + first query ----
db.close()
{
  const t = performance.now()
  const db2 = new DatabaseSync(DB_PATH, { readOnly: true, allowExtension: true })
  db2.loadExtension(getLoadablePath())
  db2
    .prepare(`SELECT rowid FROM chunks_vec WHERE embedding MATCH vec_int8(?) AND k = 50 ORDER BY distance`)
    .all(queryVecs[0].i8)
  report.coldOpenFirstQueryMs = +(performance.now() - t).toFixed(0)
  log(`cold open + first query: ${report.coldOpenFirstQueryMs}ms`)
  db2.close()
}

if (JSON_OUT) writeFileSync(JSON_OUT, JSON.stringify(report, null, 2))
log('done', JSON.stringify(report))
