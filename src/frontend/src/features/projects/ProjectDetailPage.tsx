import { useState, type ReactNode } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon, type IconName } from '../../components/ui/Icon';
import { StatusPill } from '../../components/ui/StatusPill';
import { Modal } from '../../components/ui/Modal';
import { ConfirmModal } from '../../components/ui/ConfirmModal';
import { ErrorState, LoadingState } from '../../components/ui/EmptyState';
import { toast } from '../../components/ui/Toast';
import { useProject } from '../../app/project';
import { fmtTime } from '../../lib/format';
import { api, ApiError, type ProjectRead } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { errorText } from '../../lib/errors';
import { useLibraries } from '../libraries/hooks';
import { LibraryPicker } from '../libraries/LibraryPicker';
import { InterdisciplinaryScopePanel } from './InterdisciplinaryScopePanel';

/* ============================================================
   /projects/:id — 课题设置：只留真正的课题属性
   （名称 / 课题定义 statement / 关联文献库 / 删除课题）。
   收录配置（rubric/锚点/关键词/arXiv 分类/节奏）已迁到文献库收录设置（P8）。
   ============================================================ */

function SectionCard({ icon, zh, en, action, children }: {
  icon: IconName;
  zh: string;
  en: string;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="card card-pad">
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 12 }}>
        <span className="section-h">
          <Icon name={icon} size={15} style={{ color: 'var(--accent)' }} />
          {tr(zh, en)}
        </span>
        {action}
      </div>
      {children}
    </div>
  );
}

function EditButton({ editing, onClick }: { editing: boolean; onClick: () => void }) {
  return (
    <button className="btn btn-soft sm" onClick={onClick}>
      <Icon name={editing ? 'x' : 'pen'} size={12} />
      {editing ? tr('取消', 'Cancel') : tr('编辑', 'Edit')}
    </button>
  );
}

