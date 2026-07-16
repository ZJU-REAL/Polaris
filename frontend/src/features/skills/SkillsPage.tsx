import { useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { PageHead } from '../../components/ui/PageHead';
import { Segmented } from '../../components/ui/Segmented';
import { EmptyState } from '../../components/ui/EmptyState';
import { Modal } from '../../components/ui/Modal';
import { FormField } from '../../components/ui/FormField';
import { toast } from '../../components/ui/Toast';
import { useProject } from '../../app/project';
import {
  api,
  isAdmin,
  skillTargetLabel,
  SKILL_KIND_LABEL,
  SKILL_TARGET_LABEL,
  type ProjectSkillRead,
  type SkillDetail,
  type SkillKind,
  type SkillListingRead,
  type SkillExportData,
  type SkillManifest,
  type SkillRead,
  type SkillTestResult,
} from '../../lib/api';

/** 下载 JSON 文件（技能包导出）。 */
function downloadJson(filename: string, data: unknown): void {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

/* ============================================================
   /skills — 技能库：内置/我的技能列表 + 启用到当前研究方向。
   - 技能 = 各环节 AI 的补充判断标准（指引/评分标准/评审人设/流程模板）
   - 详情弹窗：全文预览 / 试运行 / 启用 / 复制为我的 / 编辑（追加版本）
   - 流程模板可直接「运行此流程」→ 创建 AI 任务
   ============================================================ */

type ScopeFilter = 'all' | 'builtin' | 'mine';
type KindFilter = 'all' | SkillKind;

const KIND_FILTERS: { v: KindFilter; label: string }[] = [
  { v: 'all', label: '全部类型' },
  { v: 'guidance', label: '指引' },
  { v: 'rubric', label: '评分标准' },
  { v: 'persona', label: '评审人设' },
  { v: 'workflow', label: '流程模板' },
];

const KIND_COLOR: Record<SkillKind, { bg: string; tx: string }> = {
  guidance: { bg: 'var(--info-bg)', tx: 'var(--info-tx)' },
  rubric: { bg: 'var(--warn-bg)', tx: 'var(--warn-tx)' },
  persona: { bg: 'var(--violet-bg)', tx: 'var(--violet-tx)' },
  workflow: { bg: 'var(--ok-bg)', tx: 'var(--ok-tx)' },
};

function KindPill({ kind }: { kind: SkillKind }) {
  const c = KIND_COLOR[kind];
  return (
    <span className="pill sm" style={{ background: c.bg, color: c.tx, flexShrink: 0 }}>
      {SKILL_KIND_LABEL[kind]}
    </span>
  );
}

function errMsg(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

// ---- 技能卡片 ----

function SkillCard({ s, onOpen }: { s: SkillRead; onOpen: () => void }) {
  return (
    <button
      className="card"
      onClick={onOpen}
      style={{ display: 'block', width: '100%', textAlign: 'left', padding: '14px 16px', cursor: 'pointer' }}
    >
      <div className="row gap8" style={{ alignItems: 'center' }}>
        <span style={{ fontSize: 13.5, fontWeight: 650 }}>{s.name}</span>
        <KindPill kind={s.kind} />
        {s.scope === 'builtin' && (
          <span className="pill sm" style={{ background: 'var(--surface-3)', color: 'var(--text-3)' }}>
            内置
          </span>
        )}
        <span className="mono" style={{ marginLeft: 'auto', fontSize: 10.5, color: 'var(--text-3)' }}>
          {s.slug}
        </span>
      </div>
      {s.description && (
        <div style={{ fontSize: 12, color: 'var(--text-2)', marginTop: 6, lineHeight: 1.5 }}>{s.description}</div>
      )}
    </button>
  );
}

// ---- 详情弹窗 ----

function SkillDetailModal({
  skillId,
  onClose,
}: {
  skillId: string;
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { currentProjectId } = useProject();
  const [test, setTest] = useState<SkillTestResult | null>(null);
  const [enableTarget, setEnableTarget] = useState<string>('');
  const [editOpen, setEditOpen] = useState(false);
  const [runGoal, setRunGoal] = useState('');

  const { data: skill } = useQuery({
    queryKey: ['skill', skillId],
    queryFn: () => api.getSkill(skillId),
  });

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['skills'] });
    void queryClient.invalidateQueries({ queryKey: ['skill', skillId] });
    void queryClient.invalidateQueries({ queryKey: ['project-skills'] });
  };

  const testMutation = useMutation({
    mutationFn: () => api.testSkill(skillId),
    onSuccess: setTest,
    onError: (e) => toast(`试运行失败：${errMsg(e)}`, 'error'),
  });
  const forkMutation = useMutation({
    mutationFn: () => api.forkSkill(skillId),
    onSuccess: (fork) => {
      toast(`已复制为我的技能：${fork.name}`, 'ok');
      invalidate();
    },
    onError: (e) => toast(`复制失败：${errMsg(e)}`, 'error'),
  });
  const exportMutation = useMutation({
    mutationFn: () => api.exportSkill(skillId),
    onSuccess: (data) => {
      downloadJson(`${data.slug}.polaris-skill.json`, data);
      toast('技能包已下载', 'ok');
    },
    onError: (e) => toast(`导出失败：${errMsg(e)}`, 'error'),
  });
  const publishMutation = useMutation({
    mutationFn: () => api.publishSkill(skillId),
    onSuccess: () => {
      toast('已提交发布，等待管理员审核', 'ok');
      void queryClient.invalidateQueries({ queryKey: ['market-skills'] });
    },
    onError: (e) =>
      toast(e instanceof Error && e.message === 'ALREADY_LISTED' ? '该技能已在市场（或待审核中）' : `发布失败：${errMsg(e)}`, 'error'),
  });
  const archiveMutation = useMutation({
    mutationFn: () => api.archiveSkill(skillId),
    onSuccess: () => {
      toast('技能已删除', 'ok');
      invalidate();
      onClose();
    },
    onError: (e) => toast(`删除失败：${errMsg(e)}`, 'error'),
  });
  const enableMutation = useMutation({
    mutationFn: (target: string) =>
      api.enableProjectSkill(currentProjectId!, { skill_id: skillId, target }),
    onSuccess: (row) => {
      toast(`已启用到当前方向：${skillTargetLabel(row.target)}`, 'ok');
      invalidate();
    },
    onError: (e) =>
      toast(e instanceof Error && e.message === 'SKILL_ALREADY_ENABLED' ? '该环节已启用过此技能' : `启用失败：${errMsg(e)}`, 'error'),
  });
  const runMutation = useMutation({
    mutationFn: () => api.runWorkflowSkill(skillId, { project_id: currentProjectId!, goal: runGoal.trim() }),
    onSuccess: (run) => {
      toast('AI 任务已创建', 'ok');
      void queryClient.invalidateQueries({ queryKey: ['voyages'] });
      onClose();
      navigate(`/voyages/${run.id}`);
    },
    onError: (e) => toast(`运行失败：${errMsg(e)}`, 'error'),
  });

  if (!skill) return null;
  const version = skill.current_version;
  const targets = version?.manifest.targets ?? [];
  const mine = skill.scope !== 'builtin';

  return (
    <Modal
      open
      onClose={onClose}
      width={640}
      title={
        <>
          {skill.name}
          <KindPill kind={skill.kind} />
        </>
      }
      sub={`${skill.slug} · v${version?.version ?? '—'} · ${skill.scope === 'builtin' ? '内置技能' : '我的技能'}`}
      footer={
        <>
          {mine && (
            <button
              className="btn btn-ghost"
              style={{ marginRight: 'auto', color: 'var(--danger, #c0392b)' }}
              onClick={() => {
                if (window.confirm('删除这个技能？已启用的项目会同时失效（进行中的任务不受影响）。')) {
                  archiveMutation.mutate();
                }
              }}
            >
              <Icon name="trash" size={13} /> 删除
            </button>
          )}
          <button className="btn btn-ghost" disabled={exportMutation.isPending} onClick={() => exportMutation.mutate()}>
            导出
          </button>
          {skill.scope === 'builtin' && (
            <button className="btn btn-soft" disabled={forkMutation.isPending} onClick={() => forkMutation.mutate()}>
              复制为我的技能
            </button>
          )}
          {mine && (
            <button className="btn btn-soft" onClick={() => setEditOpen(true)}>
              编辑（存为新版本）
            </button>
          )}
          {mine && (
            <button className="btn btn-soft" disabled={publishMutation.isPending} onClick={() => publishMutation.mutate()}>
              发布到市场
            </button>
          )}
          {skill.kind !== 'workflow' && (
            <button className="btn btn-soft" disabled={testMutation.isPending} onClick={() => testMutation.mutate()}>
              <Icon name="play" size={13} /> {testMutation.isPending ? '运行中…' : '试运行'}
            </button>
          )}
        </>
      }
    >
      {skill.description && <p style={{ fontSize: 12.5, color: 'var(--text-2)', marginBottom: 12 }}>{skill.description}</p>}

      {/* 适用环节 + 启用 */}
      <div className="row gap8" style={{ flexWrap: 'wrap', marginBottom: 14, alignItems: 'center' }}>
        <span style={{ fontSize: 11.5, color: 'var(--text-3)' }}>适用环节：</span>
        {targets.map((t) => (
          <span key={t} className="pill sm" style={{ background: 'var(--surface-3)', color: 'var(--text-2)' }}>
            {skillTargetLabel(t)}
          </span>
        ))}
        {skill.kind !== 'workflow' && currentProjectId && targets.length > 0 && (
          <span className="row gap8" style={{ marginLeft: 'auto' }}>
            {targets.length > 1 && (
              <select className="input" style={{ width: 160 }} value={enableTarget} onChange={(e) => setEnableTarget(e.target.value)}>
                {targets.map((t) => (
                  <option key={t} value={t}>
                    {skillTargetLabel(t)}
                  </option>
                ))}
              </select>
            )}
            <button
              className="btn btn-primary"
              disabled={enableMutation.isPending}
              onClick={() => {
                const t = enableTarget || targets[0];
                if (t) enableMutation.mutate(t);
              }}
            >
              启用到当前方向
            </button>
          </span>
        )}
      </div>

      {/* workflow：运行此流程 */}
      {skill.kind === 'workflow' && (
        <div className="card" style={{ padding: '12px 14px', marginBottom: 14, background: 'var(--surface-2)' }}>
          <FormField label="任务目标" en="Goal" hint="流程各步骤可引用 {goal} 模板变量">
            <textarea
              className="textarea"
              rows={2}
              placeholder="例如：综述 LLM 推理加速的研究现状"
              value={runGoal}
              onChange={(e) => setRunGoal(e.target.value)}
            />
          </FormField>
          <div className="row" style={{ justifyContent: 'flex-end', marginTop: 8 }}>
            <button
              className="btn btn-primary"
              disabled={!currentProjectId || !runGoal.trim() || runMutation.isPending}
              onClick={() => runMutation.mutate()}
            >
              <Icon name="play" size={13} /> 运行此流程
            </button>
          </div>
        </div>
      )}

      {/* persona 预览 */}
      {skill.kind === 'persona' && (version?.manifest.personas?.length ?? 0) > 0 && (
        <div style={{ marginBottom: 14 }}>
          {version!.manifest.personas!.map((p) => (
            <div key={p.name} className="row gap8" style={{ padding: '6px 0', alignItems: 'baseline' }}>
              <span style={{ fontSize: 12.5, fontWeight: 650, flexShrink: 0 }}>{p.name}</span>
              <span style={{ fontSize: 12, color: 'var(--text-2)' }}>{p.stance}</span>
            </div>
          ))}
        </div>
      )}

      {/* workflow 步骤预览 */}
      {skill.kind === 'workflow' && (version?.manifest.steps?.length ?? 0) > 0 && (
        <ol style={{ margin: '0 0 14px 18px', padding: 0 }}>
          {version!.manifest.steps!.map((s, i) => (
            <li key={i} style={{ fontSize: 12.5, color: 'var(--text-2)', marginBottom: 4 }}>
              {String(s.title ?? '')}
              <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)', marginLeft: 6 }}>
                {String(s.action ?? '')}
              </span>
            </li>
          ))}
        </ol>
      )}

      {/* 正文 */}
      {version && (
        <pre
          className="scroll"
          style={{
            whiteSpace: 'pre-wrap',
            fontSize: 12,
            lineHeight: 1.65,
            background: 'var(--surface-2)',
            border: '0.5px solid var(--border)',
            borderRadius: 8,
            padding: '12px 14px',
            maxHeight: 260,
            overflowY: 'auto',
          }}
        >
          {version.body}
        </pre>
      )}

      {/* 试运行结果 */}
      {test && (
        <div style={{ marginTop: 14 }}>
          <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginBottom: 6 }}>
            试运行输出{test.model ? `（模型：${test.model}）` : ''}
          </div>
          <pre
            style={{
              whiteSpace: 'pre-wrap',
              fontSize: 12,
              lineHeight: 1.6,
              background: 'var(--accent-soft)',
              borderRadius: 8,
              padding: '12px 14px',
              maxHeight: 200,
              overflowY: 'auto',
            }}
          >
            {test.output ?? test.rendered}
          </pre>
        </div>
      )}

      {editOpen && <SkillEditModal skill={skill} onClose={() => setEditOpen(false)} onSaved={invalidate} />}
    </Modal>
  );
}

