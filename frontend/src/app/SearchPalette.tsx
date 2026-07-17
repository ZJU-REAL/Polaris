import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { Icon, type IconName } from '../components/ui/Icon';
import { PaperStatusPill, StatusPill } from '../components/ui/StatusPill';
import { api, type GlobalSearchHit, type GlobalSearchHitType } from '../lib/api';
import { useProject } from './project';

/** 各实体类型的展示顺序 / 文案 / 图标 / 跳转目标。 */
const TYPE_META: Record<GlobalSearchHitType, { zh: string; en: string; icon: IconName; to: (h: GlobalSearchHit) => string }> = {
  paper: { zh: '论文', en: 'Papers', icon: 'book', to: (h) => `/papers/${h.id}/read` },
  concept: { zh: '概念', en: 'Concepts', icon: 'sparkle', to: (h) => `/wiki?concept=${encodeURIComponent(h.title)}` },
  idea: { zh: '想法', en: 'Ideas', icon: 'bulb', to: (h) => `/ideas/${h.id}` },
  experiment: { zh: '实验', en: 'Experiments', icon: 'flask', to: (h) => `/experiment/${h.id}` },
  voyage: { zh: 'AI 任务', en: 'Tasks', icon: 'compass', to: (h) => `/voyages/${h.id}` },
  manuscript: { zh: '论文稿', en: 'Drafts', icon: 'pen', to: (h) => `/writer/${h.id}` },
};

const TYPE_ORDER = Object.keys(TYPE_META) as GlobalSearchHitType[];

