/* ============================================================
   市场索引拉取（#700）：GET endpoint → 校验后的 MarketIndex。

   endpoint 与 fetch 都注入：默认打主仓 raw URL，但换源（镜像/自建
   索引）是一等能力——吸取 Koishi 官方市场绑死 npm search API 停摆
   数月的教训（设计报告 §7.4）。超时/非 200/坏 JSON/坏 schema 各给
   独立错误码，上层可以区分「网络断了（走本地缓存）」和「索引坏了
   （该报警）」。
   本文件必须保持 electron-free（tests/electron-free.test.ts 强制）。
   ============================================================ */

import type { FetchImpl } from '../sources/contract.ts'
import { MarketError, validateMarketIndex, type MarketIndexEntry } from './contract.ts'

/** 官方索引的 raw URL（主仓 market/index.json）。 */
export const OFFICIAL_INDEX_URL =
  'https://raw.githubusercontent.com/ZJU-REAL/Polaris/main/market/index.json'

const DEFAULT_TIMEOUT_MS = 15_000

export interface FetchIndexOptions {
  fetchImpl?: FetchImpl
  /** 整次请求的超时；索引很小，超时即当网络不可用处理。 */
  timeoutMs?: number
}

/** 拉取并校验市场索引，返回条目数组。 */
export async function fetchIndex(
  endpoint: string = OFFICIAL_INDEX_URL,
  options: FetchIndexOptions = {},
): Promise<MarketIndexEntry[]> {
  const fetchImpl = options.fetchImpl ?? globalThis.fetch
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS

  // AbortController 而不是 Promise.race：race 赢了之后请求还挂在后台
  // 占着 socket，abort 才是真取消
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  let res: Response
  try {
    res = await fetchImpl(endpoint, { signal: controller.signal })
  } catch (error) {
    if (controller.signal.aborted) {
      throw new MarketError('index-timeout', `index fetch timed out after ${timeoutMs}ms: ${endpoint}`)
    }
    throw new MarketError('index-http', `index fetch failed: ${endpoint}`, { cause: error })
  } finally {
    clearTimeout(timer)
  }

  if (!res.ok) {
    throw new MarketError('index-http', `index fetch failed with HTTP ${res.status}: ${endpoint}`)
  }
  let body: unknown
  try {
    body = await res.json()
  } catch (error) {
    throw new MarketError('index-parse', `index is not valid JSON: ${endpoint}`, { cause: error })
  }
  return validateMarketIndex(body).plugins
}
