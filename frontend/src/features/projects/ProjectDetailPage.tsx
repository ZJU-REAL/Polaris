import { useState, type ReactNode } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon, type IconName } from '../../components/ui/Icon';
import { StatusPill } from '../../components/ui/StatusPill';
import { Segmented } from '../../components/ui/Segmented';
import { toast } from '../../components/ui/Toast';
import { fmtTime } from '../../lib/format';
import { api, ApiError, type ProjectDefinition, type ProjectRead } from '../../lib/api';

/* ============================================================
   /projects/:id — 方向详情：definition 各节卡片化展示 + 就地编辑
   （name/statement/goals/scope/questions/cadence）+ 成员管理。
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
          {zh} <span className="en-label" style={{ fontSize: 11 }}>{en}</span>
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
      {editing ? '取消' : '编辑'}
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
        <div style={{ flex: 1, fontSize: 13.5, lineHeight: 1.6, color: value ? 'var(--text)' : 'var(--text-4)' }}>
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
          保存
        </button>
        <button className="btn btn-ghost sm" onClick={() => setEditing(false)}>取消</button>
      </div>
    </div>
  );
}

/** 可编辑字符串列表（view = 编号列表；edit = 一行一条 textarea）。 */
function EditableList({ items, placeholder, onSave, saving }: {
  items: string[];
  placeholder: string;
  onSave: (items: string[]) => void;
  saving: boolean;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  if (!editing) {
    return (
      <div className="row gap10" style={{ alignItems: 'flex-start' }}>
        <div style={{ flex: 1 }}>
          {items.length ? (
            <ol style={{ margin: 0, paddingLeft: 20, fontSize: 13, lineHeight: 1.7, color: 'var(--text)' }}>
              {items.map((g, i) => (
                <li key={i}>{g}</li>
              ))}
            </ol>
          ) : (
            <div style={{ fontSize: 13, color: 'var(--text-4)' }}>{placeholder}</div>
          )}
        </div>
        <EditButton editing={false} onClick={() => { setDraft(items.join('\n')); setEditing(true); }} />
      </div>
    );
  }
  return (
    <div className="col gap8">
      <textarea className="textarea" rows={Math.max(4, items.length + 1)} value={draft}
        onChange={(e) => setDraft(e.target.value)} placeholder="一行一条" />
      <div className="field-hint">一行一条</div>
      <div className="row gap8">
        <button className="btn btn-primary sm" disabled={saving}
          onClick={() => {
            onSave(draft.split('\n').map((x) => x.trim()).filter(Boolean));
            setEditing(false);
          }}>
          保存
        </button>
        <button className="btn btn-ghost sm" onClick={() => setEditing(false)}>取消</button>
      </div>
    </div>
  );
}

const CADENCES = [
  { v: 'daily', label: '每日 daily' },
  { v: 'weekly', label: '每周 weekly' },
  { v: 'manual', label: '手动 manual' },
] as const;
type Cadence = (typeof CADENCES)[number]['v'];

export function ProjectDetailPage() {
  const { id = '' } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const { data: project, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['project', id],
    queryFn: () => api.getProject(id),
    retry: false,
    enabled: !!id,
  });

  const patchMutation = useMutation({
    mutationFn: (input: { name?: string; definition?: ProjectDefinition }) => api.patchProject(id, input),
    onSuccess: (updated: ProjectRead) => {
      queryClient.setQueryData(['project', id], updated);
      void queryClient.invalidateQueries({ queryKey: ['projects'] });
      toast('已保存', 'ok');
    },
    onError: (err) => toast(`保存失败：${err instanceof Error ? err.message : String(err)}`, 'error'),
  });

  // —— 成员 ——
  const [memberEmail, setMemberEmail] = useState('');
  const [memberRole, setMemberRole] = useState<'member' | 'owner'>('member');
  const addMemberMutation = useMutation({
    mutationFn: () => api.addProjectMember(id, { email: memberEmail.trim(), role: memberRole }),
    onSuccess: () => {
      toast('成员已添加', 'ok');
      setMemberEmail('');
      void queryClient.invalidateQueries({ queryKey: ['project', id] });
    },
    onError: (err) => toast(`添加失败：${err instanceof Error ? err.message : String(err)}`, 'error'),
  });

  // —— 名称编辑 ——
  const [editingName, setEditingName] = useState(false);
  const [nameDraft, setNameDraft] = useState('');

  if (isLoading) {
    return (
      <div className="page fadeup">
        <div className="empty" style={{ padding: 80 }}>加载中…</div>
      </div>
    );
  }
  if (isError || !project) {
    const notFound = error instanceof ApiError && error.status === 404;
    return (
      <div className="page fadeup">
        <div className="card card-pad" style={{ textAlign: 'center', padding: 60 }}>
          <div style={{ fontSize: 15, fontWeight: 650, marginBottom: 8 }}>
            {notFound ? '方向不存在' : '无法加载方向详情'}
          </div>
          <div style={{ fontSize: 12.5, color: 'var(--text-3)', marginBottom: 18 }}>
            {error instanceof Error ? error.message : '后端不可用，请稍后重试'}
          </div>
          <div className="row gap8" style={{ justifyContent: 'center' }}>
            <button className="btn btn-soft" onClick={() => void refetch()}>重试 retry</button>
            <button className="btn btn-ghost" onClick={() => navigate('/')}>返回总览</button>
          </div>
        </div>
      </div>
    );
  }

  const def: ProjectDefinition = project.definition ?? {};
  const saving = patchMutation.isPending;

  const patchDef = (partial: Partial<ProjectDefinition>) =>
    patchMutation.mutate({ definition: { ...def, ...partial } });

  const kw = def.keywords ?? {};
  const synonymEntries = Object.entries(kw.synonyms ?? {});

  return (
    <div className="page fadeup">
      {/* 页头 */}
      <div className="row" style={{ alignItems: 'flex-start', marginBottom: 24 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="h-eyebrow">Polaris · Direction</div>
          {editingName ? (
            <div className="row gap8" style={{ marginTop: 8 }}>
              <input className="input" style={{ fontSize: 17, fontWeight: 650, width: 380 }} value={nameDraft}
                onChange={(e) => setNameDraft(e.target.value)} />
              <button className="btn btn-primary sm" disabled={saving}
                onClick={() => {
                  if (nameDraft.trim()) patchMutation.mutate({ name: nameDraft.trim() });
                  setEditingName(false);
                }}>
                保存
              </button>
              <button className="btn btn-ghost sm" onClick={() => setEditingName(false)}>取消</button>
            </div>
          ) : (
            <div className="row gap10" style={{ marginTop: 6 }}>
              <h1 className="h-title" style={{ margin: 0 }}>{project.name}</h1>
              <button className="icon-btn" style={{ width: 26, height: 26, border: 'none', background: 'transparent' }}
                title="编辑名称" onClick={() => { setNameDraft(project.name); setEditingName(true); }}>
                <Icon name="pen" size={14} />
              </button>
            </div>
          )}
          <div className="row gap8" style={{ marginTop: 10 }}>
            {project.status && <StatusPill status={project.status} sm />}
            <span className="mono muted" style={{ fontSize: 11 }}>创建于 {fmtTime(project.created_at)}</span>
          </div>
        </div>
        <button className="btn btn-ghost" onClick={() => navigate('/voyages')}>
          <Icon name="compass" size={14} />
          查看航程
        </button>
      </div>

      <div className="col gap16">
        {/* 一句话定义 */}
        <SectionCard icon="sparkle" zh="方向定义" en="Statement">
          <EditableText value={def.statement ?? ''} placeholder="尚未填写一句话定义"
            onSave={(v) => patchDef({ statement: v })} saving={saving} />
        </SectionCard>

        {/* 目标与范围 */}
        <div className="row gap16" style={{ alignItems: 'stretch' }}>
          <div style={{ flex: 1.2, minWidth: 0 }}>
            <SectionCard icon="chart" zh="研究目标" en="Goals">
              <EditableList items={def.goals ?? []} placeholder="尚未填写目标"
                onSave={(items) => patchDef({ goals: items })} saving={saving} />
            </SectionCard>
          </div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <SectionCard icon="scale" zh="范围" en="Scope">
              <div className="field-label" style={{ marginBottom: 6 }}>范围内 in-scope</div>
              <EditableList items={def.in_scope ?? []} placeholder="—"
                onSave={(items) => patchDef({ in_scope: items })} saving={saving} />
              <div className="hr" style={{ margin: '12px 0' }} />
              <div className="field-label" style={{ marginBottom: 6 }}>范围外 out-of-scope</div>
              <EditableList items={def.out_of_scope ?? []} placeholder="—"
                onSave={(items) => patchDef({ out_of_scope: items })} saving={saving} />
            </SectionCard>
          </div>
        </div>

        {/* 研究问题 */}
        <SectionCard icon="bulb" zh="研究问题" en="Questions">
          <EditableList items={def.questions ?? []} placeholder="尚未填写研究问题"
            onSave={(items) => patchDef({ questions: items })} saving={saving} />
        </SectionCard>

        {/* Rubric */}
        <SectionCard icon="scale" zh="打分 Rubric" en="Scoring rubric">
          {def.rubric?.length ? (
            <table className="table">
              <thead>
                <tr>
                  <th style={{ width: 140 }}>维度</th>
                  <th>打分标准</th>
                  <th style={{ width: 70, textAlign: 'right' }}>权重</th>
                </tr>
              </thead>
              <tbody>
                {def.rubric.map((r, i) => (
                  <tr key={i}>
                    <td style={{ fontWeight: 600 }}>{r.name}</td>
                    <td style={{ color: 'var(--text-2)' }}>{r.description}</td>
                    <td className="mono" style={{ textAlign: 'right' }}>{r.weight}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div style={{ fontSize: 13, color: 'var(--text-4)' }}>尚未定义打分维度</div>
          )}
        </SectionCard>

        {/* 锚点论文 */}
        <SectionCard icon="book" zh="锚点论文" en="Anchor papers">
          {def.anchor_papers?.length ? (
            <div className="col gap10">
              {def.anchor_papers.map((a, i) => (
                <div key={i} style={{ padding: '10px 12px', background: 'var(--surface-2)', borderRadius: 10 }}>
                  <div className="row gap8">
                    <span style={{ fontSize: 13, fontWeight: 600, flex: 1 }}>{a.title}</span>
                    {a.arxiv_id && <span className="tag mono">{a.arxiv_id}</span>}
                    {a.url && (
                      <a href={a.url} target="_blank" rel="noreferrer" className="icon-btn"
                        style={{ width: 24, height: 24, border: 'none', background: 'transparent' }} title={a.url}>
                        <Icon name="link" size={13} />
                      </a>
                    )}
                  </div>
                  {a.reason && <div style={{ fontSize: 12, color: 'var(--text-2)', marginTop: 4, lineHeight: 1.5 }}>{a.reason}</div>}
                </div>
              ))}
            </div>
          ) : (
            <div style={{ fontSize: 13, color: 'var(--text-4)' }}>尚未添加锚点论文</div>
          )}
        </SectionCard>

        {/* 关键词 */}
        <SectionCard icon="search" zh="关键词" en="Keywords">
          <div className="col gap12">
            <div>
              <div className="field-label" style={{ marginBottom: 6 }}>arXiv 分类</div>
              <div className="row gap6 wrap">
                {kw.arxiv_categories?.length
                  ? kw.arxiv_categories.map((c) => <span key={c} className="tag mono">{c}</span>)
                  : <span style={{ fontSize: 12.5, color: 'var(--text-4)' }}>—</span>}
              </div>
            </div>
            <div>
              <div className="field-label" style={{ marginBottom: 6 }}>include 词</div>
              <div className="row gap6 wrap">
                {kw.include?.length
                  ? kw.include.map((c) => <span key={c} className="tag">{c}</span>)
                  : <span style={{ fontSize: 12.5, color: 'var(--text-4)' }}>—</span>}
              </div>
            </div>
            {synonymEntries.length > 0 && (
              <div>
                <div className="field-label" style={{ marginBottom: 6 }}>同义词映射</div>
                <div className="col gap6">
                  {synonymEntries.map(([term, syns]) => (
                    <div key={term} className="row gap8" style={{ fontSize: 12.5 }}>
                      <span className="tag">{term}</span>
                      <span style={{ color: 'var(--text-4)' }}>→</span>
                      <span style={{ color: 'var(--text-2)' }}>{syns.join('，')}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        </SectionCard>

        {/* 节奏 */}
        <SectionCard icon="clock" zh="运行节奏" en="Cadence">
          <Segmented
            options={CADENCES.map((c) => ({ v: c.v, label: c.label }))}
            value={(CADENCES.some((c) => c.v === def.cadence) ? def.cadence : 'daily') as Cadence}
            onChange={(v) => patchDef({ cadence: v })}
          />
        </SectionCard>

        {/* 成员 */}
        <SectionCard icon="users" zh="成员" en="Members">
          <div className="col gap8" style={{ marginBottom: 16 }}>
            {project.members?.length ? (
              project.members.map((m, i) => (
                <div key={m.user_id ?? i} className="row gap10" style={{ padding: '8px 10px', background: 'var(--surface-2)', borderRadius: 9 }}>
                  <div className="av" style={{ width: 24, height: 24, fontSize: 10 }}>
                    {(m.display_name ?? m.email ?? '?').slice(0, 2).toUpperCase()}
                  </div>
                  <span style={{ fontSize: 13, flex: 1 }}>{m.display_name ?? m.email ?? m.user_id}</span>
                  {m.email && m.display_name && <span className="mono muted" style={{ fontSize: 11 }}>{m.email}</span>}
                  <span className="pill sm" style={m.role === 'owner' ? { background: 'var(--accent-soft)', color: 'var(--accent-text)' } : {}}>
                    {m.role ?? 'member'}
                  </span>
                </div>
              ))
            ) : (
              <div style={{ fontSize: 13, color: 'var(--text-4)' }}>暂无成员信息</div>
            )}
          </div>
          <div className="row gap8">
            <input className="input" style={{ flex: 1 }} placeholder="成员邮箱 email" type="email"
              value={memberEmail} onChange={(e) => setMemberEmail(e.target.value)} />
            <select className="input" style={{ width: 120 }} value={memberRole}
              onChange={(e) => setMemberRole(e.target.value as 'member' | 'owner')}>
              <option value="member">member</option>
              <option value="owner">owner</option>
            </select>
            <button className="btn btn-primary" style={{ height: 38 }}
              disabled={!memberEmail.trim() || addMemberMutation.isPending}
              onClick={() => addMemberMutation.mutate()}>
              <Icon name="plus" size={13} />
              添加成员
            </button>
          </div>
        </SectionCard>
      </div>
    </div>
  );
}