/** 顶栏 ⌘K 全局搜索面板：跨论文/概念/想法/实验/AI 任务/论文稿检索并跳转。 */
export function SearchPalette({ open, onClose }: { open: boolean; onClose: () => void }) {
  const navigate = useNavigate();
  const { currentProjectId } = useProject();
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [q, setQ] = useState('');
  const [debounced, setDebounced] = useState('');
  const [active, setActive] = useState(0);

  // 打开时清空上次输入并聚焦
  useEffect(() => {
    if (!open) return;
    setQ('');
    setDebounced('');
    setActive(0);
    const t = window.setTimeout(() => inputRef.current?.focus(), 0);
    return () => window.clearTimeout(t);
  }, [open]);

  // 输入防抖 200ms
  useEffect(() => {
    const t = window.setTimeout(() => setDebounced(q.trim()), 200);
    return () => window.clearTimeout(t);
  }, [q]);

  const query = useQuery({
    queryKey: ['global-search', currentProjectId, debounced],
    queryFn: () => api.globalSearch(currentProjectId!, debounced),
    enabled: open && !!currentProjectId && debounced.length > 0,
    staleTime: 30_000,
    retry: false,
    placeholderData: keepPreviousData,
  });

  // 按类型分组 + 拍平（键盘上下移动跨组连续）
  const groups = useMemo(() => {
    const hits = query.data?.hits ?? [];
    return TYPE_ORDER.map((type) => ({ type, hits: hits.filter((h) => h.type === type) })).filter(
      (g) => g.hits.length > 0,
    );
  }, [query.data]);
  const flat = useMemo(() => groups.flatMap((g) => g.hits), [groups]);

  useEffect(() => setActive(0), [flat.length, debounced]);

  function go(hit: GlobalSearchHit) {
    onClose();
    navigate(TYPE_META[hit.type].to(hit));
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setActive((i) => Math.min(i + 1, flat.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActive((i) => Math.max(i - 1, 0));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (flat[active]) go(flat[active]);
    } else if (e.key === 'Escape') {
      onClose();
    }
  }

  if (!open) return null;

  const showing = debounced.length > 0;
  let body: React.ReactNode;
  if (!currentProjectId) {
    body = <div className="empty" style={{ padding: 24 }}>请先在顶栏选择一个研究方向</div>;
  } else if (!showing) {
    body = (
      <div className="empty" style={{ padding: 24 }}>
        输入关键词，搜索当前方向下的论文、概念、想法、实验、AI 任务与论文稿
      </div>
    );
  } else if (query.isLoading) {
    body = <div className="empty" style={{ padding: 24 }}>搜索中…</div>;
  } else if (query.isError) {
    body = <div className="empty" style={{ padding: 24 }}>搜索失败（后端不可用）</div>;
  } else if (flat.length === 0) {
    body = <div className="empty" style={{ padding: 24 }}>没有找到与 “{debounced}” 相关的内容</div>;
  } else {
    let offset = 0;
    body = (
      <div className="scroll" style={{ maxHeight: '52vh', overflowY: 'auto', padding: '2px 6px 6px' }}>
        {groups.map((g) => {
          const start = offset;
          offset += g.hits.length;
          const meta = TYPE_META[g.type];
          return (
            <div key={g.type}>
              <div className="mono" style={{ fontSize: 10, color: 'var(--text-4)', letterSpacing: '0.06em', padding: '8px 10px 4px' }}>
                {meta.zh} · {meta.en.toUpperCase()}
              </div>
              {g.hits.map((h, i) => {
                const idx = start + i;
                const isActive = idx === active;
                return (
                  <button
                    key={`${h.type}-${h.id}`}
                    onClick={() => go(h)}
                    onMouseEnter={() => setActive(idx)}
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: 10,
                      width: '100%',
                      padding: '8px 10px',
                      border: 'none',
                      borderRadius: 8,
                      background: isActive ? 'var(--accent-soft)' : 'transparent',
                      cursor: 'pointer',
                      textAlign: 'left',
                      fontFamily: 'var(--sans)',
                    }}
                  >
                    <Icon name={meta.icon} size={15} style={{ color: isActive ? 'var(--accent)' : 'var(--text-3)', flexShrink: 0 }} />
                    <span style={{ flex: 1, minWidth: 0 }}>
                      <span
                        style={{
                          display: 'block',
                          fontSize: 13,
                          fontWeight: 550,
                          color: 'var(--text)',
                          overflow: 'hidden',
                          textOverflow: 'ellipsis',
                          whiteSpace: 'nowrap',
                        }}
                      >
                        {h.title}
                      </span>
                      {h.snippet && (
                        <span
                          style={{
                            display: 'block',
                            fontSize: 11.5,
                            color: 'var(--text-3)',
                            overflow: 'hidden',
                            textOverflow: 'ellipsis',
                            whiteSpace: 'nowrap',
                            marginTop: 1,
                          }}
                        >
                          {h.snippet}
                        </span>
                      )}
                    </span>
                    {h.status &&
                      (h.type === 'paper' ? (
                        <PaperStatusPill status={h.status} sm />
                      ) : (
                        <StatusPill status={h.status} sm />
                      ))}
                  </button>
                );
              })}
            </div>
          );
        })}
      </div>
    );
  }

  return (
    <div
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 90,
        background: 'rgba(15, 30, 55, 0.32)',
        backdropFilter: 'blur(2px)',
        display: 'flex',
        justifyContent: 'center',
        alignItems: 'flex-start',
        paddingTop: '14vh',
      }}
    >
      <div
        className="card"
        style={{ width: 620, maxWidth: 'calc(100vw - 48px)', overflow: 'hidden', boxShadow: 'var(--shadow-pop)', animation: 'fadeUp 0.12s ease' }}
        onKeyDown={onKeyDown}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '12px 16px', borderBottom: '0.5px solid var(--border)' }}>
          <Icon name="search" size={16} style={{ color: 'var(--text-3)', flexShrink: 0 }} />
          <input
            ref={inputRef}
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="搜索论文 / 概念 / 想法 / 实验 / AI 任务 / 论文稿…"
            style={{
              flex: 1,
              border: 'none',
              outline: 'none',
              background: 'transparent',
              fontSize: 14,
              fontFamily: 'var(--sans)',
              color: 'var(--text)',
            }}
          />
          <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)', border: '0.5px solid var(--border-2)', borderRadius: 5, padding: '2px 6px' }}>
            ESC
          </span>
        </div>
        {body}
        <div
          className="mono"
          style={{
            display: 'flex',
            gap: 14,
            padding: '8px 16px',
            borderTop: '0.5px solid var(--border)',
            fontSize: 10.5,
            color: 'var(--text-4)',
          }}
        >
          <span>↑↓ 选择</span>
          <span>↵ 打开</span>
          <span style={{ marginLeft: 'auto' }}>仅搜索当前研究方向</span>
        </div>
      </div>
    </div>
  );
}
