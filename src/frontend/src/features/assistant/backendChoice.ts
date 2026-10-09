/* ============================================================
   每场对话「谁来答」的记忆（#836）。

   后端把选择存在会话设置里，会话列表也回这一项（backend）。前端另记一份
   「用户在这台机器上亲手选过的」：会话 id → 后端 id，外加「新会话默认用谁」。
   老会话本地没记过时以服务端存着的为准，而且**不带 backend**——否则会拿
   「上次随手选的默认值」把那场对话原本的选择盖掉。
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

/** 这场对话由谁来答。

    - 新会话（convId 为空）：用上次选过的默认值；选过才算「明确选择」。
    - 老会话：本地记过就用本地的（明确选择）；没记过就用服务端存着的，不算明确选择。
    只有 explicit 为 true 时才该把 backend 发给后端。 */
export function readBackendChoice(
  convId: string | null,
  serverBackend: string | null | undefined = null,
  s: Storage | null = store(),
): { choice: string; explicit: boolean } {
  if (convId) {
    const hit = readMap(s)[convId];
    if (hit) return { choice: hit, explicit: true };
    return { choice: serverBackend || POLARIS_BACKEND, explicit: false };
  }
  try {
    const def = s?.getItem(DEFAULT_KEY);
    if (def) return { choice: def, explicit: true };
  } catch {
    /* 读不到就当没选过 */
  }
  return { choice: POLARIS_BACKEND, explicit: false };
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

/** 选过的 agent 被删了 / 停用时退回 Polaris，别带着一个不存在的 id 去问。 */
export function resolveBackend(choice: string, available: { id: string }[]): string {
  return available.some((b) => b.id === choice) ? choice : POLARIS_BACKEND;
}