// ---- 编辑（追加版本）----

function SkillEditModal({
  skill,
  onClose,
  onSaved,
}: {
  skill: SkillDetail;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [body, setBody] = useState(skill.current_version?.body ?? '');
  const [changelog, setChangelog] = useState('');
  const [manifestText, setManifestText] = useState(JSON.stringify(skill.current_version?.manifest ?? { targets: [] }, null, 2));
  const [showAdvanced, setShowAdvanced] = useState(false);

  const saveMutation = useMutation({
    mutationFn: () => {
      let manifest: SkillManifest;
      try {
        manifest = JSON.parse(manifestText) as SkillManifest;
      } catch {
        throw new Error('高级设置不是合法 JSON');
      }
      return api.addSkillVersion(skill.id, { manifest, body, changelog: changelog || undefined });
    },
    onSuccess: (v) => {
      toast(`已保存为 v${v.version}`, 'ok');
      onSaved();
      onClose();
    },
    onError: (e) => toast(`保存失败：${errMsg(e)}`, 'error'),
  });

  return (
    <Modal
      open
      onClose={onClose}
      width={620}
      title={`编辑：${skill.name}`}
      sub="保存后生成新版本；已按旧版本固定的项目不受影响"
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>
            取消
          </button>
          <button className="btn btn-primary" disabled={!body.trim() || saveMutation.isPending} onClick={() => saveMutation.mutate()}>
            保存为新版本
          </button>
        </>
      }
    >
      <FormField label="技能内容" en="Body" hint="Markdown；这段文字会注入对应环节的 AI 指令">
        <textarea className="textarea mono" rows={12} value={body} onChange={(e) => setBody(e.target.value)} />
      </FormField>
      <FormField label="修改说明" en="Changelog">
        <input className="input" value={changelog} onChange={(e) => setChangelog(e.target.value)} placeholder="这次改了什么（可选）" />
      </FormField>
      <button className="btn btn-ghost" style={{ fontSize: 12 }} onClick={() => setShowAdvanced((v) => !v)}>
        {showAdvanced ? '收起高级设置' : '高级设置（适用环节 / 人设 / 步骤，JSON）'}
      </button>
      {showAdvanced && (
        <textarea
          className="textarea mono"
          rows={8}
          style={{ marginTop: 8, fontSize: 11.5 }}
          value={manifestText}
          onChange={(e) => setManifestText(e.target.value)}
        />
      )}
    </Modal>
  );
}

