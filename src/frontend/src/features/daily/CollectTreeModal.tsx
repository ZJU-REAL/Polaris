import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Modal } from '../../components/ui/Modal';
import { Icon } from '../../components/ui/Icon';
import { toast } from '../../components/ui/Toast';
import { api, type DailyCollectResponse } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { useProject } from '../../app/project';
import { useLibraries } from '../libraries/hooks';
import { SearchInput } from '../wiki/shared';

/* ============================================================
   「收进文献库」两级树弹窗（单篇入口）：
   实验室文献库（可管理的方向库）/ 课题相关研究（我的课题）/ 个人库。
   已收录目标预勾选 + 禁用；提交只发新增目标。
   ============================================================ */

export interface CollectPaperRef {
  paper_id: string;
  entry_id: string;
  title: string;
}

type CheckState = 'none' | 'some' | 'all';

/** 三态复选框（与 LibraryPicker 勾选框同款视觉；半选用减号）。 */
function CheckBox({ state, disabled }: { state: CheckState; disabled?: boolean }) {
  const on = state !== 'none';
  return (
    <span
      style={{
        width: 18,
        height: 18,
        borderRadius: 5,
        flexShrink: 0,
        border: on ? 'none' : '1.5px solid var(--border-strong)',
        background: on ? (disabled ? 'var(--border-strong)' : 'var(--accent)') : 'transparent',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        color: '#fff',
      }}
    >
      {state === 'all' && <Icon name="check" size={12} />}
      {state === 'some' && <Icon name="minus" size={12} />}
    </span>
  );
}

/** 父节点行：展开三角 + 三态复选框 + 名称。 */
function ParentRow({
  label,
  state,
  expanded,
  onToggleExpand,
  onToggleCheck,
  disabled,
  tail,
}: {
  label: string;
  state: CheckState;
  expanded?: boolean;
  onToggleExpand?: () => void;
  onToggleCheck: () => void;
  disabled?: boolean;
  tail?: string;
}) {
  return (
    <div className="row gap8" style={{ padding: '7px 6px', borderRadius: 8 }}>
      {onToggleExpand ? (
        <button
          className="icon-btn"
          style={{ width: 20, height: 20 }}
          onClick={onToggleExpand}
          aria-label={expanded ? tr('收起', 'Collapse') : tr('展开', 'Expand')}
        >
          <Icon name={expanded ? 'chevDown' : 'chevron'} size={12} style={{ color: 'var(--text-3)' }} />
        </button>
      ) : (
        <span style={{ width: 20, flexShrink: 0 }} />
      )}
      <div
        className="row gap8"
        role="button"
        tabIndex={0}
        onClick={disabled ? undefined : onToggleCheck}
        onKeyDown={(e) => {
          if (!disabled && (e.key === 'Enter' || e.key === ' ')) {
            e.preventDefault();
            onToggleCheck();
          }
        }}
        style={{ flex: 1, minWidth: 0, cursor: disabled ? 'default' : 'pointer', userSelect: 'none' }}
      >
        <CheckBox state={state} disabled={disabled} />
        <span style={{ fontSize: 12.5, fontWeight: 680, color: 'var(--text)' }}>{label}</span>
        {tail && (
          <span style={{ fontSize: 11, color: 'var(--text-4)', marginLeft: 'auto', flexShrink: 0 }}>{tail}</span>
        )}
      </div>
    </div>
  );
}

/** 叶子行：复选框 + 名称（+ 已在库中尾注）。 */
function LeafRow({
  name,
  checked,
  disabled,
  onToggle,
}: {
  name: string;
  checked: boolean;
  disabled: boolean;
  onToggle: () => void;
}) {
  return (
    <div
      className="row gap8"
      role="button"
      tabIndex={disabled ? -1 : 0}
      onClick={disabled ? undefined : onToggle}
      onKeyDown={(e) => {
        if (!disabled && (e.key === 'Enter' || e.key === ' ')) {
          e.preventDefault();
          onToggle();
        }
      }}
      style={{
        padding: '6px 6px 6px 34px',
        borderRadius: 8,
        cursor: disabled ? 'default' : 'pointer',
        userSelect: 'none',
        opacity: disabled ? 0.62 : 1,
        background: !disabled && checked ? 'var(--accent-soft)' : 'transparent',
      }}
      onMouseEnter={(e) => {
        if (!disabled && !checked) e.currentTarget.style.background = 'var(--surface-2)';
      }}
      onMouseLeave={(e) => {
        if (!disabled && !checked) e.currentTarget.style.background = 'transparent';
      }}
    >
      <CheckBox state={checked || disabled ? 'all' : 'none'} disabled={disabled} />
      <span
        style={{
          fontSize: 12.5,
          minWidth: 0,
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
          color: 'var(--text)',
        }}
        title={name}
      >
        {name}
      </span>
      {disabled && (
        <span style={{ fontSize: 11, color: 'var(--text-4)', marginLeft: 'auto', flexShrink: 0 }}>
          {tr('已在库中', 'Already added')}
        </span>
      )}
    </div>
  );
}

