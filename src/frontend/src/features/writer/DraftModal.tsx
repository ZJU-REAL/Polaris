import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { Modal } from '../../components/ui/Modal';
import { ConfirmModal } from '../../components/ui/ConfirmModal';
import { FormField } from '../../components/ui/FormField';
import { toast } from '../../components/ui/Toast';
import { api, ApiError, type ManuscriptDetail, type ManuscriptFileRead } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { DEFAULT_SECTIONS, sectionText } from './shared';

/* ============================================================
   AI 起草Modal：全部节 / 选节 checkbox + 备注 →
   POST /manuscripts/{id}/draft（kind=paper_writing 的 AI 任务）。
   同稿件已有进行中任务时后端 409。
   ============================================================ */

export interface DraftModalProps {
  open: boolean;
  onClose: () => void;
  manuscript: ManuscriptDetail;
  /** 初始化结构成功后回调：把新生成的 draft.tex 交给编辑器打开。 */
  onInitialized?: (file: ManuscriptFileRead) => void;
}

export function DraftModal({ open, onClose, manuscript, onInitialized }: DraftModalProps) {
  const queryClient = useQueryClient();
  const [all, setAll] = useState(true);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [notes, setNotes] = useState('');
  const [initConfirmOpen, setInitConfirmOpen] = useState(false);

  // 模板 sections 优先（GET templates），拿不到用固定顺序兜底
  const templatesQuery = useQuery({
    queryKey: ['manuscript-templates', manuscript.project_id],
    queryFn: () => api.listManuscriptTemplates(manuscript.project_id),
    enabled: open,
    retry: false,
    staleTime: 5 * 60_000,
  });
  const sections = useMemo(() => {
    const tpl = templatesQuery.data?.find((t) => t.id === manuscript.template);
    return tpl?.sections && tpl.sections.length > 0 ? tpl.sections : DEFAULT_SECTIONS;
  }, [templatesQuery.data, manuscript.template]);

  useEffect(() => {
    if (!open) return;
    setAll(true);
    setSelected(new Set());
    setNotes('');
    setInitConfirmOpen(false);
  }, [open]);

  // 初始化结构：新建 draft.tex（照抄当前主文件导言区 + 分节骨架），把编译主文件
  // 切到 draft.tex，原主文件不动。成功后交由父组件在编辑器里打开 draft.tex。
  const initMutation = useMutation({
    mutationFn: () => api.initializeManuscriptStructure(manuscript.id),
    onSuccess: (file) => {
      toast(tr('已创建 draft.tex 并设为编译主文件', 'Created draft.tex and set it as the main file'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['manuscript-file'] });
      void queryClient.invalidateQueries({ queryKey: ['manuscript-file-raw'] });
      void queryClient.invalidateQueries({ queryKey: ['file-versions', manuscript.id] });
      setInitConfirmOpen(false);
      onClose();
      // 父组件负责刷新详情并在编辑器里打开 draft.tex
      onInitialized?.(file);
    },
    onError: (e) => {
      if (e instanceof ApiError && e.status === 422 && e.message.includes('MAIN_TEX_NO_DOCUMENT')) {
        toast(
          tr(
            '主文件缺少 \\begin{document}，请先选择正确的主文件',
            'The main file has no \\begin{document}. Choose the right main file first.',
          ),
          'error',
        );
      } else {
        toast(`${tr('无法创建 draft.tex：', 'Couldn’t create draft.tex: ')}${e instanceof Error ? e.message : String(e)}`, 'error');
      }
    },
  });

  const mutation = useMutation({
    mutationFn: () =>
      api.draftManuscript(manuscript.id, {
        sections: all ? null : Array.from(selected),
        ...(notes.trim() ? { notes: notes.trim() } : {}),
      }),
    onSuccess: () => {
      toast(tr('已开始起草，进度见顶栏', 'Drafting started. Track it in the top bar'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['manuscript', manuscript.id] });
      void queryClient.invalidateQueries({ queryKey: ['manuscripts'] });
      void queryClient.invalidateQueries({ queryKey: ['voyages'] });
      onClose();
    },
    onError: (e) => {
      if (e instanceof ApiError && e.status === 409) {
        toast(tr('这篇稿件正在起草中', 'This manuscript is already being drafted'), 'error');
      } else {
        toast(`${tr('无法开始起草：', 'Couldn’t start drafting: ')}${e instanceof Error ? e.message : String(e)}`, 'error');
      }
    },
  });

  const canSubmit = !mutation.isPending && (all || selected.size > 0);

  function toggleSection(key: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  return (
    <>
    <Modal
      open={open}
      onClose={onClose}
      width={540}
      title={
        <>
          <Icon name="sparkle" size={16} style={{ color: 'var(--accent)' }} />
          {tr('AI 起草', 'Draft with AI')}
        </>
      }
      sub={tr(
        '只使用事实包中的引用、图表和数字。',
        'Uses only the citations, figures and numbers in the fact pack.',
      )}
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>{tr('取消', 'Cancel')}</button>
          <button className="btn btn-primary" disabled={!canSubmit} onClick={() => mutation.mutate()}>
            {mutation.isPending ? (
              <>
                <Icon name="refresh" size={14} style={{ animation: 'spin 1s linear infinite' }} />
                {tr('启动中…', 'Starting…')}
              </>
            ) : (
              <>
                <Icon name="play" size={14} />
                {tr('开始起草', 'Start drafting')}
              </>
            )}
          </button>
        </>
      }
    >
      <div
        style={{
          borderRadius: 8,
          padding: '10px 12px',
          background: 'var(--surface-2)',
          marginBottom: 14,
        }}
      >
        <div className="row gap8" style={{ marginBottom: 6 }}>
          <span style={{ fontSize: 13, fontWeight: 600 }}>
            {tr('首次起草前', 'Before the first draft')}
          </span>
          <button
            className="btn btn-ghost sm"
            style={{ marginLeft: 'auto' }}
            disabled={initMutation.isPending}
            onClick={() => setInitConfirmOpen(true)}
          >
            {initMutation.isPending ? (
              <>
                <Icon name="refresh" size={13} style={{ animation: 'spin 1s linear infinite' }} />
                {tr('创建中…', 'Creating…')}
              </>
            ) : (
              <>
                <Icon name="layers" size={13} />
                {tr('创建 draft.tex', 'Create draft.tex')}
              </>
            )}
          </button>
        </div>
        <div style={{ fontSize: 11, color: 'var(--text-3)', lineHeight: 1.6 }}>
          {tr(
            '新建带章节标题的 draft.tex 并设为编译主文件，原主文件不变。',
            'Adds draft.tex with section headings and compiles from it. Your main file stays as it is.',
          )}
        </div>
      </div>

      <FormField label={tr('章节', 'Sections')}>
        <div className="col gap8">
          <label className="row gap8" style={{ fontSize: 13, cursor: 'pointer' }}>
            <input type="radio" checked={all} onChange={() => setAll(true)} />
            {tr('全部章节', 'All sections')}
          </label>
          <label className="row gap8" style={{ fontSize: 13, cursor: 'pointer' }}>
            <input type="radio" checked={!all} onChange={() => setAll(false)} />
            {tr('选择章节', 'Choose sections')}
          </label>
          {!all && (
            <div className="row gap8 wrap" style={{ paddingLeft: 22 }}>
              {sections.map((s) => (
                <label key={s} className={`chip${selected.has(s) ? ' on' : ''}`} style={{ gap: 5 }}>
                  <input
                    type="checkbox"
                    checked={selected.has(s)}
                    onChange={() => toggleSection(s)}
                    style={{ display: 'none' }}
                  />
                  {sectionText(s)}
                </label>
              ))}
            </div>
          )}
        </div>
      </FormField>

      <FormField
        label={tr('额外要求（可选）', 'Instructions (optional)')}
      >
        <textarea
          className="textarea"
          rows={3}
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          placeholder={tr(
            '例如：突出方法的效率，相关工作部分重点对比 XX 系列方法',
            'e.g. Stress efficiency; compare with the XX line of work in related work',
          )}
        />
      </FormField>

      <div style={{ fontSize: 11, color: 'var(--text-3)', lineHeight: 1.6 }}>
        {tr(
          '起草内容会实时出现在编辑器中，每节写完后核对引用、图表和数字的出处。',
          'Text appears in the editor as it’s written. Each section’s citations, figures and numbers are checked against their sources.',
        )}
      </div>
    </Modal>

    <ConfirmModal
      open={initConfirmOpen}
      onClose={() => setInitConfirmOpen(false)}
      title={tr('创建 draft.tex？', 'Create draft.tex?')}
      message={tr(
        '之后将编译 draft.tex，当前主文件保持不变。',
        'Compiling will switch to draft.tex. Your current main file is kept.',
      )}
      confirmText={tr('创建', 'Create')}
      busy={initMutation.isPending}
      onConfirm={() => initMutation.mutate()}
    />
    </>
  );
}