/** 可编辑文本段（view ↔ textarea）。 */
function EditableText({ value, placeholder, onSave, saving }: {
  value: string;
  placeholder: string;
  onSave: (v: string) => void;
  saving: boolean;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  if (!editing) {
    return (
      <div className="row gap10" style={{ alignItems: 'flex-start' }}>
        <div style={{ flex: 1, fontSize: 13, lineHeight: 1.6, color: value ? 'var(--text)' : 'var(--text-4)' }}>
          {value || placeholder}
        </div>
        <EditButton editing={false} onClick={() => { setDraft(value); setEditing(true); }} />
      </div>
    );
  }
  return (
    <div className="col gap8">
      <textarea className="textarea" rows={3} value={draft} onChange={(e) => setDraft(e.target.value)} />
      <div className="row gap8">
        <button className="btn btn-primary sm" disabled={saving}
          onClick={() => { onSave(draft.trim()); setEditing(false); }}>
          {tr('保存', 'Save')}
        </button>
        <button className="btn btn-ghost sm" onClick={() => setEditing(false)}>{tr('取消', 'Cancel')}</button>
      </div>
    </div>
  );
}

/** 课题设置主体（接课题 id）：既作独立页 `/projects/:id` 的内容，
    也被工作台课题设置标签以 `embedded` 内嵌（内嵌时不渲染大标题/eyebrow，避免与工作台页头重复）。 */
export function ProjectSettings({ id, embedded = false }: { id: string; embedded?: boolean }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { currentProjectId, setCurrentProjectId } = useProject();

  // —— 删除方向（owner / admin） ——
  const [deleteOpen, setDeleteOpen] = useState(false);
  const deleteMutation = useMutation({
    mutationFn: () => api.deleteProject(id),
    onSuccess: () => {
      toast(tr('课题已删除', 'Topic deleted'), 'ok');
      setDeleteOpen(false);
      if (currentProjectId === id) setCurrentProjectId(null);
      void queryClient.invalidateQueries({ queryKey: ['projects'] });
      navigate('/');
    },
    onError: (err) => {
      const forbidden = err instanceof ApiError && err.status === 403;
      toast(forbidden ? tr('没有权限删除这个课题', 'You can’t delete this topic') : `${tr('无法删除课题：', 'Couldn’t delete the topic: ')}${errorText(err)}`, 'error');
    },
  });

  const { data: project, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['project', id],
    queryFn: () => api.getProject(id),
    retry: false,
    enabled: !!id,
  });

  const patchMutation = useMutation({
    mutationFn: (input: { name?: string; statement?: string }) => api.patchProject(id, input),
    onSuccess: (updated: ProjectRead) => {
      queryClient.setQueryData(['project', id], updated);
      void queryClient.invalidateQueries({ queryKey: ['projects'] });
      toast(tr('已保存', 'Saved'), 'ok');
    },
    onError: (err) => toast(`${tr('无法保存：', 'Couldn’t save: ')}${errorText(err)}`, 'error'),
  });

  // —— 关联文献库 ——
  const { data: sourceLibraries } = useQuery({
    queryKey: ['sourceLibraries', id],
    queryFn: () => api.getSourceLibraries(id),
    retry: false,
    enabled: !!id,
  });
  const librariesQuery = useLibraries();
  const allLibraries = librariesQuery.data ?? [];
  const protectedLibraryIds = new Set(
    (sourceLibraries ?? [])
      .filter((library) => library.library_kind === 'interdisciplinary')
      .map((library) => library.id),
  );
  const [linkOpen, setLinkOpen] = useState(false);
  const [linkDraft, setLinkDraft] = useState<Set<string>>(new Set());
  function openLinkEditor() {
    if (!sourceLibraries) return;
    setLinkDraft(new Set((sourceLibraries ?? []).map((l) => l.id)));
    setLinkOpen(true);
  }
  function toggleLinkDraft(libId: string) {
    if (protectedLibraryIds.has(libId)) return;
    setLinkDraft((prev) => {
      const next = new Set(prev);
      if (next.has(libId)) next.delete(libId);
      else next.add(libId);
      return next;
    });
  }
  const setSourceLibsMutation = useMutation({
    mutationFn: (ids: string[]) => api.setSourceLibraries(
      id,
      [...new Set([...ids, ...protectedLibraryIds])],
    ),
    onSuccess: (libs) => {
      queryClient.setQueryData(['sourceLibraries', id], libs);
      // 课题语料 = 关联库并集：相关缓存全部失效
      void queryClient.invalidateQueries({ queryKey: ['papers', id] });
      void queryClient.invalidateQueries({ queryKey: ['project-graph', id] });
      void queryClient.invalidateQueries({ queryKey: ['concepts', id] });
      void queryClient.invalidateQueries({ queryKey: ['shelf', id] });
      setLinkOpen(false);
      toast(tr('关联文献库已更新', 'Linked libraries updated'), 'ok');
    },
    onError: (err) => toast(`${tr('无法更新关联文献库：', 'Couldn’t update linked libraries: ')}${errorText(err)}`, 'error'),
  });

  // —— 名称编辑 ——
  const [editingName, setEditingName] = useState(false);
  const [nameDraft, setNameDraft] = useState('');

  if (isLoading) {
    return <LoadingState />;
  }
  if (isError || !project) {
    const notFound = error instanceof ApiError && error.status === 404;
    return (
      <ErrorState
        title={notFound ? tr('找不到这个课题，可能已被删除', 'This topic no longer exists.') : tr('无法加载课题设置，请确认本机引擎正在运行', 'Couldn’t load topic settings. Check that the local engine is running.')}
        detail={notFound ? null : error instanceof Error ? error.message : null}
        onRetry={notFound ? undefined : () => void refetch()}
        action={<button className="btn btn-ghost sm" onClick={() => navigate('/')}>{tr('返回工作台', 'Back to workbench')}</button>}
      />
    );
  }

  const saving = patchMutation.isPending;

  return (
    <>
      {/* 页头：嵌入工作台标签时降级为紧凑标题（不重复大标题 / eyebrow） */}
      <div className="row" style={{ alignItems: 'flex-start', marginBottom: 24 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          {!embedded && <div className="h-eyebrow">{tr('课题设置', 'Topic Settings')}</div>}
          {editingName ? (
            <div className="row gap8" style={{ marginTop: 8 }}>
              <input className="input" style={{ fontSize: 17, fontWeight: 600, width: 380, maxWidth: '100%' }} value={nameDraft}
                onChange={(e) => setNameDraft(e.target.value)} />
              <button className="btn btn-primary sm" disabled={saving}
                onClick={() => {
                  if (nameDraft.trim()) patchMutation.mutate({ name: nameDraft.trim() });
                  setEditingName(false);
                }}>
                {tr('保存', 'Save')}
              </button>
              <button className="btn btn-ghost sm" onClick={() => setEditingName(false)}>{tr('取消', 'Cancel')}</button>
            </div>
          ) : (
            <div className="row gap10" style={{ marginTop: embedded ? 0 : 6 }}>
              <h1 className="h-title" style={{ margin: 0, fontSize: embedded ? 18 : undefined }}>{project.name}</h1>
              <button className="icon-btn" style={{ width: 26, height: 26, border: 'none', background: 'transparent' }}
                title={tr('编辑名称', 'Edit name')} onClick={() => { setNameDraft(project.name); setEditingName(true); }}>
                <Icon name="pen" size={14} />
              </button>
            </div>
          )}
          <div className="row gap8" style={{ marginTop: 10 }}>
            {project.status && <StatusPill status={project.status} sm />}
            <span className="muted" style={{ fontSize: 'var(--fs-sm)' }}>{tr(`创建于 ${fmtTime(project.created_at)}`, `Created ${fmtTime(project.created_at)}`)}</span>
          </div>
        </div>
        <div className="row gap8">
          <button
            className="btn btn-ghost"
            style={{ color: 'var(--danger-tx)' }}
            onClick={() => setDeleteOpen(true)}
          >
            <Icon name="x" size={13} />
            {tr('删除课题', 'Delete topic')}
          </button>
        </div>
      </div>

      {/* —— 删除确认 —— */}
      <ConfirmModal
        open={deleteOpen}
        onClose={() => setDeleteOpen(false)}
        title={tr(`删除课题「${project.name}」？`, `Delete topic “${project.name}”?`)}
        message={tr(
          '它的想法、实验和任务记录会被删除，无法恢复。文献库和论文会保留。',
          'Its ideas, experiments and task history are deleted for good. Libraries and papers are kept.',
        )}
        confirmText={tr('删除', 'Delete')}
        danger
        busy={deleteMutation.isPending}
        onConfirm={() => deleteMutation.mutate()}
      />

      {/* —— 管理关联文献库 —— */}
      <Modal
        open={linkOpen}
        onClose={() => setLinkOpen(false)}
        title={tr('关联文献库', 'Linked libraries')}
        width={600}
        footer={
          <>
            <button className="btn btn-ghost sm" onClick={() => setLinkOpen(false)}>{tr('取消', 'Cancel')}</button>
            <button className="btn btn-primary sm" disabled={setSourceLibsMutation.isPending}
              onClick={() => setSourceLibsMutation.mutate([...linkDraft])}>
              {setSourceLibsMutation.isPending ? tr('保存中…', 'Saving…') : tr('保存', 'Save')}
            </button>
          </>
        }
      >
        {librariesQuery.isLoading ? (
          <LoadingState compact />
        ) : allLibraries.length === 0 ? (
          <div className="empty" style={{ padding: 30 }}>{tr('还没有文献库', 'No libraries yet')}</div>
        ) : (
          <div style={{ maxHeight: '55vh', overflowY: 'auto', marginTop: 4 }}>
            <LibraryPicker
              libraries={allLibraries}
              selectedIds={linkDraft}
              onToggle={toggleLinkDraft}
              disabled={setSourceLibsMutation.isPending}
              disabledIds={protectedLibraryIds}
            />
          </div>
        )}
      </Modal>

      <div className="col gap16">
        {project.research_mode === 'interdisciplinary' && (
          <InterdisciplinaryScopePanel
            project={project}
            dedicatedLibrary={sourceLibraries?.find((library) => library.library_kind === 'interdisciplinary')}
          />
        )}

        {/* 一句话定义 */}
        <SectionCard icon="sparkle" zh="简介" en="Description">
          <EditableText value={project.statement ?? ''} placeholder={tr('一句话说明研究什么', 'One sentence on what you study')}
            onSave={(v) => patchMutation.mutate({ statement: v })} saving={saving} />
        </SectionCard>

        {/* 关联文献库 */}
        <SectionCard icon="book" zh="关联文献库" en="Linked libraries"
          action={
            <button className="btn btn-soft sm" onClick={openLinkEditor} disabled={!sourceLibraries}>
              <Icon name="pen" size={12} />
              {tr('管理', 'Manage')}
            </button>
          }
        >
          {sourceLibraries === undefined ? (
            <div style={{ fontSize: 13, color: 'var(--text-3)' }}>{tr('加载中…', 'Loading…')}</div>
          ) : sourceLibraries.length === 0 ? (
            <div style={{ fontSize: 13, color: 'var(--text-3)' }}>
              {tr('还没有关联文献库', 'No linked libraries yet')}
            </div>
          ) : (
            <div className="col gap8">
              {sourceLibraries.map((lib) => (
                <div key={lib.id} className="row gap10" style={{ padding: '9px 11px', background: 'var(--surface-2)', borderRadius: 9 }}>
                  <span style={{ width: 26, height: 26, borderRadius: 8, background: 'var(--accent-soft)', color: 'var(--accent)', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
                    <Icon name="book" size={14} />
                  </span>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div className="row gap8" style={{ flexWrap: 'wrap' }}>
                      <span style={{ fontSize: 13, fontWeight: 600 }}>{lib.name}</span>
                      {lib.library_kind === 'interdisciplinary' && (
                        <span className="pill sm">{tr('课题专属', 'Topic library')}</span>
                      )}
                    </div>
                    {lib.statement && <div style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 2, lineHeight: 1.4 }}>{lib.statement}</div>}
                    {!!lib.interdisciplinary_domains?.length && (
                      <div className="muted" style={{ fontSize: 11, marginTop: 3 }}>
                        {lib.interdisciplinary_domains.join(' · ')}
                      </div>
                    )}
                  </div>
                  <span className="mono muted" style={{ fontSize: 11, flexShrink: 0 }}>{tr(`${lib.paper_count} 篇`, lib.paper_count === 1 ? '1 paper' : `${lib.paper_count} papers`)}</span>
                </div>
              ))}
            </div>
          )}
        </SectionCard>

      </div>
    </>
  );
}

/** 独立页 `/projects/:id`：套页壳后渲染课题设置主体。 */
export function ProjectDetailPage() {
  const { id = '' } = useParams();
  return (
    <div className="page fadeup">
      <ProjectSettings id={id} />
    </div>
  );
}