// ---- 新建技能 ----

const CREATE_KINDS: { v: SkillKind; label: string; hint: string }[] = [
  { v: 'guidance', label: '指引', hint: '为某个环节追加做事方式与注意点' },
  { v: 'rubric', label: '评分标准', hint: '为打分/评审环节定义评价细则' },
];

function SkillCreateModal({ onClose, onCreated }: { onClose: () => void; onCreated: (s: SkillDetail) => void }) {
  const [name, setName] = useState('');
  const [slug, setSlug] = useState('');
  const [kind, setKind] = useState<SkillKind>('guidance');
  const [description, setDescription] = useState('');
  const [targets, setTargets] = useState<string[]>([]);
  const [body, setBody] = useState('');

  const createMutation = useMutation({
    mutationFn: () =>
      api.createSkill({
        slug: slug.trim(),
        kind,
        name: name.trim(),
        description: description.trim() || undefined,
        manifest: { targets },
        body,
      }),
    onSuccess: (s) => {
      toast(`技能已创建：${s.name}`, 'ok');
      onCreated(s);
      onClose();
    },
    onError: (e) =>
      toast(e instanceof Error && e.message === 'SKILL_SLUG_TAKEN' ? '标识已被占用，换一个吧' : `创建失败：${errMsg(e)}`, 'error'),
  });

  const canSubmit = name.trim() && /^[a-z0-9][a-z0-9-]{1,62}$/.test(slug.trim()) && targets.length > 0 && body.trim();

  return (
    <Modal
      open
      onClose={onClose}
      width={620}
      title="新建技能"
      sub="评审人设与流程模板建议从内置技能复制后修改"
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>
            取消
          </button>
          <button className="btn btn-primary" disabled={!canSubmit || createMutation.isPending} onClick={() => createMutation.mutate()}>
            创建
          </button>
        </>
      }
    >
      <div className="row gap10">
        <FormField label="名称" en="Name" style={{ flex: 1 }}>
          <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="如：严格的想法打分标准" />
        </FormField>
        <FormField label="标识" en="Slug" style={{ flex: 1 }} hint="小写字母/数字/连字符">
          <input className="input mono" value={slug} onChange={(e) => setSlug(e.target.value)} placeholder="my-scoring-rubric" />
        </FormField>
      </div>
      <FormField label="类型" en="Kind">
        <div className="row gap8">
          {CREATE_KINDS.map((k) => (
            <button
              key={k.v}
              className={`btn ${kind === k.v ? 'btn-primary' : 'btn-soft'}`}
              title={k.hint}
              onClick={() => setKind(k.v)}
            >
              {k.label}
            </button>
          ))}
        </div>
      </FormField>
      <FormField label="适用环节" en="Applies to" hint="技能会注入这些环节的 AI 指令（可多选）">
        <div className="row gap8" style={{ flexWrap: 'wrap' }}>
          {Object.entries(SKILL_TARGET_LABEL)
            .filter(([t]) => t !== 'navigator.free_plan')
            .map(([t, label]) => {
              const on = targets.includes(t);
              return (
                <button
                  key={t}
                  className="pill sm"
                  style={{
                    cursor: 'pointer',
                    background: on ? 'var(--accent-soft)' : 'var(--surface-3)',
                    color: on ? 'var(--accent-text)' : 'var(--text-2)',
                    border: on ? '1px solid var(--accent)' : '1px solid transparent',
                  }}
                  onClick={() => setTargets((prev) => (on ? prev.filter((x) => x !== t) : [...prev, t]))}
                >
                  {label}
                </button>
              );
            })}
        </div>
      </FormField>
      <FormField label="描述" en="Description">
        <input className="input" value={description} onChange={(e) => setDescription(e.target.value)} placeholder="一句话说明这个技能做什么（可选）" />
      </FormField>
      <FormField label="技能内容" en="Body" hint="Markdown；写清楚判断标准/注意点，AI 会照此执行">
        <textarea className="textarea mono" rows={10} value={body} onChange={(e) => setBody(e.target.value)} />
      </FormField>
    </Modal>
  );
}

