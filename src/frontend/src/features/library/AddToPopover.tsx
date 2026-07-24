import { useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import { createPortal } from 'react-dom';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Icon, type IconName } from '../../components/ui/Icon';
import { toast } from '../../components/ui/Toast';
import { useProject } from '../../app/project';
import { api } from '../../lib/api';
import { tr } from '../../lib/i18n';

/* ============================================================
   把一篇论文加入到「个人文献库 + 一个或多个课题」的浮层。
   父组件只放一个 <AddToButton paperId=… />，浮层开合自管理。
   - 树状分段多选：个人库一段 + 课题一段（缩进/带竖向导轨），逐行自绘勾选框。
   - 无批量端点：确认时前端逐个 addToShelf / 一次 saveToLibrary，Promise.allSettled。
   - 幂等，不预查「已加入」；重复加无害，加完给成功/失败计数 toast。
   ============================================================ */

const POP_W = 288;
const MAX_LIST_H = 208; // 课题列表超出后内部滚动
const FILTER_THRESHOLD = 6; // 课题多于此数才显示搜索框
const POP_Z = 50; // 高于内容(20/30)，低于抽屉(40)/toast(60)

/** 圆角小勾选框（体系内样式，复用自实验/写作页）。 */
function CheckBox({ checked }: { checked: boolean }) {
  return (
    <span
      aria-hidden
      style={{
        width: 17,
        height: 17,
        flexShrink: 0,
        borderRadius: 5,
        border: `1.5px solid ${checked ? 'var(--accent)' : 'var(--border-2)'}`,
        background: checked ? 'var(--accent)' : 'transparent',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        color: '#fff',
        transition: 'all .12s',
      }}
    >
      {checked && <Icon name="check" size={11} sw={2.6} />}
    </span>
  );
}

/** 一条可勾选行（整行可点切换）。 */
function CheckRow({
  icon,
  label,
  sub,
  checked,
  indent,
  onToggle,
}: {
  icon: IconName;
  label: string;
  sub?: string;
  checked: boolean;
  indent?: boolean;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      role="checkbox"
      aria-checked={checked}
      onClick={onToggle}
      style={{
        width: '100%',
        display: 'flex',
        alignItems: 'center',
        gap: 9,
        padding: indent ? '7px 10px 7px 14px' : '8px 10px',
        border: 0,
        background: checked ? 'var(--accent-soft)' : 'transparent',
        borderRadius: 7,
        cursor: 'pointer',
        textAlign: 'left',
        transition: 'background .1s',
      }}
      onMouseEnter={(e) => {
        if (!checked) e.currentTarget.style.background = 'var(--surface-2)';
      }}
      onMouseLeave={(e) => {
        if (!checked) e.currentTarget.style.background = 'transparent';
      }}
    >
      <CheckBox checked={checked} />
      <Icon name={icon} size={14} style={{ color: checked ? 'var(--accent)' : 'var(--text-3)', flexShrink: 0 }} />
      <span style={{ flex: 1, minWidth: 0 }}>
        <span
          style={{
            display: 'block',
            fontSize: 12.5,
            fontWeight: 550,
            color: checked ? 'var(--accent-text)' : 'var(--text)',
            whiteSpace: 'nowrap',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
          }}
        >
          {label}
        </span>
        {sub && (
          <span
            style={{
              display: 'block',
              fontSize: 10.5,
              color: 'var(--text-4)',
              whiteSpace: 'nowrap',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
            }}
          >
            {sub}
          </span>
        )}
      </span>
    </button>
  );
}

type Anchor = { left: number; top?: number; bottom?: number };

/** 计算浮层相对触发按钮的定位（右对齐；下方放不下则上翻）。 */
function computeAnchor(btn: HTMLElement): Anchor {
  const r = btn.getBoundingClientRect();
  const left = Math.max(8, Math.min(r.right - POP_W, window.innerWidth - POP_W - 8));
  const spaceBelow = window.innerHeight - r.bottom;
  if (spaceBelow < 320 && r.top > spaceBelow) {
    return { left, bottom: window.innerHeight - r.top + 6 };
  }
  return { left, top: r.bottom + 6 };
}

