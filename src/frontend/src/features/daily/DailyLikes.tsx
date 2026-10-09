import { useState, type MouseEvent } from 'react';
import { useMutation, useQueryClient, type QueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import {
  api,
  type DailyLikeState,
  type DailyPage,
  type DailyPaperDetail,
  type DailyPaperItem,
} from '../../lib/api';
import { tr } from '../../lib/i18n';

/* ============================================================
   每日新论文的点赞开关：只有一颗爱心，实心 = 已赞（进「我赞过的」）。
   单用户本地版没有别人，点赞人数与名单都没有意义，所以不再显示。
   点击乐观更新（翻转 liked_by_me），服务端返回后对账。
   ============================================================ */

// 点赞爱心的红色（各端点赞通用色，刻意不随主题变化的局部常量）
const HEART_RED = '#e0245e';

/** 把点赞状态写回所有相关缓存（列表分页 / 我赞过的 / 详情）。 */
function applyLikeState(qc: QueryClient, state: DailyLikeState) {
  const fields = {
    like_count: state.like_count,
    liked_by_me: state.liked_by_me,
    likers_preview: state.likers_preview,
  };
  const patch = (it: DailyPaperItem): DailyPaperItem =>
    it.entry_id === state.entry_id ? { ...it, ...fields } : it;
  const patchPage = (old: DailyPage | undefined): DailyPage | undefined =>
    old ? { ...old, items: old.items.map(patch) } : old;
  qc.setQueriesData<DailyPage>({ queryKey: ['daily-papers'] }, patchPage);
  qc.setQueriesData<DailyPage>({ queryKey: ['daily-liked'] }, patchPage);
  qc.setQueriesData<DailyPaperDetail>({ queryKey: ['daily-paper', state.entry_id] }, (old) =>
    old ? { ...old, ...fields } : old,
  );
}

export function DailyLikes({ item }: { item: DailyPaperItem }) {
  const qc = useQueryClient();
  // 点赞的瞬间爱心弹跳：key 换新触发 CSS 动画重放，不影响布局
  const [popKey, setPopKey] = useState(0);

  const likeMutation = useMutation({
    mutationFn: (liked: boolean) =>
      liked ? api.likeDailyPaper(item.entry_id) : api.unlikeDailyPaper(item.entry_id),
    onMutate: (liked) => {
      applyLikeState(qc, {
        entry_id: item.entry_id,
        like_count: liked ? 1 : 0,
        liked_by_me: liked,
        likers_preview: [],
      });
    },
    onSuccess: (state) => applyLikeState(qc, state),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ['daily-papers'] });
      void qc.invalidateQueries({ queryKey: ['daily-liked'] });
    },
  });

  const liked = item.liked_by_me;
  const label = liked ? tr('取消点赞', 'Unlike') : tr('点赞', 'Like');
  const toggle = (e: MouseEvent) => {
    e.stopPropagation();
    if (!liked) setPopKey((k) => k + 1);
    likeMutation.mutate(!liked);
  };

  return (
    <button
      className="icon-btn"
      style={{ width: 24, height: 24, flexShrink: 0 }}
      title={label}
      aria-label={label}
      aria-pressed={liked}
      onClick={toggle}
    >
      <span key={popKey} className={popKey > 0 ? 'heart-pop' : undefined} style={{ display: 'flex' }}>
        <Icon
          name={liked ? 'heartFill' : 'heart'}
          size={15}
          style={{ color: liked ? HEART_RED : 'var(--text-3)' }}
        />
      </span>
    </button>
  );
}