// ---- 技能市场 ----

function Stars({ avg, count }: { avg: number | null; count: number }) {
  if (!count) return <span style={{ fontSize: 11, color: 'var(--text-3)' }}>暂无评分</span>;
  return (
    <span style={{ fontSize: 11.5, color: 'var(--warn-tx)' }}>
      ★ {avg?.toFixed(1)}
      <span style={{ color: 'var(--text-3)', marginLeft: 4 }}>（{count} 人）</span>
    </span>
  );
}

function ListingCard({ l, onOpen }: { l: SkillListingRead; onOpen: () => void }) {
  return (
    <button
      className="card"
      onClick={onOpen}
      style={{ display: 'block', width: '100%', textAlign: 'left', padding: '14px 16px', cursor: 'pointer' }}
    >
      <div className="row gap8" style={{ alignItems: 'center' }}>
        <span style={{ fontSize: 13.5, fontWeight: 650 }}>{l.skill?.name ?? '（技能已删除）'}</span>
        {l.skill && <KindPill kind={l.skill.kind} />}
        <span className="pill sm" style={{ background: 'var(--surface-3)', color: 'var(--text-3)' }}>
          v{l.version ?? '—'}
        </span>
        <span style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--text-3)' }}>{l.install_count} 次安装</span>
      </div>
      {l.summary && <div style={{ fontSize: 12, color: 'var(--text-2)', marginTop: 6, lineHeight: 1.5 }}>{l.summary}</div>}
      <div className="row gap8" style={{ marginTop: 8, alignItems: 'center' }}>
        <Stars avg={l.rating_avg} count={l.rating_count} />
        {(l.tags ?? []).map((t) => (
          <span key={t} className="pill sm" style={{ background: 'var(--accent-soft)', color: 'var(--accent-text)' }}>
            {t}
          </span>
        ))}
      </div>
    </button>
  );
}

