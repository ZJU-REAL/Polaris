import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { Modal } from '../../components/ui/Modal';
import { FormField } from '../../components/ui/FormField';
import { toast } from '../../components/ui/Toast';
import { api, type TemplateInfo, type TemplateDownloadProgress } from '../../lib/api';
import { subscribeTemplateDownloadProgress } from '../../lib/sse';
import { tr } from '../../lib/i18n';
import { TemplateUploadModal } from './TemplateUploadModal';

/* ============================================================
   「新建论文草稿」Modal：标题 + 模板画廊（GET /manuscripts/templates）
   + 可选关联 idea（promoted）与 experiment（done）。
   创建成功后进入编辑工作台 /writer/:id。
   ============================================================ */

export interface NewManuscriptModalProps {
  open: boolean;
  onClose: () => void;
  pid: string;
}

function sourceBadge(t: TemplateInfo): { label: string; color: string; bg: string } {
  switch (t.source) {
    case 'builtin':
      return { label: tr('内置', 'Built-in'), color: 'var(--text-2)', bg: 'var(--surface-3)' };
    case 'seeded':
      return t.downloaded
        ? { label: tr('官方', 'Official'), color: 'var(--accent-text)', bg: 'var(--accent-soft)' }
        : { label: tr('官方 · 未下载', 'Official · not downloaded'), color: 'var(--text-2)', bg: 'var(--surface-3)' };
    case 'uploaded':
    default:
      return { label: tr('自定义', 'Custom'), color: 'var(--text-2)', bg: 'var(--surface-3)' };
  }
}

/** 下载进度文案。 */
function downloadLabel(p: TemplateDownloadProgress): string {
  switch (p.phase) {
    case 'downloading':
      return tr(`下载中 ${p.percent}%`, `Downloading ${p.percent}%`);
    case 'extracting':
      return tr('解压中…', 'Extracting…');
    case 'pending':
      return tr('准备下载…', 'Preparing…');
    default:
      return p.detail || tr('处理中…', 'Working…');
  }
}

