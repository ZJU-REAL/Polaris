import type { AssistantBlock } from '../../lib/assistantStream';

/* ============================================================
   每场对话各自的轮次（#850）。

   以前面板只有一份 turns，流里来的帧一律写进「当前显示的那场」的最后一轮：
   A 还在跑时切到 B，A 后面的帧就落进了 B。

   现在按会话 id 分开存：流只写它自己那场；在跑的会话切回来时用内存里这份
   （这一轮还没落库，服务端拉不到），不在跑的才从服务端重新拉。
   ============================================================ */

export interface Turn {
  role: 'user' | 'assistant';
  blocks: AssistantBlock[];
}

export type TurnsByConv = Readonly<Record<string, Turn[]>>;

const EMPTY: Turn[] = [];

export function turnsOf(map: TurnsByConv, key: string): Turn[] {
  return map[key] ?? EMPTY;
}

/** 整体改某一场的轮次；没变就原样返回，免得白白重渲染。 */
export function updateConvTurns(map: TurnsByConv, key: string, fn: (turns: Turn[]) => Turn[]): TurnsByConv {
  const prev = turnsOf(map, key);
  const next = fn(prev);
  return next === prev ? map : { ...map, [key]: next };
}

/** 流里的帧：只改**这场**的最后一轮（必须是助手轮），别的会话一概不碰。 */
export function patchConvTurn(
  map: TurnsByConv,
  key: string,
  fn: (blocks: AssistantBlock[]) => AssistantBlock[],
): TurnsByConv {
  return updateConvTurns(map, key, (turns) => {
    const last = turns[turns.length - 1];
    if (!last || last.role !== 'assistant') return turns;
    const next = [...turns];
    next[next.length - 1] = { ...last, blocks: fn(last.blocks) };
    return next;
  });
}

/** 新会话拿到真 id：占位那份挪到真 id 名下。 */
export function renameConv(map: TurnsByConv, from: string, to: string): TurnsByConv {
  if (!(from in map) || from === to) return map;
  const { [from]: turns, ...rest } = map;
  return { ...rest, [to]: turns ?? EMPTY };
}

/** 只留还用得着的：正在显示的那场 + 还在跑的。其余切回来时从服务端重拉。 */
export function pruneConvTurns(map: TurnsByConv, keep: ReadonlySet<string>): TurnsByConv {
  const keys = Object.keys(map);
  if (keys.every((k) => keep.has(k))) return map;
  const out: Record<string, Turn[]> = {};
  for (const k of keys) if (keep.has(k)) out[k] = map[k] ?? EMPTY;
  return out;
}

/** 切到某场时要不要用内存里的：它还在跑、且内存里有，就用（这一轮还没落库）。 */
export function keepLocalTurns(map: TurnsByConv, id: string, running: ReadonlySet<string>): boolean {
  return running.has(id) && (map[id]?.length ?? 0) > 0;
}