function ListingDetailModal({ listingId, onClose }: { listingId: string; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [myRating, setMyRating] = useState(0);
  const [myComment, setMyComment] = useState('');

  const { data: listing } = useQuery({
    queryKey: ['market-skill', listingId],
    queryFn: () => api.getMarketSkill(listingId),
  });
  const { data: reviews } = useQuery({
    queryKey: ['market-reviews', listingId],
    queryFn: () => api.listListingReviews(listingId),
  });

  const installMutation = useMutation({
    mutationFn: () => api.installMarketSkill(listingId),
    onSuccess: (skill) => {
      toast(`已安装到我的技能：${skill.name}`, 'ok');
      void queryClient.invalidateQueries({ queryKey: ['skills'] });
      void queryClient.invalidateQueries({ queryKey: ['market-skills'] });
    },
    onError: (e) => toast(`安装失败：${errMsg(e)}`, 'error'),
  });
  const rateMutation = useMutation({
    mutationFn: () => api.addListingReview(listingId, { rating: myRating, comment: myComment.trim() || undefined }),
    onSuccess: () => {
      toast('评分已提交', 'ok');
      setMyComment('');
      void queryClient.invalidateQueries({ queryKey: ['market-reviews', listingId] });
      void queryClient.invalidateQueries({ queryKey: ['market-skills'] });
    },
    onError: (e) => toast(`评分失败：${errMsg(e)}`, 'error'),
  });

  if (!listing) return null;
  return (
    <Modal
      open
      onClose={onClose}
      width={640}
      title={
        <>
          {listing.skill?.name ?? '技能'}
          {listing.skill && <KindPill kind={listing.skill.kind} />}
        </>
      }
      sub={`${listing.skill?.slug ?? ''} · 发布版本 v${listing.version ?? '—'} · ${listing.install_count} 次安装`}
      footer={
        <button className="btn btn-primary" disabled={installMutation.isPending} onClick={() => installMutation.mutate()}>
          <Icon name="download" size={13} /> 安装到我的技能
        </button>
      }
    >
      {listing.summary && <p style={{ fontSize: 12.5, color: 'var(--text-2)', marginBottom: 12 }}>{listing.summary}</p>}
      {listing.body && (
        <pre
          className="scroll"
          style={{
            whiteSpace: 'pre-wrap',
            fontSize: 12,
            lineHeight: 1.65,
            background: 'var(--surface-2)',
            border: '0.5px solid var(--border)',
            borderRadius: 8,
            padding: '12px 14px',
            maxHeight: 240,
            overflowY: 'auto',
          }}
        >
          {listing.body}
        </pre>
      )}

      {/* 我的评分 */}
      <div className="row gap8" style={{ marginTop: 14, alignItems: 'center' }}>
        {[1, 2, 3, 4, 5].map((n) => (
          <button
            key={n}
            className="icon-btn"
            style={{ width: 26, height: 26, color: n <= myRating ? 'var(--warn-tx)' : 'var(--text-3)', border: 'none', background: 'transparent', fontSize: 16 }}
            onClick={() => setMyRating(n)}
            aria-label={`${n} 星`}
          >
            {n <= myRating ? '★' : '☆'}
          </button>
        ))}
        <input
          className="input"
          style={{ flex: 1 }}
          placeholder="用后感受（可选）"
          value={myComment}
          onChange={(e) => setMyComment(e.target.value)}
        />
        <button className="btn btn-soft" disabled={!myRating || rateMutation.isPending} onClick={() => rateMutation.mutate()}>
          提交评分
        </button>
      </div>

      {(reviews ?? []).length > 0 && (
        <div style={{ marginTop: 12 }}>
          {reviews!.map((r) => (
            <div key={r.id} className="row gap8" style={{ padding: '5px 0', alignItems: 'baseline' }}>
              <span style={{ fontSize: 11.5, color: 'var(--warn-tx)', flexShrink: 0 }}>{'★'.repeat(r.rating)}</span>
              <span style={{ fontSize: 12, color: 'var(--text-2)' }}>{r.comment ?? ''}</span>
            </div>
          ))}
        </div>
      )}
    </Modal>
  );
}

function MarketView() {
  const queryClient = useQueryClient();
  const [q, setQ] = useState('');
  const [sort, setSort] = useState<'-created_at' | 'installs'>('-created_at');
  const [openListing, setOpenListing] = useState<string | null>(null);

  const { data: me } = useQuery({ queryKey: ['me'], queryFn: () => api.me(), retry: false, staleTime: 60_000 });
  const admin = isAdmin(me);

  const { data: listings, isLoading, isError } = useQuery({
    queryKey: ['market-skills', sort, q],
    queryFn: () => api.listMarketSkills({ q: q || undefined, sort }),
    retry: false,
  });
  const { data: pending } = useQuery({
    queryKey: ['market-skills', 'pending'],
    queryFn: () => api.listMarketSkills({ status: 'pending' }),
    retry: false,
    enabled: admin,
  });

  const decideMutation = useMutation({
    mutationFn: ({ id, decision }: { id: string; decision: 'approve' | 'reject' }) => api.decideListing(id, decision),
    onSuccess: (listing) => {
      toast(listing.status === 'approved' ? '已上架' : '已驳回', 'ok');
      void queryClient.invalidateQueries({ queryKey: ['market-skills'] });
    },
    onError: (e) => toast(`审核失败：${errMsg(e)}`, 'error'),
  });

  return (
    <div>
      <div className="row gap10" style={{ marginBottom: 16 }}>
        <Segmented
          options={[
            { v: '-created_at', label: '最新' },
            { v: 'installs', label: '最多安装' },
          ]}
          value={sort}
          onChange={setSort}
        />
        <input className="input" style={{ width: 200, marginLeft: 'auto' }} placeholder="搜索市场…" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>

      {admin && (pending ?? []).length > 0 && (
        <div className="card" style={{ padding: '14px 16px', marginBottom: 14, borderLeft: '3px solid var(--warn-tx)' }}>
          <div style={{ fontSize: 13, fontWeight: 660, marginBottom: 8 }}>待审核（管理员）</div>
          {pending!.map((l) => (
            <div key={l.id} className="row gap8" style={{ padding: '5px 0', alignItems: 'center' }}>
              <span style={{ fontSize: 12.5, fontWeight: 600 }}>{l.skill?.name}</span>
              <span style={{ fontSize: 11.5, color: 'var(--text-3)', flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {l.summary}
              </span>
              <button className="btn btn-soft" style={{ fontSize: 11.5, padding: '3px 10px' }} onClick={() => decideMutation.mutate({ id: l.id, decision: 'approve' })}>
                通过
              </button>
              <button className="btn btn-ghost" style={{ fontSize: 11.5, padding: '3px 10px' }} onClick={() => decideMutation.mutate({ id: l.id, decision: 'reject' })}>
                驳回
              </button>
            </div>
          ))}
        </div>
      )}

      <div style={{ display: 'grid', gap: 10 }}>
        {isError ? (
          <EmptyState icon="sparkle" title="市场加载失败" desc="后端服务未启动或版本过旧" />
        ) : isLoading ? null : (listings ?? []).length === 0 ? (
          <EmptyState icon="sparkle" title="市场还是空的" desc="在技能库把你的技能发布到市场，审核通过后即可安装" />
        ) : (
          listings!.map((l) => <ListingCard key={l.id} l={l} onOpen={() => setOpenListing(l.id)} />)
        )}
      </div>

      {openListing && <ListingDetailModal listingId={openListing} onClose={() => setOpenListing(null)} />}
    </div>
  );
}

// ---- 已启用面板 ----

function EnabledPanel({ projectId }: { projectId: string }) {
  const queryClient = useQueryClient();
  const { data: rows, isLoading } = useQuery({
    queryKey: ['project-skills', projectId],
    queryFn: () => api.listProjectSkills(projectId),
  });

  const patchMutation = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) => api.patchProjectSkill(id, { enabled }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['project-skills', projectId] }),
    onError: (e) => toast(`操作失败：${errMsg(e)}`, 'error'),
  });
  const removeMutation = useMutation({
    mutationFn: (id: string) => api.removeProjectSkill(id),
    onSuccess: () => {
      toast('已从当前方向移除', 'ok');
      void queryClient.invalidateQueries({ queryKey: ['project-skills', projectId] });
    },
    onError: (e) => toast(`移除失败：${errMsg(e)}`, 'error'),
  });

  const grouped = useMemo(() => {
    const m = new Map<string, ProjectSkillRead[]>();
    for (const r of rows ?? []) {
      const list = m.get(r.target) ?? [];
      list.push(r);
      m.set(r.target, list);
    }
    return [...m.entries()];
  }, [rows]);

  return (
    <div className="card" style={{ padding: '14px 16px' }}>
      <div style={{ fontSize: 13, fontWeight: 660, marginBottom: 4 }}>当前方向已启用</div>
      <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginBottom: 12 }}>
        新发起的 AI 任务会按下面的技能执行；进行中的任务不受影响
      </div>
      {isLoading ? null : grouped.length === 0 ? (
        <EmptyState compact icon="sparkle" title="还没有启用技能" desc="从左侧技能列表点启用到当前方向" />
      ) : (
        grouped.map(([target, list]) => (
          <div key={target} style={{ marginBottom: 12 }}>
            <div style={{ fontSize: 11, color: 'var(--text-3)', marginBottom: 6 }}>{skillTargetLabel(target)}</div>
            {list.map((r) => (
              <div key={r.id} className="row gap8" style={{ padding: '5px 0', alignItems: 'center' }}>
                <span
                  style={{
                    fontSize: 12.5,
                    fontWeight: 600,
                    color: r.enabled ? 'var(--text)' : 'var(--text-3)',
                    textDecoration: r.enabled ? 'none' : 'line-through',
                  }}
                >
                  {r.skill?.name ?? '（技能已删除）'}
                </span>
                {r.pinned_version != null && (
                  <span className="pill sm" style={{ background: 'var(--surface-3)', color: 'var(--text-3)' }}>
                    v{r.pinned_version}
                  </span>
                )}
                <span className="row gap8" style={{ marginLeft: 'auto' }}>
                  <button
                    className="btn btn-ghost"
                    style={{ fontSize: 11.5, padding: '3px 8px' }}
                    onClick={() => patchMutation.mutate({ id: r.id, enabled: !r.enabled })}
                  >
                    {r.enabled ? '停用' : '启用'}
                  </button>
                  <button
                    className="icon-btn"
                    style={{ width: 24, height: 24 }}
                    title="从当前方向移除"
                    onClick={() => removeMutation.mutate(r.id)}
                  >
                    <Icon name="x" size={12} />
                  </button>
                </span>
              </div>
            ))}
          </div>
        ))
      )}
    </div>
  );
}

