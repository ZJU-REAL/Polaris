import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { Avatar } from '../../components/ui/Avatar';
import { Facepile } from '../../components/ui/Facepile';
import {
  api,
  type DailyLiker,
  type DailyLikeState,
  type DailyPage,
  type DailyPaperDetail,
  type DailyPaperItem,
} from '../../lib/api';
import { tr } from '../../lib/i18n';

/* ============================================================
   每日新论文的行内点赞区：[♥] [头像堆] [+N] [数字]。
   - 点击爱心乐观更新（翻转 liked_by_me、count±1、头像堆增删自己）；
   - 悬停头像堆 ~300ms 弹出完整点赞名单（懒加载）；
   - like_count=0 时只渲染灰爱心 + 0，行高不抖动。
   ============================================================ */

// 点赞爱心的红色（各端点赞通用色，刻意不随主题变化的局部常量）
const HEART_RED = '#e0245e';

const POPOVER_W = 208;

/** 把点赞汇总写回所有相关缓存（列表分页 / 我赞过的 / 详情）。 */
function applyLikeState(qc: QueryClient, state: DailyLikeState) {
  const patch = (it: DailyPaperItem): DailyPaperItem =>
    it.entry_id === state.entry_id
      ? {
          ...it,
          like_count: state.like_count,
          liked_by_me: state.liked_by_me,
          likers_preview: state.likers_preview,
        }
      : it;
  const patchPage = (old: DailyPage | undefined): DailyPage | undefined =>
    old ? { ...old, items: old.items.map(patch) } : old;
  qc.setQueriesData<DailyPage>({ queryKey: ['daily-papers'] }, patchPage);
  qc.setQueriesData<DailyPage>({ queryKey: ['daily-liked'] }, patchPage);
  qc.setQueriesData<DailyPaperDetail>({ queryKey: ['daily-paper', state.entry_id] }, (old) =>
    old
      ? {
          ...old,
          like_count: state.like_count,
          liked_by_me: state.liked_by_me,
          likers_preview: state.likers_preview,
        }
      : old,
  );
}

