/* ============================================================
   每场对话「由谁回答」。

   默认是 Polaris 自己（Claude）。用户可以为一场对话选一个接在本机上的智能体
   （Claude Code、Codex、pi……）；选择跟着这场对话走，不带到下一场——新对话
   总是从 Polaris 开始，想交给智能体就再选一次。

   后端把选择存在会话设置里，会话列表也回这一项（backend）。前端另记一份
   「用户在这台机器上亲手选过的」：会话 id → 回答方 id。只有亲手选过才把 backend
   发给后端；老会话本地没记过时以服务端存着的为准，而且不带 backend，免得把那场
   对话原本的选择盖掉。
   localStorage 可能不可用（隐私模式），一律 try/catch，失败就只在本次生效。
   ============================================================ */

export const POLARIS_BACKEND = 'polaris';
/** 设置里「本机智能体」那一页 */
export const LOCAL_AGENTS_HREF = '/settings?tab=agents';
const MAP_KEY = 'polaris.buddyBackends';
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

    - 新对话（convId 为空）：Polaris，不算明确选择。
    - 老对话：本地记过就用本地的（明确选择）；没记过就用服务端存着的，不算明确选择。
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
  return { choice: POLARIS_BACKEND, explicit: false };
}

/** 记下这场对话的选择（新对话还没有 id 时不记，建好之后由调用方补记）。 */
export function writeBackendChoice(convId: string | null, backend: string, s: Storage | null = store()): void {
  if (!s || !convId) return;
  try {
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

/** 选过的智能体被删了 / 停用时退回 Polaris，别带着一个不存在的 id 去问。 */
export function resolveBackend(choice: string, available: { id: string }[]): string {
  return available.some((b) => b.id === choice) ? choice : POLARIS_BACKEND;
}

/** 选择器按钮上的字：Polaris 写成「Polaris · Claude」，智能体用它自己的名字。 */
export function backendLabel(item: { kind: string; name: string } | undefined): string {
  if (!item || item.kind === 'polaris') return 'Polaris · Claude';
  return item.name;
}
