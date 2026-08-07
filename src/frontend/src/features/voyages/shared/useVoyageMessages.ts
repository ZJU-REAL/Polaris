import { useCallback } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api, ApiError, type VoyageDetail, type VoyageMessageRead } from '../../../lib/api';

/* ============================================================
   任务对话流数据：初始拉取 + SSE 增量合并（按 id 去重、seq 排序）。
   与 useVoyageChannel 配合：把它的 onExtraEvent 交给 handleExtraEvent，
   message / ask.created / ask.answered 事件自动进缓存。
   ============================================================ */

function upsert(list: VoyageMessageRead[] | undefined, message: VoyageMessageRead) {
  const prev = list ?? [];
  const i = prev.findIndex((m) => m.id === message.id);
  const next = i >= 0 ? prev.map((m, j) => (j === i ? message : m)) : [...prev, message];
  return next.sort((a, b) => a.seq - b.seq);
}

export function useVoyageMessages(voyageId: string | null) {
  const id = voyageId ?? '';
  const queryClient = useQueryClient();

  const { data: messages } = useQuery({
    queryKey: ['voyage-messages', id],
    // 旧后端没有该端点：404 当空流（页面其余部分照常）
    queryFn: () =>
      api.listVoyageMessages(id).catch((e) => {
        if (e instanceof ApiError && e.status === 404) return [] as VoyageMessageRead[];
        throw e;
      }),
    enabled: !!id,
    staleTime: Infinity, // 增量全走 SSE
    refetchOnWindowFocus: false,
    retry: false,
  });

  /** 交给 useVoyageChannel 的 onExtraEvent：消费 message / ask.* 事件。 */
  const handleExtraEvent = useCallback(
    (event: string, data: unknown) => {
      if (event !== 'message' && event !== 'ask.created' && event !== 'ask.answered') return;
      const payload = data as { message?: VoyageMessageRead; ask_id?: string };
      if (!payload?.message) return;
      queryClient.setQueryData<VoyageMessageRead[]>(['voyage-messages', id], (old) =>
        upsert(old, payload.message!),
      );
      if (event === 'ask.answered' && payload.ask_id) {
        // 事件只带回答消息；被回答的提问行要就地翻状态，否则界面卡在「等你回复」
        queryClient.setQueryData<VoyageMessageRead[]>(['voyage-messages', id], (old) =>
          (old ?? []).map((m) =>
            m.id === payload.ask_id && m.status === 'open' ? { ...m, status: 'answered' } : m,
          ),
        );
      }
      if (event !== 'message') {
        // ask 状态变化伴随任务状态变化（paused_ask / executing）：刷详情
        void queryClient.invalidateQueries({ queryKey: ['voyage', id] });
      }
    },
    [id, queryClient],
  );

  return { messages: messages ?? [], handleExtraEvent };
}

/** 当前等回答的提问：详情字段优先，消息流兜底（SSE 先到、详情还没刷时）。 */
export function openAskOf(
  voyage: VoyageDetail | undefined,
  messages: VoyageMessageRead[],
): VoyageMessageRead | null {
  if (voyage?.open_ask && voyage.open_ask.status === 'open') return voyage.open_ask;
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const m = messages[i]!;
    if (m.kind === 'ask' && m.status === 'open') return m;
  }
  return null;
}