// ---- 页面 ----

export function SkillsPage() {
  const queryClient = useQueryClient();
  const { currentProjectId } = useProject();
  const [view, setView] = useState<'library' | 'market'>('library');
  const [scope, setScope] = useState<ScopeFilter>('all');
  const [kind, setKind] = useState<KindFilter>('all');
  const [q, setQ] = useState('');
  const [openId, setOpenId] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const importInputRef = useRef<HTMLInputElement>(null);

  const { data: skills, isLoading, isError } = useQuery({
    queryKey: ['skills'],
    queryFn: () => api.listSkills(),
    retry: false,
  });

  const importMutation = useMutation({
    mutationFn: (data: SkillExportData) => api.importSkill(data),
    onSuccess: (s) => {
      toast(`已导入：${s.name}`, 'ok');
      void queryClient.invalidateQueries({ queryKey: ['skills'] });
      setOpenId(s.id);
    },
    onError: (e) => toast(`导入失败：${errMsg(e)}`, 'error'),
  });

  async function onImportFile(file: File | undefined): Promise<void> {
    if (!file) return;
    try {
      const data = JSON.parse(await file.text()) as SkillExportData;
      importMutation.mutate(data);
    } catch {
      toast('不是合法的技能包文件（JSON 解析失败）', 'error');
    }
  }

  const filtered = useMemo(() => {
    const kw = q.trim().toLowerCase();
    return (skills ?? []).filter((s) => {
      if (scope === 'builtin' && s.scope !== 'builtin') return false;
      if (scope === 'mine' && s.scope === 'builtin') return false;
      if (kind !== 'all' && s.kind !== kind) return false;
      if (kw && !`${s.name} ${s.slug} ${s.description ?? ''}`.toLowerCase().includes(kw)) return false;
      return true;
    });
  }, [skills, scope, kind, q]);

  return (
    <div className="page fadeup">
      <PageHead
        eyebrow="Polaris"
        title="技能"
        sub="为各环节 AI 定制判断标准、评审人设与流程模板"
        en="Skills"
        right={
          <>
            <Segmented
              options={[
                { v: 'library', label: '技能库' },
                { v: 'market', label: '技能市场' },
              ]}
              value={view}
              onChange={setView}
            />
            {view === 'library' && (
              <>
                <input
                  ref={importInputRef}
                  type="file"
                  accept=".json,application/json"
                  style={{ display: 'none' }}
                  onChange={(e) => {
                    void onImportFile(e.target.files?.[0]);
                    e.target.value = '';
                  }}
                />
                <button className="btn btn-soft" disabled={importMutation.isPending} onClick={() => importInputRef.current?.click()}>
                  导入
                </button>
                <button className="btn btn-primary" onClick={() => setCreateOpen(true)}>
                  <Icon name="plus" size={14} /> 新建技能
                </button>
              </>
            )}
          </>
        }
      />

      {view === 'market' && <MarketView />}

      <div className="row gap10" style={{ marginBottom: 16, flexWrap: 'wrap', display: view === 'library' ? undefined : 'none' }}>
        <Segmented
          options={[
            { v: 'all', label: '全部' },
            { v: 'builtin', label: '内置' },
            { v: 'mine', label: '我的' },
          ]}
          value={scope}
          onChange={setScope}
        />
        <Segmented options={KIND_FILTERS} value={kind} onChange={setKind} />
        <input
          className="input"
          style={{ width: 200, marginLeft: 'auto' }}
          placeholder="搜索技能…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
      </div>

      <div
        style={{
          display: view === 'library' ? 'grid' : 'none',
          gridTemplateColumns: 'minmax(0, 1fr) 320px',
          gap: 18,
          alignItems: 'start',
        }}
      >
        <div style={{ display: 'grid', gap: 10 }}>
          {isError ? (
            <EmptyState icon="sparkle" title="技能列表加载失败" desc="后端服务未启动或版本过旧" />
          ) : isLoading ? null : filtered.length === 0 ? (
            <EmptyState
              icon="sparkle"
              title="没有匹配的技能"
              desc="换个筛选条件，或新建一个技能"
              action={
                <button className="btn btn-primary" onClick={() => setCreateOpen(true)}>
                  新建技能
                </button>
              }
            />
          ) : (
            filtered.map((s) => <SkillCard key={s.id} s={s} onOpen={() => setOpenId(s.id)} />)
          )}
        </div>

        {currentProjectId ? (
          <EnabledPanel projectId={currentProjectId} />
        ) : (
          <div className="card" style={{ padding: '14px 16px' }}>
            <EmptyState compact icon="compass" title="未选择研究方向" desc="选择研究方向后可把技能启用到该方向" />
          </div>
        )}
      </div>

      {openId && <SkillDetailModal skillId={openId} onClose={() => setOpenId(null)} />}
      {createOpen && <SkillCreateModal onClose={() => setCreateOpen(false)} onCreated={(s) => setOpenId(s.id)} />}
    </div>
  );
}
