import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { Modal } from '../../components/ui/Modal';
import { FormField } from '../../components/ui/FormField';
import { SelectMenu } from '../../components/ui/SelectMenu';
import { toast } from '../../components/ui/Toast';
import { fmtTime } from '../../lib/format';
import { tr } from '../../lib/i18n';
import { copyText } from '../../lib/clipboard';
import {
  api,
  type FeedbackRead,
  type FeedbackSeverity,
  type FeedbackStatus,
  type FeedbackType,
  type IssueDraft,
} from '../../lib/api';
import { AuthImage } from './AuthImage';
import {
  ALL_TYPES,
  FeedbackStatusPill,
  SEVERITIES,
  STATUSES,
  SeverityPill,
  TypePill,
  severityLabel,
  statusLabel,
  typeLabel,
} from './labels';

/* ============================================================
   /admin · 反馈 Tab（admin）：列表 + 过滤 + triage + 生成 GitHub issue。
   ============================================================ */

function parseLabels(raw: string): string[] {
  return [...new Set(raw.split(/[,，]/).map((s) => s.trim()).filter(Boolean))];
}

// ---------------- 单条反馈卡片 ----------------

function FeedbackCard({
  fb,
  expanded,
  onToggle,
  generating,
  onGenerateDraft,
  onOpenImage,
}: {
  fb: FeedbackRead;
  expanded: boolean;
  onToggle: () => void;
  generating: boolean;
  onGenerateDraft: () => void;
  onOpenImage: (seq: number) => void;
}) {
  const queryClient = useQueryClient();
  const [note, setNote] = useState(fb.admin_note ?? '');
  useEffect(() => setNote(fb.admin_note ?? ''), [fb.admin_note]);

  const update = useMutation({
    mutationFn: (patch: { status?: FeedbackStatus; severity?: FeedbackSeverity; type?: FeedbackType; admin_note?: string }) =>
      api.adminUpdateFeedback(fb.id, patch),
    onSuccess: () => {
      toast(tr('已更新', 'Updated'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['admin-feedback'] });
    },
    onError: (e) => toast(`${tr('更新失败', 'Update failed')}：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const contextEntries = Object.entries(fb.context ?? {});
  const hasIssue = fb.github_issue_number != null;

  return (
    <div className="card" style={{ overflow: 'hidden' }}>
      {/* 头部（点开/收起） */}
      <div
        className="row gap10"
        role="button"
        onClick={onToggle}
        style={{ padding: '11px 14px', cursor: 'pointer', alignItems: 'center' }}
      >
        <Icon name="chevron" size={13} style={{ color: 'var(--text-3)', transform: expanded ? 'rotate(90deg)' : 'none', transition: 'transform .12s', flexShrink: 0 }} />
        <TypePill type={fb.type} />
        <SeverityPill severity={fb.severity} />
        <span style={{ flex: 1, minWidth: 0, fontSize: 13, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {fb.title}
        </span>
        {fb.module && (
          <span className="pill sm mono" style={{ background: 'var(--surface-3)', color: 'var(--text-3)' }}>{fb.module}</span>
        )}
        <FeedbackStatusPill status={fb.status} />
        {hasIssue && <Icon name="git" size={13} style={{ color: 'var(--accent)' }} />}
        <span style={{ fontSize: 11.5, color: 'var(--text-3)', flexShrink: 0 }}>{fb.author?.display_name ?? tr('匿名', 'Anonymous')}</span>
        <span className="mono" style={{ fontSize: 11, color: 'var(--text-4)', flexShrink: 0 }}>{fmtTime(fb.created_at)}</span>
      </div>

      {expanded && (
        <div style={{ padding: '4px 16px 16px', borderTop: '0.5px solid var(--border)' }}>
          {/* 描述 */}
          {fb.body && (
            <div style={{ fontSize: 12.5, color: 'var(--text-2)', whiteSpace: 'pre-wrap', lineHeight: 1.6, margin: '12px 0' }}>{fb.body}</div>
          )}

          {/* 截图 */}
          {fb.images.length > 0 && (
            <div className="row gap8" style={{ flexWrap: 'wrap', marginBottom: 14 }}>
              {fb.images.map((img) => (
                <AuthImage
                  key={img.id}
                  feedbackId={fb.id}
                  seq={img.seq}
                  style={{ width: 96, height: 96, cursor: 'zoom-in' }}
                  onClick={() => onOpenImage(img.seq)}
                />
              ))}
            </div>
          )}

          {/* 上下文 */}
          <div className="card" style={{ background: 'var(--surface-2)', padding: '10px 12px', marginBottom: 14 }}>
            <div className="mono" style={{ fontSize: 11, lineHeight: 1.7, color: 'var(--text-3)' }}>
              {fb.route && <div><span style={{ color: 'var(--text-4)' }}>route</span> = {fb.route}</div>}
              {contextEntries.map(([k, v]) => (
                <div key={k} style={{ overflowWrap: 'anywhere' }}>
                  <span style={{ color: 'var(--text-4)' }}>{k}</span> = {typeof v === 'string' ? v : JSON.stringify(v)}
                </div>
              ))}
              {!fb.route && contextEntries.length === 0 && <span style={{ color: 'var(--text-4)' }}>{tr('无上下文', 'No context')}</span>}
            </div>
          </div>

          {/* triage 控件 */}
          <div className="row gap12" style={{ alignItems: 'flex-start', flexWrap: 'wrap', marginBottom: 14 }}>
            <FormField label={tr('状态', 'Status')} style={{ width: 150 }}>
              <SelectMenu
                value={fb.status}
                options={STATUSES.map((s) => ({ value: s, label: statusLabel(s) }))}
                onChange={(v) => update.mutate({ status: v as FeedbackStatus })}
              />
            </FormField>
            <FormField label={tr('严重度', 'Severity')} style={{ width: 130 }}>
              <SelectMenu
                value={fb.severity}
                options={SEVERITIES.map((s) => ({ value: s, label: severityLabel(s) }))}
                onChange={(v) => update.mutate({ severity: v as FeedbackSeverity })}
              />
            </FormField>
            <FormField label={tr('类型', 'Type')} style={{ width: 150 }}>
              <SelectMenu
                value={fb.type}
                options={ALL_TYPES.map((t) => ({ value: t, label: typeLabel(t) }))}
                onChange={(v) => update.mutate({ type: v as FeedbackType })}
              />
            </FormField>
          </div>

          {/* 内部备注 */}
          <FormField label={tr('内部备注', 'Admin note')}>
            <textarea
              className="textarea"
              style={{ minHeight: 60, resize: 'vertical' }}
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder={tr('仅管理员可见', 'Visible to admins only')}
            />
            <div className="row" style={{ justifyContent: 'flex-end', marginTop: 6 }}>
              <button
                className="btn btn-soft sm"
                disabled={update.isPending || note === (fb.admin_note ?? '')}
                onClick={() => update.mutate({ admin_note: note })}
              >
                {tr('保存备注', 'Save note')}
              </button>
            </div>
          </FormField>

          {/* issue 区 */}
          <div className="row gap10" style={{ justifyContent: 'flex-end', marginTop: 4, alignItems: 'center' }}>
            {hasIssue ? (
              <a className="btn btn-soft sm" href={fb.github_issue_url ?? undefined} target="_blank" rel="noreferrer">
                <Icon name="git" size={12} />
                {tr('已建 issue', 'Issue')} #{fb.github_issue_number}
                <Icon name="link" size={11} />
              </a>
            ) : (
              <button className="btn btn-primary sm" disabled={generating} onClick={onGenerateDraft}>
                <Icon name="sparkle" size={12} />
                {generating ? tr('生成中…', 'Generating…') : tr('生成 issue 草稿', 'Generate issue draft')}
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------- 反馈 Tab ----------------

export function FeedbackTab() {
  const queryClient = useQueryClient();
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['admin-feedback'],
    queryFn: () => api.adminListFeedback(),
    retry: false,
  });
  const githubQuery = useQuery({
    queryKey: ['admin-feedback', 'github-status'],
    queryFn: () => api.adminFeedbackGithubStatus(),
    retry: false,
  });
  const githubEnabled = githubQuery.data?.enabled === true;
  const list = data ?? [];

  const [fType, setFType] = useState('');
  const [fStatus, setFStatus] = useState('');
  const [fSeverity, setFSeverity] = useState('');
  const [expanded, setExpanded] = useState<string | null>(null);

  // 草稿 Modal 状态
  const [draftFor, setDraftFor] = useState<string | null>(null);
  const [draftTitle, setDraftTitle] = useState('');
  const [draftBody, setDraftBody] = useState('');
  const [draftLabels, setDraftLabels] = useState('');

  // 图片放大
  const [lightbox, setLightbox] = useState<{ id: string; seq: number } | null>(null);

  const filtered = useMemo(
    () =>
      list.filter(
        (f) => (!fType || f.type === fType) && (!fStatus || f.status === fStatus) && (!fSeverity || f.severity === fSeverity),
      ),
    [list, fType, fStatus, fSeverity],
  );

  const generate = useMutation({
    mutationFn: (id: string) => api.adminGenerateIssueDraft(id),
    onSuccess: (d: IssueDraft, id) => {
      setDraftFor(id);
      setDraftTitle(d.title);
      setDraftBody(d.body);
      setDraftLabels(d.labels.join(', '));
    },
    onError: (e) => toast(`${tr('生成失败', 'Generate failed')}：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const createIssue = useMutation({
    mutationFn: () =>
      api.adminCreateIssue(draftFor!, { title: draftTitle.trim(), body: draftBody, labels: parseLabels(draftLabels) }),
    onSuccess: (r) => {
      toast(`${tr('issue 已创建', 'Issue created')} #${r.number}`, 'ok');
      setDraftFor(null);
      void queryClient.invalidateQueries({ queryKey: ['admin-feedback'] });
    },
    onError: (e) => {
      const msg = e instanceof Error ? e.message : String(e);
      toast(
        msg.includes('GITHUB_NOT_CONFIGURED')
          ? tr('未配置 GitHub token，无法创建', 'GitHub token not configured')
          : `${tr('创建失败', 'Create failed')}：${msg}`,
        'error',
      );
    },
  });

  const copyMarkdown = () => {
    const md = `# ${draftTitle}\n\n${draftBody}`;
    void copyText(md).then((ok) =>
      ok
        ? toast(tr('已复制 markdown', 'Markdown copied'), 'ok')
        : toast(tr('复制失败', 'Copy failed'), 'error'),
    );
  };

  const filterSelect = (
    value: string,
    setValue: (v: string) => void,
    allLabel: string,
    options: { value: string; label: string }[],
  ) => (
    <SelectMenu
      value={value}
      options={[{ value: '', label: allLabel }, ...options]}
      onChange={setValue}
      wrapStyle={{ width: 150 }}
    />
  );

  return (
    <div className="card card-pad">
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 12, flexWrap: 'wrap', gap: 10 }}>
        <span className="section-h">
          <Icon name="chat" size={15} style={{ color: 'var(--accent)' }} />
          {tr('用户反馈', 'User feedback')}{' '}
          <span className="en-label" style={{ fontSize: 11 }}>{tr('分诊、备注、一键建 GitHub issue', 'triage, notes, one-click GitHub issue')}</span>
        </span>
        <div className="row gap8" style={{ flexWrap: 'wrap' }}>
          {filterSelect(fType, setFType, tr('全部类型', 'All types'), ALL_TYPES.map((t) => ({ value: t, label: typeLabel(t) })))}
          {filterSelect(fStatus, setFStatus, tr('全部状态', 'All status'), STATUSES.map((s) => ({ value: s, label: statusLabel(s) })))}
          {filterSelect(fSeverity, setFSeverity, tr('全部严重度', 'All severity'), SEVERITIES.map((s) => ({ value: s, label: severityLabel(s) })))}
        </div>
      </div>

      {!githubEnabled && !githubQuery.isLoading && (
        <div className="field-hint" style={{ marginBottom: 12, color: 'var(--warn-tx)' }}>
          {tr(
            '未配置 GitHub token —— 可生成草稿并复制，但不能直接创建 issue。在后端 .env 配 POLARIS_GITHUB_TOKEN 后开启。',
            'GitHub token not configured — you can generate and copy drafts, but cannot create issues directly. Set POLARIS_GITHUB_TOKEN in the backend .env to enable.',
          )}
        </div>
      )}

      {isLoading ? (
        <div className="empty" style={{ padding: 24 }}>{tr('加载中…', 'Loading…')}</div>
      ) : isError ? (
        <div className="empty" style={{ padding: 24 }}>
          {tr('无法加载（后端不可用或无权限）', 'Failed to load (backend unavailable or no permission)')}
          <div style={{ marginTop: 10 }}>
            <button className="btn btn-soft sm" onClick={() => void refetch()}>{tr('重试', 'Retry')}</button>
          </div>
        </div>
      ) : filtered.length === 0 ? (
        <div className="empty" style={{ padding: 24 }}>
          {list.length === 0 ? tr('还没有反馈', 'No feedback yet') : tr('没有符合筛选条件的反馈', 'No feedback matches the filters')}
        </div>
      ) : (
        <div className="col gap10">
          {filtered.map((fb) => (
            <FeedbackCard
              key={fb.id}
              fb={fb}
              expanded={expanded === fb.id}
              onToggle={() => setExpanded(expanded === fb.id ? null : fb.id)}
              generating={generate.isPending && generate.variables === fb.id}
              onGenerateDraft={() => generate.mutate(fb.id)}
              onOpenImage={(seq) => setLightbox({ id: fb.id, seq })}
            />
          ))}
        </div>
      )}

      {/* 草稿 Modal */}
      <Modal
        open={draftFor !== null}
        onClose={() => setDraftFor(null)}
        width={640}
        title={
          <>
            <Icon name="git" size={16} style={{ color: 'var(--accent)' }} />
            {tr('创建 GitHub issue', 'Create GitHub issue')}
          </>
        }
        sub={tr('AI 已按仓库模板生成草稿，可编辑后再创建', 'AI drafted this per the repo template; edit before creating')}
        footer={
          <>
            <button className="btn btn-ghost" onClick={() => setDraftFor(null)}>{tr('取消', 'Cancel')}</button>
            <button className="btn btn-soft" onClick={copyMarkdown}>
              <Icon name="link" size={12} />
              {tr('复制 markdown', 'Copy markdown')}
            </button>
            <button
              className="btn btn-primary"
              disabled={!githubEnabled || createIssue.isPending || draftTitle.trim() === ''}
              title={githubEnabled ? undefined : tr('未配置 GitHub token，先在后端 .env 配 POLARIS_GITHUB_TOKEN', 'GitHub token not configured; set POLARIS_GITHUB_TOKEN in the backend .env first')}
              onClick={() => createIssue.mutate()}
            >
              {createIssue.isPending ? tr('创建中…', 'Creating…') : tr('确认创建 issue', 'Create issue')}
            </button>
          </>
        }
      >
        <FormField label={tr('标题', 'Title')}>
          <input className="input" value={draftTitle} onChange={(e) => setDraftTitle(e.target.value)} />
        </FormField>
        <FormField label={tr('正文', 'Body')} hint={tr('Markdown', 'Markdown')}>
          <textarea
            className="textarea mono"
            style={{ minHeight: 260, resize: 'vertical', fontSize: 12 }}
            value={draftBody}
            onChange={(e) => setDraftBody(e.target.value)}
          />
        </FormField>
        <FormField label={tr('标签', 'Labels')} hint={tr('逗号分隔', 'Comma separated')}>
          <input className="input mono" value={draftLabels} onChange={(e) => setDraftLabels(e.target.value)} placeholder="bug, frontend" />
        </FormField>
        {!githubEnabled && (
          <div className="field-hint" style={{ color: 'var(--warn-tx)' }}>
            {tr('未配置 GitHub token，先在后端 .env 配 POLARIS_GITHUB_TOKEN', 'GitHub token not configured; set POLARIS_GITHUB_TOKEN in the backend .env first')}
          </div>
        )}
      </Modal>

      {/* 图片放大 */}
      {lightbox && (
        <div
          onClick={() => setLightbox(null)}
          style={{
            position: 'fixed',
            inset: 0,
            zIndex: 70,
            background: 'rgba(10, 22, 44, 0.72)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            cursor: 'zoom-out',
            padding: 24,
          }}
        >
          <AuthImage
            feedbackId={lightbox.id}
            seq={lightbox.seq}
            style={{ maxWidth: '92vw', maxHeight: '88vh', objectFit: 'contain', border: 'none', borderRadius: 8 }}
          />
        </div>
      )}
    </div>
  );
}