export function DailyLikes({ item }: { item: DailyPaperItem }) {
  const qc = useQueryClient();
  const { data: me } = useQuery({
    queryKey: ['me'],
    queryFn: () => api.me(),
    retry: false,
    staleTime: 60_000,
  });

  // 点赞瞬间爱心弹跳：key 换新触发 CSS 动画重放，不影响布局
  const [popKey, setPopKey] = useState(0);

  const likeMutation = useMutation({
    mutationFn: (liked: boolean) =>
      liked ? api.likeDailyPaper(item.entry_id) : api.unlikeDailyPaper(item.entry_id),
    onMutate: (liked) => {
      // 乐观更新：立即翻转状态；自己的头像插到堆头部 / 从堆里移除
      let preview = item.likers_preview.filter((u) => u.id !== me?.id);
      if (liked && me) {
        const meLiker: DailyLiker = {
          id: me.id,
          display_name: me.display_name ?? me.email.split('@')[0] ?? '',
          has_avatar: me.has_avatar ?? false,
        };
        preview = [meLiker, ...preview].slice(0, 5);
      }
      applyLikeState(qc, {
        entry_id: item.entry_id,
        like_count: Math.max(0, item.like_count + (liked ? 1 : -1)),
        liked_by_me: liked,
        likers_preview: preview,
      });
    },
    onSuccess: (state) => applyLikeState(qc, state),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ['daily-papers'] });
      void qc.invalidateQueries({ queryKey: ['daily-liked'] });
      void qc.invalidateQueries({ queryKey: ['daily-likers', item.entry_id] });
    },
  });

  // —— 悬停头像堆 → 延迟弹出完整名单 ——
  const [hovered, setHovered] = useState(false);
  const [anchor, setAnchor] = useState<{ top: number; bottom: number; right: number } | null>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const openTimer = useRef<number | null>(null);
  const closeTimer = useRef<number | null>(null);
  useEffect(
    () => () => {
      if (openTimer.current) window.clearTimeout(openTimer.current);
      if (closeTimer.current) window.clearTimeout(closeTimer.current);
    },
    [],
  );

  const onEnter = () => {
    if (closeTimer.current) {
      window.clearTimeout(closeTimer.current);
      closeTimer.current = null;
    }
    if (hovered || openTimer.current) return;
    openTimer.current = window.setTimeout(() => {
      openTimer.current = null;
      const r = wrapRef.current?.getBoundingClientRect();
      if (r) setAnchor({ top: r.top, bottom: r.bottom, right: r.right });
      setHovered(true);
    }, 300);
  };
  const onLeave = () => {
    if (openTimer.current) {
      window.clearTimeout(openTimer.current);
      openTimer.current = null;
    }
    // 稍作延迟：从头像堆滑进弹层的途中不立即关闭
    closeTimer.current = window.setTimeout(() => {
      closeTimer.current = null;
      setHovered(false);
    }, 180);
  };

  const likersQuery = useQuery({
    queryKey: ['daily-likers', item.entry_id],
    queryFn: () => api.listDailyLikers(item.entry_id),
    enabled: hovered && item.like_count > 0,
    staleTime: 30_000,
    retry: false,
  });

  const toggle = () => {
    const next = !item.liked_by_me;
    if (next) setPopKey((k) => k + 1);
    likeMutation.mutate(next);
  };

  // 弹层放行上方；贴近视口顶部时改放下方
  const below = anchor !== null && anchor.top < 240;

  return (
    <div
      className="row"
      style={{ gap: 5, flexShrink: 0 }}
      onClick={(e) => e.stopPropagation()}
    >
      <button
        className="icon-btn"
        style={{ width: 24, height: 24 }}
        title={item.liked_by_me ? tr('取消点赞', 'Unlike') : tr('点赞', 'Like')}
        onClick={toggle}
      >
        <span key={popKey} className={popKey > 0 ? 'heart-pop' : undefined} style={{ display: 'flex' }}>
          <Icon
            name={item.liked_by_me ? 'heartFill' : 'heart'}
            size={15}
            style={{ color: item.liked_by_me ? HEART_RED : 'var(--text-4)' }}
          />
        </span>
      </button>

      {item.like_count > 0 && item.likers_preview.length > 0 && (
        <div
          ref={wrapRef}
          onMouseEnter={onEnter}
          onMouseLeave={onLeave}
          style={{ display: 'flex', position: 'relative' }}
        >
          <Facepile
            users={item.likers_preview}
            total={item.like_count}
            size={20}
            accentFirst={item.liked_by_me}
          />

          {hovered && anchor && (
            <div
              className="card"
              style={{
                position: 'fixed',
                zIndex: 60,
                width: POPOVER_W,
                padding: '8px 0 6px',
                boxShadow: 'var(--shadow-pop)',
                left: Math.max(8, anchor.right - POPOVER_W),
                ...(below
                  ? { top: anchor.bottom + 6 }
                  : { bottom: window.innerHeight - anchor.top + 6 }),
                animation: 'fadeUp 0.12s ease',
              }}
            >
              <div style={{ fontSize: 11, fontWeight: 700, color: 'var(--text-3)', padding: '0 12px 6px' }}>
                {tr(`${item.like_count} 人点了赞`, `${item.like_count} likes`)}
              </div>
              <div className="scroll" style={{ maxHeight: 12 * 26, overflowY: 'auto' }}>
                {likersQuery.isLoading ? (
                  <div style={{ padding: '5px 12px', fontSize: 11.5, color: 'var(--text-4)' }}>
                    {tr('加载中…', 'Loading…')}
                  </div>
                ) : likersQuery.isError ? (
                  <div style={{ padding: '5px 12px', fontSize: 11.5, color: 'var(--text-4)' }}>
                    {tr('名单加载失败', 'Failed to load')}
                  </div>
                ) : (
                  (likersQuery.data ?? []).map((u) => (
                    <div key={u.id} className="row gap8" style={{ padding: '5px 12px' }}>
                      <Avatar userId={u.id} hasAvatar={u.has_avatar} name={u.display_name} size={16} />
                      <span
                        style={{
                          fontSize: 12,
                          minWidth: 0,
                          overflow: 'hidden',
                          textOverflow: 'ellipsis',
                          whiteSpace: 'nowrap',
                          color: u.id === me?.id ? 'var(--accent-text)' : 'var(--text)',
                          fontWeight: u.id === me?.id ? 650 : 450,
                        }}
                      >
                        {u.display_name}
                      </span>
                    </div>
                  ))
                )}
              </div>
            </div>
          )}
        </div>
      )}

      <span
        className="mono"
        style={{
          fontSize: 11,
          color: item.like_count > 0 ? 'var(--text-3)' : 'var(--text-4)',
          minWidth: 14,
        }}
      >
        {item.like_count}
      </span>
    </div>
  );
}