export function NewManuscriptModal({ open, onClose, pid }: NewManuscriptModalProps) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const [title, setTitle] = useState('');
  const [template, setTemplate] = useState('');
  const [ideaId, setIdeaId] = useState('');
  const [experimentId, setExperimentId] = useState('');
  const [uploadOpen, setUploadOpen] = useState(false);
  // 官方模板按需下载：当前正在下载的 manifest key + 进度
  const [downloadKey, setDownloadKey] = useState<string | null>(null);
  const [downloadProgress, setDownloadProgress] = useState<TemplateDownloadProgress | null>(null);

  const templatesQuery = useQuery({
    queryKey: ['manuscript-templates', pid],
    queryFn: () => api.listManuscriptTemplates(pid),
    enabled: open && !!pid,
    retry: false,
    staleTime: 5 * 60_000,
  });
  const ideasQuery = useQuery({
    queryKey: ['ideas', pid, 'promoted'],
    queryFn: () => api.listIdeas(pid, { status: 'promoted' }),
    enabled: open && !!pid,
    retry: false,
  });
  const expsQuery = useQuery({
    queryKey: ['experiments', pid],
    queryFn: () => api.listExperiments(pid),
    enabled: open && !!pid,
    retry: false,
  });

  const templates = templatesQuery.data ?? [];
  const ideas = ideasQuery.data ?? [];
  const doneExps = (expsQuery.data ?? []).filter((e) => e.status === 'done');
  const selected = templates.find((t) => t.id === template) ?? null;

  // 打开时重置；关闭时取消未完成的下载订阅（下方 effect 会在 downloadKey 清空时 cleanup）
  useEffect(() => {
    if (open) {
      setTitle('');
      setIdeaId('');
      setExperimentId('');
      // 每次打开都清空选择，交给下方 effect 兜底选中第一个（避免跨项目残留旧 id）
      setTemplate('');
    } else {
      setDownloadKey(null);
      setDownloadProgress(null);
    }
  }, [open]);
  // 只在「尚未选择」时兜底选中第一个模板。绝不覆盖已选中的项——
  // 下载完成 / 上传完成会先把真实模板 id 写进 template，此时列表可能还没
  // refetch 到该 id，若在这里按「不在列表里就重置」会把刚下载好的模板选中给清掉。
  useEffect(() => {
    if (!open || templates.length === 0) return;
    if (!template) setTemplate(templates[0]!.id);
  }, [open, templates, template]);

  // 官方模板按需下载：仅跟踪当前选中项的订阅，切换/关闭时 cleanup 取消
  useEffect(() => {
    if (!downloadKey) return;
    let cancelled = false;
    let cancelSub: (() => void) | null = null;

    const finishDone = (templateId: string) => {
      if (cancelled) return;
      setDownloadProgress(null);
      setDownloadKey(null);
      void queryClient.invalidateQueries({ queryKey: ['manuscript-templates', pid] });
      if (templateId) setTemplate(templateId);
      toast(tr('模板下载完成', 'Template downloaded'), 'ok');
    };
    const finishError = (detail: string) => {
      if (cancelled) return;
      setDownloadProgress(null);
      setDownloadKey(null);
      toast(`${tr('模板下载失败：', 'Template download failed: ')}${detail}`, 'error');
    };

    setDownloadProgress({ key: downloadKey, name: '', phase: 'pending', percent: 0, detail: '', template_id: null, error: null });
    api
      .startTemplateDownload(downloadKey)
      .then((p) => {
        if (cancelled) return;
        if (p.phase === 'done' && p.template_id) {
          finishDone(p.template_id);
          return;
        }
        setDownloadProgress(p);
        cancelSub = subscribeTemplateDownloadProgress(downloadKey, {
          onProgress: (pr) => {
            if (!cancelled) setDownloadProgress(pr);
          },
          onDone: finishDone,
          onError: finishError,
        });
      })
      .catch((e) => finishError(e instanceof Error ? e.message : String(e)));

    return () => {
      cancelled = true;
      cancelSub?.();
    };
  }, [downloadKey, pid, queryClient]);

  const onSelectTemplate = (t: TemplateInfo) => {
    setTemplate(t.id);
    // 选中未下载的官方模板 → 自动触发下载（重新点击即重试）；否则取消进行中的下载
    setDownloadKey(!t.downloaded && t.download_key ? t.download_key : null);
  };

  const mutation = useMutation({
    mutationFn: () =>
      api.createManuscript(pid, {
        title: title.trim(),
        template,
        ...(ideaId ? { idea_id: ideaId } : {}),
        ...(experimentId ? { experiment_id: experimentId } : {}),
      }),
    onSuccess: (m) => {
      toast(tr('论文草稿已创建', 'Manuscript created'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['manuscripts', pid] });
      onClose();
      navigate(`/writer/${m.id}`);
    },
    onError: (e) => toast(`${tr('创建失败：', 'Create failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  // seed: 开头是未下载官方模板伪条目，下载完成前不能建稿
  const isSeedSelected = template.startsWith('seed:');
  const downloading = downloadProgress != null;
  const canSubmit = !!title.trim() && !!template && !isSeedSelected && !downloading && !mutation.isPending;

  return (
    <Modal
      open={open}
      onClose={onClose}
      width={620}
      title={
        <>
          <Icon name="pen" size={16} style={{ color: 'var(--accent)' }} />
          {tr('新建论文草稿', 'New manuscript')}
        </>
      }
      sub={tr('从选定模板展开文件，并自动从关联实验与文献库组装事实包', 'Expands files from the chosen template and assembles a fact pack from the linked experiment and library')}
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>{tr('取消', 'Cancel')}</button>
          <button className="btn btn-primary" disabled={!canSubmit} onClick={() => mutation.mutate()}>
            {mutation.isPending ? (
              <>
                <Icon name="refresh" size={14} style={{ animation: 'spin 1s linear infinite' }} />
                {tr('创建中…', 'Creating…')}
              </>
            ) : (
              <>
                <Icon name="plus" size={14} />
                {tr('创建草稿', 'Create manuscript')}
              </>
            )}
          </button>
        </>
      }
    >
      <FormField label={tr('论文标题', 'Title')}>
        <input
          className="input"
          autoFocus
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder={tr('论文工作标题（之后可以改）', 'Working title (you can change it later)')}
        />
      </FormField>

      <div className="field">
        <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center' }}>
          <label className="field-label" style={{ margin: 0 }}>{tr('论文模板', 'Template')}</label>
          <button className="btn btn-soft sm" onClick={() => setUploadOpen(true)}>
            <Icon name="plus" size={13} />
            {tr('上传模板 zip', 'Upload zip')}
          </button>
        </div>

        {templatesQuery.isError ? (
          <div className="field-error">{tr('无法加载模板列表（后端不可用或接口尚未就绪）。', 'Failed to load templates (backend unavailable or API not ready).')}</div>
        ) : templatesQuery.isLoading ? (
          <div className="field-hint">{tr('加载中…', 'Loading…')}</div>
        ) : templates.length === 0 ? (
          <div className="field-hint">{tr('（暂无模板）', '(no templates)')}</div>
        ) : (
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))',
              gap: 8,
              marginTop: 6,
            }}
          >
            {templates.map((t) => {
              const on = t.id === template;
              const badge = sourceBadge(t);
              return (
                <div
                  key={t.id}
                  role="button"
                  tabIndex={0}
                  onClick={() => onSelectTemplate(t)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault();
                      onSelectTemplate(t);
                    }
                  }}
                  style={{
                    border: `1px solid ${on ? 'var(--accent)' : 'var(--border)'}`,
                    background: on ? 'var(--accent-soft)' : 'var(--surface-2)',
                    borderRadius: 10,
                    padding: '10px 11px',
                    cursor: 'pointer',
                    display: 'flex',
                    flexDirection: 'column',
                    gap: 6,
                    transition: 'border-color .12s, background .12s',
                  }}
                >
                  <div className="row" style={{ justifyContent: 'space-between', alignItems: 'flex-start', gap: 6 }}>
                    <span style={{ flex: 1, minWidth: 0, fontSize: 12.5, fontWeight: 620, lineHeight: 1.3, color: 'var(--text-1)' }}>
                      {t.name}
                    </span>
                    {/* 始终占位，避免选中出现 ✓ 后标题被挤换行、卡片/弹窗变高 */}
                    <span style={{ width: 14, flexShrink: 0, display: 'flex' }}>
                      {on && <Icon name="check" size={14} style={{ color: 'var(--accent)' }} />}
                    </span>
                  </div>

                  <div className="row gap8 wrap" style={{ gap: 4 }}>
                    <span className="pill sm" style={{ height: 16, fontSize: 9.5, padding: '0 6px', background: badge.bg, color: badge.color }}>
                      {badge.label}
                    </span>
                    {t.page_limit != null && (
                      <span className="pill sm" style={{ height: 16, fontSize: 9.5, padding: '0 6px', background: 'var(--surface-3)' }}>
                        {tr(`≤${t.page_limit} 页`, `≤${t.page_limit} pp`)}
                      </span>
                    )}
                    <span className="pill sm mono" style={{ height: 16, fontSize: 9.5, padding: '0 6px', background: 'var(--surface-3)' }}>
                      {t.engine}
                    </span>
                  </div>

                  {t.description && (
                    <div style={{ fontSize: 10.5, color: 'var(--text-3)', lineHeight: 1.4 }}>
                      {t.description}
                    </div>
                  )}

                  {t.unofficial && (
                    <div style={{ fontSize: 10, color: 'var(--warn, var(--text-3))', lineHeight: 1.4 }}>
                      {tr('简化样式，投稿前请换官方模板核对格式', 'Simplified styling — switch to the official template before submitting')}
                    </div>
                  )}

                  {downloadProgress && t.download_key != null && t.download_key === downloadProgress.key && (
                    <div style={{ marginTop: 'auto', paddingTop: 4 }}>
                      <div style={{ height: 4, borderRadius: 999, background: 'var(--surface-3)', overflow: 'hidden' }}>
                        <div
                          style={{
                            height: '100%',
                            width: `${Math.max(0, Math.min(100, downloadProgress.percent))}%`,
                            background: 'var(--accent)',
                            borderRadius: 999,
                            transition: 'width .2s ease',
                          }}
                        />
                      </div>
                      <div style={{ fontSize: 10, color: 'var(--text-3)', marginTop: 3, lineHeight: 1.3 }}>
                        {downloadLabel(downloadProgress)}
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}

        <div className="field-hint" style={{ marginTop: 6, color: isSeedSelected ? 'var(--accent-text, var(--accent))' : undefined }}>
          {isSeedSelected
            ? tr('请先等待模板下载完成，再创建草稿。', 'Please wait for the template to finish downloading before creating a manuscript.')
            : selected?.unofficial
              ? tr('该模板为平台内置的近似排版，非会议官方模板包，正式投稿前请换用官方模板核对格式。', 'This template is a built-in approximation, not an official venue package — switch to the official template and check formatting before submitting.')
              : tr('选择用于展开论文文件结构的模板；也可上传自己的 zip 模板。', 'Pick a template to expand the manuscript file structure; you can also upload your own zip.')}
        </div>
      </div>

      <div className="row gap12" style={{ alignItems: 'flex-start' }}>
        <FormField
          label={tr('关联想法（可选）', 'Linked idea (optional)')}
          style={{ flex: 1, minWidth: 0 }}
          hint={tr('论文事实包中研究想法分区的来源；仅列出已晋级的想法。', 'Source for the idea section of the fact pack; only promoted ideas are listed.')}
        >
          <select className="input" style={{ width: '100%' }} value={ideaId} onChange={(e) => setIdeaId(e.target.value)}>
            <option value="">
              {ideasQuery.isLoading ? tr('加载中…', 'Loading…') : tr('— 不关联 —', '— none —')}
            </option>
            {ideas.map((i) => (
              <option key={i.id} value={i.id}>{i.title}</option>
            ))}
          </select>
        </FormField>
        <FormField
          label={tr('关联实验（可选）', 'Linked experiment (optional)')}
          style={{ flex: 1, minWidth: 0 }}
          hint={tr('事实包的指标 / 图表 / 假设来源；仅列已完成实验。', 'Source of fact-pack metrics / figures / hypotheses; only finished experiments are listed.')}
        >
          <select className="input" style={{ width: '100%' }} value={experimentId} onChange={(e) => setExperimentId(e.target.value)}>
            <option value="">
              {expsQuery.isLoading ? tr('加载中…', 'Loading…') : tr('— 不关联 —', '— none —')}
            </option>
            {doneExps.map((x) => (
              <option key={x.id} value={x.id}>{x.idea_title}</option>
            ))}
          </select>
        </FormField>
      </div>

      <TemplateUploadModal
        open={uploadOpen}
        onClose={() => setUploadOpen(false)}
        pid={pid}
        onUploaded={(tpl) => {
          void queryClient.invalidateQueries({ queryKey: ['manuscript-templates'] });
          setTemplate(tpl.id);
        }}
      />
    </Modal>
  );
}
