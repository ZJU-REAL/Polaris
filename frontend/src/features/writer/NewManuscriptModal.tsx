import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { Modal } from '../../components/ui/Modal';
import { FormField } from '../../components/ui/FormField';
import { toast } from '../../components/ui/Toast';
import { api, type TemplateInfo } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { saveBlob } from '../wiki/shared';
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

function sourceBadge(source: TemplateInfo['source']): { label: string; color: string; bg: string } {
  switch (source) {
    case 'builtin':
      return { label: tr('内置', 'Built-in'), color: 'var(--text-2)', bg: 'var(--surface-3)' };
    case 'seeded':
      return { label: tr('官方', 'Official'), color: 'var(--accent-text)', bg: 'var(--accent-soft)' };
    case 'uploaded':
    default:
      return { label: tr('自定义', 'Custom'), color: 'var(--text-2)', bg: 'var(--surface-3)' };
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
  const [downloadingId, setDownloadingId] = useState<string | null>(null);

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

  // 打开时重置；模板列表加载后默认选第一个
  useEffect(() => {
    if (!open) return;
    setTitle('');
    setIdeaId('');
    setExperimentId('');
  }, [open]);
  useEffect(() => {
    if (!open || templates.length === 0) return;
    if (!templates.some((t) => t.id === template)) setTemplate(templates[0]!.id);
  }, [open, templates, template]);

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

  const download = async (t: TemplateInfo) => {
    setDownloadingId(t.id);
    try {
      const blob = await api.downloadManuscriptTemplate(t.id);
      saveBlob(blob, `${t.name.replace(/[/\\:*?"<>|]/g, ' ').slice(0, 60) || 'template'}.zip`);
    } catch (e) {
      toast(`${tr('下载失败：', 'Download failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error');
    } finally {
      setDownloadingId(null);
    }
  };

  const canSubmit = !!title.trim() && !!template && !mutation.isPending;

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
              const badge = sourceBadge(t.source);
              return (
                <div
                  key={t.id}
                  role="button"
                  tabIndex={0}
                  onClick={() => setTemplate(t.id)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault();
                      setTemplate(t.id);
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
                    <span style={{ fontSize: 12.5, fontWeight: 620, lineHeight: 1.3, color: 'var(--text-1)' }}>
                      {t.name}
                    </span>
                    {on && <Icon name="check" size={14} style={{ color: 'var(--accent)', flexShrink: 0 }} />}
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

                  {t.downloadable && (
                    <button
                      className="btn btn-ghost sm"
                      style={{ alignSelf: 'flex-start', marginTop: 'auto', padding: '2px 6px', height: 22, fontSize: 10.5 }}
                      disabled={downloadingId === t.id}
                      onClick={(e) => {
                        e.stopPropagation();
                        void download(t);
                      }}
                    >
                      <Icon name="download" size={12} />
                      {downloadingId === t.id ? tr('下载中…', 'Downloading…') : tr('下载', 'Download')}
                    </button>
                  )}
                </div>
              );
            })}
          </div>
        )}

        <div className="field-hint" style={{ marginTop: 6 }}>
          {selected?.unofficial
            ? tr('该模板为平台内置的近似排版，非会议官方模板包，正式投稿前请换用官方模板核对格式。', 'This template is a built-in approximation, not an official venue package — switch to the official template and check formatting before submitting.')
            : tr('选择用于展开论文文件结构的模板；也可上传自己的 zip 模板。', 'Pick a template to expand the manuscript file structure; you can also upload your own zip.')}
        </div>
      </div>

      <div className="row gap12" style={{ alignItems: 'flex-start' }}>
        <FormField
          label={tr('关联想法（可选）', 'Linked idea (optional)')}
          style={{ flex: 1 }}
          hint={tr('论文事实包中研究想法分区的来源；仅列出已晋级的想法。', 'Source for the idea section of the fact pack; only promoted ideas are listed.')}
        >
          <select className="input" value={ideaId} onChange={(e) => setIdeaId(e.target.value)}>
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
          style={{ flex: 1 }}
          hint={tr('事实包的指标 / 图表 / 假设来源；仅列已完成实验。', 'Source of fact-pack metrics / figures / hypotheses; only finished experiments are listed.')}
        >
          <select className="input" value={experimentId} onChange={(e) => setExperimentId(e.target.value)}>
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
