/* ============================================================
   每场对话「谁来答」的记忆（#836）。

   后端把选择存在会话设置里，但会话接口不回这一项，所以前端自己记一份：
   会话 id → 后端 id，外加「新会话默认用谁」。每轮都把选择带上，两边不会走岔。
   localStorage 可能不可用（隐私模式），一律 try/catch，失败就只在本次生效。
   ============================================================ */

export const POLARIS_BACKEND = 'polaris';
const MAP_KEY = 'polaris.buddyBackends';
const DEFAULT_KEY = 'polaris.buddyBackendDefault';
/** 记太多没意义：只留最近这些场 */
const MAX_ENTRIES = 200;

type Storage = Pick<globalThis.Storage, 'getItem' | 'setItem'>;

function store(): Storage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage;
  } catch {
    return null;
  }
}

function readMap(s: Storage | null): Record<string, string> {
  if (!s) return {};
  try {
    const parsed: unknown = JSON.parse(s.getItem(MAP_KEY) ?? '{}');
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {};
    const out: Record<string, string> = {};
    for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
      if (typeof v === 'string' && v) out[k] = v;
    }
    return out;
  } catch {
    return {};
  }
}

/** 这场对话选的是谁；没记过就用新会话默认值。 */
export function readBackendChoice(convId: string | null, s: Storage | null = store()): string {
  if (convId) {
    const hit = readMap(s)[convId];
    if (hit) return hit;
  }
  try {
    return s?.getItem(DEFAULT_KEY) || POLARIS_BACKEND;
  } catch {
    return POLARIS_BACKEND;
  }
}

/** 记下选择：有会话就记到这场上，同时作为之后新会话的默认值。 */
export function writeBackendChoice(convId: string | null, backend: string, s: Storage | null = store()): void {
  if (!s) return;
  try {
    s.setItem(DEFAULT_KEY, backend);
    if (!convId) return;
    const map = readMap(s);
    delete map[convId];
    map[convId] = backend;
    const keys = Object.keys(map);
    for (const k of keys.slice(0, Math.max(0, keys.length - MAX_ENTRIES))) delete map[k];
    s.setItem(MAP_KEY, JSON.stringify(map));
  } catch {
    /* 写不进去就只在本次生效 */
  }
}

/** 选过的 agent 被删了 / 不再共享时退回 Polaris，别带着一个不存在的 id 去问。 */
export function resolveBackend(choice: string, available: { id: string }[]): string {
  return available.some((b) => b.id === choice) ? choice : POLARIS_BACKEND;
}