/** 组三态：disabled 的叶子视为已勾选但不计入 all/none 的可操作判断。 */
function groupState(leafIds: string[], selected: Set<string>, disabledIds: Set<string>): CheckState {
  if (leafIds.length === 0) return 'none';
  const selectable = leafIds.filter((id) => !disabledIds.has(id));
  const anyChecked = leafIds.some((id) => selected.has(id) || disabledIds.has(id));
  if (selectable.length > 0 && selectable.every((id) => selected.has(id))) return 'all';
  if (selectable.length === 0 && anyChecked) return 'all';
  return anyChecked ? 'some' : 'none';
}

export function CollectTreeModal({
  paper,
  open,
  onClose,
  onCollected,
}: {
  paper: CollectPaperRef;
  open: boolean;
  onClose: () => void;
  /** 收录触发了后台补全时回调（taskId + 标题），父级据此弹分阶段进度框（同手动添加）。 */
  onCollected?: (task: { taskId: string; title: string }) => void;
}) {
  const queryClient = useQueryClient();
  const { projects } = useProject();

  // 只展示可管理的方向库（无写权限的目标后端也会兜底 forbidden）
  const libsQuery = useLibraries({ status: 'active' }, open);
  const libraries = useMemo(
    () => (libsQuery.data ?? []).filter((l) => l.can_manage),
    [libsQuery.data],
  );

  const collectionsQuery = useQuery({
    queryKey: ['daily-collections', paper.entry_id],
    queryFn: () => api.getDailyCollections(paper.entry_id),
    enabled: open,
    retry: false,
  });
  const collections = collectionsQuery.data;

  // —— 勾选态（sel 集合里只放新增目标，禁用项不入集合） ——
  const [selLibs, setSelLibs] = useState<Set<string>>(new Set());
  const [selTopics, setSelTopics] = useState<Set<string>>(new Set());
  const [personal, setPersonal] = useState(false);
  const [expandLibs, setExpandLibs] = useState(true);
  const [expandTopics, setExpandTopics] = useState(true);
  const [q, setQ] = useState('');

  // 重开 / 换论文时重置
  useEffect(() => {
    if (!open) return;
    setSelLibs(new Set());
    setSelTopics(new Set());
    setPersonal(false);
    setExpandLibs(true);
    setExpandTopics(true);
    setQ('');
  }, [open, paper.entry_id]);

  const disabledLibs = useMemo(
    () => new Set(collections?.direction_library_ids ?? []),
    [collections],
  );
  const disabledTopics = useMemo(() => new Set(collections?.topic_ids ?? []), [collections]);
  const personalDisabled = collections?.in_personal ?? false;

  // —— 搜索过滤叶子（命中时父节点强制展开） ——
  const needle = q.trim().toLowerCase();
  const shownLibs = needle
    ? libraries.filter((l) => l.name.toLowerCase().includes(needle))
    : libraries;
  const shownTopics = needle
    ? projects.filter((p) => p.name.toLowerCase().includes(needle))
    : projects;
  const personalShown = !needle || tr('个人库', 'My library').toLowerCase().includes(needle);
  const libsOpen = expandLibs || !!needle;
  const topicsOpen = expandTopics || !!needle;

  const toggleIn = (set: Set<string>, id: string): Set<string> => {
    const next = new Set(set);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    return next;
  };
  /** 父节点点击：可选叶子全选 ↔ 全不选（作用于当前过滤后的可见叶子）。 */
  const toggleGroup = (
    leafIds: string[],
    disabledIds: Set<string>,
    set: Set<string>,
    apply: (s: Set<string>) => void,
  ) => {
    const selectable = leafIds.filter((id) => !disabledIds.has(id));
    if (selectable.length === 0) return;
    const allOn = selectable.every((id) => set.has(id));
    const next = new Set(set);
    for (const id of selectable) {
      if (allOn) next.delete(id);
      else next.add(id);
    }
    apply(next);
  };

  const selectedCount = selLibs.size + selTopics.size + (personal ? 1 : 0);

  const collectMutation = useMutation({
    mutationFn: () =>
      api.collectDaily({
        paper_ids: [paper.paper_id],
        direction_library_ids: [...selLibs],
        topic_ids: [...selTopics],
        personal,
      }),
    onSuccess: (resp: DailyCollectResponse) => {
      const ok = resp.results.filter((r) => !r.forbidden && r.added > 0).length;
      const skipped = resp.results.reduce((s, r) => s + r.skipped_existing, 0);
      const forbidden = resp.results.some((r) => r.forbidden);
      toast(
        tr(
          `已收进 ${ok} 个位置${skipped > 0 ? `（${skipped} 篇本来就在）` : ''}`,
          `Added to ${ok} places${skipped > 0 ? ` (${skipped} already there)` : ''}`,
        ),
        'ok',
      );
      if (forbidden) {
        toast(tr('部分目标无权限，已跳过', 'Some targets were skipped: no permission'), 'error');
      }
      void queryClient.invalidateQueries({ queryKey: ['daily-collections', paper.entry_id] });
      onClose();
      // 收录若启动了后台补全（同手动添加），交给父级弹分阶段进度框
      const task = resp.tasks?.find((t) => t.paper_id === paper.paper_id);
      if (task) onCollected?.({ taskId: task.task_id, title: paper.title });
    },
    onError: (e) =>
      toast(
        `${tr('收录失败：', 'Failed to add: ')}${e instanceof Error ? e.message : String(e)}`,
        'error',
      ),
  });

  const libState = groupState(
    shownLibs.map((l) => l.id),
    selLibs,
    disabledLibs,
  );
  const topicState = groupState(
    shownTopics.map((p) => p.id),
    selTopics,
    disabledTopics,
  );

  const loading = libsQuery.isLoading || collectionsQuery.isLoading;

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={
        <>
          <Icon name="book" size={16} style={{ color: 'var(--accent)' }} />
          {tr('收进文献库', 'Add to libraries')}
        </>
      }
      sub={paper.title}
      width={520}
      footer={
        <>
          <span style={{ marginRight: 'auto', fontSize: 11.5, color: 'var(--text-3)' }}>
            {tr(`已选 ${selectedCount} 个位置`, `${selectedCount} destinations selected`)}
          </span>
          <button className="btn btn-ghost sm" onClick={onClose} disabled={collectMutation.isPending}>
            {tr('取消', 'Cancel')}
          </button>
          <button
            className="btn btn-primary sm"
            disabled={selectedCount === 0 || collectMutation.isPending}
            onClick={() => collectMutation.mutate()}
          >
            {collectMutation.isPending ? tr('收录中…', 'Adding…') : tr('确认收录', 'Add')}
          </button>
        </>
      }
    >
      <div className="row gap8" style={{ marginBottom: 10 }}>
        <SearchInput value={q} onChange={setQ} placeholder={tr('按名称过滤…', 'Filter by name…')} />
      </div>

      {loading ? (
        <div className="empty" style={{ padding: 24 }}>
          {tr('加载中…', 'Loading…')}
        </div>
      ) : (
        <div className="col" style={{ gap: 2 }}>
          {/* —— 实验室文献库 —— */}
          <ParentRow
            label={tr('实验室文献库', 'Shared libraries')}
            state={libState}
            expanded={libsOpen}
            onToggleExpand={() => setExpandLibs((o) => !o)}
            onToggleCheck={() =>
              toggleGroup(shownLibs.map((l) => l.id), disabledLibs, selLibs, setSelLibs)
            }
            tail={tr(`${shownLibs.length} 个库`, `${shownLibs.length}`)}
          />
          {libsOpen &&
            (shownLibs.length === 0 ? (
              <div style={{ padding: '2px 6px 6px 34px', fontSize: 11.5, color: 'var(--text-4)' }}>
                {needle
                  ? tr('没有匹配的库', 'No matching library')
                  : tr('没有你可以管理的方向库', 'No libraries you can manage')}
              </div>
            ) : (
              shownLibs.map((l) => (
                <LeafRow
                  key={l.id}
                  name={l.name}
                  checked={selLibs.has(l.id)}
                  disabled={disabledLibs.has(l.id)}
                  onToggle={() => setSelLibs((s) => toggleIn(s, l.id))}
                />
              ))
            ))}

          {/* —— 课题相关研究 —— */}
          <ParentRow
            label={tr('课题相关研究', 'Topic related work')}
            state={topicState}
            expanded={topicsOpen}
            onToggleExpand={() => setExpandTopics((o) => !o)}
            onToggleCheck={() =>
              toggleGroup(shownTopics.map((p) => p.id), disabledTopics, selTopics, setSelTopics)
            }
            tail={tr(`${shownTopics.length} 个课题`, `${shownTopics.length}`)}
          />
          {topicsOpen &&
            (shownTopics.length === 0 ? (
              <div style={{ padding: '2px 6px 6px 34px', fontSize: 11.5, color: 'var(--text-4)' }}>
                {needle ? tr('没有匹配的课题', 'No matching topic') : tr('还没有课题', 'No topics yet')}
              </div>
            ) : (
              shownTopics.map((p) => (
                <LeafRow
                  key={p.id}
                  name={p.name}
                  checked={selTopics.has(p.id)}
                  disabled={disabledTopics.has(p.id)}
                  onToggle={() => setSelTopics((s) => toggleIn(s, p.id))}
                />
              ))
            ))}

          {/* —— 个人库（父即叶） —— */}
          {personalShown && (
            <ParentRow
              label={tr('个人库', 'My library')}
              state={personal || personalDisabled ? 'all' : 'none'}
              onToggleCheck={() => setPersonal((v) => !v)}
              disabled={personalDisabled}
              tail={personalDisabled ? tr('已在库中', 'Already added') : undefined}
            />
          )}
        </div>
      )}
    </Modal>
  );
}