function Popover({
  paperId,
  btnRef,
  onClose,
}: {
  paperId: string;
  btnRef: React.RefObject<HTMLButtonElement | null>;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const { projects } = useProject();
  const popRef = useRef<HTMLDivElement>(null);

  const [lib, setLib] = useState(false);
  const [picked, setPicked] = useState<Set<string>>(() => new Set());
  const [filter, setFilter] = useState('');
  const [anchor, setAnchor] = useState<Anchor | null>(null);

  // 定位：开时算一次，随滚动/缩放跟随。
  useLayoutEffect(() => {
    const update = () => btnRef.current && setAnchor(computeAnchor(btnRef.current));
    update();
    window.addEventListener('scroll', update, true);
    window.addEventListener('resize', update);
    return () => {
      window.removeEventListener('scroll', update, true);
      window.removeEventListener('resize', update);
    };
  }, [btnRef]);

  // 点外部 / Esc 收起（浮层在 portal 里，需同时豁免按钮与浮层本身）。
  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (btnRef.current?.contains(t) || popRef.current?.contains(t)) return;
      onClose();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [btnRef, onClose]);

  const filtered = useMemo(() => {
    const q = filter.trim().toLowerCase();
    if (!q) return projects;
    return projects.filter((p) => p.name.toLowerCase().includes(q));
  }, [projects, filter]);

  const count = (lib ? 1 : 0) + picked.size;

  const mutation = useMutation({
    mutationFn: async () => {
      const projectIds = [...picked];
      const tasks: Promise<unknown>[] = [];
      if (lib) tasks.push(api.saveToLibrary({ paper_id: paperId }));
      for (const pid of projectIds) tasks.push(api.addToShelf(pid, { paper_id: paperId }));
      const results = await Promise.allSettled(tasks);
      const ok = results.filter((r) => r.status === 'fulfilled').length;
      return { ok, fail: results.length - ok, projectIds };
    },
    onSuccess: ({ ok, fail, projectIds }) => {
      // 加入课题会同步收藏进个人库 → 库相关缓存一并失效。
      void queryClient.invalidateQueries({ queryKey: ['library'] });
      void queryClient.invalidateQueries({ queryKey: ['library-state', paperId] });
      for (const pid of projectIds) {
        void queryClient.invalidateQueries({ queryKey: ['shelf', pid] });
        void queryClient.invalidateQueries({ queryKey: ['shelf-ids', pid] });
      }
      if (ok === 0) {
        toast(tr('全部加入失败', 'All additions failed'), 'error');
      } else if (fail === 0) {
        toast(tr(`已加入 ${ok} 处`, `Added to ${ok} place(s)`), 'ok');
      } else {
        toast(tr(`已加入 ${ok} 处，${fail} 处失败`, `Added to ${ok}, ${fail} failed`), 'info');
      }
      onClose();
    },
    onError: (e) => toast(`${tr('加入失败：', 'Failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const toggleProj = (id: string) =>
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  if (!anchor) return null;

  const wrapStyle: CSSProperties = {
    position: 'fixed',
    left: anchor.left,
    ...(anchor.top !== undefined ? { top: anchor.top } : { bottom: anchor.bottom }),
    width: POP_W,
    // 窄屏（POP_W + 16 放不下）时收进视口，否则右侧溢出
    maxWidth: 'calc(100vw - 16px)',
    zIndex: POP_Z,
    background: 'var(--surface)',
    border: '0.5px solid var(--border-2)',
    borderRadius: 'var(--radius)',
    boxShadow: 'var(--shadow-pop)',
    display: 'flex',
    flexDirection: 'column',
    overflow: 'hidden',
  };

  return createPortal(
    <div ref={popRef} style={wrapStyle} onClick={(e) => e.stopPropagation()}>
      {/* 标题 */}
      <div
        style={{
          padding: '11px 14px 9px',
          fontSize: 12.5,
          fontWeight: 700,
          color: 'var(--text-2)',
          borderBottom: '0.5px solid var(--border)',
        }}
      >
        {tr('加入到…', 'Add to…')}
      </div>

      <div style={{ padding: '8px 8px 4px', overflowY: 'auto', maxHeight: MAX_LIST_H + 120 }}>
        {/* 第一段：个人文献库 */}
        <CheckRow
          icon="bookmark"
          label={tr('我的个人文献库', 'My library')}
          checked={lib}
          onToggle={() => setLib((v) => !v)}
        />

        {/* 第二段：课题（缩进 + 竖向导轨体现层级） */}
        <div
          style={{
            marginTop: 6,
            padding: '3px 4px 2px 10px',
            fontSize: 10.5,
            fontWeight: 700,
            letterSpacing: '.04em',
            color: 'var(--text-3)',
          }}
        >
          {tr('课题', 'Topics')}
        </div>
        {projects.length > FILTER_THRESHOLD && (
          <div style={{ padding: '2px 6px 6px 12px' }}>
            <input
              className="input"
              style={{ height: 30, fontSize: 12, width: '100%' }}
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder={tr('搜索课题…', 'Filter topics…')}
              type="search"
            />
          </div>
        )}
        <div style={{ paddingLeft: 6, borderLeft: '1.5px solid var(--border)', marginLeft: 12 }}>
          {projects.length === 0 ? (
            <div style={{ padding: '10px 8px', fontSize: 11.5, color: 'var(--text-4)' }}>
              {tr('还没有课题', 'No topics yet')}
            </div>
          ) : filtered.length === 0 ? (
            <div style={{ padding: '10px 8px', fontSize: 11.5, color: 'var(--text-4)' }}>
              {tr('没有匹配的课题', 'No matching topics')}
            </div>
          ) : (
            filtered.map((p) => (
              <CheckRow
                key={p.id}
                icon="compass"
                label={p.name}
                sub={p.statement ?? undefined}
                checked={picked.has(p.id)}
                indent
                onToggle={() => toggleProj(p.id)}
              />
            ))
          )}
        </div>
      </div>

      {/* 耦合提示 */}
      <div
        style={{
          display: 'flex',
          gap: 6,
          padding: '8px 14px',
          fontSize: 10.5,
          lineHeight: 1.5,
          color: 'var(--text-4)',
          borderTop: '0.5px solid var(--border)',
        }}
      >
        <Icon name="bulb" size={12} style={{ flexShrink: 0, marginTop: 1 }} />
        <span>{tr('加入课题会一并收藏进你的个人文献库。', 'Adding to a topic also saves it to your library.')}</span>
      </div>

      {/* 操作 */}
      <div
        style={{
          display: 'flex',
          gap: 8,
          padding: '10px 12px',
          borderTop: '0.5px solid var(--border)',
        }}
      >
        <button className="btn btn-ghost sm" style={{ flex: 1 }} onClick={onClose} disabled={mutation.isPending}>
          {tr('取消', 'Cancel')}
        </button>
        <button
          className="btn btn-primary sm"
          style={{ flex: 1.4 }}
          disabled={count === 0 || mutation.isPending}
          onClick={() => mutation.mutate()}
        >
          {mutation.isPending
            ? tr('加入中…', 'Adding…')
            : count > 0
              ? tr(`确认加入 · ${count}`, `Add · ${count}`)
              : tr('确认加入', 'Add')}
        </button>
      </div>
    </div>,
    document.body,
  );
}

/** 论文行里的「+」按钮：点开加入浮层（自管理开合，阻断行点击）。 */
export function AddToButton({ paperId }: { paperId: string }) {
  const [open, setOpen] = useState(false);
  const btnRef = useRef<HTMLButtonElement>(null);

  return (
    <>
      <button
        ref={btnRef}
        type="button"
        className="icon-btn"
        title={tr('加入个人库 / 课题', 'Add to library / topics')}
        aria-label={tr('加入到…', 'Add to…')}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={(e) => {
          e.stopPropagation();
          setOpen((v) => !v);
        }}
        style={{
          width: 24,
          height: 24,
          borderRadius: 7,
          flexShrink: 0,
          color: open ? 'var(--accent)' : 'var(--text-3)',
          borderColor: open ? 'var(--accent)' : undefined,
          background: open ? 'var(--accent-soft)' : undefined,
        }}
      >
        <Icon name="plus" size={15} />
      </button>
      {open && <Popover paperId={paperId} btnRef={btnRef} onClose={() => setOpen(false)} />}
    </>
  );
}
